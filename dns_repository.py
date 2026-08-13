from datetime import datetime, timedelta, timezone

from pymongo import MongoClient


class DNSRepository:
    def __init__(self, mongodb_uri: str, database_name: str = "dokploy", collection_name: str = "dns"):
        if not mongodb_uri:
            raise ValueError("MONGODB_URI is required")

        self.client = MongoClient(mongodb_uri)
        self.collection = self.client[database_name][collection_name]
        self.audit_collection = self.client[database_name]["dns_audit"]

    def upsert_record(
        self,
        record_name: str,
        target: str,
        actor_email: str,
        *,
        source: str = "manual",
        project_name: str = "",
        environment_name: str = "",
        service_name: str = "",
        service_type: str = "",
        service_app_name: str = "",
        domain_created_at: datetime | None = None,
        domain_id: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        normalized_source = (source or "manual").strip().lower()
        if normalized_source not in {"manual", "dokploy"}:
            normalized_source = "manual"

        display_actor = "Dokploy" if normalized_source == "dokploy" else actor_email
        created_on_value = domain_created_at or now

        set_fields = {
            "target": target,
            "updated_on": now,
            "updated_by": display_actor,
            "source": normalized_source,
            "project_name": project_name or "",
            "environment_name": environment_name or "",
            "service_name": service_name or "",
            "service_type": service_type or "",
            "service_app_name": service_app_name or "",
            "domain_id": domain_id or "",
        }
        if domain_created_at is not None:
            set_fields["domain_created_at"] = domain_created_at

        if normalized_source == "manual":
            set_fields["project_name"] = ""
            set_fields["environment_name"] = ""
            set_fields["service_name"] = ""
            set_fields["service_type"] = ""
            set_fields["service_app_name"] = ""
            set_fields["domain_id"] = ""

        self.collection.update_one(
            {"record_name": record_name},
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "created_on": created_on_value,
                    "created_by": display_actor,
                },
            },
            upsert=True,
        )

        if normalized_source == "dokploy" and domain_created_at is not None:
            self.collection.update_one(
                {"record_name": record_name, "source": "dokploy"},
                {
                    "$set": {
                        "created_on": domain_created_at,
                        "created_by": "Dokploy",
                    }
                },
            )

    def delete_record(self, record_name: str) -> None:
        self.collection.delete_one({"record_name": record_name})

    def get_metadata_map(self, record_names: list[str]) -> dict[str, dict]:
        if not record_names:
            return {}

        normalized = [name.strip().rstrip(".").lower() for name in record_names]
        cursor = self.collection.find(
            {"record_name": {"$in": normalized}},
            {
                "_id": 0,
                "record_name": 1,
                "protected": 1,
                "created_by": 1,
                "created_on": 1,
                "updated_by": 1,
                "updated_on": 1,
                "source": 1,
                "project_name": 1,
                "environment_name": 1,
                "service_name": 1,
                "service_type": 1,
                "service_app_name": 1,
                "domain_created_at": 1,
                "domain_id": 1,
            },
        )

        metadata_map: dict[str, dict] = {}
        for item in cursor:
            key = item.get("record_name", "").strip().rstrip(".").lower()
            if key:
                metadata_map[key] = item

        return metadata_map

    def is_record_protected(self, record_name: str) -> bool:
        normalized = record_name.strip().rstrip(".").lower()
        if not normalized:
            return False

        document = self.collection.find_one(
            {"record_name": normalized},
            {"_id": 0, "protected": 1},
        )
        if not document:
            return False

        return bool(document.get("protected", False))

    def get_dashboard_metrics(self, days: int = 14) -> dict:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=max(days, 1) - 1)
        last_7_days = now - timedelta(days=7)
        last_30_days = now - timedelta(days=30)

        total_records = self.collection.count_documents({})
        dokploy_count = self.collection.count_documents({"source": "dokploy"})
        manual_count = self.collection.count_documents({"source": "manual"})
        protected_count = self.collection.count_documents({"protected": True})
        unknown_source_count = max(total_records - dokploy_count - manual_count, 0)

        created_last_7_days = self.collection.count_documents(
            {"created_on": {"$gte": last_7_days}}
        )
        created_last_30_days = self.collection.count_documents(
            {"created_on": {"$gte": last_30_days}}
        )

        project_rows = list(
            self.collection.aggregate(
                [
                    {"$match": {"source": "dokploy", "project_name": {"$nin": ["", None]}}},
                    {"$group": {"_id": "$project_name", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1, "_id": 1}},
                    {"$limit": 8},
                ]
            )
        )
        top_projects = [
            {"name": str(row.get("_id") or "Unknown"), "count": int(row.get("count") or 0)}
            for row in project_rows
        ]

        created_trend_rows = list(
            self.collection.aggregate(
                [
                    {"$match": {"created_on": {"$gte": window_start}}},
                    {
                        "$group": {
                            "_id": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": "$created_on",
                                    "timezone": "UTC",
                                }
                            },
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"_id": 1}},
                ]
            )
        )
        created_by_day = {
            str(row.get("_id")): int(row.get("count") or 0) for row in created_trend_rows
        }

        labels = []
        created_series = []
        for offset in range(max(days, 1)):
            day = (window_start + timedelta(days=offset)).date().isoformat()
            labels.append(day)
            created_series.append(created_by_day.get(day, 0))

        return {
            "total_records": total_records,
            "dokploy_count": dokploy_count,
            "manual_count": manual_count,
            "unknown_source_count": unknown_source_count,
            "protected_count": protected_count,
            "created_last_7_days": created_last_7_days,
            "created_last_30_days": created_last_30_days,
            "top_projects": top_projects,
            "trend_labels": labels,
            "created_trend": created_series,
        }

    def log_audit_event(
        self,
        action: str,
        actor_email: str,
        status: str,
        record_name: str,
        target: str,
        details: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.audit_collection.insert_one(
            {
                "event_on": now,
                "action": action,
                "status": status,
                "record_name": record_name,
                "target": target,
                "details": details,
                "actor_email": actor_email,
                "ip_address": ip_address,
                "user_agent": user_agent,
            }
        )
