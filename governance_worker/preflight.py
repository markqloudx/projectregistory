from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.models import ProjectRecord, ProductionRequestRecord


@dataclass
class PreflightResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resource_counts: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = []
        if self.errors:
            parts.append("Errors: " + " | ".join(self.errors))
        if self.warnings:
            parts.append("Warnings: " + " | ".join(self.warnings))
        if self.resource_counts:
            parts.append(
                "Resources: "
                + ", ".join(f"{name}={count}" for name, count in sorted(self.resource_counts.items()))
            )
        return " ".join(parts) or "Bundle source preflight passed."


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def _included_yaml(bundle_root: Path, root_payload: dict[str, Any]) -> Iterable[tuple[Path, dict[str, Any]]]:
    yield bundle_root / "databricks.yml", root_payload
    seen: set[Path] = set()
    for pattern in root_payload.get("include") or ():
        for raw in glob.glob(str(bundle_root / str(pattern)), recursive=True):
            path = Path(raw).resolve()
            if path in seen or path.name == "databricks.yml" or not path.is_file():
                continue
            seen.add(path)
            yield path, _load_yaml(path)


def _target_catalog(target: dict[str, Any]) -> str:
    variables = target.get("variables") or {}
    value = variables.get("catalog_name")
    if isinstance(value, dict):
        value = value.get("default")
    return str(value or "").strip()


def _target_host(target: dict[str, Any]) -> str:
    return str((target.get("workspace") or {}).get("host") or "").strip().rstrip("/")


def _tag_value_is_valid(value: Any, *, key: str, project: ProjectRecord) -> bool:
    normalized = str(value or "").strip()
    accepted = {
        "project_tag": {"${var.project_id}", project.project_id},
        "environment": {"${var.environment_name}", "${bundle.target}"},
        "data_classification": {
            "${var.data_classification}",
            project.data_classification,
        },
    }
    return normalized in accepted[key]


def validate_bundle_source(
    repository_root: Path,
    request: ProductionRequestRecord,
    project: ProjectRecord,
    *,
    expected_dev_host: str,
    expected_prod_host: str,
    expected_dev_catalog: str,
    expected_prod_catalog: str,
    allowed_prod_workspace_roots: tuple[str, ...] = (),
) -> PreflightResult:
    result = PreflightResult()
    bundle_root = (repository_root / request.bundle_path).resolve()
    try:
        bundle_root.relative_to(repository_root.resolve())
    except ValueError:
        result.errors.append("Bundle path escapes the checked-out repository.")
        return result
    root_file = bundle_root / "databricks.yml"
    if not root_file.exists():
        result.errors.append(f"Bundle file was not found at {root_file.relative_to(repository_root)}.")
        return result
    try:
        root = _load_yaml(root_file)
    except Exception as exc:
        result.errors.append(f"Unable to parse databricks.yml: {exc}")
        return result

    variables = root.get("variables") or {}
    for required in ("project_id", "environment_name", "catalog_name", "data_classification"):
        if required not in variables:
            result.errors.append(f"Bundle variable '{required}' is required by v4 governance.")

    targets = root.get("targets") or {}
    dev_target = dict(targets.get(request.dev_target) or {})
    prod_target = dict(targets.get(request.prod_target) or {})
    if not dev_target:
        result.errors.append(f"Development Bundle target '{request.dev_target}' does not exist.")
    if not prod_target:
        result.errors.append(f"Production Bundle target '{request.prod_target}' does not exist.")
    if dev_target:
        host = _target_host(dev_target)
        if host and host != expected_dev_host.rstrip("/"):
            result.errors.append(f"Development target host must be {expected_dev_host}.")
        catalog = _target_catalog(dev_target)
        if catalog and catalog != expected_dev_catalog:
            result.errors.append(f"Development target catalog must be {expected_dev_catalog}.")
    if prod_target:
        if str(prod_target.get("mode") or "").strip().lower() != "production":
            result.errors.append(
                f"Production Bundle target '{request.prod_target}' must use mode: production."
            )
        host = _target_host(prod_target)
        if host and host != expected_prod_host.rstrip("/"):
            result.errors.append(f"Production target host must be {expected_prod_host}.")
        catalog = _target_catalog(prod_target)
        if catalog and catalog != expected_prod_catalog:
            result.errors.append(f"Production target catalog must be {expected_prod_catalog}.")
        allowed_roots = tuple(root.rstrip("/") for root in allowed_prod_workspace_roots if root)
        if allowed_roots:
            prod_root_path = str((prod_target.get("workspace") or {}).get("root_path") or "").strip()
            if not prod_root_path:
                result.errors.append(
                    "Production target workspace.root_path is required by the team deployment profile."
                )
            elif not any(
                prod_root_path == root or prod_root_path.startswith(root + "/")
                for root in allowed_roots
            ):
                result.errors.append(
                    "Production target workspace.root_path must be under one of: "
                    + ", ".join(allowed_roots)
                )

    resources: dict[str, dict[str, Any]] = {}
    try:
        documents = list(_included_yaml(bundle_root, root))
    except Exception as exc:
        result.errors.append(f"Unable to parse an included Bundle YAML file: {exc}")
        return result
    for _, document in documents:
        for kind, definitions in (document.get("resources") or {}).items():
            if not isinstance(definitions, dict):
                continue
            bucket = resources.setdefault(str(kind), {})
            bucket.update(definitions)
    result.resource_counts = {kind: len(values) for kind, values in resources.items()}

    if request.run_resource_key:
        runnable = {
            key
            for kind in ("jobs", "pipelines", "apps", "scripts")
            for key in resources.get(kind, {})
        }
        if request.run_resource_key not in runnable:
            result.errors.append(
                f"Bundle run resource '{request.run_resource_key}' was not found in the source."
            )

    for kind in ("jobs", "pipelines"):
        for key, definition in resources.get(kind, {}).items():
            tags = dict((definition or {}).get("tags") or {})
            for tag_key in ("project_tag", "environment", "data_classification"):
                if tag_key not in tags:
                    result.errors.append(f"{kind[:-1]} '{key}' is missing source tag '{tag_key}'.")
                elif not _tag_value_is_valid(tags[tag_key], key=tag_key, project=project):
                    result.errors.append(
                        f"{kind[:-1]} '{key}' has an unsupported value for '{tag_key}'."
                    )

    # Prefixes produced by Bundle development mode are intentionally accepted. Governance only
    # checks ownership tags and deployability; it does not alter Bundle naming behavior.
    source_files = [path for path in bundle_root.rglob("*") if path.is_file()]
    hardcoded_dev_catalog = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(expected_dev_catalog)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    for path in source_files:
        if path.suffix.lower() not in {".sql", ".json", ".py", ".yml", ".yaml", ".ipynb"}:
            continue
        if any(
            part in {".git", ".databricks", "__pycache__", ".pytest_cache", "tests", "docs", "dist"}
            for part in path.parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "/Workspace/Users/" in text and "/.bundle/" in text:
            result.errors.append(
                f"{path.relative_to(repository_root)} contains an identity-specific Bundle path."
            )
        if path.suffix.lower() in {".json", ".sql", ".py", ".ipynb"} and hardcoded_dev_catalog.search(text):
            result.errors.append(
                f"{path.relative_to(repository_root)} hardcodes {expected_dev_catalog} in "
                "executable source; use a Bundle variable or task parameter so production "
                f"resolves to {expected_prod_catalog}."
            )
    return result
