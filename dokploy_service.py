from __future__ import annotations

from typing import Any, Set

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

    def _extract_ids(self, payload: Any, key_name: str, out: Set[str]) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key == key_name and isinstance(value, str) and value.strip():
                    out.add(value.strip())
                self._extract_ids(value, key_name, out)
            return

        if isinstance(payload, list):
            for item in payload:
                self._extract_ids(item, key_name, out)

    def _extract_domains(self, payload: Any, out: Set[str]) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                lower_key = key.lower()
                if lower_key in {"host", "domain"} and isinstance(value, str):
                    clean = value.strip().rstrip(".").lower()
                    if clean:
                        out.add(clean)

                if lower_key == "domains" and isinstance(value, list):
                    for item in value:
                        self._extract_domains(item, out)

                self._extract_domains(value, out)
            return

        if isinstance(payload, list):
            for item in payload:
                self._extract_domains(item, out)

    def list_project_service_domains(self) -> list[str]:
        projects_payload = self._get("project.all")

        domains: Set[str] = set()
        self._extract_domains(projects_payload, domains)

        application_ids: Set[str] = set()
        compose_ids: Set[str] = set()
        self._extract_ids(projects_payload, "applicationId", application_ids)
        self._extract_ids(projects_payload, "composeId", compose_ids)

        for application_id in application_ids:
            payload = self._get("domain.byApplicationId", {"applicationId": application_id})
            self._extract_domains(payload, domains)

        for compose_id in compose_ids:
            payload = self._get("domain.byComposeId", {"composeId": compose_id})
            self._extract_domains(payload, domains)

        return sorted(domains)