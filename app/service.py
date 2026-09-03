from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Mapping

from app.auth import Actor, Authorization
from app.clients import DeploymentDispatcher
from app.db import RegistryDatabase
from app.governance import (
    MANDATORY_TAG_KEYS,
    PROJECT_ACTIVE,
    PROJECT_ARCHIVED,
    PROJECT_STATUSES,
    REQUEST_ACTION_REQUIRED,
    REQUEST_VALIDATING,
    REQUEST_DEPLOYED,
    REQUEST_DEPLOYING,
    REQUEST_FAILED,
    REQUEST_QUEUED,
    REQUEST_READY,
    REQUEST_REJECTED,
    AuthorizationError,
    ConflictError,
    ValidationError,
    expected_tags,
    generate_audit_id,
    generate_claim_id,
    generate_evidence_id,
    generate_project_id,
    generate_request_id,
    normalized_asset_name,
    stable_hash,
    utc_now,
)
from app.models import (
    AdministrativeRecoveryRequest,
    AssetScanResponse,
    AssetSelection,
    DecisionRequest,
    ProductionRequestCreate,
    ProductionRequestRecord,
    ProjectCreate,
    ProjectRecord,
    ProjectStatusRequest,
    ProjectUpdate,
    ScannedAsset,
    SourceValidationClaimResponse,
    SourceValidationCompletion,
    TagEvidence,
    WorkerClaimResponse,
    WorkerCompletion,
)
from app.scanner import AssetScanner, DatabricksTagManager
from app.settings import Settings


class RegistryService:
    def __init__(
        self,
        settings: Settings,
        database: RegistryDatabase,
        scanner: AssetScanner,
        *,
        tag_manager: DatabricksTagManager | None = None,
        validation_dispatcher: DeploymentDispatcher | None = None,
        dispatcher: DeploymentDispatcher | None = None,
    ):
        self.settings = settings
        self.database = database
        self.scanner = scanner
        self.tag_manager = tag_manager
        self.validation_dispatcher = validation_dispatcher
        self.dispatcher = dispatcher
        self.authorization = Authorization(settings)

    def bootstrap(self) -> None:
        # Registry DDL is applied explicitly through sql/001 or sql/002 before the App starts.
        # Keeping production startup validation-only preserves least-privilege table grants.
        if self.settings.backend != "databricks":
            self.database.ensure_tables()
        self.database.validate_schema()

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "4.0.0",
            "workspace_route": self.settings.workspace_route,
            "dev_catalog": self.settings.dev_catalog,
            "prod_catalog": self.settings.prod_catalog,
            **self.database.health(),
        }

    def dashboard(self) -> dict[str, Any]:
        return self.database.dashboard()

    def identity(self, actor: Actor) -> dict[str, Any]:
        roles: list[str] = []
        if self.authorization.is_admin(actor):
            roles.append("ADMIN")
        if self.authorization.is_approver(actor):
            roles.append("APPROVER")
        if self.authorization.is_auditor(actor):
            roles.append("AUDITOR")
        if self.authorization.is_development_validator(actor):
            roles.append("DEVELOPMENT_VALIDATOR")
        if self.authorization.is_production_deployer(actor):
            roles.append("PRODUCTION_DEPLOYER")
        if not roles:
            roles.append("PROJECT_USER")
        return {
            "actor": actor.subject,
            "normalized_actor": actor.normalized,
            "roles": roles,
            "version": "4.0.0",
            "workspace_route": self.settings.workspace_route,
            "dev_workspace": self.settings.dev_workspace_name,
            "dev_catalog": self.settings.dev_catalog,
            "prod_workspace": self.settings.prod_workspace_name,
            "prod_catalog": self.settings.prod_catalog,
        }

    # Projects -----------------------------------------------------------------
    def create_project(self, payload: ProjectCreate, actor: Actor) -> ProjectRecord:
        # ========== TERMS AND CONDITIONS - Validate terms ==========
        if not payload.terms_accepted:
            raise ValidationError("Terms and conditions must be accepted")

        # Get current terms version from settings
        current_terms_version = self.settings.current_terms_version

        # Verify terms version matches current version
        if payload.terms_version != current_terms_version:
            raise ValidationError(
                f"Terms version mismatch. Required: {current_terms_version}, "
                f"Provided: {payload.terms_version}"
            )

        self._validate_project_options(payload.team_name, payload.data_classification)
        now = utc_now()
        
        # ========== WORKSPACE: Use payload.workspace ==========
        workspace_value = payload.workspace or self.settings.workspace_route
        
        record = {
            "project_id": generate_project_id(),
            "name": payload.name,
            "team_name": payload.team_name,
            "technical_owner_email": payload.technical_owner_email,
            "description": payload.description,
            "lifecycle_status": PROJECT_ACTIVE,
            "created_at": now,
            "created_by": actor.normalized,
            "updated_at": now,
            "updated_by": actor.normalized,
            "workspace": workspace_value,  # ========== WORKSPACE: Updated ==========
            "data_classification": payload.data_classification,
            "go_live_date": payload.go_live_date,
            "documentation_link": payload.documentation_link,
            "data_sources": payload.data_sources,
            "technical_details": payload.technical_details,
            "jira_link": payload.jira_link,
            "business_owner_email": payload.business_owner_email,
            "decision_comment": "",
            # ========== TERMS AND CONDITIONS - ADD THESE 3 LINES ==========
            "terms_accepted_at": now,
            "terms_accepted_by": actor.normalized,
            "terms_version": current_terms_version,
        }
        project = self.database.create_project(record)
        self._audit(
            "PROJECT_REGISTERED",
            actor,
            project_id=record["project_id"],
            new_status=PROJECT_ACTIVE,
            payload={
                "name": payload.name,
                "team_name": payload.team_name,
                "workspace": workspace_value,  # ========== WORKSPACE: Updated ==========
                "dev_catalog": self.settings.dev_catalog,
                "prod_catalog": self.settings.prod_catalog,
                "terms_version": current_terms_version,
            },
        )
        return ProjectRecord.model_validate(project)

    def list_projects(self, actor: Actor, status: str | None = None) -> list[ProjectRecord]:
        projects = self.database.list_projects(status)
        if self.authorization.is_admin(actor) or self.authorization.is_auditor(actor):
            visible = projects
        else:
            visible = [
                item for item in projects if self.authorization.is_project_manager(item, actor)
            ]
        return [ProjectRecord.model_validate(item) for item in visible]

    def get_project(self, project_id: str, actor: Actor | None = None) -> ProjectRecord:
        project = self.database.get_project(project_id)
        if actor is not None and not (
            self.authorization.is_project_manager(project, actor)
            or self.authorization.is_approver(actor)
            or self.authorization.is_auditor(actor)
        ):
            raise ConflictError("Project is not available to this identity.")
        return ProjectRecord.model_validate(project)

    # ========== TERMS AND CONDITIONS - GET PROJECT WITH TERMS STATUS ==========
    def get_project_with_terms_status(self, project_id: str, actor: Actor) -> dict:
        """Get project with terms status information for frontend"""
        # Get the project
        project = self.database.get_project(project_id)

        # Check authorization
        if actor is not None and not (
            self.authorization.is_project_manager(project, actor)
            or self.authorization.is_approver(actor)
            or self.authorization.is_auditor(actor)
        ):
            raise ConflictError("Project is not available to this identity.")

        # Convert to dict
        if hasattr(project, 'model_dump'):
            result = project.model_dump()
        else:
            result = dict(project)

        # Get terms status
        current_terms_version = self.settings.current_terms_version
        terms_accepted_at = result.get("terms_accepted_at")
        terms_accepted = terms_accepted_at is not None
        terms_version = result.get("terms_version", "")
        terms_up_to_date = terms_accepted and terms_version == current_terms_version

        # Add terms status information
        result["terms_accepted"] = terms_accepted
        result["terms_up_to_date"] = terms_up_to_date
        result["terms_version_current"] = current_terms_version
        if not terms_version:
            result["terms_version"] = current_terms_version

        return result

    def update_project(self, project_id: str, payload: ProjectUpdate, actor: Actor) -> ProjectRecord:
        current = self.database.get_project(project_id)
        self.authorization.require_project_manager(current, actor)

        # Get all values from payload
        values = payload.model_dump(exclude_unset=True)
        
        # ========== TERMS AND CONDITIONS ==========
        # Check if terms_accepted is in the payload
        terms_accepted = values.get("terms_accepted")
        if terms_accepted is not None:
            if not terms_accepted:
                raise ValidationError("Terms and conditions must be accepted")
            
            # Verify terms version
            current_terms_version = self.settings.current_terms_version
            provided_terms_version = values.get("terms_version")
            if provided_terms_version != current_terms_version:
                raise ValidationError(
                    f"Terms version mismatch. Required: {current_terms_version}, "
                    f"Provided: {provided_terms_version}"
                )
            
            # Add terms fields to values for the UPDATE
            now = utc_now()
            values["terms_accepted_at"] = now
            values["terms_accepted_by"] = actor.normalized
            values["terms_version"] = current_terms_version
            
            # Audit terms re-acceptance
            self._audit(
                "TERMS_REACCEPTED",
                actor,
                project_id=project_id,
                payload={
                    "terms_version": current_terms_version,
                    "previous_version": current.get("terms_version"),
                },
            )
        
        # Remove terms_accepted (not a database column)
        values.pop("terms_accepted", None)
        
        # ========== FIX: Handle URL fields ==========
        if "jira_link" in values:
            jira_link = values.get("jira_link")
            if jira_link and jira_link.upper() in ("NA", "N/A"):
                values["jira_link"] = "NA"

        if "documentation_link" in values:
            doc_link = values.get("documentation_link")
            if doc_link and doc_link.upper() in ("NA", "N/A"):
                values["documentation_link"] = "NA"

        # Validate project options
        self._validate_project_options(
            str(values.get("team_name") or current["team_name"]),
            str(values.get("data_classification") or current["data_classification"]),
        )

        # If no values to update, return current
        if not values:
            return ProjectRecord.model_validate(current)

        # Add updated_at and updated_by
        values.update({"updated_at": utc_now(), "updated_by": actor.normalized})
        
        # ========== FIX: Single UPDATE operation ==========
        updated = self.database.update_project(project_id, values)

        self._audit(
            "PROJECT_UPDATED",
            actor,
            project_id=project_id,
            payload={"updated_fields": sorted(payload.model_fields_set)},
        )
        return ProjectRecord.model_validate(updated)

    def set_project_status(
        self, project_id: str, payload: ProjectStatusRequest, actor: Actor
    ) -> ProjectRecord:
        self.authorization.require_admin(actor)
        if payload.status not in PROJECT_STATUSES:
            raise ValidationError("Unsupported project lifecycle status.")
        current = self.database.get_project(project_id)
        if current["lifecycle_status"] == payload.status:
            return ProjectRecord.model_validate(current)
        now = utc_now()
        updated = self.database.update_project(
            project_id,
            {
                "lifecycle_status": payload.status,
                "decision_comment": payload.comment.strip(),
                "updated_at": now,
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            f"PROJECT_{payload.status}",
            actor,
            project_id=project_id,
            previous_status=str(current["lifecycle_status"]),
            new_status=payload.status,
            comment=payload.comment,
        )
        return ProjectRecord.model_validate(updated)

    # Asset compliance ----------------------------------------------------------
    def scan_assets(
        self,
        project_id: str,
        actor: Actor,
        *,
        environment: str = "dev",
        assets: tuple[AssetSelection, ...] | None = None,
    ) -> AssetScanResponse:
        project = self.database.get_project(project_id)
        if environment == "dev":
            self.authorization.require_project_manager(project, actor)
        elif not (
            self.authorization.is_admin(actor)
            or self.authorization.is_approver(actor)
            or self.authorization.is_production_deployer(actor)
        ):
            raise ConflictError("Production scans require governance permission.")
        result = self.scanner.scan(project, environment, assets)
        self._audit(
            "ASSET_SCAN_COMPLETED",
            actor,
            project_id=project_id,
            payload={
                "environment": environment,
                "asset_count": len(result.assets),
                "compliant": sum(
                    1 for item in result.assets if item.compliance_status == "COMPLIANT"
                ),
                "warnings": list(result.warnings),
            },
        )
        return result

    def fix_missing_tags(
        self, project_id: str, asset: AssetSelection, actor: Actor
    ) -> ScannedAsset:
        project = self.database.get_project(project_id)
        self.authorization.require_project_manager(project, actor)
        if self.tag_manager is None:
            raise ConflictError("Automatic tag repair is not enabled in this environment.")
        result = self.tag_manager.apply_missing(project, "dev", asset)
        self._audit(
            "TAG_REPAIR_ATTEMPTED",
            actor,
            project_id=project_id,
            payload={
                "resource_type": asset.resource_type,
                "resource_name": asset.resource_name,
                "result": result.compliance_status,
            },
        )
        return result

    # Production requests -------------------------------------------------------
    def create_production_request(
        self, project_id: str, payload: ProductionRequestCreate, actor: Actor
    ) -> ProductionRequestRecord:
        project = self.database.get_project(project_id)
        self.authorization.require_project_manager(project, actor)
        self._require_active(project)
        active_statuses = {
            REQUEST_ACTION_REQUIRED,
            REQUEST_VALIDATING,
            REQUEST_READY,
            REQUEST_QUEUED,
            REQUEST_DEPLOYING,
        }
        existing_active = [
            item
            for item in self.database.list_requests(project_id=project_id)
            if str(item.get("request_status")) in active_statuses
        ]
        if existing_active:
            raise ConflictError(
                "This project already has an active production request: "
                f"{existing_active[0]['request_id']}. Complete, reject, or resolve it first."
            )
        profile = self.settings.deployment_profile_for_team(str(project["team_name"]))
        self._validate_asset_scope(payload.assets, profile.allowed_schemas)

        scan = self.scanner.scan(project, "dev", payload.assets)
        passed = self._scan_passed(scan)
        now = utc_now()
        request_id = generate_request_id()
        manifest = [item.model_dump(mode="json") for item in scan.assets]
        detail = self._scan_summary(scan)
        status = REQUEST_VALIDATING if passed else REQUEST_ACTION_REQUIRED
        record = {
            "request_id": request_id,
            "project_id": project_id,
            "request_status": status,
            "repository_uri": payload.repository_uri,
            "git_branch": payload.git_branch,
            "bundle_path": payload.bundle_path,
            "dev_target": payload.dev_target,
            "prod_target": payload.prod_target,
            "run_resource_key": payload.run_resource_key,
            "asset_manifest": manifest,
            "asset_manifest_hash": stable_hash(manifest),
            "change_summary": payload.change_summary,
            "jira_link": payload.jira_link or str(project.get("jira_link") or ""),
            "dev_tag_check_passed": passed,
            "dev_tag_check_detail": detail,
            "dev_tag_checked_at": now,
            "source_preflight_status": "QUEUED" if passed else "NOT_STARTED",
            "source_preflight_passed": None,
            "source_preflight_detail": (
                "Source validation is queued." if passed else "Fix development tags first."
            ),
            "source_preflight_checked_at": None,
            "source_validation_claim_id": "",
            "source_validation_claimed_by": "",
            "source_validation_claimed_at": None,
            "source_validation_job_run_id": "",
            "source_validated_revision": "",
            "requested_at": now,
            "requested_by": actor.normalized,
            "approved_at": None,
            "approved_by": "",
            "decision_comment": "",
            "deployer_profile": profile.name,
            "deployment_attempt": 0,
            "deployment_claim_id": "",
            "deployment_claimed_by": "",
            "deployment_claimed_at": None,
            "resolved_git_revision": "",
            "prod_deployment_status": "NOT_STARTED",
            "prod_tag_check_passed": None,
            "prod_tag_check_detail": "",
            "dispatch_job_run_id": "",
            "deployment_run_id": "",
            "deployment_run_url": "",
            "deploy_started_at": None,
            "prod_deployed_at": None,
            "deployment_message": "",
            "created_at": now,
            "created_by": actor.normalized,
            "updated_at": now,
            "updated_by": actor.normalized,
        }
        request = self.database.create_request(record)
        self._save_scan_evidence(request_id, project_id, scan, actor)
        existing_config = self.database.get_delivery_config(project_id)
        self.database.upsert_delivery_config(
            {
                "project_id": project_id,
                "repository_uri": payload.repository_uri,
                "git_branch": payload.git_branch,
                "bundle_path": payload.bundle_path,
                "dev_target": payload.dev_target,
                "prod_target": payload.prod_target,
                "deployment_mode": "TAG_GATED_BUNDLE",
                "run_resource_key": payload.run_resource_key,
                "source_status": "CONNECTED",
                "created_at": (
                    existing_config.get("created_at") if existing_config else now
                ),
                "created_by": (
                    existing_config.get("created_by") if existing_config else actor.normalized
                ),
                "updated_at": now,
                "updated_by": actor.normalized,
            }
        )
        self._audit(
            "PRODUCTION_REQUEST_CREATED",
            actor,
            project_id=project_id,
            request_id=request_id,
            new_status=status,
            payload={
                "asset_count": len(manifest),
                "repository_uri": payload.repository_uri,
                "git_branch": payload.git_branch,
                "tag_check_passed": passed,
                "deployer_profile": profile.name,
            },
        )
        if passed:
            self._dispatch_source_validation(request_id, project, actor)
        return ProductionRequestRecord.model_validate(self.database.get_request(request_id))

    def revalidate_request(self, request_id: str, actor: Actor) -> ProductionRequestRecord:
        request = self.database.get_request(request_id)
        project = self.database.get_project(str(request["project_id"]))
        self.authorization.require_project_manager(project, actor)
        if request["request_status"] not in {
            REQUEST_ACTION_REQUIRED,
            REQUEST_VALIDATING,
            REQUEST_READY,
        }:
            raise ConflictError("Only pending requests can be revalidated.")
        scan = self.scanner.scan(project, "dev", self._manifest_selections(request))
        passed = self._scan_passed(scan)
        status = REQUEST_VALIDATING if passed else REQUEST_ACTION_REQUIRED
        now = utc_now()
        manifest = [item.model_dump(mode="json") for item in scan.assets]
        updated = self.database.update_request(
            request_id,
            {
                "request_status": status,
                "asset_manifest": manifest,
                "asset_manifest_hash": stable_hash(manifest),
                "dev_tag_check_passed": passed,
                "dev_tag_check_detail": self._scan_summary(scan),
                "dev_tag_checked_at": now,
                "source_preflight_status": "QUEUED" if passed else "NOT_STARTED",
                "source_preflight_passed": None,
                "source_preflight_detail": (
                    "Source validation is queued." if passed else "Fix development tags first."
                ),
                "source_preflight_checked_at": None,
                "source_validation_claim_id": "",
                "source_validation_claimed_by": "",
                "source_validation_claimed_at": None,
                "source_validation_job_run_id": "",
                "source_validated_revision": "",
                # Revalidation creates a fresh approval window. The original requester remains
                # unchanged so self-approval and request ownership semantics are preserved.
                "requested_at": now,
                "updated_at": now,
                "updated_by": actor.normalized,
            },
        )
        self._save_scan_evidence(request_id, str(request["project_id"]), scan, actor)
        self._audit(
            "PRODUCTION_REQUEST_REVALIDATED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=str(request["request_status"]),
            new_status=status,
            payload={"tag_check_passed": passed},
        )
        if passed:
            self._dispatch_source_validation(request_id, project, actor)
        return ProductionRequestRecord.model_validate(self.database.get_request(request_id))

    def decide_request(
        self, request_id: str, payload: DecisionRequest, actor: Actor
    ) -> ProductionRequestRecord:
        self.authorization.require_approver(actor)
        request = self.database.get_request(request_id)
        project = self.database.get_project(str(request["project_id"]))
        self._require_active(project)
        if request["request_status"] != REQUEST_READY:
            raise ConflictError("Production request is not ready for approval.")
        if not request.get("source_preflight_passed"):
            raise ConflictError("Bundle/source preflight has not passed.")
        requested_at = request.get("requested_at")
        if requested_at and requested_at < utc_now() - timedelta(
            hours=self.settings.request_expiry_hours
        ):
            raise ConflictError(
                "Production request has expired. Revalidate it before approval."
            )
        if (
            not self.settings.allow_self_approval
            and actor.normalized == str(request.get("requested_by") or "").lower()
        ):
            raise ConflictError("Self-approval is not allowed.")

        if not payload.approve:
            now = utc_now()
            updated = self.database.update_request(
                request_id,
                {
                    "request_status": REQUEST_REJECTED,
                    "decision_comment": payload.comment.strip(),
                    "approved_at": now,
                    "approved_by": actor.normalized,
                    "updated_at": now,
                    "updated_by": actor.normalized,
                },
            )
            self._audit(
                "PRODUCTION_REQUEST_REJECTED",
                actor,
                project_id=str(request["project_id"]),
                request_id=request_id,
                previous_status=REQUEST_READY,
                new_status=REQUEST_REJECTED,
                comment=payload.comment,
            )
            return ProductionRequestRecord.model_validate(updated)

        # Approval always revalidates current dev tags in place. Git SHA is deliberately not part
        # of this approval contract; the worker records the revision it actually deploys.
        scan = self.scanner.scan(project, "dev", self._manifest_selections(request))
        if not self._scan_passed(scan):
            now = utc_now()
            manifest = [item.model_dump(mode="json") for item in scan.assets]
            updated = self.database.update_request(
                request_id,
                {
                    "request_status": REQUEST_ACTION_REQUIRED,
                    "asset_manifest": manifest,
                    "asset_manifest_hash": stable_hash(manifest),
                    "dev_tag_check_passed": False,
                    "dev_tag_check_detail": self._scan_summary(scan),
                    "dev_tag_checked_at": now,
                    "decision_comment": "Approval blocked by current development tag state.",
                    "updated_at": now,
                    "updated_by": actor.normalized,
                },
            )
            self._save_scan_evidence(request_id, str(request["project_id"]), scan, actor)
            self._audit(
                "APPROVAL_BLOCKED_BY_TAGS",
                actor,
                project_id=str(request["project_id"]),
                request_id=request_id,
                previous_status=REQUEST_READY,
                new_status=REQUEST_ACTION_REQUIRED,
                payload={"detail": self._scan_summary(scan)},
            )
            return ProductionRequestRecord.model_validate(updated)

        now = utc_now()
        profile = self.settings.deployment_profile_for_team(str(project["team_name"]))
        updated = self.database.update_request(
            request_id,
            {
                "request_status": REQUEST_QUEUED,
                "approved_at": now,
                "approved_by": actor.normalized,
                "decision_comment": payload.comment.strip(),
                "deployer_profile": profile.name,
                "prod_deployment_status": "QUEUED",
                "updated_at": now,
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            "PRODUCTION_REQUEST_APPROVED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=REQUEST_READY,
            new_status=REQUEST_QUEUED,
            comment=payload.comment,
            payload={"deployer_profile": profile.name},
        )
        self._dispatch_queued_request(request_id, project, profile.name, actor)
        return ProductionRequestRecord.model_validate(self.database.get_request(request_id))

    def retry_failed_request(self, request_id: str, actor: Actor) -> ProductionRequestRecord:
        self.authorization.require_approver(actor)
        request = self.database.get_request(request_id)
        if request["request_status"] != REQUEST_FAILED:
            raise ConflictError("Only a failed production request can be retried.")
        project = self.database.get_project(str(request["project_id"]))
        self._require_active(project)
        scan = self.scanner.scan(project, "dev", self._manifest_selections(request))
        if not self._scan_passed(scan):
            raise ConflictError("Development tags no longer pass; create or revalidate a request.")
        now = utc_now()
        updated = self.database.update_request(
            request_id,
            {
                "request_status": REQUEST_QUEUED,
                "prod_deployment_status": "QUEUED",
                "deployment_claim_id": "",
                "deployment_claimed_by": "",
                "deployment_claimed_at": None,
                "deploy_started_at": None,
                "prod_deployed_at": None,
                "prod_tag_check_passed": None,
                "prod_tag_check_detail": "",
                "deployment_message": "Retry queued after fresh development tag validation.",
                "updated_at": now,
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            "PRODUCTION_DEPLOYMENT_RETRIED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=REQUEST_FAILED,
            new_status=REQUEST_QUEUED,
        )
        self._dispatch_queued_request(
            request_id, project, str(request.get("deployer_profile") or "default"), actor
        )
        return ProductionRequestRecord.model_validate(self.database.get_request(request_id))

    def recover_stalled_request(
        self,
        request_id: str,
        payload: AdministrativeRecoveryRequest,
        actor: Actor,
    ) -> ProductionRequestRecord:
        """Recover a worker that stopped after claiming a request.

        Source validation is safely requeued with a new claim. A production deployment cannot be
        resumed safely in place, so it is moved to DEPLOY_FAILED and must pass the normal
        approver-controlled retry path. Any late completion from the abandoned worker is rejected
        by the request status/claim checks.
        """

        self.authorization.require_admin(actor)
        request = self.database.get_request(request_id)
        project = self.database.get_project(str(request["project_id"]))
        now = utc_now()

        if request["request_status"] == REQUEST_VALIDATING:
            updated = self.database.update_request(
                request_id,
                {
                    "source_preflight_status": "QUEUED",
                    "source_preflight_passed": None,
                    "source_preflight_detail": (
                        f"Administratively requeued after a stalled validator: {payload.comment}"
                    ),
                    "source_preflight_checked_at": None,
                    "source_validation_claim_id": "",
                    "source_validation_claimed_by": "",
                    "source_validation_claimed_at": None,
                    "source_validation_job_run_id": "",
                    "source_validated_revision": "",
                    "requested_at": now,
                    "updated_at": now,
                    "updated_by": actor.normalized,
                },
            )
            self._audit(
                "SOURCE_VALIDATION_RECOVERED",
                actor,
                project_id=str(request["project_id"]),
                request_id=request_id,
                previous_status=REQUEST_VALIDATING,
                new_status=REQUEST_VALIDATING,
                comment=payload.comment,
            )
            self._dispatch_source_validation(request_id, project, actor)
            return ProductionRequestRecord.model_validate(
                self.database.get_request(request_id)
            )

        if request["request_status"] == REQUEST_DEPLOYING:
            updated = self.database.update_request(
                request_id,
                {
                    "request_status": REQUEST_FAILED,
                    "prod_deployment_status": "FAILED",
                    "prod_tag_check_passed": False,
                    "prod_tag_check_detail": (
                        "Production verification did not complete because the prior worker was "
                        "administratively recovered."
                    ),
                    "deployment_message": (
                        f"Deployment marked failed after a stalled worker: {payload.comment}"
                    ),
                    "updated_at": now,
                    "updated_by": actor.normalized,
                },
            )
            self._audit(
                "PRODUCTION_DEPLOYMENT_RECOVERED",
                actor,
                project_id=str(request["project_id"]),
                request_id=request_id,
                previous_status=REQUEST_DEPLOYING,
                new_status=REQUEST_FAILED,
                comment=payload.comment,
                payload={"abandoned_claim_id": request.get("deployment_claim_id") or ""},
            )
            return ProductionRequestRecord.model_validate(updated)

        raise ConflictError(
            "Administrative recovery is available only for VALIDATING or DEPLOYING requests."
        )

    def get_request(self, request_id: str, actor: Actor) -> ProductionRequestRecord:
        request = self.database.get_request(request_id)
        project = self.database.get_project(str(request["project_id"]))
        if not (
            self.authorization.is_project_manager(project, actor)
            or self.authorization.is_approver(actor)
            or self.authorization.is_auditor(actor)
            or self.authorization.is_production_deployer(actor)
        ):
            raise ConflictError("Production request is not available to this identity.")
        return ProductionRequestRecord.model_validate(request)

    def list_requests(
        self,
        actor: Actor,
        *,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[ProductionRequestRecord]:
        rows = self.database.list_requests(project_id, status)
        if (
            self.authorization.is_approver(actor)
            or self.authorization.is_auditor(actor)
            or self.authorization.is_development_validator(actor)
            or self.authorization.is_production_deployer(actor)
        ):
            visible = rows
        else:
            visible = []
            projects: dict[str, dict[str, Any]] = {}
            for item in rows:
                pid = str(item["project_id"])
                project = projects.setdefault(pid, self.database.get_project(pid))
                if self.authorization.is_project_manager(project, actor):
                    visible.append(item)
        return [ProductionRequestRecord.model_validate(item) for item in visible]

    def request_detail(self, request_id: str, actor: Actor) -> dict[str, Any]:
        request = self.get_request(request_id, actor)
        project = self.get_project(request.project_id, actor)
        return {
            "request": request,
            "project": project,
            "tag_evidence": self.database.list_tag_evidence(request_id),
        }

    # Protected source-validation worker --------------------------------------
    def next_source_validation(self, actor: Actor) -> dict[str, str]:
        self.authorization.require_development_validator(actor)
        request = self.database.next_source_validation()
        return {"request_id": str(request["request_id"])} if request else {}

    def claim_source_validation(
        self, request_id: str, actor: Actor
    ) -> SourceValidationClaimResponse:
        self.authorization.require_development_validator(actor)
        request = self.database.get_request(request_id)
        project = self.database.get_project(str(request["project_id"]))
        self._require_active(project)
        claim_id = generate_claim_id()
        claimed = self.database.claim_source_validation(
            request_id, claim_id, actor.normalized, utc_now()
        )
        self._audit(
            "SOURCE_VALIDATION_CLAIMED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=REQUEST_VALIDATING,
            new_status=REQUEST_VALIDATING,
            payload={"claim_id": claim_id},
        )
        profile = self.settings.deployment_profile_for_team(str(project["team_name"]))
        return SourceValidationClaimResponse(
            claim_id=claim_id,
            request=ProductionRequestRecord.model_validate(claimed),
            project=ProjectRecord.model_validate(project),
            dev_workspace_host=self.settings.dev_host,
            prod_workspace_host=self.settings.prod_host,
            dev_catalog=self.settings.dev_catalog,
            prod_catalog=self.settings.prod_catalog,
            allowed_prod_workspace_roots=profile.allowed_workspace_roots,
        )

    def complete_source_validation(
        self, request_id: str, payload: SourceValidationCompletion, actor: Actor
    ) -> ProductionRequestRecord:
        self.authorization.require_development_validator(actor)
        request = self.database.get_request(request_id)
        if request.get("request_status") != REQUEST_VALIDATING:
            raise ConflictError("Production request is not waiting for source validation.")
        if payload.claim_id != request.get("source_validation_claim_id"):
            raise ConflictError("Source-validation claim does not match the active claim.")
        if actor.normalized != str(request.get("source_validation_claimed_by") or ""):
            raise AuthorizationError(
                "Only the development validator that claimed this request can complete it."
            )
        now = utc_now()
        final_status = REQUEST_READY if payload.success else REQUEST_ACTION_REQUIRED
        updated = self.database.update_request(
            request_id,
            {
                "request_status": final_status,
                "source_preflight_status": "PASSED" if payload.success else "FAILED",
                "source_preflight_passed": payload.success,
                "source_preflight_detail": payload.detail.strip(),
                "source_preflight_checked_at": now,
                "source_validated_revision": payload.resolved_git_revision,
                "updated_at": now,
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            "SOURCE_VALIDATION_PASSED" if payload.success else "SOURCE_VALIDATION_FAILED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=REQUEST_VALIDATING,
            new_status=final_status,
            comment=payload.detail,
            payload={"resolved_git_revision": payload.resolved_git_revision},
        )
        return ProductionRequestRecord.model_validate(updated)

    # Protected production worker ---------------------------------------------
    def next_queued_request(self, deployer_profile: str, actor: Actor) -> dict[str, str]:
        self.authorization.require_production_deployer(actor)
        profile_name = deployer_profile.strip()
        if not profile_name:
            allowed = [
                profile
                for profile in self.settings.deployment_profiles
                if profile.allows_actor(actor.normalized)
            ]
            if len(allowed) != 1:
                raise ValidationError(
                    "deployer_profile is required when the production identity can access "
                    "zero or multiple deployment profiles."
                )
            profile = allowed[0]
        else:
            profile = self.settings.deployment_profile(profile_name)
        self._require_profile_actor(profile, actor)
        request = self.database.next_queued_request(profile.name)
        return {"request_id": str(request["request_id"])} if request else {}

    def claim_request(self, request_id: str, actor: Actor) -> WorkerClaimResponse:
        self.authorization.require_production_deployer(actor)
        request = self.database.get_request(request_id)
        if request["request_status"] != REQUEST_QUEUED:
            raise ConflictError("Production request is not queued.")
        project = self.database.get_project(str(request["project_id"]))
        self._require_active(project)
        profile = self.settings.deployment_profile(
            str(request.get("deployer_profile") or "default")
        )
        expected_profile = self.settings.deployment_profile_for_team(str(project["team_name"]))
        if profile.name != expected_profile.name:
            raise ConflictError(
                "The request deployment profile no longer matches the project's team."
            )
        self._require_profile_actor(profile, actor)
        claim_id = generate_claim_id()
        claimed = self.database.claim_request(request_id, claim_id, actor.normalized, utc_now())
        self._audit(
            "PRODUCTION_DEPLOYMENT_CLAIMED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=REQUEST_QUEUED,
            new_status=REQUEST_DEPLOYING,
            payload={"claim_id": claim_id},
        )
        return WorkerClaimResponse(
            claim_id=claim_id,
            request=ProductionRequestRecord.model_validate(claimed),
            project=ProjectRecord.model_validate(project),
            dev_workspace_host=self.settings.dev_host,
            prod_workspace_host=self.settings.prod_host,
            dev_catalog=self.settings.dev_catalog,
            prod_catalog=self.settings.prod_catalog,
            required_tags=expected_tags(project, "prod"),
            allowed_prod_workspace_roots=profile.allowed_workspace_roots,
        )

    def complete_request(
        self, request_id: str, payload: WorkerCompletion, actor: Actor
    ) -> ProductionRequestRecord:
        self.authorization.require_production_deployer(actor)
        request = self.database.get_request(request_id)
        if request["request_status"] != REQUEST_DEPLOYING:
            raise ConflictError("Production request is not being deployed.")
        if payload.claim_id != request.get("deployment_claim_id"):
            raise ConflictError("Deployment claim does not match the active request claim.")
        if actor.normalized != str(request.get("deployment_claimed_by") or ""):
            raise AuthorizationError(
                "Only the production deployer that claimed this request can complete it."
            )
        profile = self.settings.deployment_profile(
            str(request.get("deployer_profile") or "default")
        )
        self._require_profile_actor(profile, actor)
        project = self.database.get_project(str(request["project_id"]))
        tag_passed, tag_detail = self._worker_tag_result(
            request, payload.tag_results, expected_tags(project, "prod")
        )
        succeeded = bool(payload.success and tag_passed)
        final_status = REQUEST_DEPLOYED if succeeded else REQUEST_FAILED
        now = utc_now()
        rows = [
            {
                "evidence_id": generate_evidence_id(),
                "request_id": request_id,
                "project_id": str(request["project_id"]),
                "environment": "prod",
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "resource_name": item.resource_name,
                "resource_path": item.resource_path,
                "tag_key": item.tag_key,
                "expected_value": item.expected_value,
                "actual_value": item.actual_value,
                "validation_result": item.validation_result,
                "detail": item.detail,
                "checked_at": now,
                "checked_by": actor.normalized,
            }
            for item in payload.tag_results
        ]
        self.database.replace_tag_evidence(request_id, "prod", rows)
        message_parts = [part for part in (payload.detail.strip(), tag_detail) if part]
        updated = self.database.update_request(
            request_id,
            {
                "request_status": final_status,
                "prod_deployment_status": "SUCCEEDED" if succeeded else "FAILED",
                "resolved_git_revision": payload.resolved_git_revision,
                "prod_tag_check_passed": tag_passed,
                "prod_tag_check_detail": tag_detail,
                "deployment_run_id": payload.deployment_run_id,
                "deployment_run_url": payload.deployment_run_url,
                "prod_deployed_at": now,
                "deployment_message": " ".join(message_parts),
                "updated_at": now,
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            "PRODUCTION_DEPLOYMENT_SUCCEEDED" if succeeded else "PRODUCTION_DEPLOYMENT_FAILED",
            actor,
            project_id=str(request["project_id"]),
            request_id=request_id,
            previous_status=REQUEST_DEPLOYING,
            new_status=final_status,
            comment=payload.detail,
            payload={
                "resolved_git_revision": payload.resolved_git_revision,
                "tag_check_passed": tag_passed,
                "tag_detail": tag_detail,
            },
        )
        return ProductionRequestRecord.model_validate(updated)

    # Audit --------------------------------------------------------------------
    def list_audit(self, actor: Actor, limit: int = 500) -> list[dict[str, Any]]:
        self.authorization.require_auditor(actor)
        return self.database.list_audit(limit)

    # ========== WORKSPACE: ADDED METHOD ==========
 # ========== WORKSPACE: UPDATED METHOD TO USE VIEW ==========
    def get_available_workspaces(self) -> list[str]:
        """
        Get list of available workspace business-aliases.
        
        Tries in order:
        1. vw_workspaces view (created in registry schema)
        2. governed_projects table (existing projects)
        3. Settings (dev_workspace_name, prod_workspace_name)
        4. Hardcoded fallback
        """
        workspaces = []
        seen = set()
        
        # ========== SOURCE 1: View ==========
        try:
            rows = self.database._fetchall(f"""
                SELECT 
                    workspace_alias
                FROM `{self.settings.registry_catalog}`.`{self.settings.registry_schema}`.`vw_workspaces`
                ORDER BY workspace_alias
            """)
            
            for row in rows:
                if row.get("workspace_alias"):
                    alias = str(row["workspace_alias"]).strip()
                    if alias and alias not in seen:
                        seen.add(alias)
                        workspaces.append(alias)
            
            # If we got data from the view, return it
            if workspaces:
                return workspaces
                
        except Exception as e:
            print(f"Error fetching workspaces from view: {e}")
        
        # ========== SOURCE 2: governed_projects ==========
        try:
            rows = self.database._fetchall(f"""
                SELECT DISTINCT 
                    workspace
                FROM {self.settings.project_table}
                WHERE workspace IS NOT NULL 
                AND workspace != ''
                AND workspace != 'Not Set'
                ORDER BY workspace
            """)
            
            for row in rows:
                if row.get("workspace"):
                    name = str(row["workspace"])
                    if ' to ' in name:
                        parts = name.split(' to ')
                        if len(parts) >= 1:
                            base = parts[0].replace('-dev', '').replace('-prod', '')
                            if base and base not in seen:
                                seen.add(base)
                                workspaces.append(base)
                    else:
                        base = name.split('-')[0] if '-' in name else name
                        if base and base not in seen:
                            seen.add(base)
                            workspaces.append(base)
        except Exception as e:
            print(f"Error fetching from governed_projects: {e}")
        
        # ========== SOURCE 3: Settings ==========
        if not workspaces:
            try:
                dev_name = self.settings.dev_workspace_name
                if dev_name:
                    base = dev_name.split('-')[0] if '-' in dev_name else dev_name
                    if base and base not in seen:
                        seen.add(base)
                        workspaces.append(base)
                
                prod_name = self.settings.prod_workspace_name
                if prod_name and prod_name != dev_name:
                    base = prod_name.split('-')[0] if '-' in prod_name else prod_name
                    if base and base not in seen:
                        seen.add(base)
                        workspaces.append(base)
            except:
                pass
        
        # ========== SOURCE 4: Hardcoded ==========
        if not workspaces:
            default_workspaces = ['it', 'operations', 'platform', 'sales', 'software', 'technology']
            for ws in default_workspaces:
                if ws not in seen:
                    seen.add(ws)
                    workspaces.append(ws)
        
        return workspaces
    # Internal helpers ----------------------------------------------------------
    def _validate_project_options(self, team_name: str, data_classification: str) -> None:
        if self.settings.teams and team_name not in self.settings.teams:
            raise ValidationError(f"Team '{team_name}' is not configured for this registry.")
        if (
            self.settings.data_classifications
            and data_classification not in self.settings.data_classifications
        ):
            raise ValidationError(
                f"Data classification '{data_classification}' is not configured."
            )
        self.settings.deployment_profile_for_team(team_name)

    def _dispatch_source_validation(
        self, request_id: str, project: Mapping[str, Any], actor: Actor
    ) -> None:
        job_id = self.settings.dev_validation_job_id
        if self.validation_dispatcher is None or not job_id:
            self.database.update_request(
                request_id,
                {
                    "source_preflight_detail": (
                        "Development tags passed. Source validation is queued, but the "
                        "development validator job is not configured."
                    ),
                    "updated_at": utc_now(),
                    "updated_by": actor.normalized,
                },
            )
            return
        try:
            request = self.database.get_request(request_id)
            requested_at = request.get("requested_at") or request.get("created_at") or ""
            run_id = self.validation_dispatcher.run(
                job_id,
                request_id,
                f"source:{request_id}:{requested_at}",
            )
        except Exception as exc:
            self.database.update_request(
                request_id,
                {
                    "source_preflight_detail": f"Source validator wake-up failed: {exc}",
                    "updated_at": utc_now(),
                    "updated_by": actor.normalized,
                },
            )
            self._audit(
                "SOURCE_VALIDATION_WAKEUP_FAILED",
                actor,
                project_id=str(project["project_id"]),
                request_id=request_id,
                payload={"error": str(exc)},
            )
            return
        self.database.update_request(
            request_id,
            {
                "source_validation_job_run_id": run_id,
                "source_preflight_detail": "Protected source validator started.",
                "updated_at": utc_now(),
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            "SOURCE_VALIDATION_STARTED",
            actor,
            project_id=str(project["project_id"]),
            request_id=request_id,
            payload={"validation_job_run_id": run_id},
        )

    def _dispatch_queued_request(
        self, request_id: str, project: Mapping[str, Any], profile_name: str, actor: Actor
    ) -> None:
        profile = self.settings.deployment_profile_for_team(str(project["team_name"]))
        if profile.name != profile_name:
            raise ConflictError("Project team no longer matches the approved deployment profile.")
        if self.dispatcher is None or not profile.job_id:
            self.database.update_request(
                request_id,
                {
                    "deployment_message": (
                        "Approved and queued. Immediate dispatcher wake-up is not configured; "
                        "run the recovery dispatcher after setting the team deployment job ID."
                    ),
                    "updated_at": utc_now(),
                    "updated_by": actor.normalized,
                },
            )
            return
        try:
            request = self.database.get_request(request_id)
            next_attempt = int(request.get("deployment_attempt") or 0) + 1
            run_id = self.dispatcher.run(
                profile.job_id,
                request_id,
                f"production:{request_id}:{next_attempt}",
            )
        except Exception as exc:  # keep queued so the recovery dispatcher can retry
            self.database.update_request(
                request_id,
                {
                    "deployment_message": f"Immediate dispatcher wake-up failed: {exc}",
                    "updated_at": utc_now(),
                    "updated_by": actor.normalized,
                },
            )
            self._audit(
                "PRODUCTION_DISPATCH_WAKEUP_FAILED",
                actor,
                project_id=str(project["project_id"]),
                request_id=request_id,
                payload={"error": str(exc)},
            )
            return
        self.database.update_request(
            request_id,
            {
                "dispatch_job_run_id": run_id,
                "deployment_message": "Production dispatcher started.",
                "updated_at": utc_now(),
                "updated_by": actor.normalized,
            },
        )
        self._audit(
            "PRODUCTION_DISPATCH_STARTED",
            actor,
            project_id=str(project["project_id"]),
            request_id=request_id,
            payload={"dispatch_job_run_id": run_id},
        )

    @staticmethod
    def _require_profile_actor(profile: Any, actor: Actor) -> None:
        if not profile.allows_actor(actor.normalized):
            raise AuthorizationError(
                f"Production identity is not assigned to deployment profile '{profile.name}'."
            )

    @staticmethod
    def _require_active(project: Mapping[str, Any]) -> None:
        if project.get("lifecycle_status") != PROJECT_ACTIVE:
            raise ConflictError("Project must be ACTIVE for production activity.")

    def _validate_asset_scope(
        self, assets: tuple[AssetSelection, ...], allowed_schemas: tuple[str, ...]
    ) -> None:
        allowed = {item.lower() for item in allowed_schemas}
        for asset in assets:
            if asset.resource_type in {"schema", "table", "view", "volume"}:
                if asset.catalog_name != self.settings.dev_catalog:
                    raise ValidationError(
                        f"Development Unity Catalog assets must be in {self.settings.dev_catalog}."
                    )
                if allowed and asset.schema_name.lower() not in allowed:
                    raise ValidationError(
                        f"Schema {asset.schema_name} is not allowed by the team's production profile."
                    )

    @staticmethod
    def _scan_passed(scan: AssetScanResponse) -> bool:
        return bool(scan.assets) and all(
            item.compliance_status == "COMPLIANT" for item in scan.assets
        )

    @staticmethod
    def _scan_summary(scan: AssetScanResponse) -> str:
        checks = len(scan.assets) * len(MANDATORY_TAG_KEYS)
        passed = sum(
            1
            for asset in scan.assets
            for key, expected in scan.expected_tags.items()
            if asset.tags.get(key) == expected
        )
        status_counts: dict[str, int] = {}
        for item in scan.assets:
            status_counts[item.compliance_status] = status_counts.get(item.compliance_status, 0) + 1
        parts = [f"{passed}/{checks} mandatory tag checks passed across {len(scan.assets)} assets."]
        if status_counts:
            parts.append(
                ", ".join(f"{key}: {value}" for key, value in sorted(status_counts.items()))
            )
        if scan.warnings:
            parts.append("Warnings: " + " | ".join(scan.warnings))
        return " ".join(parts)

    def _save_scan_evidence(
        self,
        request_id: str,
        project_id: str,
        scan: AssetScanResponse,
        actor: Actor,
    ) -> None:
        now = utc_now()
        rows = []
        for asset in scan.assets:
            for key, expected in scan.expected_tags.items():
                actual = asset.tags.get(key, "")
                if asset.compliance_status == "NOT_ACCESSIBLE":
                    result = "NOT_ACCESSIBLE"
                elif not actual:
                    result = "MISSING"
                elif actual == expected:
                    result = "PASS"
                else:
                    result = "CONFLICT"
                rows.append(
                    {
                        "evidence_id": generate_evidence_id(),
                        "request_id": request_id,
                        "project_id": project_id,
                        "environment": scan.environment,
                        "resource_type": asset.resource_type,
                        "resource_id": asset.resource_id,
                        "resource_name": asset.resource_name,
                        "resource_path": asset.resource_path,
                        "tag_key": key,
                        "expected_value": expected,
                        "actual_value": actual,
                        "validation_result": result,
                        "detail": asset.detail,
                        "checked_at": now,
                        "checked_by": actor.normalized,
                    }
                )
        self.database.replace_tag_evidence(request_id, scan.environment, rows)

    @staticmethod
    def _manifest_selections(request: Mapping[str, Any]) -> tuple[AssetSelection, ...]:
        fields = {
            "resource_type",
            "resource_id",
            "resource_name",
            "resource_path",
            "catalog_name",
            "schema_name",
        }
        return tuple(
            AssetSelection.model_validate({key: item.get(key, "") for key in fields})
            for item in request.get("asset_manifest") or ()
        )

    def _worker_tag_result(
        self,
        request: Mapping[str, Any],
        results: tuple[TagEvidence, ...],
        required_tags: Mapping[str, str],
    ) -> tuple[bool, str]:
        """Require evidence for the exact selected production assets and exact tag contract.

        Counting evidence rows is insufficient: a worker must not be able to satisfy a request by
        returning three passing tags for an unrelated asset. Bundle development prefixes are
        normalized only for display-name matching; Unity Catalog assets are matched by their full
        production name.
        """

        if not results:
            return False, "Production worker did not submit tag evidence."

        def manifest_key(raw: Mapping[str, Any]) -> tuple[str, str]:
            resource_type = str(raw.get("resource_type") or "").strip().lower()
            if resource_type in {"schema", "table", "view", "volume"}:
                schema_name = str(raw.get("schema_name") or "").strip()
                resource_name = str(raw.get("resource_name") or "").strip()
                if resource_type == "schema":
                    locator = f"{self.settings.prod_catalog}.{schema_name or resource_name}"
                else:
                    locator = (
                        f"{self.settings.prod_catalog}.{schema_name}.{resource_name}"
                    )
                return resource_type, locator.casefold()
            return resource_type, normalized_asset_name(
                str(raw.get("resource_name") or "")
            )

        def evidence_key(item: TagEvidence) -> tuple[str, str]:
            if item.resource_type in {"schema", "table", "view", "volume"}:
                return item.resource_type, item.resource_path.strip().casefold()
            return item.resource_type, normalized_asset_name(item.resource_name)

        manifest = tuple(request.get("asset_manifest") or ())
        expected_assets: dict[tuple[str, str], Mapping[str, Any]] = {}
        duplicate_assets: list[str] = []
        for raw in manifest:
            key = manifest_key(raw)
            if not key[1]:
                duplicate_assets.append(
                    f"{raw.get('resource_type', '')}:{raw.get('resource_name', '')}:missing locator"
                )
            elif key in expected_assets:
                duplicate_assets.append(f"{key[0]}:{key[1]}")
            else:
                expected_assets[key] = raw
        if duplicate_assets:
            return (
                False,
                "Selected asset manifest is ambiguous: " + "; ".join(duplicate_assets[:20]),
            )

        coverage: dict[tuple[str, str], set[str]] = {key: set() for key in expected_assets}
        seen_rows: set[tuple[tuple[str, str], str]] = set()
        failures: list[str] = []
        for item in results:
            key = evidence_key(item)
            label = f"{item.resource_type}:{item.resource_name}"
            if item.environment != "prod":
                failures.append(f"{label}:non-production evidence")
                continue
            if key not in expected_assets:
                failures.append(f"{label}:not in the approved asset manifest")
                continue
            row_key = (key, item.tag_key)
            if row_key in seen_rows:
                failures.append(f"{label}:{item.tag_key}:duplicate evidence")
                continue
            seen_rows.add(row_key)
            contract_value = str(required_tags.get(item.tag_key) or "")
            if not contract_value:
                failures.append(f"{label}:{item.tag_key}:not in the mandatory tag contract")
                continue
            if item.expected_value != contract_value:
                failures.append(
                    f"{label}:{item.tag_key}:worker expected '{item.expected_value}' instead of "
                    f"'{contract_value}'"
                )
                continue
            if item.validation_result != "PASS" or item.actual_value != contract_value:
                failures.append(
                    f"{label}:{item.tag_key}={item.actual_value or '<missing>'}"
                )
                continue
            coverage[key].add(item.tag_key)

        complete = [
            key
            for key, keys in coverage.items()
            if set(MANDATORY_TAG_KEYS).issubset(keys)
        ]
        missing_assets = [
            f"{key[0]}:{key[1]}"
            for key, keys in coverage.items()
            if not set(MANDATORY_TAG_KEYS).issubset(keys)
        ]
        passed = not failures and not missing_assets and len(complete) == len(expected_assets)
        if passed:
            return True, f"All mandatory production tags passed for {len(complete)} selected assets."

        detail = (
            f"Production tag coverage passed for {len(complete)}/{len(expected_assets)} selected "
            "assets."
        )
        if missing_assets:
            detail += " Missing or incomplete: " + "; ".join(missing_assets[:20])
        if failures:
            detail += " Failures: " + "; ".join(failures[:20])
        return False, detail

    def _audit(
        self,
        event_type: str,
        actor: Actor,
        *,
        project_id: str = "",
        request_id: str = "",
        previous_status: str = "",
        new_status: str = "",
        comment: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        actor_type = (
            "SERVICE_PRINCIPAL"
            if (
                self.authorization.is_production_deployer(actor)
                or self.authorization.is_development_validator(actor)
            )
            else "USER"
        )
        self.database.append_audit(
            {
                "event_id": generate_audit_id(),
                "event_type": event_type,
                "project_id": project_id,
                "request_id": request_id,
                "actor": actor.normalized,
                "actor_type": actor_type,
                "event_at": utc_now(),
                "previous_status": previous_status,
                "new_status": new_status,
                "comment": comment.strip(),
                "payload_json": json.dumps(payload or {}, separators=(",", ":"), default=str),
                "correlation_id": request_id or project_id,
            }
        )