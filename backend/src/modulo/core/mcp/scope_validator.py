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

import types
from logging import getLogger

from modulo.auth.permissions import (
    PermissionConfigurationError,
    PermissionDenied,
    assert_org_role,
    resolve_required,
)
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

_log = getLogger(__name__)

__all__ = [
    "READ_ONLY_TOOLS",
    "TOOL_SCOPE_REQUIREMENTS",
    "MCPAuthorizationError",
    "MCPConfigurationError",
    "check_tool_scope",
]


class MCPAuthorizationError(Exception):
    """Raised when the MCP principal lacks the required scope for a tool."""


class MCPConfigurationError(Exception):
    """Raised when a scope-requirement configuration error is detected."""


# tool (or ``tool:action``) -> permission key in ``PERMISSIONS``
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
    "create_secret": "secret.manage",
    "delete_secret": "secret.manage",
    "list_secrets": "secret.manage",
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
) -> None:
    if current_role is None:
        _log.warning("Scope check failed: no authentication context")
        raise MCPAuthorizationError("No authentication context: role not set")

    if not isinstance(tool_name, str):
        _log.error("Scope check failed: tool_name is not a string (type=%s)", type(tool_name).__name__)
        raise MCPAuthorizationError("Tool name must be a string")

    normalized = _sanitize(tool_name, name="tool_name")

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
