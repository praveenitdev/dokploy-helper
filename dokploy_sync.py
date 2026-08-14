from __future__ import annotations

from datetime import datetime, timezone

import app_config
from audit_repository import AuditRepository
from dns_repository import DNSRepository
from dokploy_service import DokployService
from ecr_repository import EcrRepository
from ecr_service import EcrService
from route53_service import Route53Service


def _hosted_zone_name() -> str:
    return app_config.HOSTED_ZONE_NAME.strip().rstrip(".").lower()


def _default_cname_target() -> str:
    return _hosted_zone_name()


def _parse_iso_datetime(value) -> datetime | None:
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


def _dns_repository() -> DNSRepository:
    return DNSRepository(
        mongodb_uri=app_config.MONGODB_URI,
        database_name=app_config.MONGODB_DB_NAME,
        collection_name="dns",
    )


def _ecr_service() -> EcrService:
    return EcrService(
        aws_region=app_config.AWS_REGION,
        aws_access_key_id=app_config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
        aws_session_token=app_config.AWS_SESSION_TOKEN,
        iam_role_arn=app_config.IAM_ROLE_ARN,
        registry_id=app_config.ECR_REGISTRY_ID,
        repo_prefix=app_config.ECR_REPO_PREFIX,
        scan_on_push=app_config.ECR_SCAN_ON_PUSH,
        lifecycle_keep_count=app_config.ECR_LIFECYCLE_KEEP_COUNT,
    )


def _ecr_repository() -> EcrRepository:
    return EcrRepository(
        mongodb_uri=app_config.MONGODB_URI,
        database_name=app_config.MONGODB_DB_NAME,
        collection_name="ecr",
    )


def _audit_repository() -> AuditRepository:
    return AuditRepository(
        mongodb_uri=app_config.MONGODB_URI,
        database_name=app_config.MONGODB_DB_NAME,
        collection_name="audit",
    )


def _dokploy_service() -> DokployService:
    return DokployService(
        base_url=app_config.DOKPLOY_BASE_URL,
        api_key=app_config.DOKPLOY_API_KEY,
        timeout_seconds=app_config.DOKPLOY_API_TIMEOUT_SECONDS,
    )


def sync_dns_once(
    *,
    actor_email: str | None = None,
    audit_action: str = "SYNC_DOKPLOY_AUTO",
    user_agent: str = "dokploy-sync-worker",
    ip_address: str = "",
) -> dict[str, int]:
    hosted_zone = _hosted_zone_name()
    suffix = f".{hosted_zone}"
    synced_count = 0
    protected_skipped = 0
    outside_zone_skipped = 0
    target_name = _default_cname_target()
    actor = actor_email or app_config.DOKPLOY_SYNC_ACTOR

    domains = _dokploy_service().list_project_service_domain_details()
    route53 = _route53_service()
    dns_repo = _dns_repository()

    for domain in domains:
        record_name = str(domain.get("host") or "").strip().rstrip(".").lower()
        if not record_name.endswith(suffix) or record_name == hosted_zone:
            outside_zone_skipped += 1
            continue

        if dns_repo.is_record_protected(record_name):
            protected_skipped += 1
            continue

        route53.upsert_cname(name=record_name, target=target_name, ttl=300)
        dns_repo.upsert_record(
            record_name=record_name,
            target=target_name,
            actor_email=actor,
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

    details = (
        f"Synced={synced_count}; ProtectedSkipped={protected_skipped}; "
        f"OutsideZoneSkipped={outside_zone_skipped}"
    )
    try:
        _audit_repository().log_event(
            module="dns",
            action=audit_action,
            status="SUCCESS",
            actor_email=actor,
            entity_name=f"*.{hosted_zone}",
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        pass

    return {
        "synced": synced_count,
        "protected_skipped": protected_skipped,
        "outside_zone_skipped": outside_zone_skipped,
    }


def sync_ecr_once(
    *,
    actor_email: str | None = None,
    audit_action: str = "ENSURE_ECR_AUTO",
    user_agent: str = "dokploy-sync-worker",
    ip_address: str = "",
) -> dict[str, int]:
    if not app_config.ECR_AUTO_CREATE_ENABLED:
        return {
            "discovered": 0,
            "created": 0,
            "existed": 0,
            "skipped": 0,
            "protected_skipped": 0,
            "failed": 0,
            "disabled": 1,
        }

    actor = actor_email or app_config.DOKPLOY_SYNC_ACTOR
    created = 0
    existed = 0
    skipped = 0
    protected_skipped = 0
    failed = 0

    apps = _dokploy_service().list_project_service_apps()
    ecr = _ecr_service()
    ecr_repo = _ecr_repository()
    discovered = len(apps)

    for app in apps:
        service_app_name = str(app.get("service_app_name") or "").strip()
        if ecr.should_skip_app(service_app_name):
            skipped += 1
            continue

        try:
            repository_name = ecr.repository_name_for_app(service_app_name)
        except ValueError:
            skipped += 1
            continue

        if ecr_repo.is_record_protected(repository_name):
            protected_skipped += 1
            continue

        try:
            result = ecr.ensure_repository(repository_name)
            status = str(result.get("status") or "exists")
            ecr_repo.upsert_record(
                repository_name=repository_name,
                repository_uri=str(result.get("repository_uri") or ""),
                actor_email=actor,
                source="dokploy",
                status=status,
                project_name=str(app.get("project_name") or ""),
                environment_name=str(app.get("environment_name") or ""),
                service_name=str(app.get("service_name") or ""),
                service_type=str(app.get("service_type") or ""),
                service_app_name=service_app_name,
            )
            if status == "created":
                created += 1
            else:
                existed += 1
        except Exception as error:  # pylint: disable=broad-except
            failed += 1
            try:
                ecr_repo.upsert_record(
                    repository_name=repository_name,
                    repository_uri=ecr.repository_uri(repository_name),
                    actor_email=actor,
                    source="dokploy",
                    status="failed",
                    project_name=str(app.get("project_name") or ""),
                    environment_name=str(app.get("environment_name") or ""),
                    service_name=str(app.get("service_name") or ""),
                    service_type=str(app.get("service_type") or ""),
                    service_app_name=service_app_name,
                    last_error=str(error),
                )
            except Exception:
                pass

    details = (
        f"Discovered={discovered}; Created={created}; Existed={existed}; Skipped={skipped}; "
        f"ProtectedSkipped={protected_skipped}; Failed={failed}"
    )
    try:
        _audit_repository().log_event(
            module="ecr",
            action=audit_action,
            status="FAILED" if failed else "SUCCESS",
            actor_email=actor,
            entity_name=f"{ecr.repo_prefix}/*",
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        pass

    return {
        "discovered": discovered,
        "created": created,
        "existed": existed,
        "skipped": skipped,
        "protected_skipped": protected_skipped,
        "failed": failed,
        "disabled": 0,
    }


def sync_all_once(
    *,
    actor_email: str | None = None,
    user_agent: str = "dokploy-sync-worker",
    ip_address: str = "",
    dns_audit_action: str = "SYNC_DOKPLOY_AUTO",
    ecr_audit_action: str = "ENSURE_ECR_AUTO",
) -> dict[str, dict[str, int]]:
    dns_stats = sync_dns_once(
        actor_email=actor_email,
        audit_action=dns_audit_action,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    ecr_stats = sync_ecr_once(
        actor_email=actor_email,
        audit_action=ecr_audit_action,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    return {"dns": dns_stats, "ecr": ecr_stats}
