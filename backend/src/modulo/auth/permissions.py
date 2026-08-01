"""Centralized permission registry for REST + MCP authorization (ADR 017).

One registry and one comparison function, REST and MCP as thin adapters.
The registry is the single source of truth; MCP tool requirements reference
it rather than duplicating roles.

``PERMISSIONS`` maps ``"resource.operation"`` keys to the minimum org role
required to perform them. Roles resolve through ``ORG_ROLE_HIERARCHY`` from
``modulo.auth.team_rbac`` (viewer < runner < operator < admin).
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

# Per-request, tenancy-bounded authorization kill switch (ADR 017 DECISION 3).
# ``None`` means enforcement is ON (fail-closed default); ``False`` fail-opens
# the generic org-role gate for the current request. Set by the REST
# ``require_permission`` variants and the MCP auth middleware per-request.
_authz_enforce_ctx: ContextVar[bool | None] = ContextVar("authz_enforce", default=None)


def set_authz_enforce(value: bool) -> Token[bool | None]:
    """Set the per-request authz-enforce flag; return a token for reset.

    ``True``/unset means enforcement is ON. ``False`` fail-opens the org-role
    gate. Callers reset the ContextVar with ``reset_authz_enforce(token)`` so
    no stale value leaks across requests.
    """
    return _authz_enforce_ctx.set(bool(value))


def reset_authz_enforce(token: Token[bool | None]) -> None:
    """Restore the authz-enforce ContextVar to its pre-request value."""
    _authz_enforce_ctx.reset(token)


PERMISSIONS: dict[str, str] = {
    # pipelines
    "pipeline.create": "operator",
    "pipeline.update": "operator",
    "pipeline.delete": "operator",
    "pipeline.graph.update": "operator",
    "pipeline.bind_connector": "operator",
    "pipeline.graph.read": "viewer",
    "pipeline.list": "viewer",
    # runs
    "run.trigger": "runner",
    "run.cancel": "runner",
    "run.list": "runner",
    "run.output": "runner",
    "run.evals": "runner",
    "run.status": "viewer",
    # connectors
    "connector.create": "operator",
    "connector.update": "operator",
    "connector.delete": "operator",
    "connector.list": "viewer",
    # secrets
    "secret.manage": "operator",
    # triggers
    "trigger.create": "operator",
    "trigger.update": "operator",
    "trigger.delete": "operator",
    "trigger.list": "runner",
    "trigger.events.list": "runner",
    "trigger.cleanup": "runner",
    # api keys
    "api_key.create": "runner",
    "api_key.update": "runner",
    "api_key.revoke": "runner",
    # metrics
    "metrics.ingest": "viewer",
    # oauth
    "oauth.client.create": "operator",
    "oauth.client.list": "operator",
    "oauth.client.update": "operator",
    "oauth.client.delete": "operator",
    # org
    "org.email.view": "operator",
    "org.email.manage": "admin",
    "org.license.view": "operator",
    "org.delete": "admin",
    # agents
    "agent.create": "operator",
    "agent.update": "operator",
    "agent.delete": "operator",
    "agent.list": "viewer",
    # schemas
    "schema.create": "operator",
    "schema.update": "operator",
    "schema.delete": "operator",
    "schema.infer": "operator",
    "schema.validate": "viewer",
    "schema.list": "viewer",
    # model backends
    "model_backend.create": "operator",
    "model_backend.update": "operator",
    "model_backend.delete": "operator",
    "model_backend.list": "viewer",
    # hitl
    "hitl.claim": "runner",
    "hitl.approve": "operator",
    "hitl.reject": "operator",
    "hitl.deliver_manual": "operator",
    "hitl.review": "operator",
    "hitl.list": "runner",
    # library
    "library.copy": "runner",
    "library.search": "viewer",
    # evals
    "eval.list": "runner",
    "eval.run": "operator",
    "eval.definition.create": "operator",
    # housekeeping
    "housekeeping.list": "runner",
    "housekeeping.perform": "operator",
    # teams
    "team.create": "admin",
    "team.update": "admin",
    "team.delete": "admin",
    "team.manage": "admin",
    "team.list": "viewer",
    # audit
    "audit.list": "viewer",
    "audit.manage": "admin",
    # integrations and read-only retrieval
    "integration.status": "viewer",
    "org.config": "viewer",
    "features.list": "viewer",
    "docs.search": "viewer",
    "resource.read_only": "viewer",
}


class PermissionConfigurationError(Exception):
    """Raised when the permission registry is misconfigured (unknown key or role)."""


class PermissionDenied(Exception):  # noqa: N818 — name mandated by ADR 017 exception contract
    """Raised when a principal lacks the minimum org role for a permission.

    Attributes:
        permission: the ``resource.operation`` key (or subject) that was checked.
        required_role: the minimum org role required.
        actual_role: the role the principal actually holds (``None`` if absent).
    """

    def __init__(
        self,
        *,
        permission: str,
        required_role: str,
        actual_role: str | None,
        reason: str = "insufficient",
    ) -> None:
        self.permission = permission
        self.required_role = required_role
        self.actual_role = actual_role
        self.reason = reason
        if reason == "unknown_role":
            message = f"Unknown role: '{actual_role}'"
        else:
            message = f"Insufficient scope for '{permission}': requires '{required_role}' role, got '{actual_role}'"
        super().__init__(message)


def resolve_required(permission: str) -> str:
    """Return the minimum org role for a permission key.

    Raises ``PermissionConfigurationError`` on unknown keys so that
    misconfiguration fails fast at import time (the registry is the single
    source of truth; a missing key is a programming error).
    """
    try:
        return PERMISSIONS[permission]
    except KeyError as exc:
        raise PermissionConfigurationError(f"Unknown permission key '{permission}' — add it to PERMISSIONS") from exc


def assert_org_role(
    role: str | None,
    required: str,
    subject: str,
    *,
    kill_switch_eligible: bool = True,
) -> None:
    """Assert ``role`` is at least ``required`` in the org-role hierarchy.

    Fail-closed: unknown role, empty string, or ``None`` are denied. The
    comparison is the single place that consults ``ORG_ROLE_HIERARCHY``.

    When ``kill_switch_eligible`` is True (default) and the per-request,
    tenancy-bounded kill switch is OFF for the current org
    (``_authz_enforce_ctx`` is False), the hierarchy-level comparison is
    skipped (fail-open). Only the level gate is lifted — the fail-closed
    identity checks (missing/unknown role) still deny, and destructive
    mutations (org deletion via ``require_system_or_org_admin``) pass
    ``kill_switch_eligible=False`` so they are never bypassed. ADR 017
    DECISION 3.
    """
    if required not in ORG_ROLE_HIERARCHY:
        raise PermissionConfigurationError(f"Required role '{required}' is not in the org-role hierarchy")
    if role is None or role == "":
        raise PermissionDenied(
            permission=subject,
            required_role=required,
            actual_role=role,
            reason="unknown_role",
        )
    normalized = role.strip().lower()
    actual_level = ORG_ROLE_HIERARCHY.get(normalized)
    if actual_level is None:
        raise PermissionDenied(
            permission=subject,
            required_role=required,
            actual_role=role,
            reason="unknown_role",
        )
    if kill_switch_eligible and _authz_enforce_ctx.get() is False:
        return
    if actual_level < ORG_ROLE_HIERARCHY[required]:
        raise PermissionDenied(
            permission=subject,
            required_role=required,
            actual_role=role,
            reason="insufficient",
        )


# Import-time validation: every value must resolve to a known org role.
for _permission, _role in PERMISSIONS.items():
    if _role not in ORG_ROLE_HIERARCHY:
        raise PermissionConfigurationError(f"Permission '{_permission}' maps to unknown role '{_role}'")
