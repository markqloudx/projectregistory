from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from app.governance import ValidationError, validate_identifier


class SettingsError(RuntimeError):
    pass


def _env_or(value: Any, env_name: str, default: str = "") -> str:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value.strip()
    if value is None:
        return default
    return str(value).strip()


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    return tuple(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip()))


def _host(value: str, label: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SettingsError(f"{label} must be an HTTPS Databricks workspace URL.")
    return normalized


@dataclass(frozen=True)
class DeploymentProfile:
    name: str
    display_name: str
    team_names: tuple[str, ...]
    job_id: str
    principals: tuple[str, ...]
    allowed_schemas: tuple[str, ...]
    allowed_workspace_roots: tuple[str, ...]

    def allows_schema(self, schema_name: str) -> bool:
        return not self.allowed_schemas or schema_name.lower() in {
            item.lower() for item in self.allowed_schemas
        }

    def allows_actor(self, actor: str) -> bool:
        return not self.principals or actor.strip().lower() in set(self.principals)


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    registry_host: str
    registry_catalog: str
    registry_schema: str
    warehouse_id: str
    dev_warehouse_id: str
    prod_warehouse_id: str
    dev_validation_job_id: str
    dev_host: str
    prod_host: str
    dev_workspace_name: str
    prod_workspace_name: str
    dev_catalog: str
    prod_catalog: str
    workspace_route: str
    data_classifications: tuple[str, ...]
    teams: tuple[str, ...]
    admin_principals: tuple[str, ...]
    approver_principals: tuple[str, ...]
    auditor_principals: tuple[str, ...]
    development_validator_principals: tuple[str, ...]
    production_deployer_principals: tuple[str, ...]
    allow_self_approval: bool
    trust_local_identity_headers: bool
    app_direct_url: str
    request_expiry_hours: int
    backend: str
    deployment_profiles: tuple[DeploymentProfile, ...]

    @property
    def project_table(self) -> str:
        return self.table("governed_projects")

    @property
    def cicd_table(self) -> str:
        return self.table("governed_project_cicd")

    @property
    def request_table(self) -> str:
        return self.table("governed_releases")

    @property
    def tag_table(self) -> str:
        return self.table("governed_resource_tags")

    @property
    def audit_table(self) -> str:
        return self.table("governance_audit")

    def table(self, name: str) -> str:
        return f"`{self.registry_catalog}`.`{self.registry_schema}`.`{name}`"

    def expected_workspace(self) -> str:
        return self.workspace_route

    def deployment_profile(self, profile_name: str) -> DeploymentProfile:
        normalized = profile_name.strip().lower()
        for profile in self.deployment_profiles:
            if profile.name.lower() == normalized:
                return profile
        raise ValidationError(f"Unknown production deployment profile '{profile_name}'.")

    def deployment_profile_for_team(self, team_name: str) -> DeploymentProfile:
        normalized = team_name.strip().lower()
        for profile in self.deployment_profiles:
            if normalized in {item.lower() for item in profile.team_names}:
                return profile
        for profile in self.deployment_profiles:
            if profile.name == "default":
                return profile
        raise ValidationError(
            f"No production deployment profile is configured for team '{team_name}'."
        )


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("APP_CONFIG_PATH", "config/app_config.yaml"))
    if not config_path.is_absolute():
        root = Path(__file__).resolve().parents[1]
        config_path = root / config_path
    if not config_path.exists():
        raise SettingsError(f"Configuration file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    registry = raw.get("registry") or {}
    workspaces = raw.get("workspaces") or {}
    catalogs = raw.get("catalogs") or {}
    project_defaults = raw.get("project_defaults") or {}
    security = raw.get("security") or {}
    automation = raw.get("automation") or {}

    registry_host = _host(
        _env_or(registry.get("host"), "REGISTRY_DATABRICKS_HOST"),
        "registry.host",
    )
    dev_host = _host(_env_or(workspaces.get("dev_host"), "DEV_DATABRICKS_HOST"), "dev_host")
    prod_host = _host(
        _env_or(workspaces.get("prod_host"), "PROD_DATABRICKS_HOST"), "prod_host"
    )

    registry_catalog = validate_identifier(
        _env_or(registry.get("catalog"), "REGISTRY_CATALOG", "it_prod"),
        "registry catalog",
    )
    registry_schema = validate_identifier(
        _env_or(registry.get("schema"), "REGISTRY_SCHEMA", "project_registry"),
        "registry schema",
    )
    dev_catalog = validate_identifier(
        _env_or(catalogs.get("dev"), "DEV_PROJECT_CATALOG", "foundry_dev"),
        "development catalog",
    )
    prod_catalog = validate_identifier(
        _env_or(catalogs.get("prod"), "PROD_PROJECT_CATALOG", "foundry_prod"),
        "production catalog",
    )

    profiles: list[DeploymentProfile] = []
    for name, payload in (raw.get("deployment_profiles") or {}).items():
        payload = payload or {}
        profiles.append(
            DeploymentProfile(
                name=str(name),
                display_name=str(payload.get("display_name") or name),
                team_names=tuple(str(item).strip() for item in payload.get("team_names") or ()),
                job_id=_env_or(
                    payload.get("job_id"),
                    str(payload.get("job_id_env") or f"DEPLOYMENT_JOB_ID_{str(name).upper()}"),
                ),
                principals=_tuple(payload.get("principals")),
                allowed_schemas=tuple(
                    validate_identifier(str(item), "allowed production schema")
                    for item in payload.get("allowed_schemas") or ()
                ),
                allowed_workspace_roots=tuple(
                    str(item).strip() for item in payload.get("allowed_workspace_roots") or ()
                ),
            )
        )

    if not profiles:
        profiles.append(
            DeploymentProfile(
                name="default",
                display_name="Operations production deployer",
                team_names=tuple(str(item) for item in project_defaults.get("teams") or ()),
                job_id=os.getenv("PROD_DEPLOYMENT_JOB_ID", ""),
                principals=_tuple(security.get("production_deployer_principals")),
                allowed_schemas=(),
                allowed_workspace_roots=("/Workspace/Shared/Operations",),
            )
        )

    profile_names: set[str] = set()
    mapped_teams: dict[str, str] = {}
    for profile in profiles:
        normalized_name = profile.name.strip().lower()
        if not normalized_name:
            raise SettingsError("Production deployment profile names cannot be empty.")
        if normalized_name in profile_names:
            raise SettingsError(
                f"Duplicate production deployment profile '{profile.name}'."
            )
        profile_names.add(normalized_name)
        for team_name in profile.team_names:
            normalized_team = team_name.strip().lower()
            if not normalized_team:
                continue
            prior_profile = mapped_teams.get(normalized_team)
            if prior_profile and prior_profile != profile.name:
                raise SettingsError(
                    f"Team '{team_name}' is mapped to both '{prior_profile}' and "
                    f"'{profile.name}'."
                )
            mapped_teams[normalized_team] = profile.name

    if len(profiles) > 1:
        profiles_without_principals = [
            profile.name for profile in profiles if not profile.principals
        ]
        if profiles_without_principals:
            raise SettingsError(
                "Every production deployment profile must declare principals when multiple "
                "profiles are configured. Missing: "
                + ", ".join(profiles_without_principals)
            )

    backend = _env_or(raw.get("backend"), "REGISTRY_BACKEND", "databricks").lower()
    if backend not in {"databricks", "memory"}:
        raise SettingsError("backend must be databricks or memory.")

    return Settings(
        raw=raw,
        registry_host=registry_host,
        registry_catalog=registry_catalog,
        registry_schema=registry_schema,
        warehouse_id=_env_or(
            registry.get("warehouse_id"), "DATABRICKS_WAREHOUSE_ID"
        ),
        dev_warehouse_id=_env_or(
            workspaces.get("dev_warehouse_id"), "DEV_SQL_WAREHOUSE_ID"
        ),
        prod_warehouse_id=_env_or(
            workspaces.get("prod_warehouse_id"), "PROD_SQL_WAREHOUSE_ID"
        ),
        dev_validation_job_id=_env_or(
            automation.get("dev_validation_job_id"), "DEV_VALIDATION_JOB_ID"
        ),
        dev_host=dev_host,
        prod_host=prod_host,
        dev_workspace_name=str(workspaces.get("dev_name") or "operations-dev"),
        prod_workspace_name=str(workspaces.get("prod_name") or "operations-prod"),
        dev_catalog=dev_catalog,
        prod_catalog=prod_catalog,
        workspace_route=str(
            project_defaults.get("workspace") or "operations-dev to operations-prod"
        ),
        data_classifications=tuple(
            str(item) for item in project_defaults.get("data_classifications") or ("Internal",)
        ),
        teams=tuple(str(item) for item in project_defaults.get("teams") or ()),
        admin_principals=_tuple(security.get("admin_principals")),
        approver_principals=_tuple(security.get("approver_principals")),
        auditor_principals=_tuple(security.get("auditor_principals")),
        development_validator_principals=_tuple(
            security.get("development_validator_principals")
        ),
        production_deployer_principals=_tuple(
            security.get("production_deployer_principals")
        ),
        allow_self_approval=bool(security.get("allow_self_approval", False)),
        trust_local_identity_headers=bool(
            security.get("trust_local_identity_headers", backend == "memory")
        ),
        app_direct_url=_env_or(automation.get("app_direct_url"), "REGISTRY_APP_URL"),
        request_expiry_hours=int(automation.get("request_expiry_hours") or 72),
        backend=backend,
        deployment_profiles=tuple(profiles),
    )
