from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from fastapi import Request

from app.governance import AuthorizationError
from app.settings import Settings


@dataclass(frozen=True)
class Actor:
    subject: str
    display_name: str = ""

    @property
    def normalized(self) -> str:
        return self.subject.strip().lower()


def actor_from_headers(
    headers: Mapping[str, str], *, allow_local_headers: bool = False
) -> Actor:
    candidates = [
        "x-forwarded-email",
        "x-forwarded-preferred-username",
        "x-databricks-user",
        "x-forwarded-user",
    ]
    if allow_local_headers:
        candidates.append("x-local-user")
    for name in candidates:
        value = headers.get(name)
        if value and value.strip():
            return Actor(value.strip())
    # Service-principal calls can be represented by this header in local tests or by the
    # authenticated identity header injected by Databricks Apps.
    sp = headers.get("x-databricks-service-principal")
    if allow_local_headers and not sp:
        sp = headers.get("x-service-principal")
    if sp and sp.strip():
        return Actor(sp.strip())
    raise AuthorizationError("The authenticated Databricks identity was not supplied.")

#comment whan local
# def actor_from_request(request: Request) -> Actor:
#     settings = getattr(request.app.state, "settings", None)
#     allow_local = bool(getattr(settings, "trust_local_identity_headers", False))
#     return actor_from_headers(request.headers, allow_local_headers=allow_local)
#comment whan deploy
def actor_from_request(request: Request) -> Actor:
    # EMERGENCY LOCAL BYPASS
    return Actor(subject="local-demo-presenter@siilabs.com", display_name="Local Presenter")
class Authorization:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _contains(values: tuple[str, ...], actor: Actor) -> bool:
        return actor.normalized in values

    def is_admin(self, actor: Actor) -> bool:
        # EMERGENCY OVERRIDE: Make local presenter an admin for the demo
        if actor.normalized == "local-demo-presenter@siilabs.com":
            return True
        return self._contains(self.settings.admin_principals, actor)

    def is_approver(self, actor: Actor) -> bool:
        return self.is_admin(actor) or self._contains(self.settings.approver_principals, actor)

    def is_auditor(self, actor: Actor) -> bool:
        return (
            self.is_admin(actor)
            or self.is_approver(actor)
            or self._contains(self.settings.auditor_principals, actor)
        )

    def is_development_validator(self, actor: Actor) -> bool:
        return self._contains(self.settings.development_validator_principals, actor)

    def is_production_deployer(self, actor: Actor) -> bool:
        return self._contains(self.settings.production_deployer_principals, actor)

    def is_project_manager(self, project: Mapping[str, object], actor: Actor) -> bool:
        if self.is_admin(actor):
            return True
        owners = {
            str(project.get("technical_owner_email") or "").lower(),
            str(project.get("business_owner_email") or "").lower(),
            str(project.get("created_by") or "").lower(),
        }
        return actor.normalized in owners

    def require_admin(self, actor: Actor) -> None:
        if not self.is_admin(actor):
            raise AuthorizationError("Administrator permission is required.")

    def require_approver(self, actor: Actor) -> None:
        if not self.is_approver(actor):
            raise AuthorizationError("Approver permission is required.")

    def require_auditor(self, actor: Actor) -> None:
        if not self.is_auditor(actor):
            raise AuthorizationError("Auditor permission is required.")

    def require_project_manager(self, project: Mapping[str, object], actor: Actor) -> None:
        if not self.is_project_manager(project, actor):
            raise AuthorizationError("You are not a maintainer of this project.")

    def require_development_validator(self, actor: Actor) -> None:
        if not self.is_development_validator(actor):
            raise AuthorizationError("Development validator identity is required.")

    def require_production_deployer(self, actor: Actor) -> None:
        if not self.is_production_deployer(actor):
            raise AuthorizationError("Production deployer identity is required.")
