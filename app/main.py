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

    @application.exception_handler(GovernanceError)
    async def governance_error(request: Request, exc: GovernanceError):
        return _error_response(request, exc.status_code, str(exc))

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        return _error_response(request, 422, "Request validation failed.", exc.errors())

    @application.exception_handler(PydanticValidationError)
    async def pydantic_error(request: Request, exc: PydanticValidationError):
        return _error_response(request, 422, "Request validation failed.", exc.errors())

    @application.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("Unhandled registry error", exc_info=exc)
        return _error_response(request, 500, "An unexpected application error occurred.")

    return application


def _error_response(
    request: Request, status_code: int, message: str, detail: Any | None = None
):
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or "application/json" in accept:
        return JSONResponse(
            status_code=status_code,
            content={"error": message, "detail": detail},
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"request": request, "title": "Request failed", "message": message},
        status_code=status_code,
    )


app = create_app()
