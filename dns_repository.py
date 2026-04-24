from datetime import datetime, timezone

from pymongo import MongoClient


class DNSRepository:
    def __init__(self, mongodb_uri: str, database_name: str = "dokploy", collection_name: str = "dns"):
        if not mongodb_uri:
            raise ValueError("MONGODB_URI is required")

        self.client = MongoClient(mongodb_uri)
        self.collection = self.client[database_name][collection_name]
        self.audit_collection = self.client[database_name]["dns_audit"]

    def upsert_record(self, record_name: str, target: str, actor_email: str) -> None:
        now = datetime.now(timezone.utc)
        self.collection.update_one(
            {"record_name": record_name},
            {
                "$set": {
                    "target": target,
                    "updated_on": now,
                    "updated_by": actor_email,
                },
                "$setOnInsert": {
                    "created_on": now,
                    "created_by": actor_email,
                },
            },
            upsert=True,
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
