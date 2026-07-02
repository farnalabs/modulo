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
    "check_tool_scope",
]


class MCPAuthorizationError(Exception):
    """Raised when the MCP principal lacks the required scope for a tool.

    The caller should catch this and return an appropriate error response
    to the MCP client — never let it propagate to the HTTP layer.
    """


TOOL_SCOPE_REQUIREMENTS: dict[str, str] = {
    "trigger_pipeline": "runner",
    "cancel_run": "runner",
    # Tool-level default for review_hitl (no action = operator)
    "review_hitl": "operator",
    # Action-scoped sub-requirements
    "review_hitl:claim": "runner",
    "review_hitl:approve": "operator",
    "review_hitl:reject": "operator",
    "copy_library_primitive": "runner",
    "list_pending_hitl": "runner",
    "get_run_output": "runner",
    "get_trigger_events": "runner",
    "create_pipeline": "operator",
    "update_pipeline_graph": "operator",
}


def _require_role_exists(role: str) -> None:
    """Validate that *role* is a known key in the role hierarchy.

    Raises ``MCPAuthorizationError`` if the role is not defined — this is
    a configuration error that must be surfaced, not silently tolerated.
    """
    if role not in ORG_ROLE_HIERARCHY:
        raise MCPAuthorizationError(
            f"Misconfigured scope requirement: role '{role}' is not in the role hierarchy",
        )


def check_tool_scope(
    current_role: str | None,
    tool_name: str,
    action: str | None = None,
) -> None:
    """Validate that ``current_role`` meets the tool's minimum role requirement.

    Args:
        current_role: The role derived by the middleware (``_ctx_role``).
        tool_name: The MCP tool being invoked.
        action: Optional sub-action (e.g. ``"approve"`` for ``review_hitl``).

    Raises:
        MCPAuthorizationError: If the principal's role is below the minimum.
    """
    if current_role is None:
        raise MCPAuthorizationError("No authentication context: role not set")

    name = tool_name.strip().lower()
    if not name:
        raise MCPAuthorizationError("Tool name is empty or whitespace-only")

    if action is not None:
        act = action.strip().lower()
        if not act:
            raise MCPAuthorizationError("Action is empty or whitespace-only")
        key = f"{name}:{act}"
        required = TOOL_SCOPE_REQUIREMENTS.get(key)
        if required is None:
            # Action not found in scope requirements — caller bug
            raise MCPAuthorizationError(
                f"Unknown action '{action}' for tool '{tool_name}'",
            )
    else:
        required = TOOL_SCOPE_REQUIREMENTS.get(name)
        if required is None:
            return  # No scope gate — accessible to all authenticated roles

    _require_role_exists(required)

    current_level = org_role_level(current_role)
    required_level = ORG_ROLE_HIERARCHY[required]

    if current_level < 0:
        raise MCPAuthorizationError(f"Unknown role: '{current_role}'")

    if current_level < required_level:
        raise MCPAuthorizationError(
            f"Insufficient scope for '{tool_name}': "
            f"requires '{required}' role, got '{current_role}'",
        )
