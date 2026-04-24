import os
import subprocess
from typing import Any, Dict, Optional

import msal
import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix

import app_config
from audit_repository import AuditRepository
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


def _default_cname_target() -> str:
    return _hosted_zone_name()


def _format_dt(value) -> str:
    if not value:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


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

    return render_template("dashboard.html", section="dashboard", user=session.get("user"))


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

    records = []
    try:
        records = _route53_service().list_cname_records()
        expected_target = _hosted_zone_name()
        records = [
            record
            for record in records
            if record.get("value", "").strip().rstrip(".").lower() == expected_target
        ]

        metadata_map = _dns_repository().get_metadata_map([r.get("name", "") for r in records])
        for record in records:
            key = record.get("name", "").strip().rstrip(".").lower()
            meta = metadata_map.get(key, {})
            status = _record_availability_status(record.get("name", ""), expected_target)
            record["protected"] = bool(meta.get("protected", False))
            record["created_by"] = meta.get("created_by", "-")
            record["created_on"] = _format_dt(meta.get("created_on"))
            record["updated_by"] = meta.get("updated_by", "-")
            record["updated_on"] = _format_dt(meta.get("updated_on"))
            record["status_label"] = status["label"]
            record["status_class"] = status["class"]
            record["status_message"] = status["message"]
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to fetch Route53 records: {exc}", "danger")

    return render_template(
        "dns.html",
        section="dns",
        user=session.get("user"),
        hosted_zone=app_config.HOSTED_ZONE_NAME,
        records=records,
    )


@app.route("/audit")
def audit_page():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    events = []
    try:
        rows = _audit_repository().list_events(limit=300)
        for row in rows:
            row["event_on_fmt"] = _format_dt(row.get("event_on"))
            events.append(row)
    except Exception as exc:  # pylint: disable=broad-except
        flash(f"Unable to fetch audit events: {exc}", "danger")

    return render_template(
        "audit.html",
        section="audit",
        user=session.get("user"),
        events=events,
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
        _dns_repository().upsert_record(record_name=record_name, target=target_name, actor_email=_actor_email())
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
        _dns_repository().upsert_record(record_name=new_name, target=new_target, actor_email=actor_email)
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


@app.route("/databases")
def databases():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    return render_template("databases.html", section="databases", user=session.get("user"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9000")), debug=True)
