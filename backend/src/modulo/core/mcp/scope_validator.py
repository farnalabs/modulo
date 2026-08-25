"""ViewModel-level scope validation for MCP tools.

Dual-layer enforcement:
1. Middleware (McpAuthMiddleware): validates API key / OAuth token at HTTP level,
   sets _ctx_role ContextVar.
2. ViewModel (this module): re-checks role against per-tool requirements at the
   business logic layer, preventing bypass if the middleware has a bug.

The per-tool requirement map references the centralized permission registry
(``modulo.auth.permissions.PERMISSIONS``) rather than duplicating roles — the
registry is the single source of truth (ADR 017).
"""

import contextvars
import types
from collections.abc import Sequence
from logging import getLogger

from modulo.auth.permissions import (
    PermissionConfigurationError,
    PermissionDenied,
    assert_org_role,
    resolve_required,
)
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

_log = getLogger(__name__)

# FAR-418: request-scoped node-level allowed_tools allow-list. The agent's MCP
# tool calls arrive at the MCP server as independent HTTP requests; the calling
# node's capability_scope.allowed_tools is forwarded by the agent runtime as the
# ``X-Modulo-Allowed-Tools`` header and lifted into this ContextVar by
# McpAuthMiddleware. ``check_tool_scope`` consults it as the default narrowing
# filter so the production tool-dispatch chokepoint enforces it without every
# handler call-site having to thread the value. Unset (None) = UNRESTRICTED.
_ctx_allowed_tools: contextvars.ContextVar[Sequence[str] | None] = contextvars.ContextVar(
    "scope_allowed_tools", default=None
)


def set_request_allowed_tools(allowed_tools: Sequence[str] | None) -> None:
    """Set the request-scoped node allowed_tools allow-list (called by middleware)."""
    _ctx_allowed_tools.set(allowed_tools)


def get_request_allowed_tools() -> Sequence[str] | None:
    """Return the request-scoped node allowed_tools allow-list, if any."""
    return _ctx_allowed_tools.get()


__all__ = [
    "READ_ONLY_TOOLS",
    "TOOL_SCOPE_REQUIREMENTS",
    "MCPAuthorizationError",
    "MCPConfigurationError",
    "check_tool_scope",
    "get_request_allowed_tools",
    "set_request_allowed_tools",
]


class MCPAuthorizationError(Exception):
    """Raised when the MCP principal lacks the required scope for a tool."""


class MCPConfigurationError(Exception):
    """Raised when a scope-requirement configuration error is detected."""


# tool (or ``tool:action``) -> permission key in ``PERMISSIONS``
# Secret-management permission shared by the create/delete/list secret tools.
_SCOPE_SECRET_MANAGE = "secret.manage"  # nosec B105 — permission scope name, not a credential

_TOOL_SCOPE_REQUIREMENTS: dict[str, str] = {
    "trigger_pipeline": "run.trigger",
    "cancel_run": "run.cancel",
    "review_hitl": "hitl.review",
    "review_hitl:claim": "hitl.claim",
    "review_hitl:approve": "hitl.approve",
    "review_hitl:reject": "hitl.reject",
    "review_hitl:deliver_manual": "hitl.deliver_manual",
    "copy_library_primitive": "library.copy",
    "list_pending_hitl": "hitl.list",
    "get_run_output": "run.output",
    "create_pipeline": "pipeline.create",
    "update_pipeline_graph": "pipeline.graph.update",
    "bind_connector_to_node": "pipeline.bind_connector",
    "create_model_backend": "model_backend.create",
    "list_runs": "run.list",
    "get_run_evals": "run.evals",
    "list_eval_definitions": "eval.list",
    "list_triggers": "trigger.list",
    "get_trigger": "trigger.list",
    "update_trigger": "trigger.update",
    "delete_trigger": "trigger.delete",
    "set_org_triggers_paused": "org.triggers.pause.manage",
    "list_housekeeping": "housekeeping.list",
    "perform_housekeeping": "housekeeping.perform",
    "create_connector": "connector.create",
    "delete_connector": "connector.delete",
    "create_trigger": "trigger.create",
    "delete_pipeline": "pipeline.delete",
    "create_agent": "agent.create",
    "create_schema": "schema.create",
    "infer_schema": "schema.infer",
    "create_secret": _SCOPE_SECRET_MANAGE,
    "delete_secret": _SCOPE_SECRET_MANAGE,
    "list_secrets": _SCOPE_SECRET_MANAGE,
    "create_api_key": "api_key.create",
    "list_api_keys": "api_key.update",
    "revoke_api_key": "api_key.revoke",
    "list_trigger_events": "trigger.events.list",
    "query_analytics": "analytics.query",
    "query_analytics_concurrency": "analytics.query",
}

TOOL_SCOPE_REQUIREMENTS: types.MappingProxyType[str, str] = types.MappingProxyType(_TOOL_SCOPE_REQUIREMENTS)

# Explicit read-only tools (pinned at viewer). Unmapped mutating tools FAIL
# under deny-by-default; unmapped read-only tools are pinned at viewer here.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "list_pipelines",
        "get_pipeline_graph",
        "get_run_status",
        "search_library",
        "search_documentation",
        "get_integration_status",
        "get_org_config",
        "get_available_features",
        "list_schemas",
        "validate_payload",
    }
)

# Import-time fail-fast validation: every tool's permission key must resolve
# through PERMISSIONS and its resolved role must be in the role hierarchy.
for tool, permission_key in _TOOL_SCOPE_REQUIREMENTS.items():
    try:
        role = resolve_required(permission_key)
    except PermissionConfigurationError as exc:
        raise MCPConfigurationError(
            f"Misconfigured scope requirement for '{tool}': {exc}",
        ) from exc
    if role not in ORG_ROLE_HIERARCHY:
        raise MCPConfigurationError(
            f"Misconfigured scope requirement for '{tool}': "
            f"permission '{permission_key}' resolves to unknown role '{role}'",
        )


def _sanitize(value: str, name: str = "value") -> str:
    stripped = value.strip().lower()
    if not stripped:
        raise MCPAuthorizationError(f"{name} is empty or whitespace-only")
    return stripped


def check_tool_scope(
    current_role: str | None,
    tool_name: str,
    action: str | None = None,
    allowed_tools: Sequence[str] | None = None,
) -> None:
    # FAR-418: when no explicit allow-list is passed, fall back to the
    # request-scoped node allowed_tools (set by McpAuthMiddleware from the
    # agent-supplied ``X-Modulo-Allowed-Tools`` header). This is the production
    # run-path wiring: an agent node's tool calls are narrowed here, and the
    # UNRESTRICTED default (no header) leaves behaviour unchanged.
    if allowed_tools is None:
        allowed_tools = get_request_allowed_tools()

    if current_role is None:
        _log.warning("Scope check failed: no authentication context")
        raise MCPAuthorizationError("No authentication context: role not set")

    if not isinstance(tool_name, str):
        _log.error("Scope check failed: tool_name is not a string (type=%s)", type(tool_name).__name__)
        raise MCPAuthorizationError("Tool name must be a string")

    normalized = _sanitize(tool_name, name="tool_name")

    # FAR-418: node-level allowed_tools narrowing. When a node's capability_scope
    # declares an allowed-tool allow-list, it is an ADDITIONAL filter layered on
    # the (already-validated) role check — the role must still permit the tool,
    # and the tool must be on the node's list. Absent/empty (the UNRESTRICTED
    # default) performs no narrowing, preserving pre-scope behaviour.
    if allowed_tools:
        allowed = {_sanitize(t, name="allowed_tool") for t in allowed_tools}
        if normalized not in allowed:
            _log.warning(
                "Tool '%s' is outside the node's allowed_tools scope (allowed=%s)",
                tool_name,
                ",".join(sorted(allowed)),
            )
            raise MCPAuthorizationError(
                f"Tool '{tool_name}' is outside the node's allowed_tools scope",
            )

    if action is not None:
        if not isinstance(action, str):
            _log.error("Scope check failed: action is not a string (type=%s)", type(action).__name__)
            raise MCPAuthorizationError("Action must be a string")
        act = _sanitize(action, name="action")
        key = f"{normalized}:{act}"
        permission_key = TOOL_SCOPE_REQUIREMENTS.get(key)
        if permission_key is None:
            _log.warning("Unknown action '%s' for tool '%s'", action, tool_name)
            raise MCPAuthorizationError(
                f"Unknown action '{action}' for tool '{tool_name}'",
            )
    else:
        permission_key = TOOL_SCOPE_REQUIREMENTS.get(normalized)
        if permission_key is None:
            if normalized in READ_ONLY_TOOLS:
                permission_key = "resource.read_only"
            else:
                _log.warning("Tool '%s' is not registered in the scope policy", tool_name)
                raise MCPAuthorizationError(
                    f"Tool '{tool_name}' is not registered in the scope policy",
                )

    required = resolve_required(permission_key)
    try:
        assert_org_role(current_role, required, subject=f"MCP tool '{tool_name}'")
    except PermissionDenied as exc:
        raise MCPAuthorizationError(str(exc)) from exc
