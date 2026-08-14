from datetime import datetime, timedelta, timezone

from pymongo import DESCENDING, MongoClient


class EcrRepository:
    def __init__(self, mongodb_uri: str, database_name: str = "dokploy", collection_name: str = "ecr"):
        if not mongodb_uri:
            raise ValueError("MONGODB_URI is required")

        self.client = MongoClient(mongodb_uri)
        self.collection = self.client[database_name][collection_name]
        self.collection.create_index([("repository_name", DESCENDING)], unique=True)

    def upsert_record(
        self,
        repository_name: str,
        repository_uri: str,
        actor_email: str,
        *,
        source: str = "dokploy",
        status: str = "exists",
        project_name: str = "",
        environment_name: str = "",
        service_name: str = "",
        service_type: str = "",
        service_app_name: str = "",
        last_error: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        normalized_source = (source or "dokploy").strip().lower()
        if normalized_source not in {"manual", "dokploy"}:
            normalized_source = "dokploy"

        display_actor = "Dokploy" if normalized_source == "dokploy" else actor_email
        set_fields = {
            "repository_uri": repository_uri,
            "updated_on": now,
            "updated_by": display_actor,
            "source": normalized_source,
            "status": status,
            "project_name": project_name or "",
            "environment_name": environment_name or "",
            "service_name": service_name or "",
            "service_type": service_type or "",
            "service_app_name": service_app_name or "",
            "last_error": last_error or "",
        }

        self.collection.update_one(
            {"repository_name": repository_name},
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "created_on": now,
                    "created_by": display_actor,
                    "protected": False,
                },
            },
            upsert=True,
        )

    def list_records(self) -> list[dict]:
        cursor = self.collection.find({}, {"_id": 0}).sort("updated_on", DESCENDING)
        return list(cursor)

    def is_record_protected(self, repository_name: str) -> bool:
        document = self.collection.find_one(
            {"repository_name": repository_name},
            {"_id": 0, "protected": 1},
        )
        if not document:
            return False
        return bool(document.get("protected", False))

    def get_dashboard_metrics(self, days: int = 14) -> dict:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=max(days, 1) - 1)
        last_7_days = now - timedelta(days=7)

        total_records = self.collection.count_documents({})
        created_count = self.collection.count_documents({"status": "created"})
        exists_count = self.collection.count_documents({"status": "exists"})
        failed_count = self.collection.count_documents({"status": "failed"})
        protected_count = self.collection.count_documents({"protected": True})
        created_last_7_days = self.collection.count_documents(
            {"created_on": {"$gte": last_7_days}, "status": "created"}
        )

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
            "created_count": created_count,
            "exists_count": exists_count,
            "failed_count": failed_count,
            "protected_count": protected_count,
            "created_last_7_days": created_last_7_days,
            "trend_labels": labels,
            "created_trend": created_series,
        }
