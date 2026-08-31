from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError

from app.api import router as api_router
from app.clients import DeploymentDispatcher, OAuthTokenProvider
from app.db import RegistryDatabase, create_database
from app.governance import GovernanceError
from app.scanner import AssetScanner, DatabricksAssetScanner, DatabricksTagManager, StaticAssetScanner
from app.service import RegistryService
from app.settings import Settings, load_settings
from app.ui import router as ui_router, templates

logger = logging.getLogger(__name__)


# ============================================================================
# HELPER FUNCTIONS FOR ERROR SERIALIZATION
# ============================================================================

def _serialize_error_detail(detail: Any) -> Any:
    """
    Recursively convert error details to JSON-serializable format.
    Handles ValueError objects, Pydantic errors, and other non-serializable types.
    """
    if detail is None:
        return None
    if isinstance(detail, (str, int, float, bool)):
        return detail
    if isinstance(detail, dict):
        return {k: _serialize_error_detail(v) for k, v in detail.items()}
    if isinstance(detail, (list, tuple)):
        return [_serialize_error_detail(item) for item in detail]
    # For any other object (like ValueError), convert to string
    return str(detail)


def _format_validation_errors(errors: list) -> list:
    """Format validation errors for better readability."""
    formatted = []
    for error in errors:
        # Get the field name from location
        loc = error.get('loc', [])
        field = '.'.join(str(item) for item in loc[1:] if item != 'body') if len(loc) > 1 else 'unknown'
        
        # Get the error message
        msg = error.get('msg', 'Invalid value')
        
        # Get additional context
        ctx = error.get('ctx', {})
        if ctx and isinstance(ctx, dict):
            # Extract meaningful error from ctx
            if 'error' in ctx:
                error_msg = str(ctx['error'])
            else:
                error_msg = msg
        else:
            error_msg = msg
        
        # Format the error nicely
        formatted.append({
            'field': field,
            'message': error_msg,
            'type': error.get('type', 'unknown'),
            'input': error.get('input', ''),
            'ctx': {k: str(v) for k, v in ctx.items()} if ctx else {}
        })
    
    return formatted


def _error_response(
    request: Request, status_code: int, message: str, detail: Any | None = None
):
    """Create a JSON error response with proper serialization."""
    # Serialize everything to JSON-safe format
    serialized_detail = _serialize_error_detail(detail)
    
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or "application/json" in accept:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": message,
                "detail": serialized_detail,
                "status": status_code
            },
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"request": request, "title": "Request failed", "message": message},
        status_code=status_code,
    )


# ============================================================================
# CREATE APP
# ============================================================================

def create_app(
    *,
    settings: Settings | None = None,
    database: RegistryDatabase | None = None,
    scanner: AssetScanner | None = None,
    validation_dispatcher: DeploymentDispatcher | None = None,
    dispatcher: DeploymentDispatcher | None = None,
    bootstrap: bool = True,
) -> FastAPI:
    effective_settings = settings or load_settings()
    token_provider = OAuthTokenProvider()
    effective_database = database or create_database(effective_settings, token_provider)

    tag_manager = None
    if scanner is None:
        if effective_settings.backend == "memory":
            effective_scanner: AssetScanner = StaticAssetScanner()
        else:
            effective_scanner = DatabricksAssetScanner(effective_settings, token_provider)
            tag_manager = DatabricksTagManager(effective_settings, token_provider)
    else:
        effective_scanner = scanner

    effective_validation_dispatcher = validation_dispatcher
    if effective_validation_dispatcher is None and effective_settings.backend == "databricks":
        effective_validation_dispatcher = DeploymentDispatcher(
            effective_settings.dev_host, token_provider
        )

    effective_dispatcher = dispatcher
    if effective_dispatcher is None and effective_settings.backend == "databricks":
        effective_dispatcher = DeploymentDispatcher(effective_settings.prod_host, token_provider)

    service = RegistryService(
        effective_settings,
        effective_database,
        effective_scanner,
        tag_manager=tag_manager,
        validation_dispatcher=effective_validation_dispatcher,
        dispatcher=effective_dispatcher,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if bootstrap:
            service.bootstrap()
        yield

    application = FastAPI(
        title="Project Registry & Governance",
        version="4.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    application.state.service = service
    application.state.settings = effective_settings
    application.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    application.include_router(api_router)
    application.include_router(ui_router)

    # ============================================================================
    # EXCEPTION HANDLERS
    # ============================================================================

    @application.exception_handler(GovernanceError)
    async def governance_error(request: Request, exc: GovernanceError):
        return _error_response(request, exc.status_code, str(exc))

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        # Format and serialize errors properly
        formatted_errors = _format_validation_errors(exc.errors())
        serialized_errors = _serialize_error_detail(formatted_errors)
        return _error_response(request, 422, "Request validation failed.", serialized_errors)

    @application.exception_handler(PydanticValidationError)
    async def pydantic_error(request: Request, exc: PydanticValidationError):
        # Format and serialize errors properly
        formatted_errors = _format_validation_errors(exc.errors())
        serialized_errors = _serialize_error_detail(formatted_errors)
        return _error_response(request, 422, "Request validation failed.", serialized_errors)

    @application.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("Unhandled registry error", exc_info=exc)
        return _error_response(request, 500, "An unexpected application error occurred.")

    return application


app = create_app()