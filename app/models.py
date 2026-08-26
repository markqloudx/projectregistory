from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.governance import (
    ASSET_TYPES,
    MANDATORY_TAG_KEYS,
    normalize_repository_uri,
    safe_bundle_path,
    validate_resource_key,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _text(value: str | None) -> str:
    return "" if value is None else str(value).strip()


def _email(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValueError("A valid email address is required.")
    return normalized


def _optional_url(value: str | None, label: str) -> str:
    normalized = _text(value)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} cannot contain embedded credentials.")
    return normalized


class ProjectCreate(StrictModel):
    name: str
    team_name: str
    technical_owner_email: str
    description: str = ""
    data_classification: str
    go_live_date: date | None = None
    documentation_link: str = ""
    data_sources: str = ""
    technical_details: str = ""
    jira_link: str = ""
    business_owner_email: str

    @field_validator("name", "team_name", "data_classification")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Required project fields cannot be empty.")
        return normalized

    @field_validator("technical_owner_email", "business_owner_email")
    @classmethod
    def owner_email(cls, value: str) -> str:
        return _email(value)

    @field_validator("description", "data_sources", "technical_details")
    @classmethod
    def optional_text(cls, value: str) -> str:
        return _text(value)

    @field_validator("documentation_link", "jira_link")
    @classmethod
    def project_url(cls, value: str, info: Any) -> str:
        return _optional_url(value, info.field_name)


class ProjectUpdate(StrictModel):
    name: str | None = None
    team_name: str | None = None
    technical_owner_email: str | None = None
    description: str | None = None
    data_classification: str | None = None
    go_live_date: date | None = None
    documentation_link: str | None = None
    data_sources: str | None = None
    technical_details: str | None = None
    jira_link: str | None = None
    business_owner_email: str | None = None

    @field_validator("name", "team_name", "data_classification")
    @classmethod
    def updated_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Updated project fields cannot be empty.")
        return normalized

    @field_validator("technical_owner_email", "business_owner_email")
    @classmethod
    def updated_owner_email(cls, value: str | None) -> str | None:
        return None if value is None else _email(value)

    @field_validator("description", "data_sources", "technical_details")
    @classmethod
    def updated_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _text(value)

    @field_validator("documentation_link", "jira_link")
    @classmethod
    def updated_project_url(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _optional_url(value, info.field_name)


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    name: str
    team_name: str
    technical_owner_email: str
    description: str = ""
    lifecycle_status: str
    created_at: datetime | None = None
    created_by: str
    updated_at: datetime | None = None
    updated_by: str
    workspace: str
    data_classification: str
    go_live_date: date | None = None
    documentation_link: str = ""
    data_sources: str = ""
    technical_details: str = ""
    jira_link: str = ""
    business_owner_email: str
    decision_comment: str = ""

    @field_validator(
        "description",
        "documentation_link",
        "data_sources",
        "technical_details",
        "jira_link",
        "decision_comment",
        mode="before",
    )
    @classmethod
    def normalize_nullable_text(cls, value: Any) -> str:
        return "" if value is None else str(value)


class ProjectStatusRequest(StrictModel):
    status: Literal["ACTIVE", "SUSPENDED", "ARCHIVED"]
    comment: str = Field(default="", max_length=4000)


class AssetSelection(StrictModel):
    resource_type: str
    resource_id: str = ""
    resource_name: str
    resource_path: str = ""
    catalog_name: str = ""
    schema_name: str = ""

    @field_validator("resource_type")
    @classmethod
    def asset_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ASSET_TYPES:
            raise ValueError(f"Unsupported asset type: {normalized}")
        return normalized

    @field_validator("resource_id", "resource_name", "resource_path", "catalog_name", "schema_name")
    @classmethod
    def normalize_asset_text(cls, value: str, info: Any) -> str:
        normalized = value.strip()
        if info.field_name == "resource_name" and not normalized:
            raise ValueError("Asset name is required.")
        return normalized

    @model_validator(mode="after")
    def has_locator(self) -> "AssetSelection":
        if self.resource_type in {"schema", "table", "view", "volume"}:
            if not self.catalog_name or not self.schema_name:
                raise ValueError("Unity Catalog assets require catalog_name and schema_name.")
        elif not self.resource_id and not self.resource_path:
            raise ValueError("Workspace assets require resource_id or resource_path.")
        return self


class ScannedAsset(AssetSelection):
    tags: dict[str, str] = Field(default_factory=dict)
    compliance_status: Literal["COMPLIANT", "FIXABLE", "CONFLICT", "NOT_ACCESSIBLE"]
    detail: str = ""


class AssetScanResponse(StrictModel):
    project_id: str
    environment: Literal["dev", "prod"]
    expected_tags: dict[str, str]
    assets: tuple[ScannedAsset, ...]
    warnings: tuple[str, ...] = ()


class ProductionRequestCreate(StrictModel):
    repository_uri: str
    git_branch: str = "main"
    bundle_path: str = "."
    dev_target: str = "dev"
    prod_target: str = "prod"
    run_resource_key: str = ""
    change_summary: str = ""
    jira_link: str = ""
    assets: tuple[AssetSelection, ...]

    @field_validator("repository_uri")
    @classmethod
    def repository(cls, value: str) -> str:
        return normalize_repository_uri(value)

    @field_validator("git_branch")
    @classmethod
    def branch(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 250 or ".." in normalized or normalized.startswith("-"):
            raise ValueError("Invalid Git branch.")
        return normalized

    @field_validator("bundle_path")
    @classmethod
    def bundle_root(cls, value: str) -> str:
        return safe_bundle_path(value)

    @field_validator("dev_target", "prod_target")
    @classmethod
    def bundle_target(cls, value: str) -> str:
        return validate_resource_key(value, "Bundle target")

    @field_validator("run_resource_key")
    @classmethod
    def run_key(cls, value: str) -> str:
        normalized = value.strip()
        return validate_resource_key(normalized, "run resource key") if normalized else ""

    @field_validator("change_summary")
    @classmethod
    def summary(cls, value: str) -> str:
        return value.strip()

    @field_validator("jira_link")
    @classmethod
    def request_jira(cls, value: str) -> str:
        return _optional_url(value, "jira_link")

    @field_validator("assets")
    @classmethod
    def assets_required(cls, value: tuple[AssetSelection, ...]) -> tuple[AssetSelection, ...]:
        if not value:
            raise ValueError("Select at least one asset for production.")
        keys = [
            (item.resource_type, item.resource_id, item.resource_path, item.catalog_name, item.schema_name, item.resource_name)
            for item in value
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("The production request contains duplicate assets.")
        return value


class ProductionRequestRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    request_id: str
    project_id: str
    project_name: str = ""
    request_status: str
    repository_uri: str
    git_branch: str
    bundle_path: str
    dev_target: str
    prod_target: str
    run_resource_key: str = ""
    asset_manifest: tuple[dict[str, Any], ...] = ()
    asset_manifest_hash: str = ""
    change_summary: str = ""
    jira_link: str = ""
    dev_tag_check_passed: bool | None = None
    dev_tag_check_detail: str = ""
    dev_tag_checked_at: datetime | None = None
    source_preflight_status: str = "NOT_STARTED"
    source_preflight_passed: bool | None = None
    source_preflight_detail: str = ""
    source_preflight_checked_at: datetime | None = None
    source_validation_claim_id: str = ""
    source_validation_claimed_by: str = ""
    source_validation_claimed_at: datetime | None = None
    source_validation_job_run_id: str = ""
    source_validated_revision: str = ""
    requested_at: datetime | None = None
    requested_by: str = ""
    approved_at: datetime | None = None
    approved_by: str = ""
    decision_comment: str = ""
    deployer_profile: str = ""
    deployment_attempt: int = 0
    deployment_claim_id: str = ""
    deployment_claimed_by: str = ""
    deployment_claimed_at: datetime | None = None
    resolved_git_revision: str = ""
    prod_deployment_status: str = "NOT_STARTED"
    prod_tag_check_passed: bool | None = None
    prod_tag_check_detail: str = ""
    dispatch_job_run_id: str = ""
    deployment_run_id: str = ""
    deployment_run_url: str = ""
    deploy_started_at: datetime | None = None
    prod_deployed_at: datetime | None = None
    deployment_message: str = ""
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""

    @field_validator(
        "project_name",
        "run_resource_key",
        "change_summary",
        "jira_link",
        "dev_tag_check_detail",
        "source_preflight_status",
        "source_preflight_detail",
        "source_validation_claim_id",
        "source_validation_claimed_by",
        "source_validation_job_run_id",
        "source_validated_revision",
        "requested_by",
        "approved_by",
        "decision_comment",
        "deployer_profile",
        "deployment_claim_id",
        "deployment_claimed_by",
        "resolved_git_revision",
        "prod_tag_check_detail",
        "dispatch_job_run_id",
        "deployment_run_id",
        "deployment_run_url",
        "deployment_message",
        "created_by",
        "updated_by",
        mode="before",
    )
    @classmethod
    def nullable_request_text(cls, value: Any) -> str:
        return "" if value is None else str(value)


class DecisionRequest(StrictModel):
    approve: bool
    comment: str = Field(default="", max_length=4000)


class AdministrativeRecoveryRequest(StrictModel):
    comment: str = Field(min_length=5, max_length=4000)

    @field_validator("comment")
    @classmethod
    def recovery_comment(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 5:
            raise ValueError("A recovery reason of at least five characters is required.")
        return normalized


class WorkerClaimRequest(StrictModel):
    worker_run_id: str = ""


class SourceValidationClaimResponse(StrictModel):
    claim_id: str
    request: ProductionRequestRecord
    project: ProjectRecord
    dev_workspace_host: str
    prod_workspace_host: str
    dev_catalog: str
    prod_catalog: str
    allowed_prod_workspace_roots: tuple[str, ...] = ()


class SourceValidationCompletion(StrictModel):
    claim_id: str
    success: bool
    resolved_git_revision: str = ""
    detail: str = ""

    @field_validator("resolved_git_revision")
    @classmethod
    def validation_revision(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and (
            len(normalized) < 7
            or len(normalized) > 64
            or not all(character in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("resolved_git_revision must be a hexadecimal Git revision.")
        return normalized


class WorkerClaimResponse(StrictModel):
    claim_id: str
    request: ProductionRequestRecord
    project: ProjectRecord
    dev_workspace_host: str
    prod_workspace_host: str
    dev_catalog: str
    prod_catalog: str
    required_tags: dict[str, str]
    allowed_prod_workspace_roots: tuple[str, ...] = ()


class TagEvidence(StrictModel):
    environment: Literal["dev", "prod"]
    resource_type: str
    resource_id: str = ""
    resource_name: str
    resource_path: str = ""
    tag_key: str
    expected_value: str
    actual_value: str = ""
    validation_result: Literal["PASS", "FAIL", "MISSING", "CONFLICT", "NOT_ACCESSIBLE"]
    detail: str = ""

    @field_validator("tag_key")
    @classmethod
    def known_tag_key(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in MANDATORY_TAG_KEYS:
            raise ValueError("Only mandatory governance tags can be submitted as evidence.")
        return normalized


class WorkerCompletion(StrictModel):
    claim_id: str
    success: bool
    resolved_git_revision: str = ""
    deployment_run_id: str = ""
    deployment_run_url: str = ""
    detail: str = ""
    tag_results: tuple[TagEvidence, ...] = ()

    @field_validator("resolved_git_revision")
    @classmethod
    def optional_revision(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and (len(normalized) < 7 or len(normalized) > 64 or not all(c in "0123456789abcdef" for c in normalized)):
            raise ValueError("resolved_git_revision must be a hexadecimal Git revision.")
        return normalized

    @field_validator("deployment_run_url")
    @classmethod
    def run_url(cls, value: str) -> str:
        return _optional_url(value, "deployment_run_url")

class AssetScanRequest(StrictModel):
    environment: Literal["dev", "prod"] = "dev"
    assets: tuple[AssetSelection, ...] | None = None


class TagRepairRequest(StrictModel):
    asset: AssetSelection
