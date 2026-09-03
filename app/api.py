from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth import Actor, actor_from_request
from app.models import (
    AdministrativeRecoveryRequest,
    AssetScanRequest,
    DecisionRequest,
    ProductionRequestCreate,
    ProjectCreate,
    ProjectStatusRequest,
    ProjectUpdate,
    SourceValidationCompletion,
    TagRepairRequest,
    WorkerClaimRequest,
    WorkerCompletion,
)
from app.service import RegistryService
# ============================================================
# ========== NEW: IMPORT TEAM MODELS ==========
# ============================================================
from app.models import TeamCreate

router = APIRouter(prefix="/api", tags=["registry"])


def service_from_request(request: Request) -> RegistryService:
    return request.app.state.service


def actor(request: Request) -> Actor:
    return actor_from_request(request)


def require_mutation_header(request: Request) -> None:
    """Small CSRF defense for browser JSON mutations.

    Databricks Apps already authenticates the caller. Requiring a custom header additionally
    prevents a cross-origin HTML form from submitting a state-changing request without the app's
    JavaScript client. Protected worker calls may use the same header or an Authorization header.
    """

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("x-governance-request") != "v4" and not request.headers.get(
            "authorization"
        ):
            from app.governance import AuthorizationError

            raise AuthorizationError("Missing X-Governance-Request header.")


Service = Annotated[RegistryService, Depends(service_from_request)]
CurrentActor = Annotated[Actor, Depends(actor)]
Mutation = Annotated[None, Depends(require_mutation_header)]


@router.get("/health")
def health(service: Service) -> dict:
    return service.health()


@router.get("/dashboard")
def dashboard(service: Service, current_actor: CurrentActor) -> dict:
    # Authentication is intentionally evaluated even though the dashboard aggregate is not
    # identity-specific.
    _ = current_actor
    return service.dashboard()

@router.get("/me")
def current_identity(service: Service, current_actor: CurrentActor) -> dict:
    return service.identity(current_actor)


@router.post("/projects", status_code=201)
def create_project(
    payload: ProjectCreate,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.create_project(payload, current_actor)


@router.get("/projects")
def list_projects(
    service: Service,
    current_actor: CurrentActor,
    status: str | None = Query(default=None),
):
    return service.list_projects(current_actor, status)


@router.get("/projects/{project_id}")
def get_project(project_id: str, service: Service, current_actor: CurrentActor):
    return service.get_project(project_id, current_actor)


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.update_project(project_id, payload, current_actor)


@router.post("/projects/{project_id}/status")
def set_project_status(
    project_id: str,
    payload: ProjectStatusRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.set_project_status(project_id, payload, current_actor)


@router.post("/projects/{project_id}/scan")
def scan_assets(
    project_id: str,
    payload: AssetScanRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.scan_assets(
        project_id,
        current_actor,
        environment=payload.environment,
        assets=payload.assets,
    )


@router.post("/projects/{project_id}/fix-tags")
def fix_tags(
    project_id: str,
    payload: TagRepairRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.fix_missing_tags(project_id, payload.asset, current_actor)


@router.post("/projects/{project_id}/production-requests", status_code=201)
def create_production_request(
    project_id: str,
    payload: ProductionRequestCreate,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.create_production_request(project_id, payload, current_actor)


@router.get("/production-requests")
def list_production_requests(
    service: Service,
    current_actor: CurrentActor,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    return service.list_requests(current_actor, project_id=project_id, status=status)


@router.get("/production-requests/{request_id}")
def get_production_request(request_id: str, service: Service, current_actor: CurrentActor):
    return service.request_detail(request_id, current_actor)


@router.post("/production-requests/{request_id}/revalidate")
def revalidate_production_request(
    request_id: str,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.revalidate_request(request_id, current_actor)


@router.post("/production-requests/{request_id}/decision")
def decide_production_request(
    request_id: str,
    payload: DecisionRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.decide_request(request_id, payload, current_actor)


@router.post("/production-requests/{request_id}/retry")
def retry_production_request(
    request_id: str,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.retry_failed_request(request_id, current_actor)


@router.post("/production-requests/{request_id}/recover")
def recover_production_request(
    request_id: str,
    payload: AdministrativeRecoveryRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.recover_stalled_request(request_id, payload, current_actor)


@router.get("/v4/worker/source-validations/next")
def next_source_validation(
    service: Service,
    current_actor: CurrentActor,
):
    return service.next_source_validation(current_actor)


@router.post("/v4/worker/production-requests/{request_id}/source-validation/claim")
def claim_source_validation(
    request_id: str,
    payload: WorkerClaimRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    _ = payload.worker_run_id
    return service.claim_source_validation(request_id, current_actor)


@router.post("/v4/worker/production-requests/{request_id}/source-validation/complete")
def complete_source_validation(
    request_id: str,
    payload: SourceValidationCompletion,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.complete_source_validation(request_id, payload, current_actor)


@router.get("/v4/worker/production-requests/next")
def next_queued_production_request(
    service: Service,
    current_actor: CurrentActor,
    deployer_profile: str = Query(default=""),
):
    return service.next_queued_request(deployer_profile, current_actor)


@router.post("/v4/worker/production-requests/{request_id}/claim")
def claim_production_request(
    request_id: str,
    payload: WorkerClaimRequest,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    # worker_run_id is accepted for forward-compatible tracing; the one-use claim is generated by
    # the registry and is the actual authorization token.
    _ = payload.worker_run_id
    return service.claim_request(request_id, current_actor)


@router.post("/v4/worker/production-requests/{request_id}/complete")
def complete_production_request(
    request_id: str,
    payload: WorkerCompletion,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    return service.complete_request(request_id, payload, current_actor)


@router.get("/audit")
def audit_events(
    service: Service,
    current_actor: CurrentActor,
    limit: int = Query(default=500, ge=1, le=5000),
):
    return service.list_audit(current_actor, limit)


# ============================================================
# ========== NEW: TEAMS ENDPOINTS ==========
# ============================================================

@router.get("/teams")
def list_teams(service: Service, current_actor: CurrentActor):
    """List all active teams."""
    return service.database.list_active_teams()


@router.post("/teams", status_code=201)
def create_team(
    payload: TeamCreate,
    service: Service,
    current_actor: CurrentActor,
    _mutation: Mutation,
):
    """Create a new team."""
    # Only admins can create teams (optional)
    service.authorization.require_admin(current_actor)
    
    from app.governance import utc_now
    record = {
        "team_name": payload.team_name,
        "description": payload.description,
        "created_at": utc_now(),
        "created_by": current_actor.normalized,
        "updated_at": utc_now(),
        "updated_by": current_actor.normalized,
        "is_active": True
    }
    return service.database.create_team(record)