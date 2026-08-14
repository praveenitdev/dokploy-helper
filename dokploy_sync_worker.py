import time
from datetime import datetime, timezone

import app_config
from dokploy_sync import sync_all_once, sync_dns_once


def _log_worker_failure(details: str) -> None:
    try:
        from audit_repository import AuditRepository

        AuditRepository(
            mongodb_uri=app_config.MONGODB_URI,
            database_name=app_config.MONGODB_DB_NAME,
            collection_name="audit",
        ).log_event(
            module="sync",
            action="SYNC_ALL_AUTO",
            status="FAILED",
            actor_email=app_config.DOKPLOY_SYNC_ACTOR,
            entity_name="dokploy-helper",
            details=details,
            ip_address="",
            user_agent="dokploy-sync-worker",
        )
    except Exception:
        pass


def main() -> None:
    if not app_config.DOKPLOY_AUTO_SYNC_ENABLED:
        print("[dokploy-sync-worker] DOKPLOY_AUTO_SYNC_ENABLED is false. Exiting.")
        return

    interval = max(app_config.DOKPLOY_SYNC_INTERVAL_SECONDS, 1)
    print(
        f"[dokploy-sync-worker] Started. Interval={interval}s "
        f"ecr_auto_create={app_config.ECR_AUTO_CREATE_ENABLED}"
    )

    while True:
        started = datetime.now(timezone.utc)
        try:
            stats = sync_all_once(
                actor_email=app_config.DOKPLOY_SYNC_ACTOR,
                user_agent="dokploy-sync-worker",
                dns_audit_action="SYNC_DOKPLOY_AUTO",
                ecr_audit_action="ENSURE_ECR_AUTO",
            )
            dns = stats["dns"]
            ecr = stats["ecr"]
            print(
                f"[dokploy-sync-worker] {started.isoformat()} "
                f"dns_synced={dns['synced']} dns_protected_skipped={dns['protected_skipped']} "
                f"ecr_discovered={ecr.get('discovered', 0)} "
                f"ecr_created={ecr['created']} ecr_existed={ecr['existed']} "
                f"ecr_failed={ecr['failed']} ecr_disabled={ecr.get('disabled', 0)}"
            )
        except Exception as exc:  # pylint: disable=broad-except
            # Keep DNS-only path available if ECR module fails hard during import/config
            try:
                dns = sync_dns_once()
                print(
                    f"[dokploy-sync-worker] {started.isoformat()} "
                    f"dns_fallback_synced={dns['synced']} ecr_error={exc}"
                )
            except Exception as dns_exc:  # pylint: disable=broad-except
                _log_worker_failure(str(dns_exc))
                print(f"[dokploy-sync-worker] {started.isoformat()} failed: {dns_exc}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
