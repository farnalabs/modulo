"""ViewModel-level scope validation for MCP tools.

Dual-layer enforcement:
1. Middleware (McpAuthMiddleware): validates API key / OAuth token at HTTP level,
   sets _ctx_role ContextVar.
2. ViewModel (this module): re-checks role against per-tool requirements at the
   business logic layer, preventing bypass if the middleware has a bug.
"""

from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level


class MCPAuthorizationError(Exception):
    """Raised when the MCP principal lacks the required scope for a tool.

    The caller should catch this and return an appropriate error response
    to the MCP client — never let it propagate to the HTTP layer.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


TOOL_SCOPE_REQUIREMENTS: dict[str, str] = {
    "trigger_pipeline": "runner",
    "cancel_run": "runner",
    "review_hitl": "operator",
    "copy_library_primitive": "runner",
    "list_pending_hitl": "runner",
}


REVIEW_HITL_ACTION_REQUIREMENTS: dict[str, str] = {
    "claim": "runner",
    "approve": "operator",
    "reject": "operator",
}


def check_tool_scope(
    current_role: str | None,
    tool_name: str,
    action: str | None = None,
) -> None:
    """Validate that `current_role` meets the tool's minimum role requirement.

    Args:
        current_role: The role derived by the middleware (``_ctx_role``).
        tool_name: The MCP tool being invoked.
        action: Optional sub-action (e.g. ``"approve"`` for *review_hitl*).

    Raises:
        MCPAuthorizationError: If the principal's role is below the minimum.
    """
    if current_role is None:
        raise MCPAuthorizationError("No authentication context — role not set")

    required = TOOL_SCOPE_REQUIREMENTS.get(tool_name)
    if required is None:
        return  # Tool has no scope gate — accessible to all authenticated roles

    # Fine-grained action-level scoping for review_hitl
    if tool_name == "review_hitl" and action is not None:
        action_req = REVIEW_HITL_ACTION_REQUIREMENTS.get(action)
        if action_req is not None:
            required = action_req

    current_level = org_role_level(current_role)
    required_level = ORG_ROLE_HIERARCHY.get(required, -1)

    if current_level < 0:
        raise MCPAuthorizationError(f"Unknown role: '{current_role}'")

    if current_level < required_level:
        raise MCPAuthorizationError(
            f"Insufficient scope for '{tool_name}': "
            f"requires '{required}' role, got '{current_role}'",
        )
