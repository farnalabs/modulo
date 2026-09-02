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

from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level

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
    # hitl-gate-removal-guard-plan.md v19 §3 item 5: the weakening-capable
    # graph-write endpoints carry the operator baseline ("pipeline.graph.update")
    # at the route layer; gate-weakening enforcement is the service-layer
    # backstop (operator+ privileged under the row lock, non-privileged callers
    # denied). There is deliberately no admin-only route permission for
    # weakening — operators are "privileged" for weakening by design, and the
    # backstop is the load-bearing control.
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
    # In-Dev (pre-release) disclosure controls (ADR 010): the list endpoints
    # accept ?include_in_dev=true, but revealing In-Dev items is operator-only —
    # the base "list" permission stays viewer so normal listing is unchanged.
    "connector.list.in_dev": "operator",
    "model_backend.list.in_dev": "operator",
    "library.search.in_dev": "operator",
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
    "org.license.manage": "admin",
    "org.authz_enforce.manage": "admin",
    "org.triggers.pause.manage": "admin",
    "org.guardrails.kill_switch.manage": "admin",
    "org.settings.update": "admin",
    # guardrail definition/binding management — the admin-level permission for
    # managing guardrail definitions, node bindings, and the elevated (full,
    # unmasked) config read. Mirrors the org.guardrails.kill_switch.manage
    # admin pattern.
    "guardrail.manage": "admin",
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
    "eval.definition.update": "operator",
    "eval.definition.delete": "operator",
    # housekeeping
    "housekeeping.list": "runner",
    "housekeeping.perform": "operator",
    # determination — SDLC scan + pipeline draft generation read all of the
    # org's connected tool data (repos, PRs, issues), so it is operator-scoped
    # like schema.infer rather than viewer/runner.
    "determination.scan": "operator",
    # teams
    "team.create": "admin",
    "team.update": "admin",
    "team.delete": "admin",
    "team.manage": "admin",
    "team.list": "viewer",
    # audit
    "audit.list": "viewer",
    "audit.manage": "admin",
    # system administration (strict is_system_admin-only gates; the role
    # value is a placeholder so the registry import-time validation passes —
    # require_system_permission ignores the org hierarchy entirely)
    "system.config.manage": "admin",
    "system.org.manage": "admin",
    # admin / secondary routes
    "cost.manage": "admin",
    "sso.manage": "admin",
    "monitor_config.manage": "admin",
    "runtime_config.manage": "admin",
    "error_forwarder.manage": "admin",
    "error_notification.manage": "admin",
    "housekeeping.manage": "admin",
    "run_retention.manage": "admin",
    "admin.trigger_events": "admin",
    "admin.queue_metrics": "admin",
    "admin.notification.manage": "admin",
    "admin.remy.manage": "admin",
    "admin.rotation.manage": "admin",
    "admin.sensitive.manage": "admin",
    "errors.resolve": "viewer",
    "library.manage": "operator",
    "team.members.manage": "admin",
    "admin.rate_limit.manage": "admin",
    "view.manage": "operator",
    "view.list": "viewer",
    # variants
    "variant.create": "operator",
    "variant.update": "operator",
    "variant.delete": "operator",
    "variant.list": "viewer",
    "variant.run": "operator",
    # lifecycle maps
    "lifecycle_map.create": "operator",
    "lifecycle_map.update": "operator",
    "lifecycle_map.delete": "operator",
    "lifecycle_map.list": "viewer",
    # environment profiles
    "environment_profile.create": "operator",
    "environment_profile.update": "operator",
    "environment_profile.delete": "operator",
    "environment_profile.list": "viewer",
    "environment_profile.test": "operator",
    # pipeline folders
    "pipeline_folder.create": "operator",
    "pipeline_folder.update": "operator",
    "pipeline_folder.delete": "operator",
    "pipeline_folder.list": "viewer",
    # parameter schemas
    "parameter_schema.create": "operator",
    "parameter_schema.update": "operator",
    "parameter_schema.delete": "operator",
    "parameter_schema.list": "viewer",
    "parameter_schema.validate": "viewer",
    "parameter_schema.set.create": "operator",
    "parameter_schema.set.update": "operator",
    "parameter_schema.set.delete": "operator",
    # node categories
    "node_category.create": "operator",
    "node_category.update": "operator",
    "node_category.delete": "operator",
    "node_category.list": "viewer",
    # plugins
    "plugin.list": "viewer",
    "plugin.health": "viewer",
    # registry
    "registry.publish": "operator",
    "registry.pull": "viewer",
    "registry.list": "viewer",
    "registry.publisher.manage": "operator",
    # observability / notifications
    "observability.view": "viewer",
    "observability.manage": "operator",
    "notification.view": "viewer",
    "notification.manage": "operator",
    "notification.self": "viewer",
    # dashboard
    "dashboard.summary": "viewer",
    "dashboard.trends": "viewer",
    "dashboard.daily_run_counts": "viewer",
    # analytics
    "analytics.query": "viewer",
    # events
    "events.list": "viewer",
    # feedback
    "feedback.create": "operator",
    "feedback.list": "viewer",
    "feedback.update": "operator",
    "feedback.review": "operator",
    # contributions
    "contribution.create": "operator",
    "contribution.submit": "operator",
    "contribution.publish": "admin",
    "contribution.version": "operator",
    "contribution.list": "viewer",
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
    if role is None or not role:
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


def _clamp_role(minted_role: str, live_role: str | None) -> str:
    """Return the effective API-key role = min(minted, live) per the role hierarchy.

    Never escalates: a demoted operator's key degrades to the live role.
    ``live_role=None`` (owner removed/deactivated) returns ``""`` — the
    sentinel DENIAL marker the caller must reject (the key dies, ADR 017).
    Unknown roles also return ``""`` so the caller can deny (fail-closed).

    Pure function: no DB access, unit-testable in isolation.
    """
    if live_role is None:
        return ""
    minted_level = org_role_level(minted_role)
    live_level = org_role_level(live_role)
    if minted_level < 0 or live_level < 0:
        return ""
    effective_level = min(minted_level, live_level)
    for role, level in ORG_ROLE_HIERARCHY.items():
        if level == effective_level:
            return role
    return ""


# Import-time validation: every value must resolve to a known org role.
for _permission, _role in PERMISSIONS.items():
    if _role not in ORG_ROLE_HIERARCHY:
        raise PermissionConfigurationError(f"Permission '{_permission}' maps to unknown role '{_role}'")
