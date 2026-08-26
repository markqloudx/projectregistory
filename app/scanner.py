from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.parse import quote

from app.clients import DatabricksRestClient, OAuthTokenProvider, server_hostname
from app.governance import ConflictError, ValidationError, expected_tags
from app.models import AssetScanResponse, AssetSelection, ScannedAsset
from app.settings import Settings


class AssetScanner(Protocol):
    def scan(
        self,
        project: dict[str, Any],
        environment: str,
        assets: Iterable[AssetSelection] | None = None,
    ) -> AssetScanResponse: ...


class StaticAssetScanner:
    """Deterministic scanner used by tests and local demonstrations."""

    def __init__(self, assets: Iterable[ScannedAsset] = ()):
        self.assets = list(assets)

    def scan(
        self,
        project: dict[str, Any],
        environment: str,
        assets: Iterable[AssetSelection] | None = None,
    ) -> AssetScanResponse:
        expected = expected_tags(project, environment)
        if assets is None:
            values = list(self.assets)
        else:
            requested = {
                _asset_key(item): item
                for item in assets
            }
            values = []
            known = {_asset_key(item): item for item in self.assets}
            for key, selection in requested.items():
                if key in known:
                    values.append(known[key])
                else:
                    values.append(
                        ScannedAsset(
                            **selection.model_dump(),
                            tags={},
                            compliance_status="NOT_ACCESSIBLE",
                            detail="Asset was not found by the local test scanner.",
                        )
                    )
        return AssetScanResponse(
            project_id=str(project["project_id"]),
            environment=environment,
            expected_tags=expected,
            assets=tuple(values),
        )


class DatabricksAssetScanner:
    def __init__(self, settings: Settings, token_provider: OAuthTokenProvider | None = None):
        self.settings = settings
        self.token_provider = token_provider or OAuthTokenProvider()

    def scan(
        self,
        project: dict[str, Any],
        environment: str,
        assets: Iterable[AssetSelection] | None = None,
    ) -> AssetScanResponse:
        if environment not in {"dev", "prod"}:
            raise ValidationError("Environment must be dev or prod.")
        expected = expected_tags(project, environment)
        host = self.settings.dev_host if environment == "dev" else self.settings.prod_host
        catalog = self.settings.dev_catalog if environment == "dev" else self.settings.prod_catalog
        warehouse_id = (
            self.settings.dev_warehouse_id
            if environment == "dev"
            else self.settings.prod_warehouse_id
        )
        client = DatabricksRestClient(host, self.token_provider)
        warnings: list[str] = []

        if assets is not None:
            values = [
                self._validate_one(client, host, warehouse_id, project, environment, item)
                for item in assets
            ]
            return AssetScanResponse(
                project_id=str(project["project_id"]),
                environment=environment,
                expected_tags=expected,
                assets=tuple(values),
                warnings=tuple(warnings),
            )

        values: list[ScannedAsset] = []
        try:
            values.extend(self._scan_jobs(client, project, environment))
        except Exception as exc:  # pragma: no cover - depends on live APIs
            warnings.append(f"Job scan failed: {exc}")
        try:
            values.extend(self._scan_pipelines(client, project, environment))
        except Exception as exc:  # pragma: no cover - depends on live APIs
            warnings.append(f"Pipeline scan failed: {exc}")
        if warehouse_id:
            try:
                values.extend(
                    self._scan_uc_assets(host, warehouse_id, catalog, project, environment)
                )
            except Exception as exc:  # pragma: no cover - depends on live warehouse
                warnings.append(f"Unity Catalog scan failed: {exc}")
        else:
            warnings.append(
                f"{environment.upper()} SQL warehouse is not configured; Unity Catalog assets "
                "were not scanned."
            )
        values.sort(key=lambda item: (item.resource_type, item.resource_name.lower()))
        return AssetScanResponse(
            project_id=str(project["project_id"]),
            environment=environment,
            expected_tags=expected,
            assets=tuple(values),
            warnings=tuple(warnings),
        )

    def _scan_jobs(
        self, client: DatabricksRestClient, project: dict[str, Any], environment: str
    ) -> list[ScannedAsset]:
        expected = expected_tags(project, environment)
        jobs = client.paged_get(
            "/api/2.2/jobs/list",
            list_key="jobs",
            params={"limit": 100, "expand_tasks": "false"},
        )
        results: list[ScannedAsset] = []
        for job in jobs:
            settings = dict(job.get("settings") or {})
            tags = {str(k): str(v) for k, v in (settings.get("tags") or {}).items()}
            if tags.get("project_tag") != expected["project_tag"]:
                continue
            results.append(
                _scanned_asset(
                    AssetSelection(
                        resource_type="job",
                        resource_id=str(job.get("job_id") or ""),
                        resource_name=str(settings.get("name") or job.get("job_id") or "Unnamed job"),
                    ),
                    tags,
                    expected,
                )
            )
        return results

    def _scan_pipelines(
        self, client: DatabricksRestClient, project: dict[str, Any], environment: str
    ) -> list[ScannedAsset]:
        expected = expected_tags(project, environment)
        statuses = client.paged_get(
            "/api/2.0/pipelines",
            list_key="statuses",
            params={"max_results": 100},
        )
        results: list[ScannedAsset] = []
        for status in statuses:
            pipeline_id = str(status.get("pipeline_id") or "")
            if not pipeline_id:
                continue
            payload = client.request("GET", f"/api/2.0/pipelines/{quote(pipeline_id, safe='')}")
            spec = dict(payload.get("spec") or {})
            tags = {str(k): str(v) for k, v in (spec.get("tags") or {}).items()}
            if tags.get("project_tag") != expected["project_tag"]:
                continue
            results.append(
                _scanned_asset(
                    AssetSelection(
                        resource_type="pipeline",
                        resource_id=pipeline_id,
                        resource_name=str(spec.get("name") or status.get("name") or pipeline_id),
                    ),
                    tags,
                    expected,
                )
            )
        return results

    def _scan_uc_assets(
        self,
        host: str,
        warehouse_id: str,
        catalog: str,
        project: dict[str, Any],
        environment: str,
    ) -> list[ScannedAsset]:
        expected = expected_tags(project, environment)
        results: list[ScannedAsset] = []
        with self._sql_connection(host, warehouse_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT t.table_catalog,
                       t.table_schema,
                       t.table_name,
                       t.table_type,
                       max(CASE WHEN g.tag_name = 'project_tag' THEN g.tag_value END) AS project_tag,
                       max(CASE WHEN g.tag_name = 'environment' THEN g.tag_value END) AS environment,
                       max(CASE WHEN g.tag_name = 'data_classification' THEN g.tag_value END) AS data_classification
                FROM `{catalog}`.information_schema.tables t
                JOIN `{catalog}`.information_schema.table_tags g
                  ON t.table_catalog = g.catalog_name
                 AND t.table_schema = g.schema_name
                 AND t.table_name = g.table_name
                GROUP BY t.table_catalog, t.table_schema, t.table_name, t.table_type
                HAVING max(CASE WHEN g.tag_name = 'project_tag' THEN g.tag_value END) = ?
                """,
                [expected["project_tag"]],
            )
            names = [item[0] for item in cursor.description or ()]
            for row in cursor.fetchall():
                item = dict(zip(names, row, strict=True))
                kind = "view" if "VIEW" in str(item["table_type"]).upper() else "table"
                tags = {key: str(item.get(key) or "") for key in expected}
                results.append(
                    _scanned_asset(
                        AssetSelection(
                            resource_type=kind,
                            resource_name=str(item["table_name"]),
                            resource_path=(
                                f"{item['table_catalog']}.{item['table_schema']}.{item['table_name']}"
                            ),
                            catalog_name=str(item["table_catalog"]),
                            schema_name=str(item["table_schema"]),
                        ),
                        tags,
                        expected,
                    )
                )
            cursor.execute(
                f"""
                SELECT s.catalog_name,
                       s.schema_name,
                       max(CASE WHEN s.tag_name = 'project_tag' THEN s.tag_value END) AS project_tag,
                       max(CASE WHEN s.tag_name = 'environment' THEN s.tag_value END) AS environment,
                       max(CASE WHEN s.tag_name = 'data_classification' THEN s.tag_value END) AS data_classification
                FROM `{catalog}`.information_schema.schema_tags s
                GROUP BY s.catalog_name, s.schema_name
                HAVING max(CASE WHEN s.tag_name = 'project_tag' THEN s.tag_value END) = ?
                """,
                [expected["project_tag"]],
            )
            names = [item[0] for item in cursor.description or ()]
            for row in cursor.fetchall():
                item = dict(zip(names, row, strict=True))
                tags = {key: str(item.get(key) or "") for key in expected}
                results.append(
                    _scanned_asset(
                        AssetSelection(
                            resource_type="schema",
                            resource_name=str(item["schema_name"]),
                            resource_path=f"{item['catalog_name']}.{item['schema_name']}",
                            catalog_name=str(item["catalog_name"]),
                            schema_name=str(item["schema_name"]),
                        ),
                        tags,
                        expected,
                    )
                )
        return results

    def _validate_one(
        self,
        client: DatabricksRestClient,
        host: str,
        warehouse_id: str,
        project: dict[str, Any],
        environment: str,
        selection: AssetSelection,
    ) -> ScannedAsset:
        expected = expected_tags(project, environment)
        try:
            if selection.resource_type == "job":
                payload = client.request(
                    "GET", "/api/2.2/jobs/get", params={"job_id": selection.resource_id}
                )
                if payload.get("_not_found"):
                    return _not_accessible(selection, "Job was not found.")
                settings = dict(payload.get("settings") or {})
                tags = {str(k): str(v) for k, v in (settings.get("tags") or {}).items()}
                actual = selection.model_copy(
                    update={"resource_name": str(settings.get("name") or selection.resource_name)}
                )
                return _scanned_asset(actual, tags, expected)
            if selection.resource_type == "pipeline":
                payload = client.request(
                    "GET", f"/api/2.0/pipelines/{quote(selection.resource_id, safe='')}"
                )
                if payload.get("_not_found"):
                    return _not_accessible(selection, "Pipeline was not found.")
                spec = dict(payload.get("spec") or {})
                tags = {str(k): str(v) for k, v in (spec.get("tags") or {}).items()}
                actual = selection.model_copy(
                    update={"resource_name": str(spec.get("name") or selection.resource_name)}
                )
                return _scanned_asset(actual, tags, expected)
            if selection.resource_type in {"dashboard", "app", "notebook"}:
                entity_type = {
                    "dashboard": "dashboards",
                    "app": "apps",
                    "notebook": "notebooks",
                }[selection.resource_type]
                tags: dict[str, str] = {}
                for key in expected:
                    payload = client.request(
                        "GET",
                        "/api/2.0/entity-tag-assignments/"
                        f"{entity_type}/{quote(selection.resource_id, safe='')}/tags/{quote(key, safe='')}",
                    )
                    if not payload.get("_not_found"):
                        tags[key] = str(payload.get("tag_value") or "")
                return _scanned_asset(selection, tags, expected)
            if selection.resource_type in {"schema", "table", "view", "volume"}:
                entity_type = {
                    "schema": "schemas",
                    "table": "tables",
                    "view": "tables",
                    "volume": "volumes",
                }[selection.resource_type]
                entity_name = selection.resource_path or _uc_name(selection)
                tags = self._uc_tags(client, entity_type, entity_name)
                return _scanned_asset(selection, tags, expected)
            return _not_accessible(selection, "Unsupported asset type.")
        except Exception as exc:  # pragma: no cover - live permissions and API failures
            return _not_accessible(selection, str(exc))

    def _uc_tags(
        self, client: DatabricksRestClient, entity_type: str, entity_name: str
    ) -> dict[str, str]:
        path = (
            "/api/2.1/unity-catalog/entity-tag-assignments/"
            f"{entity_type}/{quote(entity_name, safe='')}/tags"
        )
        rows = client.paged_get(path, list_key="tag_assignments", params={"max_results": 100})
        return {
            str(item.get("tag_key") or ""): str(item.get("tag_value") or "")
            for item in rows
            if item.get("tag_key")
        }

    @contextmanager
    def _sql_connection(self, host: str, warehouse_id: str):
        if not warehouse_id:
            raise ConflictError("SQL warehouse ID is required for Unity Catalog scanning.")
        from databricks import sql

        connection = sql.connect(
            server_hostname=server_hostname(host),
            http_path=f"/sql/1.0/warehouses/{warehouse_id}",
            access_token=self.token_provider.token(host),
            user_agent_entry="project-registry-governance-scanner/4.0.0",
        )
        try:
            yield connection
        finally:
            connection.close()


class DatabricksTagManager:
    """Apply missing tags without overwriting an existing conflicting project tag.

    Job and pipeline tags remain source-managed because updating them through the App could replace
    unrelated settings. Unity Catalog and workspace-entity tags can be repaired centrally.
    """

    def __init__(self, settings: Settings, token_provider: OAuthTokenProvider | None = None):
        self.settings = settings
        self.token_provider = token_provider or OAuthTokenProvider()
        self.scanner = DatabricksAssetScanner(settings, self.token_provider)

    def apply_missing(
        self, project: dict[str, Any], environment: str, asset: AssetSelection
    ) -> ScannedAsset:
        before = self.scanner.scan(project, environment, [asset]).assets[0]
        if before.tags.get("project_tag") not in {None, "", project["project_id"]}:
            raise ConflictError("Asset is already owned by another project.")
        missing = {
            key: value
            for key, value in expected_tags(project, environment).items()
            if not before.tags.get(key)
        }
        if not missing:
            return before
        if asset.resource_type in {"job", "pipeline"}:
            raise ConflictError(
                "Job and pipeline tags must be fixed in the Bundle source, then redeployed to dev."
            )
        host = self.settings.dev_host if environment == "dev" else self.settings.prod_host
        client = DatabricksRestClient(host, self.token_provider)
        if asset.resource_type in {"dashboard", "app", "notebook"}:
            entity_type = {
                "dashboard": "dashboards",
                "app": "apps",
                "notebook": "notebooks",
            }[asset.resource_type]
            for key, value in missing.items():
                client.request(
                    "POST",
                    "/api/2.0/entity-tag-assignments",
                    json={
                        "entity_id": asset.resource_id,
                        "entity_type": entity_type,
                        "tag_key": key,
                        "tag_value": value,
                    },
                )
        elif asset.resource_type in {"schema", "table", "view", "volume"}:
            entity_type = {
                "schema": "schemas",
                "table": "tables",
                "view": "tables",
                "volume": "volumes",
            }[asset.resource_type]
            entity_name = asset.resource_path or _uc_name(asset)
            for key, value in missing.items():
                client.request(
                    "POST",
                    "/api/2.1/unity-catalog/entity-tag-assignments",
                    json={
                        "entity_name": entity_name,
                        "entity_type": entity_type,
                        "tag_key": key,
                        "tag_value": value,
                    },
                )
        else:
            raise ConflictError("Automatic tag repair is not supported for this asset type.")
        return self.scanner.scan(project, environment, [asset]).assets[0]


def _asset_key(asset: AssetSelection) -> tuple[str, str, str, str, str, str]:
    return (
        asset.resource_type,
        asset.resource_id,
        asset.resource_path,
        asset.catalog_name,
        asset.schema_name,
        asset.resource_name,
    )


def _uc_name(asset: AssetSelection) -> str:
    if asset.resource_type == "schema":
        return f"{asset.catalog_name}.{asset.schema_name}"
    return f"{asset.catalog_name}.{asset.schema_name}.{asset.resource_name}"


def _scanned_asset(
    selection: AssetSelection, tags: dict[str, str], expected: dict[str, str]
) -> ScannedAsset:
    normalized = {str(k): str(v) for k, v in tags.items()}
    project_value = normalized.get("project_tag")
    wrong = [
        key
        for key, value in expected.items()
        if normalized.get(key) not in {None, "", value}
    ]
    missing = [key for key in expected if not normalized.get(key)]
    if project_value and project_value != expected["project_tag"]:
        status = "CONFLICT"
        detail = f"Owned by project {project_value}."
    elif wrong:
        status = "CONFLICT"
        detail = "Conflicting mandatory tags: " + ", ".join(wrong)
    elif missing:
        status = "FIXABLE"
        detail = "Missing mandatory tags: " + ", ".join(missing)
    else:
        status = "COMPLIANT"
        detail = "All mandatory tags match."
    return ScannedAsset(
        **selection.model_dump(),
        tags=normalized,
        compliance_status=status,
        detail=detail,
    )


def _not_accessible(selection: AssetSelection, detail: str) -> ScannedAsset:
    return ScannedAsset(
        **selection.model_dump(),
        tags={},
        compliance_status="NOT_ACCESSIBLE",
        detail=detail,
    )
