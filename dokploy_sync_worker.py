import time
from datetime import datetime, timezone

import app_config
from audit_repository import AuditRepository
from dns_repository import DNSRepository
from dokploy_service import DokployService
from route53_service import Route53Service


def _hosted_zone_name() -> str:
    return app_config.HOSTED_ZONE_NAME.strip().rstrip(".").lower()


def _default_cname_target() -> str:
    return _hosted_zone_name()


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


def _log_sync_event(status: str, details: str, hosted_zone: str, target_name: str) -> None:
    try:
        _audit_repository().log_event(
            module="dns",
            action="SYNC_DOKPLOY_AUTO",
            status=status,
            actor_email=app_config.DOKPLOY_SYNC_ACTOR,
            entity_name=f"*.{hosted_zone}",
            details=details,
            ip_address="",
            user_agent="dokploy-sync-worker",
        )
    except Exception:
        # Worker must continue even if audit logging fails.
        pass


def sync_once() -> dict[str, int]:
    hosted_zone = _hosted_zone_name()
    suffix = f".{hosted_zone}"
    synced_count = 0
    protected_skipped = 0
    outside_zone_skipped = 0
    target_name = _default_cname_target()

    domains = _dokploy_service().list_project_service_domains()
    route53 = _route53_service()
    dns_repo = _dns_repository()

    for domain in domains:
        record_name = domain.strip().rstrip(".").lower()
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
            actor_email=app_config.DOKPLOY_SYNC_ACTOR,
        )
        synced_count += 1

    details = (
        f"Synced={synced_count}; ProtectedSkipped={protected_skipped}; "
        f"OutsideZoneSkipped={outside_zone_skipped}"
    )
    _log_sync_event(
        status="SUCCESS",
        details=details,
        hosted_zone=hosted_zone,
        target_name=target_name,
    )

    return {
        "synced": synced_count,
        "protected_skipped": protected_skipped,
        "outside_zone_skipped": outside_zone_skipped,
    }


def main() -> None:
    if not app_config.DOKPLOY_AUTO_SYNC_ENABLED:
        print("[dokploy-sync-worker] DOKPLOY_AUTO_SYNC_ENABLED is false. Exiting.")
        return

    interval = max(app_config.DOKPLOY_SYNC_INTERVAL_SECONDS, 1)
    print(f"[dokploy-sync-worker] Started. Interval={interval}s")

    while True:
        started = datetime.now(timezone.utc)
        try:
            stats = sync_once()
            print(f"[dokploy-sync-worker] {started.isoformat()} synced={stats['synced']} protected_skipped={stats['protected_skipped']} outside_zone_skipped={stats['outside_zone_skipped']}")
        except Exception as exc:  # pylint: disable=broad-except
            _log_sync_event(
                status="FAILED",
                details=str(exc),
                hosted_zone=_hosted_zone_name(),
                target_name=_default_cname_target(),
            )
            print(f"[dokploy-sync-worker] {started.isoformat()} failed: {exc}")

        time.sleep(interval)


if __name__ == "__main__":
    main()