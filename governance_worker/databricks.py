from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.clients import DatabricksRestClient, OAuthTokenProvider
from app.governance import normalized_asset_name
from app.models import AssetSelection, ProjectRecord, TagEvidence


@dataclass(frozen=True)
class BundleResource:
    resource_type: str
    resource_key: str
    resource_id: str
    name: str
    url: str = ""


def parse_bundle_summary(payload: dict[str, Any]) -> list[BundleResource]:
    resources: list[BundleResource] = []
    buckets = payload.get("resources") or {}
    kind_map = {
        "jobs": "job",
        "pipelines": "pipeline",
        "dashboards": "dashboard",
        "apps": "app",
    }
    if not isinstance(buckets, dict):
        return resources
    for bucket_name, resource_type in kind_map.items():
        values = buckets.get(bucket_name) or {}
        if not isinstance(values, dict):
            continue
        for key, item in values.items():
            item = item or {}
            resources.append(
                BundleResource(
                    resource_type=resource_type,
                    resource_key=str(key),
                    resource_id=str(item.get("id") or item.get("resource_id") or ""),
                    name=str(item.get("name") or item.get("display_name") or key),
                    url=str(item.get("url") or ""),
                )
            )
    return resources


def load_bundle_summary(path: Path) -> list[BundleResource]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return parse_bundle_summary(payload if isinstance(payload, dict) else {})


class ProductionTagVerifier:
    def __init__(
        self,
        host: str,
        *,
        token_provider: OAuthTokenProvider | None = None,
        bundle_resources: list[BundleResource] | None = None,
    ):
        self.client = DatabricksRestClient(host, token_provider or OAuthTokenProvider())
        self.bundle_resources = bundle_resources or []
        self._jobs: list[dict[str, Any]] | None = None
        self._pipelines: list[dict[str, Any]] | None = None

    def verify(
        self,
        project: ProjectRecord,
        assets: tuple[dict[str, Any], ...],
        required_tags: dict[str, str],
        *,
        dev_catalog: str,
        prod_catalog: str,
    ) -> tuple[TagEvidence, ...]:
        evidence: list[TagEvidence] = []
        for raw in assets:
            asset = AssetSelection.model_validate(
                {
                    key: raw.get(key, "")
                    for key in (
                        "resource_type",
                        "resource_id",
                        "resource_name",
                        "resource_path",
                        "catalog_name",
                        "schema_name",
                    )
                }
            )
            tags, actual = self._locate_and_tag(
                asset,
                required_tags,
                dev_catalog=dev_catalog,
                prod_catalog=prod_catalog,
            )
            for key, expected in required_tags.items():
                value = tags.get(key, "")
                if value == expected:
                    status = "PASS"
                    detail = "Mandatory production tag matches."
                elif not value:
                    status = "MISSING"
                    detail = "Mandatory production tag is missing."
                else:
                    status = "CONFLICT"
                    detail = "Mandatory production tag has a conflicting value."
                evidence.append(
                    TagEvidence(
                        environment="prod",
                        resource_type=asset.resource_type,
                        resource_id=actual.get("resource_id", ""),
                        resource_name=actual.get("resource_name", asset.resource_name),
                        resource_path=actual.get("resource_path", ""),
                        tag_key=key,
                        expected_value=expected,
                        actual_value=value,
                        validation_result=status,
                        detail=detail,
                    )
                )
        return tuple(evidence)

    def _locate_and_tag(
        self,
        asset: AssetSelection,
        required_tags: dict[str, str],
        *,
        dev_catalog: str,
        prod_catalog: str,
    ) -> tuple[dict[str, str], dict[str, str]]:
        if asset.resource_type == "job":
            item = self._match_job(asset.resource_name, required_tags["project_tag"])
            return self._string_tags((item.get("settings") or {}).get("tags")), {
                "resource_id": str(item.get("job_id") or ""),
                "resource_name": str((item.get("settings") or {}).get("name") or asset.resource_name),
            }
        if asset.resource_type == "pipeline":
            item = self._match_pipeline(asset.resource_name, required_tags["project_tag"])
            spec = dict(item.get("spec") or {})
            return self._string_tags(spec.get("tags")), {
                "resource_id": str(item.get("pipeline_id") or ""),
                "resource_name": str(spec.get("name") or asset.resource_name),
            }
        if asset.resource_type in {"dashboard", "app", "notebook"}:
            resource = self._match_bundle_resource(asset)
            if not resource or not resource.resource_id:
                return {}, {"resource_name": asset.resource_name}
            entity_type = {
                "dashboard": "dashboards",
                "app": "apps",
                "notebook": "notebooks",
            }[asset.resource_type]
            tags = self._workspace_tags(entity_type, resource.resource_id, required_tags)
            tags = self._apply_workspace_missing(
                entity_type, resource.resource_id, tags, required_tags
            )
            return tags, {
                "resource_id": resource.resource_id,
                "resource_name": resource.name,
                "resource_path": resource.url,
            }
        if asset.resource_type in {"schema", "table", "view", "volume"}:
            catalog = prod_catalog if asset.catalog_name in {"", dev_catalog} else asset.catalog_name
            entity_name = (
                f"{catalog}.{asset.schema_name}"
                if asset.resource_type == "schema"
                else f"{catalog}.{asset.schema_name}.{asset.resource_name}"
            )
            entity_type = {
                "schema": "schemas",
                "table": "tables",
                "view": "tables",
                "volume": "volumes",
            }[asset.resource_type]
            tags = self._uc_tags(entity_type, entity_name)
            tags = self._apply_uc_missing(entity_type, entity_name, tags, required_tags)
            return tags, {
                "resource_name": asset.resource_name,
                "resource_path": entity_name,
            }
        return {}, {"resource_name": asset.resource_name}

    @staticmethod
    def _string_tags(value: Any) -> dict[str, str]:
        return {str(key): str(item) for key, item in dict(value or {}).items()}

    def _match_job(self, name: str, project_id: str) -> dict[str, Any]:
        if self._jobs is None:
            summaries = self.client.paged_get(
                "/api/2.2/jobs/list", list_key="jobs", params={"limit": 100, "expand_tasks": False}
            )
            self._jobs = []
            for item in summaries:
                job_id = item.get("job_id")
                full = self.client.request("GET", "/api/2.2/jobs/get", params={"job_id": job_id})
                if not full.get("_not_found"):
                    self._jobs.append(full)
        candidates = []
        for item in self._jobs:
            settings = dict(item.get("settings") or {})
            if normalized_asset_name(str(settings.get("name") or "")) == normalized_asset_name(name):
                candidates.append(item)
        exact_owner = [
            item
            for item in candidates
            if str(((item.get("settings") or {}).get("tags") or {}).get("project_tag") or "")
            == project_id
        ]
        return (exact_owner or candidates or [{}])[0]

    def _match_pipeline(self, name: str, project_id: str) -> dict[str, Any]:
        if self._pipelines is None:
            summaries = self.client.paged_get(
                "/api/2.0/pipelines", list_key="statuses", params={"max_results": 100}
            )
            self._pipelines = []
            for item in summaries:
                pipeline_id = item.get("pipeline_id")
                full = self.client.request("GET", f"/api/2.0/pipelines/{quote(str(pipeline_id), safe='')}")
                if not full.get("_not_found"):
                    self._pipelines.append(full)
        candidates = []
        for item in self._pipelines:
            spec = dict(item.get("spec") or {})
            if normalized_asset_name(str(spec.get("name") or "")) == normalized_asset_name(name):
                candidates.append(item)
        exact_owner = [
            item
            for item in candidates
            if str(((item.get("spec") or {}).get("tags") or {}).get("project_tag") or "")
            == project_id
        ]
        return (exact_owner or candidates or [{}])[0]

    def _match_bundle_resource(self, asset: AssetSelection) -> BundleResource | None:
        candidates = [
            item
            for item in self.bundle_resources
            if item.resource_type == asset.resource_type
            and normalized_asset_name(item.name) == normalized_asset_name(asset.resource_name)
        ]
        return candidates[0] if candidates else None

    def _workspace_tags(
        self, entity_type: str, entity_id: str, required: dict[str, str]
    ) -> dict[str, str]:
        tags: dict[str, str] = {}
        for key in required:
            payload = self.client.request(
                "GET",
                "/api/2.0/entity-tag-assignments/"
                f"{entity_type}/{quote(entity_id, safe='')}/tags/{quote(key, safe='')}",
            )
            if not payload.get("_not_found"):
                tags[key] = str(payload.get("tag_value") or "")
        return tags

    def _apply_workspace_missing(
        self,
        entity_type: str,
        entity_id: str,
        existing: dict[str, str],
        required: dict[str, str],
    ) -> dict[str, str]:
        result = dict(existing)
        if result.get("project_tag") not in {None, "", required["project_tag"]}:
            return result
        for key, value in required.items():
            if result.get(key):
                continue
            self.client.request(
                "POST",
                "/api/2.0/entity-tag-assignments",
                json={
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "tag_key": key,
                    "tag_value": value,
                },
            )
            result[key] = value
        return result

    def _uc_tags(self, entity_type: str, entity_name: str) -> dict[str, str]:
        rows = self.client.paged_get(
            "/api/2.1/unity-catalog/entity-tag-assignments/"
            f"{entity_type}/{quote(entity_name, safe='')}/tags",
            list_key="tag_assignments",
            params={"max_results": 100},
        )
        return {
            str(item.get("tag_key") or ""): str(item.get("tag_value") or "")
            for item in rows
            if item.get("tag_key")
        }

    def _apply_uc_missing(
        self,
        entity_type: str,
        entity_name: str,
        existing: dict[str, str],
        required: dict[str, str],
    ) -> dict[str, str]:
        result = dict(existing)
        if result.get("project_tag") not in {None, "", required["project_tag"]}:
            return result
        for key, value in required.items():
            if result.get(key):
                continue
            self.client.request(
                "POST",
                "/api/2.1/unity-catalog/entity-tag-assignments",
                json={
                    "entity_type": entity_type,
                    "entity_name": entity_name,
                    "tag_key": key,
                    "tag_value": value,
                },
            )
            result[key] = value
        return result
