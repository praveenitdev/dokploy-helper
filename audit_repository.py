from datetime import datetime, timedelta, timezone

from pymongo import DESCENDING, MongoClient


class AuditRepository:
    def __init__(self, mongodb_uri: str, database_name: str = "dokploy", collection_name: str = "audit"):
        if not mongodb_uri:
            raise ValueError("MONGODB_URI is required")

        self.client = MongoClient(mongodb_uri)
        self.collection = self.client[database_name][collection_name]
        self.collection.create_index([("event_on", DESCENDING)])

    def log_event(
        self,
        module: str,
        action: str,
        status: str,
        actor_email: str,
        entity_name: str,
        details: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        self.collection.insert_one(
            {
                "event_on": datetime.now(timezone.utc),
                "module": module,
                "action": action,
                "status": status,
                "actor_email": actor_email,
                "entity_name": entity_name,
                "details": details,
                "ip_address": ip_address,
                "user_agent": user_agent,
            }
        )

    def list_events(self, limit: int = 200) -> list[dict]:
        cursor = self.collection.find({}, {"_id": 0}).sort("event_on", DESCENDING).limit(limit)
        return list(cursor)

    def get_dashboard_metrics(self, days: int = 14) -> dict:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=max(days, 1) - 1)
        last_7_days = now - timedelta(days=7)

        total_events = self.collection.count_documents({})
        events_last_7_days = self.collection.count_documents({"event_on": {"$gte": last_7_days}})
        failed_last_7_days = self.collection.count_documents(
            {"event_on": {"$gte": last_7_days}, "status": "FAILED"}
        )
        sync_events_last_7_days = self.collection.count_documents(
            {
                "event_on": {"$gte": last_7_days},
                "action": {"$in": ["SYNC_DOKPLOY", "SYNC_DOKPLOY_AUTO"]},
            }
        )
        sync_failed_last_7_days = self.collection.count_documents(
            {
                "event_on": {"$gte": last_7_days},
                "action": {"$in": ["SYNC_DOKPLOY", "SYNC_DOKPLOY_AUTO"]},
                "status": "FAILED",
            }
        )

        activity_rows = list(
            self.collection.aggregate(
                [
                    {"$match": {"event_on": {"$gte": window_start}}},
                    {
                        "$group": {
                            "_id": {
                                "day": {
                                    "$dateToString": {
                                        "format": "%Y-%m-%d",
                                        "date": "$event_on",
                                        "timezone": "UTC",
                                    }
                                },
                                "status": "$status",
                            },
                            "count": {"$sum": 1},
                        }
                    },
                ]
            )
        )

        success_by_day: dict[str, int] = {}
        failed_by_day: dict[str, int] = {}
        for row in activity_rows:
            group_id = row.get("_id") or {}
            day = str(group_id.get("day") or "")
            status = str(group_id.get("status") or "").upper()
            count = int(row.get("count") or 0)
            if not day:
                continue
            if status == "FAILED":
                failed_by_day[day] = failed_by_day.get(day, 0) + count
            else:
                success_by_day[day] = success_by_day.get(day, 0) + count

        labels = []
        success_series = []
        failed_series = []
        for offset in range(max(days, 1)):
            day = (window_start + timedelta(days=offset)).date().isoformat()
            labels.append(day)
            success_series.append(success_by_day.get(day, 0))
            failed_series.append(failed_by_day.get(day, 0))

        action_rows = list(
            self.collection.aggregate(
                [
                    {"$match": {"event_on": {"$gte": last_7_days}}},
                    {"$group": {"_id": "$action", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 6},
                ]
            )
        )
        top_actions = [
            {"action": str(row.get("_id") or "UNKNOWN"), "count": int(row.get("count") or 0)}
            for row in action_rows
        ]

        recent_events = self.list_events(limit=8)

        return {
            "total_events": total_events,
            "events_last_7_days": events_last_7_days,
            "failed_last_7_days": failed_last_7_days,
            "sync_events_last_7_days": sync_events_last_7_days,
            "sync_failed_last_7_days": sync_failed_last_7_days,
            "trend_labels": labels,
            "success_trend": success_series,
            "failed_trend": failed_series,
            "top_actions": top_actions,
            "recent_events": recent_events,
        }
