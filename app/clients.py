from __future__ import annotations

import os
import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.governance import ConflictError, ValidationError


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float


class OAuthTokenProvider:
    """Mint and cache Databricks workspace OAuth M2M tokens.

    Databricks Apps inject DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET. The same service
    principal must be assigned to each target workspace that this App calls.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        timeout: float = 20.0,
    ):
        self.client_id = (client_id or os.getenv("DATABRICKS_CLIENT_ID") or "").strip()
        self.client_secret = (
            client_secret or os.getenv("DATABRICKS_CLIENT_SECRET") or ""
        ).strip()
        self.timeout = timeout
        self._tokens: dict[str, _CachedToken] = {}
        self._lock = threading.Lock()

    def token(self, host: str) -> str:
        normalized = host.rstrip("/")
        now = time.time()
        with self._lock:
            cached = self._tokens.get(normalized)
            if cached and cached.expires_at - 120 > now:
                return cached.access_token
            if not self.client_id or not self.client_secret:
                token = (os.getenv("DATABRICKS_TOKEN") or "").strip()
                if token:
                    return token
                raise ValidationError(
                    "Databricks OAuth credentials are unavailable. Configure the App service "
                    "principal or DATABRICKS_TOKEN for local development."
                )
            response = httpx.post(
                f"{normalized}/oidc/v1/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials", "scope": "all-apis"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            access_token = str(payload.get("access_token") or "")
            if not access_token:
                raise ValidationError("Databricks OAuth response did not include an access token.")
            expires_in = int(payload.get("expires_in") or 3600)
            self._tokens[normalized] = _CachedToken(access_token, now + expires_in)
            return access_token


class DatabricksRestClient:
    def __init__(self, host: str, token_provider: OAuthTokenProvider):
        self.host = host.rstrip("/")
        self.token_provider = token_provider

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        response = httpx.request(
            method,
            f"{self.host}{path}",
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {self.token_provider.token(self.host)}"},
            timeout=timeout,
        )
        if response.status_code == 404:
            return {"_not_found": True}
        response.raise_for_status()
        if not response.content:
            return {}
        return dict(response.json())

    def paged_get(
        self,
        path: str,
        *,
        list_key: str,
        params: dict[str, Any] | None = None,
        page_token_key: str = "page_token",
        next_token_key: str = "next_page_token",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        current = dict(params or {})
        while True:
            payload = self.request("GET", path, params=current)
            items.extend(dict(item) for item in payload.get(list_key) or ())
            token = payload.get(next_token_key)
            if not token:
                return items
            current[page_token_key] = token


class DeploymentDispatcher:
    def __init__(self, prod_host: str, token_provider: OAuthTokenProvider):
        self.client = DatabricksRestClient(prod_host, token_provider)

    def run(self, job_id: str, request_id: str, idempotency_key: str = "") -> str:
        normalized = str(job_id).strip()
        if not normalized:
            raise ConflictError(
                "No protected governance worker job is configured."
            )
        try:
            numeric_job_id = int(normalized)
        except ValueError as exc:
            raise ConflictError("Configured governance worker job ID is invalid.") from exc
        key = (idempotency_key or request_id).strip()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        payload = self.client.request(
            "POST",
            "/api/2.2/jobs/run-now",
            json={
                "job_id": numeric_job_id,
                "job_parameters": {"request_id": request_id},
                "idempotency_token": f"governance-{digest}"[:64],
            },
        )
        run_id = payload.get("run_id")
        if run_id is None:
            raise ConflictError("Governance worker dispatch did not return a run ID.")
        return str(run_id)


def server_hostname(host: str) -> str:
    parsed = urlsplit(host)
    if not parsed.hostname:
        raise ValidationError("Invalid Databricks workspace host.")
    return parsed.hostname
