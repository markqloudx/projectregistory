from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Protocol

from app.clients import OAuthTokenProvider, server_hostname
from app.governance import ConflictError, NotFoundError, utc_now
from app.settings import Settings

PROJECT_COLUMNS = (
    "project_id",
    "name",
    "team_name",
    "technical_owner_email",
    "description",
    "lifecycle_status",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
    "workspace",
    "data_classification",
    "go_live_date",
    "documentation_link",
    "data_sources",
    "technical_details",
    "jira_link",
    "business_owner_email",
    "decision_comment",
      # ========== TERMS AND CONDITIONS - ADD THESE 3 LINES ==========
    "terms_accepted_at",
    "terms_accepted_by",
    "terms_version"
)

REQUEST_JSON_COLUMNS = {"asset_manifest_json": "asset_manifest"}

CICD_COLUMNS = (
    "project_id",
    "repository_uri",
    "git_branch",
    "bundle_path",
    "dev_target",
    "prod_target",
    "deployment_mode",
    "run_resource_key",
    "source_status",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
)

REQUEST_COLUMNS = (
    "request_id",
    "project_id",
    "request_status",
    "repository_uri",
    "git_branch",
    "bundle_path",
    "dev_target",
    "prod_target",
    "run_resource_key",
    "asset_manifest_json",
    "asset_manifest_hash",
    "change_summary",
    "jira_link",
    "dev_tag_check_passed",
    "dev_tag_check_detail",
    "dev_tag_checked_at",
    "source_preflight_status",
    "source_preflight_passed",
    "source_preflight_detail",
    "source_preflight_checked_at",
    "source_validation_claim_id",
    "source_validation_claimed_by",
    "source_validation_claimed_at",
    "source_validation_job_run_id",
    "source_validated_revision",
    "requested_at",
    "requested_by",
    "approved_at",
    "approved_by",
    "decision_comment",
    "deployer_profile",
    "deployment_attempt",
    "deployment_claim_id",
    "deployment_claimed_by",
    "deployment_claimed_at",
    "resolved_git_revision",
    "prod_deployment_status",
    "prod_tag_check_passed",
    "prod_tag_check_detail",
    "dispatch_job_run_id",
    "deployment_run_id",
    "deployment_run_url",
    "deploy_started_at",
    "prod_deployed_at",
    "deployment_message",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
)

TAG_COLUMNS = (
    "evidence_id",
    "request_id",
    "project_id",
    "environment",
    "resource_type",
    "resource_id",
    "resource_name",
    "resource_path",
    "tag_key",
    "expected_value",
    "actual_value",
    "validation_result",
    "detail",
    "checked_at",
    "checked_by",
)

AUDIT_COLUMNS = (
    "event_id",
    "event_type",
    "project_id",
    "request_id",
    "actor",
    "actor_type",
    "event_at",
    "previous_status",
    "new_status",
    "comment",
    "payload_json",
    "correlation_id",
)


class RegistryDatabase(Protocol):
    def ensure_tables(self) -> None: ...
    def validate_schema(self) -> None: ...
    def health(self) -> dict[str, Any]: ...
    def dashboard(self) -> dict[str, Any]: ...
    def create_project(self, record: Mapping[str, Any]) -> dict[str, Any]: ...
    def update_project(self, project_id: str, values: Mapping[str, Any]) -> dict[str, Any]: ...
    def get_project(self, project_id: str) -> dict[str, Any]: ...
    def list_projects(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def upsert_delivery_config(self, record: Mapping[str, Any]) -> dict[str, Any]: ...
    def get_delivery_config(self, project_id: str) -> dict[str, Any] | None: ...
    def create_request(self, record: Mapping[str, Any]) -> dict[str, Any]: ...
    def update_request(self, request_id: str, values: Mapping[str, Any]) -> dict[str, Any]: ...
    def get_request(self, request_id: str) -> dict[str, Any]: ...
    def claim_source_validation(
        self, request_id: str, claim_id: str, actor: str, now: datetime
    ) -> dict[str, Any]: ...
    def next_source_validation(self) -> dict[str, Any] | None: ...
    def claim_request(
        self, request_id: str, claim_id: str, actor: str, now: datetime
    ) -> dict[str, Any]: ...
    def next_queued_request(self, deployer_profile: str = "") -> dict[str, Any] | None: ...
    def list_requests(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]: ...
    def replace_tag_evidence(
        self, request_id: str, environment: str, rows: Iterable[Mapping[str, Any]]
    ) -> None: ...
    def list_tag_evidence(self, request_id: str) -> list[dict[str, Any]]: ...
    def append_audit(self, record: Mapping[str, Any]) -> None: ...
    def list_audit(self, limit: int = 500) -> list[dict[str, Any]]: ...


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _request_to_public(record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record)
    raw_manifest = result.pop("asset_manifest_json", None)
    if raw_manifest is not None:
        if isinstance(raw_manifest, str):
            result["asset_manifest"] = tuple(json.loads(raw_manifest or "[]"))
        else:
            result["asset_manifest"] = tuple(raw_manifest or ())
    elif "asset_manifest" in result:
        result["asset_manifest"] = tuple(result.get("asset_manifest") or ())
    return result


class MemoryDatabase:
    def __init__(self):
        self.projects: dict[str, dict[str, Any]] = {}
        self.delivery: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.evidence: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def ensure_tables(self) -> None:
        return None

    def validate_schema(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"database": "ok", "backend": "memory", "project_count": len(self.projects)}

    def dashboard(self) -> dict[str, Any]:
        with self._lock:
            return {
                "projects": len(self.projects),
                "active_projects": sum(
                    1 for item in self.projects.values() if item["lifecycle_status"] == "ACTIVE"
                ),
                "ready_for_approval": sum(
                    1 for item in self.requests.values() if item["request_status"] == "READY_FOR_APPROVAL"
                ),
                "queued_or_deploying": sum(
                    1
                    for item in self.requests.values()
                    if item["request_status"] in {"APPROVED_DEPLOY_QUEUED", "DEPLOYING"}
                ),
                "deployed": sum(
                    1 for item in self.requests.values() if item["request_status"] == "DEPLOYED"
                ),
                "failed": sum(
                    1 for item in self.requests.values() if item["request_status"] == "DEPLOY_FAILED"
                ),
            }

    def create_project(self, record: Mapping[str, Any]) -> dict[str, Any]:
        value = {column: record.get(column) for column in PROJECT_COLUMNS}
        with self._lock:
            if value["project_id"] in self.projects:
                raise ConflictError("Project ID already exists.")
            self.projects[str(value["project_id"])] = _clone(value)
            return _clone(value)

    def update_project(self, project_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.projects.get(project_id)
            if current is None:
                raise NotFoundError("Project not found.")
            for key, value in values.items():
                if key not in PROJECT_COLUMNS or key in {"project_id", "created_at", "created_by"}:
                    raise ConflictError(f"Project column cannot be updated: {key}")
                current[key] = _clone(value)
            return _clone(current)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            value = self.projects.get(project_id)
            if value is None:
                raise NotFoundError("Project not found.")
            return _clone(value)

    def list_projects(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.projects.values())
            if status:
                values = [item for item in values if item["lifecycle_status"] == status]
            values.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)
            return _clone(values)

    def upsert_delivery_config(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.delivery[str(record["project_id"])] = _clone(dict(record))
            return _clone(self.delivery[str(record["project_id"])])

    def get_delivery_config(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self.delivery.get(project_id)
            return _clone(value) if value else None

    def create_request(self, record: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(record)
        manifest = value.pop("asset_manifest", None)
        if manifest is not None:
            value["asset_manifest_json"] = json.dumps(manifest, separators=(",", ":"), default=str)
        with self._lock:
            request_id = str(value["request_id"])
            if request_id in self.requests:
                raise ConflictError("Production request already exists.")
            self.requests[request_id] = _clone(value)
            return _request_to_public(_clone(value))

    def update_request(self, request_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.requests.get(request_id)
            if current is None:
                raise NotFoundError("Production request not found.")
            for key, value in values.items():
                if key == "asset_manifest":
                    current["asset_manifest_json"] = json.dumps(
                        value, separators=(",", ":"), default=str
                    )
                else:
                    current[key] = _clone(value)
            return _request_to_public(_clone(current))

    def get_request(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            value = self.requests.get(request_id)
            if value is None:
                raise NotFoundError("Production request not found.")
            result = _request_to_public(_clone(value))
            project = self.projects.get(str(result["project_id"]))
            if project:
                result["project_name"] = project["name"]
            return result

    def claim_source_validation(
        self, request_id: str, claim_id: str, actor: str, now: datetime
    ) -> dict[str, Any]:
        with self._lock:
            current = self.requests.get(request_id)
            if current is None:
                raise NotFoundError("Production request not found.")
            if current.get("request_status") != "VALIDATING" or current.get(
                "source_preflight_status"
            ) != "QUEUED":
                raise ConflictError("Production request is not queued for source validation.")
            current.update(
                {
                    "source_preflight_status": "VALIDATING",
                    "source_validation_claim_id": claim_id,
                    "source_validation_claimed_by": actor,
                    "source_validation_claimed_at": now,
                    "updated_at": now,
                    "updated_by": actor,
                }
            )
            return self.get_request(request_id)

    def next_source_validation(self) -> dict[str, Any] | None:
        with self._lock:
            values = [
                item
                for item in self.requests.values()
                if item.get("request_status") == "VALIDATING"
                and item.get("source_preflight_status") == "QUEUED"
            ]
            values.sort(key=lambda item: item.get("created_at") or datetime.min)
            return self.get_request(str(values[0]["request_id"])) if values else None

    def claim_request(
        self, request_id: str, claim_id: str, actor: str, now: datetime
    ) -> dict[str, Any]:
        with self._lock:
            current = self.requests.get(request_id)
            if current is None:
                raise NotFoundError("Production request not found.")
            if current.get("request_status") != "APPROVED_DEPLOY_QUEUED":
                raise ConflictError("Production request is not available for deployment.")
            current.update(
                {
                    "request_status": "DEPLOYING",
                    "prod_deployment_status": "DEPLOYING",
                    "deployment_claim_id": claim_id,
                    "deployment_claimed_by": actor,
                    "deployment_claimed_at": now,
                    "deploy_started_at": now,
                    "deployment_attempt": int(current.get("deployment_attempt") or 0) + 1,
                    "updated_at": now,
                    "updated_by": actor,
                }
            )
            return self.get_request(request_id)

    def next_queued_request(self, deployer_profile: str = "") -> dict[str, Any] | None:
        with self._lock:
            values = [
                item
                for item in self.requests.values()
                if item.get("request_status") == "APPROVED_DEPLOY_QUEUED"
                and (not deployer_profile or item.get("deployer_profile") == deployer_profile)
            ]
            values.sort(key=lambda item: item.get("approved_at") or item.get("created_at") or datetime.min)
            return self.get_request(str(values[0]["request_id"])) if values else None

    def list_requests(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.requests.values())
            if project_id:
                values = [item for item in values if item["project_id"] == project_id]
            if status:
                values = [item for item in values if item["request_status"] == status]
            values.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)
            results = []
            for item in values:
                value = _request_to_public(_clone(item))
                project = self.projects.get(str(value["project_id"]))
                value["project_name"] = project["name"] if project else ""
                results.append(value)
            return results

    def replace_tag_evidence(
        self, request_id: str, environment: str, rows: Iterable[Mapping[str, Any]]
    ) -> None:
        with self._lock:
            self.evidence = [
                item
                for item in self.evidence
                if not (item["request_id"] == request_id and item["environment"] == environment)
            ]
            self.evidence.extend(_clone(dict(row)) for row in rows)

    def list_tag_evidence(self, request_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return _clone([item for item in self.evidence if item["request_id"] == request_id])

    def append_audit(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.audit.append(_clone(dict(record)))

    def list_audit(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return _clone(list(reversed(self.audit[-limit:])))


class DatabricksSqlDatabase:
    def __init__(self, settings: Settings, token_provider: OAuthTokenProvider | None = None):
        self.settings = settings
        self.token_provider = token_provider or OAuthTokenProvider()

    @contextmanager
    def _connection(self):
        if not self.settings.warehouse_id:
            raise ConflictError("DATABRICKS_WAREHOUSE_ID is not configured for the App.")
        from databricks import sql

        connection = sql.connect(
            server_hostname=server_hostname(self.settings.registry_host),
            http_path=f"/sql/1.0/warehouses/{self.settings.warehouse_id}",
            access_token=self.token_provider.token(self.settings.registry_host),
            user_agent_entry="project-registry-governance/4.0.0",
        )
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        names = [item[0] for item in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _execute(self, statement: str, parameters: list[Any] | tuple[Any, ...] = ()) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(statement, list(parameters))

    def _fetchall(
        self, statement: str, parameters: list[Any] | tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(statement, list(parameters))
            return self._rows(cursor)

    def _fetchone(
        self, statement: str, parameters: list[Any] | tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        rows = self._fetchall(statement, parameters)
        return rows[0] if rows else None

    def ensure_tables(self) -> None:
        catalog = f"`{self.settings.registry_catalog}`"
        schema = f"{catalog}.`{self.settings.registry_schema}`"
        statements = [
            f"CREATE SCHEMA IF NOT EXISTS {schema}",
            f"""
            CREATE TABLE IF NOT EXISTS {self.settings.project_table} (
              project_id STRING NOT NULL,
              name STRING NOT NULL,
              team_name STRING NOT NULL,
              technical_owner_email STRING NOT NULL,
              description STRING,
              lifecycle_status STRING NOT NULL,
              created_at TIMESTAMP NOT NULL,
              created_by STRING NOT NULL,
              updated_at TIMESTAMP NOT NULL,
              updated_by STRING NOT NULL,
              workspace STRING NOT NULL,
              data_classification STRING NOT NULL,
              go_live_date DATE,
              documentation_link STRING,
              data_sources STRING,
              technical_details STRING,
              jira_link STRING,
              business_owner_email STRING NOT NULL,
              decision_comment STRING
                -- ========== TERMS AND CONDITIONS - ADD THESE 3 LINES ==========
              terms_accepted_at TIMESTAMP,
              terms_accepted_by STRING,
              terms_version STRING
            ) USING DELTA
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.settings.cicd_table} (
              project_id STRING NOT NULL,
              repository_uri STRING NOT NULL,
              git_branch STRING NOT NULL,
              bundle_path STRING NOT NULL,
              dev_target STRING NOT NULL,
              prod_target STRING NOT NULL,
              deployment_mode STRING NOT NULL,
              run_resource_key STRING,
              source_status STRING,
              created_at TIMESTAMP NOT NULL,
              created_by STRING NOT NULL,
              updated_at TIMESTAMP NOT NULL,
              updated_by STRING NOT NULL
            ) USING DELTA
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.settings.request_table} (
              request_id STRING NOT NULL,
              project_id STRING NOT NULL,
              request_status STRING NOT NULL,
              repository_uri STRING NOT NULL,
              git_branch STRING NOT NULL,
              bundle_path STRING NOT NULL,
              dev_target STRING NOT NULL,
              prod_target STRING NOT NULL,
              run_resource_key STRING,
              asset_manifest_json STRING NOT NULL,
              asset_manifest_hash STRING NOT NULL,
              change_summary STRING,
              jira_link STRING,
              dev_tag_check_passed BOOLEAN,
              dev_tag_check_detail STRING,
              dev_tag_checked_at TIMESTAMP,
              source_preflight_status STRING,
              source_preflight_passed BOOLEAN,
              source_preflight_detail STRING,
              source_preflight_checked_at TIMESTAMP,
              source_validation_claim_id STRING,
              source_validation_claimed_by STRING,
              source_validation_claimed_at TIMESTAMP,
              source_validation_job_run_id STRING,
              source_validated_revision STRING,
              requested_at TIMESTAMP,
              requested_by STRING,
              approved_at TIMESTAMP,
              approved_by STRING,
              decision_comment STRING,
              deployer_profile STRING,
              deployment_attempt INT,
              deployment_claim_id STRING,
              deployment_claimed_by STRING,
              deployment_claimed_at TIMESTAMP,
              resolved_git_revision STRING,
              prod_deployment_status STRING,
              prod_tag_check_passed BOOLEAN,
              prod_tag_check_detail STRING,
              dispatch_job_run_id STRING,
              deployment_run_id STRING,
              deployment_run_url STRING,
              deploy_started_at TIMESTAMP,
              prod_deployed_at TIMESTAMP,
              deployment_message STRING,
              created_at TIMESTAMP NOT NULL,
              created_by STRING NOT NULL,
              updated_at TIMESTAMP NOT NULL,
              updated_by STRING NOT NULL
            ) USING DELTA
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.settings.tag_table} (
              evidence_id STRING NOT NULL,
              request_id STRING NOT NULL,
              project_id STRING NOT NULL,
              environment STRING NOT NULL,
              resource_type STRING NOT NULL,
              resource_id STRING,
              resource_name STRING NOT NULL,
              resource_path STRING,
              tag_key STRING NOT NULL,
              expected_value STRING NOT NULL,
              actual_value STRING,
              validation_result STRING NOT NULL,
              detail STRING,
              checked_at TIMESTAMP NOT NULL,
              checked_by STRING NOT NULL
            ) USING DELTA
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.settings.audit_table} (
              event_id STRING NOT NULL,
              event_type STRING NOT NULL,
              project_id STRING,
              request_id STRING,
              actor STRING NOT NULL,
              actor_type STRING NOT NULL,
              event_at TIMESTAMP NOT NULL,
              previous_status STRING,
              new_status STRING,
              comment STRING,
              payload_json STRING,
              correlation_id STRING
            ) USING DELTA
            """,
        ]
        for statement in statements:
            self._execute(statement)

    def validate_schema(self) -> None:
        required = {
            self.settings.project_table: PROJECT_COLUMNS,
            self.settings.cicd_table: CICD_COLUMNS,
            self.settings.request_table: REQUEST_COLUMNS,
            self.settings.tag_table: TAG_COLUMNS,
            self.settings.audit_table: AUDIT_COLUMNS,
        }
        failures: list[str] = []
        for table_name, expected in required.items():
            rows = self._fetchall(f"DESCRIBE TABLE {table_name}")
            actual = {
                str(row.get("col_name"))
                for row in rows
                if row.get("col_name") and not str(row.get("col_name")).startswith("#")
            }
            missing = [item for item in expected if item not in actual]
            if missing:
                failures.append(f"{table_name}: missing {', '.join(missing)}")
            if table_name == self.settings.project_table:
                unexpected = sorted(actual.difference(expected))
                if unexpected:
                    failures.append(
                        f"{table_name}: unexpected project columns {', '.join(unexpected)}"
                    )
        if failures:
            raise ConflictError(
                "Registry tables are missing v4 columns. Apply sql/002_migrate_3_2_to_4_0.sql. "
                + " | ".join(failures)
            )

    def health(self) -> dict[str, Any]:
        row = self._fetchone("SELECT 1 AS ok") or {}
        return {
            "database": "ok" if row.get("ok") == 1 else "unexpected",
            "backend": "databricks",
            "registry": f"{self.settings.registry_catalog}.{self.settings.registry_schema}",
        }

    def dashboard(self) -> dict[str, Any]:
        project = self._fetchone(
            f"""
            SELECT count(*) AS projects,
                   sum(CASE WHEN lifecycle_status = 'ACTIVE' THEN 1 ELSE 0 END) AS active_projects
            FROM {self.settings.project_table}
            """
        ) or {}
        request = self._fetchone(
            f"""
            SELECT
              sum(CASE WHEN request_status = 'READY_FOR_APPROVAL' THEN 1 ELSE 0 END) AS ready_for_approval,
              sum(CASE WHEN request_status IN ('APPROVED_DEPLOY_QUEUED','DEPLOYING') THEN 1 ELSE 0 END) AS queued_or_deploying,
              sum(CASE WHEN request_status = 'DEPLOYED' THEN 1 ELSE 0 END) AS deployed,
              sum(CASE WHEN request_status = 'DEPLOY_FAILED' THEN 1 ELSE 0 END) AS failed
            FROM {self.settings.request_table}
            """
        ) or {}
        return {key: int(value or 0) for key, value in {**project, **request}.items()}

    def create_project(self, record: Mapping[str, Any]) -> dict[str, Any]:
        existing = self._fetchone(
            f"SELECT project_id FROM {self.settings.project_table} WHERE project_id = ?",
            [record["project_id"]],
        )
        if existing:
            raise ConflictError("Project ID already exists.")
        placeholders = ", ".join("?" for _ in PROJECT_COLUMNS)
        self._execute(
            f"INSERT INTO {self.settings.project_table} ({', '.join(PROJECT_COLUMNS)}) VALUES ({placeholders})",
            [record.get(column) for column in PROJECT_COLUMNS],
        )
        return self.get_project(str(record["project_id"]))

    def update_project(self, project_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(PROJECT_COLUMNS) - {"project_id", "created_at", "created_by"}
        invalid = set(values) - allowed
        if invalid:
            raise ConflictError("Unsupported project update columns: " + ", ".join(sorted(invalid)))
        if not values:
            return self.get_project(project_id)
        assignments = ", ".join(f"{name} = ?" for name in values)
        self._execute(
            f"UPDATE {self.settings.project_table} SET {assignments} WHERE project_id = ?",
            [*values.values(), project_id],
        )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self._fetchone(
            f"SELECT {', '.join(PROJECT_COLUMNS)} FROM {self.settings.project_table} WHERE project_id = ?",
            [project_id],
        )
        if row is None:
            raise NotFoundError("Project not found.")
        return row

    def list_projects(self, status: str | None = None) -> list[dict[str, Any]]:
        where = " WHERE lifecycle_status = ?" if status else ""
        params = [status] if status else []
        return self._fetchall(
            f"SELECT {', '.join(PROJECT_COLUMNS)} FROM {self.settings.project_table}{where} ORDER BY created_at DESC",
            params,
        )

    def upsert_delivery_config(self, record: Mapping[str, Any]) -> dict[str, Any]:
        columns = CICD_COLUMNS
        values = [record.get(column) for column in columns]
        select_values = ", ".join(f"? AS {column}" for column in columns)
        updates = ", ".join(
            f"target.{column} = source.{column}"
            for column in columns
            if column not in {"project_id", "created_at", "created_by"}
        )
        insert_columns = ", ".join(columns)
        insert_values = ", ".join(f"source.{column}" for column in columns)
        self._execute(
            f"""
            MERGE INTO {self.settings.cicd_table} AS target
            USING (SELECT {select_values}) AS source
              ON target.project_id = source.project_id
            WHEN MATCHED THEN UPDATE SET {updates}
            WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})
            """,
            values,
        )
        return self.get_delivery_config(str(record["project_id"])) or {}

    def get_delivery_config(self, project_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            f"SELECT * FROM {self.settings.cicd_table} WHERE project_id = ?",
            [project_id],
        )

    def create_request(self, record: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(record)
        if "asset_manifest" in value:
            value["asset_manifest_json"] = json.dumps(
                value.pop("asset_manifest"), separators=(",", ":"), default=str
            )
        columns = tuple(value.keys())
        self._execute(
            f"INSERT INTO {self.settings.request_table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [value[column] for column in columns],
        )
        return self.get_request(str(record["request_id"]))

    def update_request(self, request_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(values)
        if "asset_manifest" in value:
            value["asset_manifest_json"] = json.dumps(
                value.pop("asset_manifest"), separators=(",", ":"), default=str
            )
        if not value:
            return self.get_request(request_id)
        assignments = ", ".join(f"{name} = ?" for name in value)
        self._execute(
            f"UPDATE {self.settings.request_table} SET {assignments} WHERE request_id = ?",
            [*value.values(), request_id],
        )
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> dict[str, Any]:
        row = self._fetchone(
            f"""
            SELECT r.*, p.name AS project_name
            FROM {self.settings.request_table} r
            LEFT JOIN {self.settings.project_table} p ON p.project_id = r.project_id
            WHERE r.request_id = ?
            """,
            [request_id],
        )
        if row is None:
            raise NotFoundError("Production request not found.")
        return _request_to_public(row)

    def claim_source_validation(
        self, request_id: str, claim_id: str, actor: str, now: datetime
    ) -> dict[str, Any]:
        self._execute(
            f"""
            UPDATE {self.settings.request_table}
               SET source_preflight_status = 'VALIDATING',
                   source_validation_claim_id = ?,
                   source_validation_claimed_by = ?,
                   source_validation_claimed_at = ?,
                   updated_at = ?,
                   updated_by = ?
             WHERE request_id = ?
               AND request_status = 'VALIDATING'
               AND source_preflight_status = 'QUEUED'
            """,
            [claim_id, actor, now, now, actor, request_id],
        )
        claimed = self.get_request(request_id)
        if (
            claimed.get("source_preflight_status") != "VALIDATING"
            or claimed.get("source_validation_claim_id") != claim_id
        ):
            raise ConflictError("Production request was already claimed for source validation.")
        return claimed

    def next_source_validation(self) -> dict[str, Any] | None:
        row = self._fetchone(
            f"""
            SELECT request_id
            FROM {self.settings.request_table}
            WHERE request_status = 'VALIDATING'
              AND source_preflight_status = 'QUEUED'
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        return self.get_request(str(row["request_id"])) if row else None

    def claim_request(
        self, request_id: str, claim_id: str, actor: str, now: datetime
    ) -> dict[str, Any]:
        self._execute(
            f"""
            UPDATE {self.settings.request_table}
               SET request_status = 'DEPLOYING',
                   prod_deployment_status = 'DEPLOYING',
                   deployment_claim_id = ?,
                   deployment_claimed_by = ?,
                   deployment_claimed_at = ?,
                   deploy_started_at = ?,
                   deployment_attempt = coalesce(deployment_attempt, 0) + 1,
                   updated_at = ?,
                   updated_by = ?
             WHERE request_id = ?
               AND request_status = 'APPROVED_DEPLOY_QUEUED'
            """,
            [claim_id, actor, now, now, now, actor, request_id],
        )
        claimed = self.get_request(request_id)
        if claimed.get("request_status") != "DEPLOYING" or claimed.get("deployment_claim_id") != claim_id:
            raise ConflictError("Production request was already claimed or is no longer queued.")
        return claimed

    def next_queued_request(self, deployer_profile: str = "") -> dict[str, Any] | None:
        where_profile = " AND deployer_profile = ?" if deployer_profile else ""
        params = [deployer_profile] if deployer_profile else []
        row = self._fetchone(
            f"""
            SELECT request_id
            FROM {self.settings.request_table}
            WHERE request_status = 'APPROVED_DEPLOY_QUEUED'{where_profile}
            ORDER BY approved_at ASC, created_at ASC
            LIMIT 1
            """,
            params,
        )
        return self.get_request(str(row["request_id"])) if row else None

    def list_requests(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("r.project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("r.request_status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._fetchall(
            f"""
            SELECT r.*, p.name AS project_name
            FROM {self.settings.request_table} r
            LEFT JOIN {self.settings.project_table} p ON p.project_id = r.project_id
            {where}
            ORDER BY r.created_at DESC
            """,
            params,
        )
        return [_request_to_public(row) for row in rows]

    def replace_tag_evidence(
        self, request_id: str, environment: str, rows: Iterable[Mapping[str, Any]]
    ) -> None:
        self._execute(
            f"DELETE FROM {self.settings.tag_table} WHERE request_id = ? AND environment = ?",
            [request_id, environment],
        )
        columns = (
            "evidence_id",
            "request_id",
            "project_id",
            "environment",
            "resource_type",
            "resource_id",
            "resource_name",
            "resource_path",
            "tag_key",
            "expected_value",
            "actual_value",
            "validation_result",
            "detail",
            "checked_at",
            "checked_by",
        )
        for row in rows:
            self._execute(
                f"INSERT INTO {self.settings.tag_table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [row.get(column) for column in columns],
            )

    def list_tag_evidence(self, request_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            f"SELECT * FROM {self.settings.tag_table} WHERE request_id = ? ORDER BY environment, resource_type, resource_name, tag_key",
            [request_id],
        )

    def append_audit(self, record: Mapping[str, Any]) -> None:
        columns = (
            "event_id",
            "event_type",
            "project_id",
            "request_id",
            "actor",
            "actor_type",
            "event_at",
            "previous_status",
            "new_status",
            "comment",
            "payload_json",
            "correlation_id",
        )
        self._execute(
            f"INSERT INTO {self.settings.audit_table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record.get(column) for column in columns],
        )

    def list_audit(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._fetchall(
            f"SELECT * FROM {self.settings.audit_table} ORDER BY event_at DESC LIMIT {max(1, min(limit, 5000))}"
        )


def create_database(settings: Settings, token_provider: OAuthTokenProvider | None = None) -> RegistryDatabase:
    if settings.backend == "memory":
        return MemoryDatabase()
    return DatabricksSqlDatabase(settings, token_provider)
