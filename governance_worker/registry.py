from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.clients import OAuthTokenProvider
from app.models import (
    SourceValidationClaimResponse,
    SourceValidationCompletion,
    WorkerClaimResponse,
    WorkerCompletion,
)


@dataclass(frozen=True)
class RegistryClientConfig:
    app_url: str
    registry_host: str
    timeout_seconds: float = 60.0

    @classmethod
    def from_environment(cls) -> "RegistryClientConfig":
        app_url = (os.getenv("REGISTRY_APP_URL") or "").strip().rstrip("/")
        registry_host = (os.getenv("REGISTRY_DATABRICKS_HOST") or "").strip().rstrip("/")
        if not app_url or not registry_host:
            raise RuntimeError(
                "REGISTRY_APP_URL and REGISTRY_DATABRICKS_HOST are required by governance workers."
            )
        return cls(app_url=app_url, registry_host=registry_host)


class RegistryClient:
    def __init__(
        self,
        config: RegistryClientConfig | None = None,
        token_provider: OAuthTokenProvider | None = None,
    ):
        self.config = config or RegistryClientConfig.from_environment()
        self.tokens = token_provider or OAuthTokenProvider(
            client_id=os.getenv("REGISTRY_CLIENT_ID") or os.getenv("DATABRICKS_CLIENT_ID"),
            client_secret=os.getenv("REGISTRY_CLIENT_SECRET")
            or os.getenv("DATABRICKS_CLIENT_SECRET"),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.tokens.token(self.config.registry_host)}",
            "X-Governance-Request": "v4",
            "Content-Type": "application/json",
        }
        # This is for the memory-backed local test configuration only. The production App must
        # rely on Databricks-authenticated identity headers and leave this variable unset.
        local_actor = (os.getenv("REGISTRY_LOCAL_WORKER_ACTOR") or "").strip()
        if local_actor:
            headers["X-Service-Principal"] = local_actor
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query_string = f"?{urlencode(query)}" if query else ""
        response = httpx.request(
            method,
            f"{self.config.app_url}{path}{query_string}",
            headers=self._headers(),
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        if not response.is_success:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(f"Registry API returned HTTP {response.status_code}: {detail}")
        if not response.content:
            return {}
        return dict(response.json())

    def next_source_validation(self) -> str:
        payload = self._request("GET", "/api/v4/worker/source-validations/next")
        return str(payload.get("request_id") or "")

    def claim_source_validation(
        self, request_id: str, worker_run_id: str = ""
    ) -> SourceValidationClaimResponse:
        payload = self._request(
            "POST",
            f"/api/v4/worker/production-requests/{request_id}/source-validation/claim",
            payload={"worker_run_id": worker_run_id},
        )
        return SourceValidationClaimResponse.model_validate(payload)

    def complete_source_validation(
        self, request_id: str, completion: SourceValidationCompletion
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v4/worker/production-requests/{request_id}/source-validation/complete",
            payload=completion.model_dump(mode="json"),
        )

    def next_production_request(self, deployer_profile: str = "") -> str:
        query = {"deployer_profile": deployer_profile} if deployer_profile else None
        payload = self._request(
            "GET", "/api/v4/worker/production-requests/next", query=query
        )
        return str(payload.get("request_id") or "")

    def claim(self, request_id: str, worker_run_id: str = "") -> WorkerClaimResponse:
        payload = self._request(
            "POST",
            f"/api/v4/worker/production-requests/{request_id}/claim",
            payload={"worker_run_id": worker_run_id},
        )
        return WorkerClaimResponse.model_validate(payload)

    def complete(self, request_id: str, completion: WorkerCompletion) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v4/worker/production-requests/{request_id}/complete",
            payload=completion.model_dump(mode="json"),
        )
