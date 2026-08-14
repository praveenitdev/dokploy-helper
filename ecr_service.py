from __future__ import annotations

import re
from typing import Any

import boto3
from botocore.exceptions import ClientError


class EcrService:
    SKIPPED_APP_NAMES = {"dokploy", "dokploy-postgres", "dokploy-redis"}
    APP_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,200}$")

    def __init__(
        self,
        aws_region: str = "ap-south-1",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        aws_session_token: str = "",
        iam_role_arn: str = "",
        registry_id: str = "",
        repo_prefix: str = "dokploy",
        scan_on_push: bool = True,
        lifecycle_keep_count: int = 30,
    ):
        self.aws_region = aws_region
        self.registry_id = (registry_id or "").strip()
        self.repo_prefix = (repo_prefix or "dokploy").strip().strip("/")
        self.scan_on_push = scan_on_push
        self.lifecycle_keep_count = max(int(lifecycle_keep_count), 1)
        self.client = self._build_ecr_client(
            aws_region=aws_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            iam_role_arn=iam_role_arn,
        )
        if not self.registry_id:
            identity = boto3.client(
                "sts",
                region_name=aws_region,
                **self._base_session_kwargs(
                    aws_access_key_id,
                    aws_secret_access_key,
                    aws_session_token,
                ),
            ).get_caller_identity()
            self.registry_id = str(identity.get("Account") or "").strip()

    def _base_session_kwargs(
        self,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        aws_session_token: str,
    ) -> dict[str, str]:
        kwargs: dict[str, str] = {}
        if aws_access_key_id and aws_secret_access_key:
            kwargs["aws_access_key_id"] = aws_access_key_id
            kwargs["aws_secret_access_key"] = aws_secret_access_key
            if aws_session_token:
                kwargs["aws_session_token"] = aws_session_token
        return kwargs

    def _build_ecr_client(
        self,
        aws_region: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        aws_session_token: str,
        iam_role_arn: str,
    ):
        base_client_kwargs: dict[str, Any] = {"region_name": aws_region}
        base_client_kwargs.update(
            self._base_session_kwargs(
                aws_access_key_id,
                aws_secret_access_key,
                aws_session_token,
            )
        )

        if iam_role_arn:
            sts_client = boto3.client("sts", **base_client_kwargs)
            assumed = sts_client.assume_role(
                RoleArn=iam_role_arn,
                RoleSessionName="dokploy-helper-ecr-session",
            )
            creds = assumed["Credentials"]
            return boto3.client(
                "ecr",
                region_name=aws_region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )

        return boto3.client("ecr", **base_client_kwargs)

    def registry_host(self) -> str:
        if not self.registry_id:
            raise ValueError("ECR_REGISTRY_ID is required (or resolvable via STS)")
        return f"{self.registry_id}.dkr.ecr.{self.aws_region}.amazonaws.com"

    def repository_name_for_app(self, service_app_name: str) -> str:
        app_name = (service_app_name or "").strip()
        if not app_name:
            raise ValueError("service_app_name is required")
        if app_name in self.SKIPPED_APP_NAMES:
            raise ValueError(f"Skipped system app name: {app_name}")
        if not self.APP_NAME_PATTERN.match(app_name):
            raise ValueError(f"Invalid app name for ECR: {app_name}")
        return f"{self.repo_prefix}/{app_name}"

    def repository_uri(self, repository_name: str) -> str:
        return f"{self.registry_host()}/{repository_name}"

    def should_skip_app(self, service_app_name: str) -> bool:
        app_name = (service_app_name or "").strip()
        if not app_name or app_name in self.SKIPPED_APP_NAMES:
            return True
        return not self.APP_NAME_PATTERN.match(app_name)

    def ensure_repository(self, repository_name: str) -> dict[str, str]:
        clean_name = (repository_name or "").strip().strip("/")
        if not clean_name.startswith(f"{self.repo_prefix}/"):
            raise ValueError(f"Repository must be under prefix {self.repo_prefix}/")

        try:
            described = self.client.describe_repositories(repositoryNames=[clean_name])
            repositories = described.get("repositories") or []
            if repositories:
                uri = str(repositories[0].get("repositoryUri") or self.repository_uri(clean_name))
                return {"status": "exists", "repository_name": clean_name, "repository_uri": uri}
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            if error_code != "RepositoryNotFoundException":
                raise

        created = self.client.create_repository(
            repositoryName=clean_name,
            imageScanningConfiguration={"scanOnPush": self.scan_on_push},
            encryptionConfiguration={"encryptionType": "AES256"},
            tags=[
                {"Key": "Project", "Value": "dokploy"},
                {"Key": "ManagedBy", "Value": "dokploy-helper"},
            ],
        )
        repository = created.get("repository") or {}
        uri = str(repository.get("repositoryUri") or self.repository_uri(clean_name))

        try:
            self.client.put_lifecycle_policy(
                repositoryName=clean_name,
                lifecyclePolicyText=(
                    '{"rules":[{'
                    '"rulePriority":1,'
                    '"description":"Keep last N images",'
                    '"selection":{"tagStatus":"any","countType":"imageCountMoreThan",'
                    f'"countNumber":{self.lifecycle_keep_count}'
                    '},"action":{"type":"expire"}}]}'
                ),
            )
        except ClientError:
            pass

        return {"status": "created", "repository_name": clean_name, "repository_uri": uri}

    def list_prefixed_repositories(self) -> list[dict[str, str]]:
        repositories: list[dict[str, str]] = []
        paginator = self.client.get_paginator("describe_repositories")
        prefix = f"{self.repo_prefix}/"
        for page in paginator.paginate():
            for repository in page.get("repositories", []):
                name = str(repository.get("repositoryName") or "")
                if name != self.repo_prefix and not name.startswith(prefix):
                    continue
                repositories.append(
                    {
                        "repository_name": name,
                        "repository_uri": str(repository.get("repositoryUri") or ""),
                    }
                )
        repositories.sort(key=lambda item: item["repository_name"])
        return repositories
