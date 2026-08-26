from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import actor_from_request
from app.governance import REQUEST_READY
from app.service import RegistryService

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).parent / "templates"))


def _service(request: Request) -> RegistryService:
    return request.app.state.service


def _context(request: Request, **values: Any) -> dict[str, Any]:
    actor = actor_from_request(request)
    service = _service(request)
    auth = service.authorization
    return {
        "request": request,
        "actor": actor,
        "version": "4.0.0",
        "settings": service.settings,
        "is_admin": auth.is_admin(actor),
        "is_approver": auth.is_approver(actor),
        "is_auditor": auth.is_auditor(actor),
        **values,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    projects = service.list_projects(actor)
    requests = service.list_requests(actor)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            title="Governance home",
            summary=service.dashboard(),
            projects=projects[:5],
            production_requests=requests[:8],
        ),
    )


@router.get("/projects", response_class=HTMLResponse)
def projects(request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    return templates.TemplateResponse(
        request,
        "projects.html",
        _context(request, title="Projects", projects=service.list_projects(actor)),
    )


@router.get("/projects/new", response_class=HTMLResponse)
def project_new(request: Request):
    return templates.TemplateResponse(
        request,
        "project_form.html",
        _context(request, title="Register project", project=None),
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: str, request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    project = service.get_project(project_id, actor)
    production_requests = service.list_requests(actor, project_id=project_id)
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        _context(
            request,
            title=project.name,
            project=project,
            production_requests=production_requests,
            required_tags={
                "project_tag": project.project_id,
                "environment": "dev",
                "data_classification": project.data_classification,
            },
        ),
    )


@router.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def project_edit(project_id: str, request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    project = service.get_project(project_id, actor)
    service.authorization.require_project_manager(project.model_dump(), actor)
    return templates.TemplateResponse(
        request,
        "project_form.html",
        _context(request, title=f"Edit {project.name}", project=project),
    )


@router.get("/projects/{project_id}/request-production", response_class=HTMLResponse)
def production_request_new(project_id: str, request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    project = service.get_project(project_id, actor)
    service.authorization.require_project_manager(project.model_dump(), actor)
    delivery = service.database.get_delivery_config(project_id) or {}
    return templates.TemplateResponse(
        request,
        "request_form.html",
        _context(
            request,
            title="Request production",
            project=project,
            delivery=delivery,
        ),
    )


@router.get("/production-requests", response_class=HTMLResponse)
def production_requests(request: Request, status: str | None = None):
    service = _service(request)
    actor = actor_from_request(request)
    return templates.TemplateResponse(
        request,
        "requests.html",
        _context(
            request,
            title="Production requests",
            production_requests=service.list_requests(actor, status=status),
            selected_status=status or "",
        ),
    )


@router.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    service.authorization.require_approver(actor)
    return templates.TemplateResponse(
        request,
        "requests.html",
        _context(
            request,
            title="Approval queue",
            production_requests=service.list_requests(actor, status=REQUEST_READY),
            selected_status=REQUEST_READY,
            approval_queue=True,
        ),
    )


@router.get("/production-requests/{request_id}", response_class=HTMLResponse)
def production_request_detail(request_id: str, request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    detail = service.request_detail(request_id, actor)
    return templates.TemplateResponse(
        request,
        "request_detail.html",
        _context(request, title=request_id, **detail),
    )


@router.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    events = service.list_audit(actor)
    return templates.TemplateResponse(
        request,
        "audit.html",
        _context(request, title="Audit", events=events),
    )


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return templates.TemplateResponse(
        request,
        "help.html",
        _context(request, title="How governance works"),
    )
