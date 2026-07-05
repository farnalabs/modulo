"""ViewModel-level scope validation for MCP tools.

Dual-layer enforcement:
1. Middleware (McpAuthMiddleware): validates API key / OAuth token at HTTP level,
   sets _ctx_role ContextVar.
2. ViewModel (this module): re-checks role against per-tool requirements at the
   business logic layer, preventing bypass if the middleware has a bug.
"""

from logging import getLogger

from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level

_log = getLogger(__name__)

__all__ = [
    "TOOL_SCOPE_REQUIREMENTS",
    "MCPAuthorizationError",
    "MCPConfigurationError",
    "check_tool_scope",
]


class MCPAuthorizationError(Exception):
    """Raised when the MCP principal lacks the required scope for a tool."""


class MCPConfigurationError(Exception):
    """Raised when a scope-requirement configuration error is detected."""


TOOL_SCOPE_REQUIREMENTS: dict[str, str] = {
    "trigger_pipeline": "runner",
    "cancel_run": "runner",
    "review_hitl": "operator",
    "review_hitl:claim": "runner",
    "review_hitl:approve": "operator",
    "review_hitl:reject": "operator",
    "copy_library_primitive": "runner",
    "list_pending_hitl": "runner",
    "get_run_output": "runner",
    "get_trigger_events": "runner",
    "create_pipeline": "operator",
    "update_pipeline_graph": "operator",
    "create_model_backend": "operator",
}

_VALID_ROLES = frozenset(ORG_ROLE_HIERARCHY)
for _tool, _role in TOOL_SCOPE_REQUIREMENTS.items():
    if _role not in _VALID_ROLES:
        raise MCPConfigurationError(
            f"Misconfigured scope requirement for '{_tool}': "
            f"role '{_role}' is not in the role hierarchy",
        )


def _sanitize(value: str) -> str:
    stripped = value.strip().lower()
    if not stripped:
        raise MCPAuthorizationError("Value is empty or whitespace-only")
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

    name = _sanitize(tool_name)

    if action is not None:
        if not isinstance(action, str):
            _log.error("Scope check failed: action is not a string (type=%s)", type(action).__name__)
            raise MCPAuthorizationError("Action must be a string")
        act = _sanitize(action)
        key = f"{name}:{act}"
        required = TOOL_SCOPE_REQUIREMENTS.get(key)
        if required is None:
            _log.warning("Unknown action '%s' for tool '%s'", action, tool_name)
            raise MCPAuthorizationError(
                f"Unknown action '{action}' for tool '{tool_name}'",
            )
    else:
        required = TOOL_SCOPE_REQUIREMENTS.get(name)
        if required is None:
            return

    current_level = org_role_level(current_role)
    required_level = ORG_ROLE_HIERARCHY[required]

    if current_level < 0:
        _log.warning("Scope check failed: unknown role '%s'", current_role)
        raise MCPAuthorizationError(f"Unknown role: '{current_role}'")

    if current_level < required_level:
        _log.warning(
            "Insufficient scope for '%s': requires '%s' role, got '%s'",
            tool_name, required, current_role,
        )
        raise MCPAuthorizationError(
            f"Insufficient scope for '{tool_name}': "
            f"requires '{required}' role, got '{current_role}'",
        )
