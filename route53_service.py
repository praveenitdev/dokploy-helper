from __future__ import annotations

from typing import Dict, List, Optional

import boto3


class Route53Service:
    def __init__(
        self,
        hosted_zone_id: str = "",
        hosted_zone_name: str = "",
        aws_region: str = "ap-south-1",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        aws_session_token: str = "",
        iam_role_arn: str = "",
    ):
        self.hosted_zone_id = hosted_zone_id
        self.hosted_zone_name = hosted_zone_name.rstrip(".").lower()
        self.client = self._build_route53_client(
            aws_region=aws_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            iam_role_arn=iam_role_arn,
        )

    def _build_route53_client(
        self,
        aws_region: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        aws_session_token: str,
        iam_role_arn: str,
    ):
        base_client_kwargs = {"region_name": aws_region}
        if aws_access_key_id and aws_secret_access_key:
            base_client_kwargs["aws_access_key_id"] = aws_access_key_id
            base_client_kwargs["aws_secret_access_key"] = aws_secret_access_key
            if aws_session_token:
                base_client_kwargs["aws_session_token"] = aws_session_token

        if iam_role_arn:
            sts_client = boto3.client("sts", **base_client_kwargs)
            assume_kwargs = {
                "RoleArn": iam_role_arn,
                "RoleSessionName": "dokploy-helper-session",
            }

            assumed = sts_client.assume_role(**assume_kwargs)
            creds = assumed["Credentials"]

            return boto3.client(
                "route53",
                region_name=aws_region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )

        return boto3.client("route53", **base_client_kwargs)

    def resolve_hosted_zone_id(self) -> str:
        if self.hosted_zone_id:
            return self.hosted_zone_id

        if not self.hosted_zone_name:
            raise ValueError("HOSTED_ZONE_NAME is required when HOSTED_ZONE_ID is not set")

        paginator = self.client.get_paginator("list_hosted_zones")
        wanted_name = f"{self.hosted_zone_name}."
        for page in paginator.paginate():
            for zone in page.get("HostedZones", []):
                if zone.get("Name") == wanted_name:
                    self.hosted_zone_id = zone["Id"].split("/")[-1]
                    return self.hosted_zone_id

        raise ValueError(f"Hosted zone not found for name: {self.hosted_zone_name}")

    def normalize_record_name(self, name: str) -> str:
        zone = self.hosted_zone_name
        clean = name.strip().rstrip(".").lower()

        if clean.endswith(zone):
            return f"{clean}."

        return f"{clean}.{zone}."

    def validate_record_name(self, name: str) -> None:
        zone = self.hosted_zone_name
        clean = name.strip().rstrip(".").lower()
        if not clean:
            raise ValueError("Record name is required")
        if clean == zone:
            raise ValueError("CNAME cannot be created at the hosted zone apex")
        if not clean.endswith(zone):
            raise ValueError(f"Record must be inside hosted zone {zone}")

    def normalize_target(self, target: str) -> str:
        clean = target.strip().rstrip(".")
        if not clean:
            raise ValueError("Target value is required")
        return f"{clean}."

    def list_cname_records(self) -> List[Dict[str, str]]:
        zone_id = self.resolve_hosted_zone_id()
        paginator = self.client.get_paginator("list_resource_record_sets")
        records: List[Dict[str, str]] = []

        for page in paginator.paginate(HostedZoneId=zone_id):
            for rset in page.get("ResourceRecordSets", []):
                if rset.get("Type") != "CNAME":
                    continue

                resource_records = rset.get("ResourceRecords", [])
                first_value = resource_records[0]["Value"] if resource_records else ""
                records.append(
                    {
                        "name": rset.get("Name", "").rstrip("."),
                        "value": first_value.rstrip("."),
                        "ttl": str(rset.get("TTL", "")),
                    }
                )

        records.sort(key=lambda x: x["name"])
        return records

    def upsert_cname(self, name: str, target: str, ttl: int = 300) -> None:
        normalized_name = self.normalize_record_name(name)
        self.validate_record_name(normalized_name)
        normalized_target = self.normalize_target(target)
        zone_id = self.resolve_hosted_zone_id()

        self.client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "Upsert CNAME from dokploy-helper dashboard",
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": normalized_name,
                            "Type": "CNAME",
                            "TTL": int(ttl),
                            "ResourceRecords": [{"Value": normalized_target}],
                        },
                    }
                ],
            },
        )

    def delete_cname(self, name: str, target: str, ttl: Optional[int] = None) -> None:
        normalized_name = self.normalize_record_name(name)
        normalized_target = self.normalize_target(target)
        zone_id = self.resolve_hosted_zone_id()

        payload = {
            "Name": normalized_name,
            "Type": "CNAME",
            "ResourceRecords": [{"Value": normalized_target}],
        }
        if ttl is not None:
            payload["TTL"] = int(ttl)
        else:
            payload["TTL"] = 300

        self.client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "Delete CNAME from dokploy-helper dashboard",
                "Changes": [{"Action": "DELETE", "ResourceRecordSet": payload}],
            },
        )
