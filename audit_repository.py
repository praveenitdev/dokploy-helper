from datetime import datetime, timezone

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
