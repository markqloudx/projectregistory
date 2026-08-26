from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients import OAuthTokenProvider
from app.models import (
    SourceValidationClaimResponse,
    SourceValidationCompletion,
    WorkerClaimResponse,
    WorkerCompletion,
)
from governance_worker.databricks import ProductionTagVerifier, load_bundle_summary
from governance_worker.preflight import PreflightResult, validate_bundle_source
from governance_worker.registry import RegistryClient


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def require_success(self, label: str) -> "CommandResult":
        if self.returncode != 0:
            detail = (self.stderr or self.stdout).strip()
            raise RuntimeError(f"{label} failed: {detail[-4000:]}")
        return self


class CommandRunner:
    def __init__(self, timeout_seconds: int | None = None):
        self.timeout_seconds = timeout_seconds or int(os.getenv("DEPLOY_COMMAND_TIMEOUT_SECONDS", "3600"))

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        check_label: str | None = None,
        stdout_path: Path | None = None,
    ) -> CommandResult:
        process = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        result = CommandResult(tuple(command), process.returncode, process.stdout, process.stderr)
        if stdout_path is not None and process.stdout:
            stdout_path.write_text(process.stdout, encoding="utf-8")
        if check_label:
            result.require_success(check_label)
        return result


class ProductionDeploymentRunner:
    def __init__(
        self,
        registry: RegistryClient | None = None,
        commands: CommandRunner | None = None,
    ):
        self.registry = registry or RegistryClient()
        self.commands = commands or CommandRunner()
        self.cli = (os.getenv("DATABRICKS_CLI_PATH") or "databricks").strip()

    def deploy(self, request_id: str, worker_run_id: str = "") -> dict[str, Any]:
        claim = self.registry.claim(request_id, worker_run_id)
        revision = ""
        run_id = worker_run_id
        run_url = ""
        tag_results = ()
        detail_parts: list[str] = []
        success = False
        try:
            with tempfile.TemporaryDirectory(prefix=f"governance-{request_id}-") as raw:
                work = Path(raw)
                repository = work / "repository"
                self._clone(claim, repository, work)
                revision = self._revision(repository)
                preflight = validate_bundle_source(
                    repository,
                    claim.request,
                    claim.project,
                    expected_dev_host=claim.dev_workspace_host,
                    expected_prod_host=claim.prod_workspace_host,
                    expected_dev_catalog=claim.dev_catalog,
                    expected_prod_catalog=claim.prod_catalog,
                    allowed_prod_workspace_roots=claim.allowed_prod_workspace_roots,
                )
                detail_parts.append(preflight.summary())
                if not preflight.passed:
                    raise RuntimeError(preflight.summary())
                bundle_root = (repository / claim.request.bundle_path).resolve()
                environment = self._bundle_environment(claim)
                self._bundle_validate(bundle_root, claim, environment)
                self._bundle_deploy(bundle_root, claim, environment)
                if claim.request.run_resource_key:
                    run = self._bundle_run(bundle_root, claim, environment)
                    run_id = self._extract_run_id(run.stdout) or run_id
                    run_url = self._extract_url(run.stdout)
                summary_file = work / "bundle-summary.json"
                self._bundle_summary(bundle_root, claim, environment, summary_file)
                resources = load_bundle_summary(summary_file)
                verifier = ProductionTagVerifier(
                    claim.prod_workspace_host,
                    token_provider=self._prod_tokens(),
                    bundle_resources=resources,
                )
                tag_results = verifier.verify(
                    claim.project,
                    claim.request.asset_manifest,
                    claim.required_tags,
                    dev_catalog=claim.dev_catalog,
                    prod_catalog=claim.prod_catalog,
                )
                success = bool(tag_results) and all(
                    item.validation_result == "PASS" for item in tag_results
                )
                detail_parts.append(
                    f"Production worker collected {len(tag_results)} mandatory tag checks."
                )
                if not success:
                    detail_parts.append("One or more production tag checks failed.")
        except Exception as exc:
            detail_parts.append(str(exc))
            success = False
        completion = WorkerCompletion(
            claim_id=claim.claim_id,
            success=success,
            resolved_git_revision=revision,
            deployment_run_id=run_id,
            deployment_run_url=run_url,
            detail=" ".join(part for part in detail_parts if part),
            tag_results=tag_results,
        )
        return self.registry.complete(request_id, completion)

    def _clone(self, claim: WorkerClaimResponse, destination: Path, work: Path) -> None:
        if shutil.which("git") is None:
            raise RuntimeError("git is not installed in the production worker environment.")
        env: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}
        token = (os.getenv("GIT_TOKEN") or "").strip()
        username = (os.getenv("GIT_USERNAME") or "x-access-token").strip()
        if token:
            askpass = work / "git-askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' \"$GIT_USERNAME\" ;;\n"
                "  *) printf '%s\\n' \"$GIT_TOKEN\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env.update({"GIT_ASKPASS": str(askpass), "GIT_USERNAME": username, "GIT_TOKEN": token})
        self.commands.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                claim.request.git_branch,
                claim.request.repository_uri,
                str(destination),
            ],
            cwd=work,
            env=env,
            check_label="Git clone",
        )

    def _revision(self, repository: Path) -> str:
        result = self.commands.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check_label="Git revision lookup"
        )
        revision = result.stdout.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", revision):
            raise RuntimeError("Git returned an invalid revision.")
        return revision

    def _bundle_environment(self, claim: WorkerClaimResponse) -> dict[str, str]:
        env = {
            "DATABRICKS_HOST": claim.prod_workspace_host,
            "DATABRICKS_AUTH_TYPE": os.getenv("PROD_DATABRICKS_AUTH_TYPE", "oauth-m2m"),
            "BUNDLE_VAR_project_id": claim.project.project_id,
            "BUNDLE_VAR_environment_name": "prod",
            "BUNDLE_VAR_catalog_name": claim.prod_catalog,
            "BUNDLE_VAR_data_classification": claim.project.data_classification,
        }
        client_id = os.getenv("PROD_DATABRICKS_CLIENT_ID") or os.getenv("DATABRICKS_CLIENT_ID")
        client_secret = os.getenv("PROD_DATABRICKS_CLIENT_SECRET") or os.getenv(
            "DATABRICKS_CLIENT_SECRET"
        )
        if client_id:
            env["DATABRICKS_CLIENT_ID"] = client_id
        if client_secret:
            env["DATABRICKS_CLIENT_SECRET"] = client_secret
        return env

    def _prod_tokens(self) -> OAuthTokenProvider:
        return OAuthTokenProvider(
            client_id=os.getenv("PROD_DATABRICKS_CLIENT_ID") or os.getenv("DATABRICKS_CLIENT_ID"),
            client_secret=os.getenv("PROD_DATABRICKS_CLIENT_SECRET")
            or os.getenv("DATABRICKS_CLIENT_SECRET"),
        )

    def _bundle_validate(
        self, root: Path, claim: WorkerClaimResponse, env: dict[str, str]
    ) -> None:
        self.commands.run(
            [self.cli, "bundle", "validate", "-t", claim.request.prod_target],
            cwd=root,
            env=env,
            check_label="Production Bundle validation",
        )

    def _bundle_deploy(self, root: Path, claim: WorkerClaimResponse, env: dict[str, str]) -> None:
        self.commands.run(
            [self.cli, "bundle", "deploy", "-t", claim.request.prod_target],
            cwd=root,
            env=env,
            check_label="Production Bundle deployment",
        )

    def _bundle_run(
        self, root: Path, claim: WorkerClaimResponse, env: dict[str, str]
    ) -> CommandResult:
        return self.commands.run(
            [
                self.cli,
                "bundle",
                "run",
                "-t",
                claim.request.prod_target,
                claim.request.run_resource_key,
            ],
            cwd=root,
            env=env,
            check_label="Production Bundle run",
        )

    def _bundle_summary(
        self,
        root: Path,
        claim: WorkerClaimResponse,
        env: dict[str, str],
        output: Path,
    ) -> None:
        result = self.commands.run(
            [
                self.cli,
                "bundle",
                "summary",
                "-t",
                claim.request.prod_target,
                "--output",
                "json",
            ],
            cwd=root,
            env=env,
        )
        if result.returncode == 0:
            output.write_text(result.stdout or "{}", encoding="utf-8")
        else:
            output.write_text("{}", encoding="utf-8")

    @staticmethod
    def _extract_run_id(value: str) -> str:
        match = re.search(r"(?:run[_ -]?id|runs/)[=: /]+(\d+)", value, re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_url(value: str) -> str:
        match = re.search(r"https://[^\s]+", value)
        return match.group(0).rstrip(".,)") if match else ""


class SourceValidationRunner(ProductionDeploymentRunner):
    """Validate the latest configured Git branch without creating or renaming dev assets."""

    def validate(self, request_id: str, worker_run_id: str = "") -> dict[str, Any]:
        claim = self.registry.claim_source_validation(request_id, worker_run_id)
        revision = ""
        detail_parts: list[str] = []
        success = False
        try:
            with tempfile.TemporaryDirectory(prefix=f"governance-source-{request_id}-") as raw:
                work = Path(raw)
                repository = work / "repository"
                self._clone_source(claim, repository, work)
                revision = self._revision(repository)
                preflight = validate_bundle_source(
                    repository,
                    claim.request,
                    claim.project,
                    expected_dev_host=claim.dev_workspace_host,
                    expected_prod_host=claim.prod_workspace_host,
                    expected_dev_catalog=claim.dev_catalog,
                    expected_prod_catalog=claim.prod_catalog,
                    allowed_prod_workspace_roots=claim.allowed_prod_workspace_roots,
                )
                detail_parts.append(preflight.summary())
                if not preflight.passed:
                    raise RuntimeError(preflight.summary())
                bundle_root = (repository / claim.request.bundle_path).resolve()
                environment = self._dev_bundle_environment(claim)
                self.commands.run(
                    [self.cli, "bundle", "validate", "-t", claim.request.dev_target],
                    cwd=bundle_root,
                    env=environment,
                    check_label="Development Bundle validation",
                )
                detail_parts.append(
                    "Current Git branch passed static governance preflight and Databricks "
                    "Bundle validation for the development target."
                )
                success = True
        except Exception as exc:
            detail_parts.append(str(exc))
            success = False
        completion = SourceValidationCompletion(
            claim_id=claim.claim_id,
            success=success,
            resolved_git_revision=revision,
            detail=" ".join(part for part in detail_parts if part),
        )
        return self.registry.complete_source_validation(request_id, completion)

    def validate_next(self, worker_run_id: str = "") -> dict[str, Any]:
        request_id = self.registry.next_source_validation()
        if not request_id:
            return {"status": "NO_SOURCE_VALIDATION_QUEUED"}
        return self.validate(request_id, worker_run_id)

    def _clone_source(
        self, claim: SourceValidationClaimResponse, destination: Path, work: Path
    ) -> None:
        # The claim types share the repository fields required by the common Git helper.
        self._clone(claim, destination, work)  # type: ignore[arg-type]

    def _dev_bundle_environment(
        self, claim: SourceValidationClaimResponse
    ) -> dict[str, str]:
        env = {
            "DATABRICKS_HOST": claim.dev_workspace_host,
            "DATABRICKS_AUTH_TYPE": os.getenv("DEV_DATABRICKS_AUTH_TYPE", "oauth-m2m"),
            "BUNDLE_VAR_project_id": claim.project.project_id,
            "BUNDLE_VAR_environment_name": "dev",
            "BUNDLE_VAR_catalog_name": claim.dev_catalog,
            "BUNDLE_VAR_data_classification": claim.project.data_classification,
        }
        client_id = os.getenv("DEV_DATABRICKS_CLIENT_ID") or os.getenv("DATABRICKS_CLIENT_ID")
        client_secret = os.getenv("DEV_DATABRICKS_CLIENT_SECRET") or os.getenv(
            "DATABRICKS_CLIENT_SECRET"
        )
        if client_id:
            env["DATABRICKS_CLIENT_ID"] = client_id
        if client_secret:
            env["DATABRICKS_CLIENT_SECRET"] = client_secret
        return env


def deploy_next_queued(
    *,
    deployer_profile: str = "",
    worker_run_id: str = "",
    registry: RegistryClient | None = None,
    commands: CommandRunner | None = None,
) -> dict[str, Any]:
    client = registry or RegistryClient()
    request_id = client.next_production_request(deployer_profile)
    if not request_id:
        return {"status": "NO_PRODUCTION_REQUEST_QUEUED"}
    return ProductionDeploymentRunner(client, commands).deploy(request_id, worker_run_id)
