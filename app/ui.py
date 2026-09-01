from __future__ import annotations

from datetime import date, datetime
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
    
    # ========== WORKSPACE: ADDED ==========
    available_workspaces = service.get_available_workspaces()
    
    return {
        "request": request,
        "actor": actor,
        "version": "4.0.0",
        "settings": service.settings,
        "is_admin": auth.is_admin(actor),
        "is_approver": auth.is_approver(actor),
        "is_auditor": auth.is_auditor(actor),
        "available_workspaces": available_workspaces,  # ========== WORKSPACE: ADDED ==========
        **values,
    }


def _serialize_project(project: Any) -> dict[str, Any]:
    """Convert a project record to a JSON-serializable dict."""
    # Get the data as dict
    if hasattr(project, 'model_dump'):
        data = project.model_dump()
    else:
        data = dict(project)
    
    # Convert datetime and date objects to ISO format strings
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
        elif isinstance(value, date):
            data[key] = value.isoformat()
        elif isinstance(value, dict):
            # Handle nested dicts if any
            for k, v in value.items():
                if isinstance(v, datetime):
                    value[k] = v.isoformat()
                elif isinstance(v, date):
                    value[k] = v.isoformat()
    
    return data


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    projects = service.list_projects(actor)
    requests = service.list_requests(actor)
    
    # Convert projects to serializable dicts
    projects_data = [_serialize_project(p) for p in projects[:5]]
    
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            title="Governance home",
            summary=service.dashboard(),
            projects=projects_data,
            production_requests=requests[:8],
        ),
    )


@router.get("/projects", response_class=HTMLResponse)
def projects(request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    
    # Convert projects to dict with datetime/date serialization
    project_records = service.list_projects(actor)
    projects_data = [_serialize_project(project) for project in project_records]
    
    return templates.TemplateResponse(
        request,
        "projects.html",
        _context(request, title="Projects", projects=projects_data),
    )


@router.get("/projects/new", response_class=HTMLResponse)
def project_new(request: Request):
    service = _service(request)
    # Set default terms values for new project
    project_data = {
        "terms_accepted": False,
        "terms_up_to_date": False,
        "terms_version": service.settings.current_terms_version,
        "workspace": "",  # ========== WORKSPACE: ADDED ==========
        "from_workspace": "",  # ========== WORKSPACE: ADDED ==========
        "to_workspace": "",  # ========== WORKSPACE: ADDED ==========
    }
    return templates.TemplateResponse(
        request,
        "project_form.html",
        _context(request, title="Register project", project=project_data),
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: str, request: Request):
    service = _service(request)
    actor = actor_from_request(request)
    project = service.get_project(project_id, actor)
    production_requests = service.list_requests(actor, project_id=project_id)
    
    # Convert project to serializable dict
    project_data = _serialize_project(project)
    
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        _context(
            request,
            title=project.name,
            project=project_data,
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
    
    # Get project with terms status
    project_data = service.get_project_with_terms_status(project_id, actor)
    
    # Convert datetime/date objects to strings
    project_data = _serialize_project(project_data)
    
    # Check authorization
    service.authorization.require_project_manager(project_data, actor)
    
    return templates.TemplateResponse(
        request,
        "project_form.html",
        _context(
            request, 
            title=f"Edit {project_data.get('name', project_id)}", 
            project=project_data
        ),
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
    
    # Convert request and project to serializable dicts
    if 'request' in detail:
        detail['request'] = _serialize_project(detail['request'])
    if 'project' in detail:
        detail['project'] = _serialize_project(detail['project'])
    
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