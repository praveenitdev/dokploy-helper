import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import msal
import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix

import app_config
from audit_repository import AuditRepository
from dokploy_service import DokployService
from dns_repository import DNSRepository
from route53_service import Route53Service


app = Flask(__name__)
app.config.from_object(app_config)
app.secret_key = app_config.APP_SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
Session(app)

_DNS_REPOSITORY = None
_AUDIT_REPOSITORY = None


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if session.get("token_cache"):
        cache.deserialize(session["token_cache"])
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        session["token_cache"] = cache.serialize()


def _build_msal_app(cache: Optional[msal.SerializableTokenCache] = None):
    return msal.ConfidentialClientApplication(
        app_config.CLIENT_ID,
        authority=app_config.AUTHORITY,
        client_credential=app_config.CLIENT_SECRET,
        token_cache=cache,
    )


def _build_auth_code_flow(scopes=None) -> Dict[str, Any]:
    return _build_msal_app().initiate_auth_code_flow(
        scopes or [],
        redirect_uri=_external_url_for("authorized"),
    )


def _external_url_for(endpoint: str, **values: Any) -> str:
    public_base_url = app_config.PUBLIC_BASE_URL.rstrip("/")
    if public_base_url:
        return f"{public_base_url}{url_for(endpoint, _external=False, **values)}"

    return url_for(
        endpoint,
        _external=True,
        _scheme=app_config.PREFERRED_URL_SCHEME,
        **values,
    )


def _get_token_from_cache(scopes=None):
    cache = _load_cache()
    cca = _build_msal_app(cache=cache)
    accounts = cca.get_accounts()
    if not accounts:
        return None

    result = cca.acquire_token_silent(scopes or [], account=accounts[0])
    _save_cache(cache)
    return result


def _require_login():
    if not session.get("user"):
        return redirect(url_for("login"))
    return None


def _route53_service() -> Route53Service:
    return Route53Service(
        hosted_zone_id=app_config.HOSTED_ZONE_ID,
        hosted_zone_name=app_config.HOSTED_ZONE_NAME,
        aws_region=app_config.AWS_REGION,
        aws_access_key_id=app_config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
        aws_session_token=app_config.AWS_SESSION_TOKEN,
        iam_role_arn=app_config.IAM_ROLE_ARN,
    )


def _dokploy_service() -> DokployService:
    return DokployService(
        base_url=app_config.DOKPLOY_BASE_URL,
        api_key=app_config.DOKPLOY_API_KEY,
        timeout_seconds=app_config.DOKPLOY_API_TIMEOUT_SECONDS,
    )


def _hosted_zone_name() -> str:
    return app_config.HOSTED_ZONE_NAME.strip().rstrip(".").lower()


def _normalize_subdomain_input(value: str) -> str:
    zone = _hosted_zone_name()
    clean = value.strip().rstrip(".").lower()

    if not clean:
        raise ValueError("Subdomain is required")

    zone_suffix = f".{zone}"
    if clean == zone:
        raise ValueError("Root hosted zone cannot be used as CNAME name")
    if clean.endswith(zone_suffix):
        clean = clean[: -len(zone_suffix)]

    if not clean:
        raise ValueError("Subdomain is required")

    return clean


def _build_record_name(subdomain: str) -> str:
    return f"{subdomain}.{_hosted_zone_name()}"


def _display_record_name(record_name: str) -> str:
    normalized = (record_name or "").strip().rstrip(".")
    hosted_zone = _hosted_zone_name()
    lower_name = normalized.lower()
    zone_suffix = f".{hosted_zone}"

    if lower_name.endswith(zone_suffix):
        return normalized[: -len(zone_suffix)]

    return normalized


def _default_cname_target() -> str:
    return _hosted_zone_name()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_dt(value) -> str:
    parsed = value if isinstance(value, datetime) else _parse_iso_datetime(value)
    if not parsed:
        return "-"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def _dokploy_source_tooltip(meta: Dict[str, Any]) -> str:
    lines = [
        f"Project: {meta.get('project_name') or '-'}",
        f"Environment: {meta.get('environment_name') or '-'}",
        f"Service: {meta.get('service_name') or '-'}",
    ]
    service_type = str(meta.get("service_type") or "").strip()
    if service_type:
        lines.append(f"Type: {service_type}")
    service_app_name = str(meta.get("service_app_name") or "").strip()
    if service_app_name:
        lines.append(f"App: {service_app_name}")
    return "\n".join(lines)


def _record_availability_status(record_name: str, expected_target: str) -> Dict[str, str]:
    record = (record_name or "").strip().rstrip(".").lower()
    target = (expected_target or "").strip().rstrip(".").lower()

    try:
        result = subprocess.run(
            ["dig", "+short", "CNAME", record],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except FileNotFoundError:
        return {
            "label": "UNKNOWN",
            "class": "neutral",
            "message": "dig command not available",
        }
    except subprocess.TimeoutExpired:
        return {
            "label": "TIMEOUT",
            "class": "danger",
            "message": "DNS lookup timed out",
        }

    answers = [line.strip().rstrip(".").lower() for line in result.stdout.splitlines() if line.strip()]

    if not answers:
        return {
            "label": "UNAVAILABLE",
            "class": "danger",
            "message": "No CNAME answer",
        }

    if target in answers:
        return {
            "label": "AVAILABLE",
            "class": "success",
            "message": target,
        }

    return {
        "label": "MISMATCH",
        "class": "danger",
        "message": ", ".join(answers),
    }


def _actor_email() -> str:
    user = session.get("user") or {}
    return (
        user.get("preferred_username")
        or user.get("email")
        or user.get("upn")
        or "unknown@example.com"
    )


def _actor_ip() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or ""


def _actor_user_agent() -> str:
    return request.headers.get("User-Agent", "")


def _dns_repository() -> DNSRepository:
    global _DNS_REPOSITORY
    if _DNS_REPOSITORY is None:
        _DNS_REPOSITORY = DNSRepository(
            mongodb_uri=app_config.MONGODB_URI,
            database_name=app_config.MONGODB_DB_NAME,
            collection_name="dns",
        )
    return _DNS_REPOSITORY


def _audit_repository() -> AuditRepository:
    global _AUDIT_REPOSITORY
    if _AUDIT_REPOSITORY is None:
        _AUDIT_REPOSITORY = AuditRepository(
            mongodb_uri=app_config.MONGODB_URI,
            database_name=app_config.MONGODB_DB_NAME,
            collection_name="audit",
        )
    return _AUDIT_REPOSITORY


def _log_audit(
    module: str,
    action: str,
    status: str,
    entity_name: str,
    details: str,
    actor_email: Optional[str] = None,
) -> None:
    try:
        _audit_repository().log_event(
            module=module,
            action=action,
            status=status,
            actor_email=actor_email or _actor_email(),
            entity_name=entity_name,
            details=details,
            ip_address=_actor_ip(),
            user_agent=_actor_user_agent(),
        )
    except Exception:
        pass


def _log_dns_audit(action: str, status: str, record_name: str, target: str, details: str) -> None:
    _log_audit(
        module="dns",
        action=action,
        status=status,
        entity_name=record_name,
        details=f"target={target}; {details}",
    )


@app.route("/login")
def login():
    session["flow"] = _build_auth_code_flow(scopes=app_config.SCOPE)
    _log_audit(module="auth", action="LOGIN_INIT", status="SUCCESS", entity_name="session", details="Login flow initiated", actor_email="anonymous")
    return render_template("login.html", auth_url=session["flow"]["auth_uri"])


@app.route("/getToken")
def authorized():
    try:
        cache = _load_cache()
        result = _build_msal_app(cache=cache).acquire_token_by_auth_code_flow(
            session.get("flow", {}), request.args
        )
        if "error" in result:
            _log_audit(module="auth", action="LOGIN_CALLBACK", status="FAILED", entity_name="session", details=result.get("error_description", result.get("error", "auth error")), actor_email="anonymous")
            return render_template("auth_error.html", result=result)

        session["user"] = result.get("id_token_claims")
        _save_cache(cache)
        _log_audit(module="auth", action="LOGIN_CALLBACK", status="SUCCESS", entity_name="session", details="User logged in")
    except ValueError:
        _log_audit(module="auth", action="LOGIN_CALLBACK", status="FAILED", entity_name="session", details="Invalid authorization response", actor_email="anonymous")
        return redirect(url_for("login"))

    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    _log_audit(module="auth", action="LOGOUT", status="SUCCESS", entity_name="session", details="User logged out")
    session.clear()
    logout_url = (
        app_config.AUTHORITY
        + "/oauth2/v2.0/logout"
        + "?post_logout_redirect_uri="
        + _external_url_for("login")
    )
    return redirect(logout_url)


@app.route("/")
def root():
    if not session.get("user"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    dns_metrics = {
        "total_records": 0,
        "dokploy_count": 0,
        "manual_count": 0,
        "unknown_source_count": 0,
        "protected_count": 0,
        "created_last_7_days": 0,
        "created_last_30_days": 0,
        "top_projects": [],
        "trend_labels": [],
        "created_trend": [],
    }
    audit_metrics = {
        "total_events": 0,
        "events_last_7_days": 0,
        "failed_last_7_days": 0,
        "sync_events_last_7_days": 0,
        "sync_failed_last_7_days": 0,
        "trend_labels": [],
        "success_trend": [],
        "failed_trend": [],
        "top_actions": [],
        "recent_events": [],
    }
    route53_total = 0

    try:
        dns_metrics = _dns_repository().get_dashboard_metrics(days=14)
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to load DNS metrics: {exc}", "danger")

    try:
        audit_metrics = _audit_repository().get_dashboard_metrics(days=14)
        for event in audit_metrics.get("recent_events") or []:
            event["event_on_fmt"] = _format_dt(event.get("event_on"))
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to load audit metrics: {exc}", "danger")

    try:
        expected_target = _hosted_zone_name()
        route53_records = _route53_service().list_cname_records()
        route53_total = sum(
            1
            for record in route53_records
            if record.get("value", "").strip().rstrip(".").lower() == expected_target
        )
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to load Route53 totals: {exc}", "danger")
        route53_total = int(dns_metrics.get("total_records") or 0)

    chart_payload = {
        "source": {
            "labels": ["Dokploy", "Manual", "Unknown"],
            "values": [
                int(dns_metrics.get("dokploy_count") or 0),
                int(dns_metrics.get("manual_count") or 0),
                int(dns_metrics.get("unknown_source_count") or 0),
            ],
        },
        "createdTrend": {
            "labels": dns_metrics.get("trend_labels") or [],
            "values": dns_metrics.get("created_trend") or [],
        },
        "activityTrend": {
            "labels": audit_metrics.get("trend_labels") or [],
            "success": audit_metrics.get("success_trend") or [],
            "failed": audit_metrics.get("failed_trend") or [],
        },
        "projects": {
            "labels": [item.get("name") for item in (dns_metrics.get("top_projects") or [])],
            "values": [item.get("count") for item in (dns_metrics.get("top_projects") or [])],
        },
    }

    return render_template(
        "dashboard.html",
        section="dashboard",
        user=session.get("user"),
        hosted_zone=app_config.HOSTED_ZONE_NAME,
        route53_total=route53_total,
        dns_metrics=dns_metrics,
        audit_metrics=audit_metrics,
        chart_payload=chart_payload,
    )


@app.route("/profile")
def profile_details():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    token = _get_token_from_cache(app_config.SCOPE)
    if not token:
        flash("Session expired. Please login again.", "warning")
        return redirect(url_for("login"))

    graph_data = requests.get(
        app_config.GRAPH_PROFILE_ENDPOINT,
        headers={"Authorization": f"Bearer {token['access_token']}"},
        timeout=15,
    ).json()

    return graph_data


@app.route("/dns")
def dns_records():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    search_query = (request.args.get("q") or "").strip()
    source_filter = (request.args.get("source") or "all").strip().lower()
    project_filter = (request.args.get("project") or "all").strip()
    environment_filter = (request.args.get("environment") or "all").strip()
    service_filter = (request.args.get("service") or "all").strip()
    created_by_filter = (request.args.get("created_by") or "all").strip()
    updated_by_filter = (request.args.get("updated_by") or "all").strip()
    protected_filter = (request.args.get("protected") or "all").strip().lower()

    if source_filter not in {"all", "dokploy", "manual", "unknown"}:
        source_filter = "all"
    if protected_filter not in {"all", "yes", "no"}:
        protected_filter = "all"

    page_size = 10
    total_records = 0
    total_pages = 1
    start_index = 0
    end_index = 0
    records = []
    filter_options = {
        "projects": [],
        "environments": [],
        "services": [],
        "created_by": [],
        "updated_by": [],
    }
    filters_active = any(
        [
            bool(search_query),
            source_filter != "all",
            project_filter != "all",
            environment_filter != "all",
            service_filter != "all",
            created_by_filter != "all",
            updated_by_filter != "all",
            protected_filter != "all",
        ]
    )
    filter_query = {
        "q": search_query or None,
        "source": source_filter if source_filter != "all" else None,
        "project": project_filter if project_filter != "all" else None,
        "environment": environment_filter if environment_filter != "all" else None,
        "service": service_filter if service_filter != "all" else None,
        "created_by": created_by_filter if created_by_filter != "all" else None,
        "updated_by": updated_by_filter if updated_by_filter != "all" else None,
        "protected": protected_filter if protected_filter != "all" else None,
    }
    try:
        records = _route53_service().list_cname_records()
        expected_target = _hosted_zone_name()
        records = [
            record
            for record in records
            if record.get("value", "").strip().rstrip(".").lower() == expected_target
        ]

        metadata_map = _dns_repository().get_metadata_map([r.get("name", "") for r in records])
        search_needle = search_query.lower()

        enriched_records = []
        project_options = set()
        environment_options = set()
        service_options = set()
        created_by_options = set()
        updated_by_options = set()

        for record in records:
            key = record.get("name", "").strip().rstrip(".").lower()
            meta = metadata_map.get(key, {})
            source = str(meta.get("source") or "").strip().lower()
            if source not in {"manual", "dokploy"}:
                if not meta:
                    source = "unknown"
                else:
                    created_by_hint = str(meta.get("created_by") or "").strip().lower()
                    source = "dokploy" if created_by_hint in {"dokploy", "system"} else "manual"

            display_name = _display_record_name(record.get("name", ""))
            created_by = "Dokploy" if source == "dokploy" else (meta.get("created_by") or "-")
            updated_by = str(meta.get("updated_by") or "-")
            project_name = str(meta.get("project_name") or "").strip()
            environment_name = str(meta.get("environment_name") or "").strip()
            service_name = str(meta.get("service_name") or "").strip()
            service_app_name = str(meta.get("service_app_name") or "").strip()
            is_protected = bool(meta.get("protected", False))

            if project_name:
                project_options.add(project_name)
            if environment_name:
                environment_options.add(environment_name)
            if service_name:
                service_options.add(service_name)
            if created_by and created_by != "-":
                created_by_options.add(created_by)
            if updated_by and updated_by != "-":
                updated_by_options.add(updated_by)

            created_on_value = meta.get("domain_created_at") if source == "dokploy" else meta.get("created_on")
            if not created_on_value:
                created_on_value = meta.get("created_on")
            created_on_sort = _parse_iso_datetime(created_on_value) or datetime.min.replace(tzinfo=timezone.utc)

            record["display_name"] = display_name
            record["protected"] = is_protected
            record["source"] = source
            record["source_label"] = {
                "dokploy": "Dokploy",
                "manual": "Manual",
                "unknown": "Unknown",
            }.get(source, "Unknown")
            record["created_by"] = created_by
            record["created_on"] = _format_dt(created_on_value)
            record["created_on_sort"] = created_on_sort
            record["updated_by"] = updated_by
            record["updated_on"] = _format_dt(meta.get("updated_on"))
            record["project_name"] = project_name
            record["environment_name"] = environment_name
            record["service_name"] = service_name
            record["service_app_name"] = service_app_name
            record["source_tooltip"] = _dokploy_source_tooltip(meta) if source == "dokploy" else ""
            enriched_records.append(record)

        filter_options = {
            "projects": sorted(project_options, key=str.lower),
            "environments": sorted(environment_options, key=str.lower),
            "services": sorted(service_options, key=str.lower),
            "created_by": sorted(created_by_options, key=str.lower),
            "updated_by": sorted(updated_by_options, key=str.lower),
        }

        filtered_records = []
        for record in enriched_records:
            if source_filter != "all" and record.get("source") != source_filter:
                continue
            if project_filter != "all" and record.get("project_name") != project_filter:
                continue
            if environment_filter != "all" and record.get("environment_name") != environment_filter:
                continue
            if service_filter != "all" and record.get("service_name") != service_filter:
                continue
            if created_by_filter != "all" and record.get("created_by") != created_by_filter:
                continue
            if updated_by_filter != "all" and record.get("updated_by") != updated_by_filter:
                continue
            if protected_filter == "yes" and not record.get("protected"):
                continue
            if protected_filter == "no" and record.get("protected"):
                continue

            if search_needle:
                haystack = " ".join(
                    [
                        str(record.get("name") or ""),
                        str(record.get("display_name") or ""),
                        str(record.get("created_by") or ""),
                        str(record.get("updated_by") or ""),
                        str(record.get("project_name") or ""),
                        str(record.get("environment_name") or ""),
                        str(record.get("service_name") or ""),
                        str(record.get("service_app_name") or ""),
                        str(record.get("source") or ""),
                    ]
                ).lower()
                if search_needle not in haystack:
                    continue

            filtered_records.append(record)

        records = sorted(
            filtered_records,
            key=lambda item: item.get("created_on_sort") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        total_records = len(records)
        total_pages = max((total_records + page_size - 1) // page_size, 1)
        page = min(page, total_pages)

        start_index = (page - 1) * page_size
        end_index = min(start_index + page_size, total_records)
        page_records = records[start_index:end_index]

        for record in page_records:
            status = _record_availability_status(record.get("name", ""), expected_target)
            record["status_label"] = status["label"]
            record["status_class"] = status["class"]
            record["status_message"] = status["message"]

        records = page_records
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to fetch Route53 records: {exc}", "danger")

    return render_template(
        "dns.html",
        section="dns",
        user=session.get("user"),
        hosted_zone=app_config.HOSTED_ZONE_NAME,
        records=records,
        page=page,
        page_size=page_size,
        total_records=total_records,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
        prev_page=page - 1,
        next_page=page + 1,
        start_index=(start_index + 1) if total_records > 0 else 0,
        end_index=end_index,
        search_query=search_query,
        source_filter=source_filter,
        project_filter=project_filter,
        environment_filter=environment_filter,
        service_filter=service_filter,
        created_by_filter=created_by_filter,
        updated_by_filter=updated_by_filter,
        protected_filter=protected_filter,
        filter_options=filter_options,
        filters_active=filters_active,
        filter_query=filter_query,
    )


@app.route("/audit")
def audit_page():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    page_size = 20
    total_records = 0
    total_pages = 1
    start_index = 0
    end_index = 0
    events = []
    try:
        rows = _audit_repository().list_events(limit=1000)
        total_records = len(rows)
        total_pages = max((total_records + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        start_index = (page - 1) * page_size
        end_index = min(start_index + page_size, total_records)

        for row in rows[start_index:end_index]:
            row["event_on_fmt"] = _format_dt(row.get("event_on"))
            events.append(row)
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to fetch audit events: {exc}", "danger")

    return render_template(
        "audit.html",
        section="audit",
        user=session.get("user"),
        events=events,
        page=page,
        page_size=page_size,
        total_records=total_records,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
        prev_page=page - 1,
        next_page=page + 1,
        start_index=(start_index + 1) if total_records > 0 else 0,
        end_index=end_index,
    )


@app.route("/dns/create", methods=["POST"])
def dns_create():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    subdomain = request.form.get("subdomain", "")
    record_name = ""
    target_name = _default_cname_target()

    try:
        normalized_subdomain = _normalize_subdomain_input(subdomain)
        record_name = _build_record_name(normalized_subdomain)
        if _dns_repository().is_record_protected(record_name):
            raise PermissionError("This record is protected and cannot be modified")

        _route53_service().upsert_cname(
            name=record_name,
            target=target_name,
            ttl=300,
        )
        _dns_repository().upsert_record(
            record_name=record_name,
            target=target_name,
            actor_email=_actor_email(),
            source="manual",
        )
        _log_dns_audit(
            action="CREATE",
            status="SUCCESS",
            record_name=record_name,
            target=target_name,
            details="CNAME created or updated",
        )
        flash("CNAME record saved successfully.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        _log_dns_audit(
            action="CREATE",
            status="FAILED",
            record_name=record_name,
            target=target_name,
            details=str(exc),
        )
        flash(f"Failed to create/update CNAME: {exc}", "danger")

    return redirect(url_for("dns_records"))


@app.route("/dns/edit", methods=["POST"])
def dns_edit():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    old_name = request.form.get("old_name", "")
    old_target = request.form.get("old_target", "")
    old_ttl = request.form.get("old_ttl", "300")

    new_subdomain = request.form.get("subdomain", "")
    new_name = ""
    new_target = _default_cname_target()

    service = _route53_service()

    try:
        normalized_subdomain = _normalize_subdomain_input(new_subdomain)
        new_name = _build_record_name(normalized_subdomain)
        actor_email = _actor_email()

        if _dns_repository().is_record_protected(old_name):
            raise PermissionError("This record is protected and cannot be modified")
        if old_name.strip().rstrip(".").lower() != new_name.strip().rstrip(".").lower() and _dns_repository().is_record_protected(new_name):
            raise PermissionError("Target record is protected and cannot be modified")

        service.upsert_cname(name=new_name, target=new_target, ttl=300)
        _dns_repository().upsert_record(
            record_name=new_name,
            target=new_target,
            actor_email=actor_email,
            source="manual",
        )
        if old_name.strip().rstrip(".").lower() != new_name.strip().rstrip(".").lower() or old_target.strip().rstrip(".").lower() != new_target.strip().rstrip(".").lower():
            service.delete_cname(name=old_name, target=old_target, ttl=int(old_ttl))
            _dns_repository().delete_record(record_name=old_name.strip().rstrip(".").lower())

        _log_dns_audit(
            action="EDIT",
            status="SUCCESS",
            record_name=new_name,
            target=new_target,
            details=f"Updated from {old_name.strip().rstrip('.').lower()}",
        )

        flash("CNAME record updated successfully.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        _log_dns_audit(
            action="EDIT",
            status="FAILED",
            record_name=new_name or old_name,
            target=new_target or old_target,
            details=str(exc),
        )
        flash(f"Failed to edit CNAME: {exc}", "danger")

    return redirect(url_for("dns_records"))


@app.route("/dns/delete", methods=["POST"])
def dns_delete():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    name = request.form.get("name", "")
    target = request.form.get("target", "")
    ttl = request.form.get("ttl", "300")

    try:
        if _dns_repository().is_record_protected(name):
            raise PermissionError("This record is protected and cannot be deleted")

        _route53_service().delete_cname(name=name, target=target, ttl=int(ttl))
        _dns_repository().delete_record(record_name=name.strip().rstrip(".").lower())
        _log_dns_audit(
            action="DELETE",
            status="SUCCESS",
            record_name=name.strip().rstrip(".").lower(),
            target=target,
            details="CNAME deleted",
        )
        flash("CNAME record deleted successfully.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        _log_dns_audit(
            action="DELETE",
            status="FAILED",
            record_name=name.strip().rstrip(".").lower(),
            target=target,
            details=str(exc),
        )
        flash(f"Failed to delete CNAME: {exc}", "danger")

    return redirect(url_for("dns_records"))


@app.route("/dns/sync-dokploy", methods=["POST"])
def dns_sync_dokploy():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    hosted_zone = _hosted_zone_name()
    suffix = f".{hosted_zone}"
    synced_count = 0
    protected_skipped = 0
    outside_zone_skipped = 0
    target_name = _default_cname_target()
    actor_email = "System"

    try:
        domains = _dokploy_service().list_project_service_domain_details()
        for domain in domains:
            record_name = str(domain.get("host") or "").strip().rstrip(".").lower()
            if not record_name.endswith(suffix) or record_name == hosted_zone:
                outside_zone_skipped += 1
                continue

            if _dns_repository().is_record_protected(record_name):
                protected_skipped += 1
                continue

            _route53_service().upsert_cname(name=record_name, target=target_name, ttl=300)
            _dns_repository().upsert_record(
                record_name=record_name,
                target=target_name,
                actor_email=actor_email,
                source="dokploy",
                project_name=str(domain.get("project_name") or ""),
                environment_name=str(domain.get("environment_name") or ""),
                service_name=str(domain.get("service_name") or ""),
                service_type=str(domain.get("service_type") or ""),
                service_app_name=str(domain.get("service_app_name") or ""),
                domain_created_at=_parse_iso_datetime(domain.get("domain_created_at")),
                domain_id=str(domain.get("domain_id") or ""),
            )
            synced_count += 1

        _log_dns_audit(
            action="SYNC_DOKPLOY",
            status="SUCCESS",
            record_name=f"*.{hosted_zone}",
            target=target_name,
            details=f"Synced={synced_count}; ProtectedSkipped={protected_skipped}; OutsideZoneSkipped={outside_zone_skipped}",
        )
        flash(
            f"Dokploy sync complete. Synced: {synced_count}, protected skipped: {protected_skipped}, outside zone skipped: {outside_zone_skipped}.",
            "success",
        )
    except Exception as exc:  # pylint: disable=broad-except
        _log_dns_audit(
            action="SYNC_DOKPLOY",
            status="FAILED",
            record_name=f"*.{hosted_zone}",
            target=target_name,
            details=str(exc),
        )
        flash(f"Failed to sync Dokploy domains: {exc}", "danger")

    return redirect(url_for("dns_records"))


@app.route("/databases")
def databases():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    return render_template("databases.html", section="databases", user=session.get("user"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9000")), debug=True)
