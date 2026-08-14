from __future__ import annotations

from typing import Any

import requests


class DokployService:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 20):
        clean_base_url = (base_url or "").strip().rstrip("/")
        clean_api_key = (api_key or "").strip()
        if not clean_base_url:
            raise ValueError("DOKPLOY_BASE_URL is required")
        if not clean_api_key:
            raise ValueError("DOKPLOY_API_KEY is required")

        if clean_base_url.endswith("/api"):
            self.api_base_url = clean_base_url
        else:
            self.api_base_url = f"{clean_base_url}/api"

        self.timeout_seconds = max(int(timeout_seconds), 1)
        self.headers = {
            "accept": "application/json",
            "x-api-key": clean_api_key,
        }

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_base_url}/{endpoint}"
        response = requests.get(url, headers=self.headers, params=params or {}, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise RuntimeError(f"Dokploy API error at {endpoint}: HTTP {response.status_code} - {response.text[:200]}")

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Dokploy API returned non-JSON response for {endpoint}") from exc

    def _normalize_host(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().rstrip(".").lower()

    def _domain_entries_from_payload(
        self,
        payload: Any,
        *,
        project_name: str,
        environment_name: str,
        service_name: str,
        service_type: str,
        service_app_name: str,
    ) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []

        entries: list[dict[str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue

            host = self._normalize_host(item.get("host") or item.get("domain"))
            if not host:
                continue

            application = item.get("application") if isinstance(item.get("application"), dict) else {}
            compose = item.get("compose") if isinstance(item.get("compose"), dict) else {}

            resolved_service_name = (
                service_name
                or str(application.get("name") or "").strip()
                or str(compose.get("name") or "").strip()
            )
            resolved_service_app_name = (
                service_app_name
                or str(application.get("appName") or "").strip()
                or str(compose.get("appName") or "").strip()
            )

            entries.append(
                {
                    "host": host,
                    "project_name": project_name,
                    "environment_name": environment_name,
                    "service_name": resolved_service_name,
                    "service_type": service_type,
                    "service_app_name": resolved_service_app_name,
                    "domain_created_at": str(item.get("createdAt") or "").strip(),
                    "domain_id": str(item.get("domainId") or "").strip(),
                }
            )

        return entries

    def list_project_service_domain_details(self) -> list[dict[str, str]]:
        projects_payload = self._get("project.all")
        if not isinstance(projects_payload, list):
            return []

        domains_by_host: dict[str, dict[str, str]] = {}

        for project in projects_payload:
            if not isinstance(project, dict):
                continue

            project_name = str(project.get("name") or "").strip()
            environments = project.get("environments")
            if not isinstance(environments, list):
                continue

            for environment in environments:
                if not isinstance(environment, dict):
                    continue

                environment_name = str(environment.get("name") or "").strip()

                applications = environment.get("applications")
                if isinstance(applications, list):
                    for application in applications:
                        if not isinstance(application, dict):
                            continue

                        application_id = str(application.get("applicationId") or "").strip()
                        if not application_id:
                            continue

                        payload = self._get("domain.byApplicationId", {"applicationId": application_id})
                        for entry in self._domain_entries_from_payload(
                            payload,
                            project_name=project_name,
                            environment_name=environment_name,
                            service_name=str(application.get("name") or "").strip(),
                            service_type="application",
                            service_app_name=str(application.get("appName") or "").strip(),
                        ):
                            domains_by_host[entry["host"]] = entry

                composes = environment.get("compose")
                if isinstance(composes, list):
                    for compose in composes:
                        if not isinstance(compose, dict):
                            continue

                        compose_id = str(compose.get("composeId") or "").strip()
                        if not compose_id:
                            continue

                        payload = self._get("domain.byComposeId", {"composeId": compose_id})
                        for entry in self._domain_entries_from_payload(
                            payload,
                            project_name=project_name,
                            environment_name=environment_name,
                            service_name=str(compose.get("name") or "").strip(),
                            service_type="compose",
                            service_app_name=str(compose.get("appName") or "").strip(),
                        ):
                            domains_by_host[entry["host"]] = entry

        return [domains_by_host[host] for host in sorted(domains_by_host.keys())]

    def list_project_service_domains(self) -> list[str]:
        return [entry["host"] for entry in self.list_project_service_domain_details()]

    def _resolve_service_app_name(
        self,
        *,
        service_type: str,
        service_id: str,
        inline_app_name: str,
        inline_service_name: str,
    ) -> tuple[str, str]:
        service_app_name = (inline_app_name or "").strip()
        service_name = (inline_service_name or "").strip()
        if service_app_name:
            return service_app_name, service_name

        clean_id = (service_id or "").strip()
        if not clean_id:
            return "", service_name

        try:
            if service_type == "application":
                detail = self._get("application.one", {"applicationId": clean_id})
            elif service_type == "compose":
                detail = self._get("compose.one", {"composeId": clean_id})
            else:
                return "", service_name
        except RuntimeError:
            return "", service_name

        if not isinstance(detail, dict):
            return "", service_name

        service_app_name = str(detail.get("appName") or "").strip()
        if not service_name:
            service_name = str(detail.get("name") or "").strip()
        return service_app_name, service_name

    def list_project_service_apps(self) -> list[dict[str, str]]:
        projects_payload = self._get("project.all")
        if not isinstance(projects_payload, list):
            return []

        apps_by_name: dict[str, dict[str, str]] = {}

        for project in projects_payload:
            if not isinstance(project, dict):
                continue

            project_name = str(project.get("name") or "").strip()
            environments = project.get("environments")
            if not isinstance(environments, list):
                continue

            for environment in environments:
                if not isinstance(environment, dict):
                    continue

                environment_name = str(environment.get("name") or "").strip()

                applications = environment.get("applications")
                if isinstance(applications, list):
                    for application in applications:
                        if not isinstance(application, dict):
                            continue

                        service_app_name, service_name = self._resolve_service_app_name(
                            service_type="application",
                            service_id=str(application.get("applicationId") or ""),
                            inline_app_name=str(application.get("appName") or ""),
                            inline_service_name=str(application.get("name") or ""),
                        )
                        if not service_app_name:
                            continue

                        apps_by_name[service_app_name] = {
                            "project_name": project_name,
                            "environment_name": environment_name,
                            "service_name": service_name,
                            "service_type": "application",
                            "service_app_name": service_app_name,
                        }

                composes = environment.get("compose")
                if isinstance(composes, list):
                    for compose in composes:
                        if not isinstance(compose, dict):
                            continue

                        service_app_name, service_name = self._resolve_service_app_name(
                            service_type="compose",
                            service_id=str(compose.get("composeId") or ""),
                            inline_app_name=str(compose.get("appName") or ""),
                            inline_service_name=str(compose.get("name") or ""),
                        )
                        if not service_app_name:
                            continue

                        apps_by_name[service_app_name] = {
                            "project_name": project_name,
                            "environment_name": environment_name,
                            "service_name": service_name,
                            "service_type": "compose",
                            "service_app_name": service_app_name,
                        }

        return [apps_by_name[name] for name in sorted(apps_by_name.keys())]
