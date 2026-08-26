from __future__ import annotations

import json
import os

import typer

from governance_worker.runner import (
    ProductionDeploymentRunner,
    SourceValidationRunner,
    deploy_next_queued,
)

app = typer.Typer(
    name="governance-worker",
    help="Protected validation and production workers for Project Registry & Governance v4.0.0.",
    no_args_is_help=True,
)


def _echo_result(result: dict, *, success_statuses: set[str]) -> None:
    typer.echo(json.dumps(result, indent=2, default=str))
    status = str(result.get("request_status") or result.get("status") or "")
    if status not in success_statuses:
        raise typer.Exit(code=1)


@app.command("validate-request")
def validate_request(
    request_id: str = typer.Option(..., "--request-id", help="Production request ID."),
    worker_run_id: str = typer.Option(
        "", "--worker-run-id", help="Optional validator run ID for audit correlation."
    ),
) -> None:
    """Validate the request's current Git branch and development Bundle target."""

    result = SourceValidationRunner().validate(request_id, worker_run_id)
    _echo_result(result, success_statuses={"READY_FOR_APPROVAL"})


@app.command("validate-next")
def validate_next(
    worker_run_id: str = typer.Option(
        "", "--worker-run-id", help="Optional validator run ID for audit correlation."
    ),
) -> None:
    """Validate the oldest queued request, or exit successfully when the queue is empty."""

    result = SourceValidationRunner().validate_next(worker_run_id)
    _echo_result(
        result,
        success_statuses={"READY_FOR_APPROVAL", "NO_SOURCE_VALIDATION_QUEUED"},
    )


@app.command("deploy-request")
def deploy_request(
    request_id: str = typer.Option(..., "--request-id", help="Approved production request ID."),
    worker_run_id: str = typer.Option(
        "", "--worker-run-id", help="Optional dispatcher run ID for audit correlation."
    ),
) -> None:
    """Claim, preflight, deploy, run, tag-check, and complete one approved request."""

    result = ProductionDeploymentRunner().deploy(request_id, worker_run_id)
    _echo_result(result, success_statuses={"DEPLOYED"})


@app.command("deploy-next")
def deploy_next(
    deployer_profile: str = typer.Option(
        "", "--deployer-profile", help="Optional team deployment profile queue."
    ),
    worker_run_id: str = typer.Option(
        "", "--worker-run-id", help="Optional dispatcher run ID for audit correlation."
    ),
) -> None:
    """Deploy the oldest approved request, or exit successfully when the queue is empty."""

    result = deploy_next_queued(
        deployer_profile=deployer_profile,
        worker_run_id=worker_run_id,
    )
    _echo_result(result, success_statuses={"DEPLOYED", "NO_PRODUCTION_REQUEST_QUEUED"})


@app.command("show-environment")
def show_environment() -> None:
    """Show non-secret worker configuration for troubleshooting."""

    values = {
        key: os.getenv(key, "")
        for key in (
            "REGISTRY_APP_URL",
            "REGISTRY_DATABRICKS_HOST",
            "DATABRICKS_CLI_PATH",
            "DEV_DATABRICKS_AUTH_TYPE",
            "DEV_DATABRICKS_CLIENT_ID",
            "PROD_DATABRICKS_AUTH_TYPE",
            "PROD_DATABRICKS_CLIENT_ID",
            "DEPLOYER_PROFILE",
        )
    }
    values["GIT_TOKEN_CONFIGURED"] = bool(os.getenv("GIT_TOKEN"))
    values["REGISTRY_SECRET_CONFIGURED"] = bool(
        os.getenv("REGISTRY_CLIENT_SECRET") or os.getenv("DATABRICKS_CLIENT_SECRET")
    )
    values["DEV_SECRET_CONFIGURED"] = bool(
        os.getenv("DEV_DATABRICKS_CLIENT_SECRET") or os.getenv("DATABRICKS_CLIENT_SECRET")
    )
    values["PROD_SECRET_CONFIGURED"] = bool(
        os.getenv("PROD_DATABRICKS_CLIENT_SECRET") or os.getenv("DATABRICKS_CLIENT_SECRET")
    )
    typer.echo(json.dumps(values, indent=2))


if __name__ == "__main__":
    app()
