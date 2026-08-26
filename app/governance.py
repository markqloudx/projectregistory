from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROJECT_ACTIVE = "ACTIVE"
PROJECT_SUSPENDED = "SUSPENDED"
PROJECT_ARCHIVED = "ARCHIVED"
PROJECT_STATUSES = {PROJECT_ACTIVE, PROJECT_SUSPENDED, PROJECT_ARCHIVED}

REQUEST_ACTION_REQUIRED = "ACTION_REQUIRED"
REQUEST_VALIDATING = "VALIDATING"
REQUEST_READY = "READY_FOR_APPROVAL"
REQUEST_REJECTED = "REJECTED"
REQUEST_QUEUED = "APPROVED_DEPLOY_QUEUED"
REQUEST_DEPLOYING = "DEPLOYING"
REQUEST_DEPLOYED = "DEPLOYED"
REQUEST_FAILED = "DEPLOY_FAILED"
REQUEST_STATUSES = {
    REQUEST_ACTION_REQUIRED,
    REQUEST_VALIDATING,
    REQUEST_READY,
    REQUEST_REJECTED,
    REQUEST_QUEUED,
    REQUEST_DEPLOYING,
    REQUEST_DEPLOYED,
    REQUEST_FAILED,
}

MANDATORY_TAG_KEYS = ("project_tag", "environment", "data_classification")
ASSET_TYPES = {
    "job",
    "pipeline",
    "dashboard",
    "app",
    "notebook",
    "schema",
    "table",
    "view",
    "volume",
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESOURCE_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEFAULT_BUNDLE_PREFIX = re.compile(r"^\[[^\]]+\]\s*")


class GovernanceError(RuntimeError):
    status_code = 400


class ValidationError(GovernanceError):
    status_code = 422


class AuthorizationError(GovernanceError):
    status_code = 403


class NotFoundError(GovernanceError):
    status_code = 404


class ConflictError(GovernanceError):
    status_code = 409


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def generate_project_id() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "PRJ-" + "".join(secrets.choice(alphabet) for _ in range(10))


def generate_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return f"PRDREQ-{stamp}-{secrets.token_hex(4).upper()}"


def generate_evidence_id() -> str:
    return "EVD-" + secrets.token_hex(8).upper()


def generate_audit_id() -> str:
    return "AUD-" + secrets.token_hex(10).upper()


def generate_claim_id() -> str:
    return "CLM-" + secrets.token_hex(12).upper()


def validate_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValidationError(
            f"{label} must start with a letter or underscore and contain only letters, digits, "
            "and underscores."
        )
    return normalized


def validate_resource_key(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or not _RESOURCE_KEY.fullmatch(normalized):
        raise ValidationError(f"{label} contains unsupported characters.")
    return normalized



def normalized_asset_name(value: str) -> str:
    """Normalize only the display prefix Databricks Bundles add in development mode.

    V4 intentionally keeps the default Bundle prefixing behavior. This helper lets validation
    match the same logical resource between development and production without renaming it.
    """

    return _DEFAULT_BUNDLE_PREFIX.sub("", value.strip()).casefold()


def normalize_repository_uri(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValidationError("Repository URI is required.")
    if raw.startswith("git@"):
        match = re.fullmatch(r"git@([^:]+):(.+?)(?:\.git)?", raw)
        if not match:
            raise ValidationError("Unsupported SSH repository URI.")
        host, path = match.groups()
        raw = f"https://{host}/{path}"
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("Repository URI must be an HTTP(S) Git URL.")
    if parsed.username or parsed.password:
        raise ValidationError("Repository URI cannot contain embedded credentials.")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if len([part for part in path.split("/") if part]) < 2:
        raise ValidationError("Repository URI must identify an organization and repository.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def safe_bundle_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/").strip("/") or "."
    if any(part in {"", ".", ".."} for part in normalized.split("/")) and normalized != ".":
        raise ValidationError("Bundle path cannot contain empty, current, or parent path segments.")
    return normalized


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sql_identifier(value: str) -> str:
    """Return a backtick-quoted Databricks SQL identifier."""
    return "`" + value.replace("`", "``") + "`"


def expected_tags(project: dict[str, Any], environment: str) -> dict[str, str]:
    if environment not in {"dev", "prod"}:
        raise ValidationError("Environment must be dev or prod.")
    return {
        "project_tag": str(project["project_id"]),
        "environment": environment,
        "data_classification": str(project["data_classification"]),
    }
