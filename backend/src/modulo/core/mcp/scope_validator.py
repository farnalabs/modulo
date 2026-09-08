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
    authz_enforce_enabled,
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
    "CALLER_SCOPE_REQUIREMENTS",
    "READ_ONLY_TOOLS",
    "TOOL_SCOPE_REQUIREMENTS",
    "MCPAuthorizationError",
    "MCPConfigurationError",
    "check_tool_scope",
    "classify_caller_scope",
    "get_request_allowed_tools",
    "resolve_tool_access",
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
    "list_hitl_gates": "hitl.list",
    "get_hitl_gate": "hitl.list",
    "get_pipeline_gates": "pipeline.graph.read",
    "get_run_output": "run.output",
    "create_pipeline": "pipeline.create",
    "update_pipeline_graph": "pipeline.graph.update",
    "bind_connector_to_node": "pipeline.bind_connector",
    "create_model_backend": "model_backend.create",
    "list_runs": "run.list",
    "get_run_evals": "run.evals",
    "list_eval_definitions": "eval.list",
    "create_eval_definition": "eval.definition.create",
    "update_eval_definition": "eval.definition.update",
    "delete_eval_definition": "eval.definition.delete",
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
    # FAR-614: the first caller-scoped (``.self``) MCP tools — get/set the
    # CALLER's own HITL email-alert preference. Target = the caller's account
    # by construction; no target parameter exists to misuse.
    "get_hitl_email_alerts": "hitl_email.self",
    "set_hitl_email_alerts": "hitl_email.self",
    "list_trigger_events": "trigger.events.list",
    "query_analytics": "analytics.query",
    "query_analytics_concurrency": "analytics.query",
    # FAR-695: read/list tools for the build-time entities that previously had
    # MCP write surfaces only. Each maps to the SAME permission key as the
    # corresponding REST route (agent.list / connector.list / etc.).
    "list_agents": "agent.list",
    "get_agent": "agent.list",
    "list_connectors": "connector.list",
    "get_connector": "connector.list",
    "list_connector_types": "connector.list",
    "list_model_backends": "model_backend.list",
    "get_model_backend": "model_backend.list",
    "list_environment_profiles": "environment_profile.list",
    "list_parameter_schemas": "parameter_schema.list",
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

# ---------------------------------------------------------------------------
# FAR-620: caller-scope dimension (org-only | caller-scoped | any)
# ---------------------------------------------------------------------------

# Classification values for CALLER_SCOPE_REQUIREMENTS:
#   "org-only"      — org-level machine identities only: a user-scoped API
#                     key (``scope='user'``) is DENIED; org-wide/team-scoped/
#                     run-scoped keys and identity-bound JWT/OAuth sessions
#                     pass (the identity sessions ARE the user).
#   "caller-scoped" — the tool operates on the CALLER'S OWN account (its
#                     permission key carries the ``.self`` suffix): DENIED
#                     unless the credential is identity-bound or a user-scoped
#                     key (``key_scope == 'user'``). An org-scoped service key
#                     must never alter user-level configuration, so org keys
#                     are denied here.
#   "any"           — no caller-scope restriction (today's behaviour).
_CALLER_SCOPE_ORG_ONLY = "org-only"
_CALLER_SCOPE_CALLER = "caller-scoped"
_CALLER_SCOPE_ANY = "any"
VALID_CALLER_SCOPE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {_CALLER_SCOPE_ORG_ONLY, _CALLER_SCOPE_CALLER, _CALLER_SCOPE_ANY}
)

# Caller-scoped tools are DERIVED from the ``.self`` permission-key suffix —
# a tool whose permission key ends in ``.self`` operates on the caller's own
# account. There is deliberately NO parallel tool set to keep in sync.
_CALLER_SCOPED_SUFFIX = ".self"

# Explicit caller-scope classification overrides, keyed by base tool name.
# The derivation below already covers the default (unmapped mutating tools are
# org-only, unmapped read-only tools are 'any'); entries here pin tools whose
# classification must be explicit regardless of derivation. The FAR-614
# ``.self`` tools (get/set_hitl_email_alerts) are classified caller-scoped
# purely through their ``.self`` permission keys - no explicit pin needed.
# FAR-620: credential minting is an org-level operation. A user-scoped key
# must never mint an org-wide key (that would escape the user scope entirely),
# so ``create_api_key`` is pinned org-only: under a user-scoped key the tool
# is denied, under org keys it behaves exactly as today.
_CALLER_SCOPE_REQUIREMENTS: dict[str, str] = {
    "create_api_key": _CALLER_SCOPE_ORG_ONLY,
}

CALLER_SCOPE_REQUIREMENTS: types.MappingProxyType[str, str] = types.MappingProxyType(_CALLER_SCOPE_REQUIREMENTS)


def _permission_key_for(tool: str, action: str | None) -> str | None:
    """Resolve the permission key for a (tool, action) pair, or None.

    Mirrors the registered-scope lookup in ``check_tool_scope``: an explicit
    ``tool:action`` mapping wins, then the base-tool mapping, then the
    read-only allowlist (pinned at ``resource.read_only``). Pure lookup —
    no ContextVar reads.
    """
    if action is not None:
        return TOOL_SCOPE_REQUIREMENTS.get(f"{tool}:{action}")
    permission_key = TOOL_SCOPE_REQUIREMENTS.get(tool)
    if permission_key is None and tool in READ_ONLY_TOOLS:
        return "resource.read_only"
    return permission_key


def _validate_caller_scope_classification(tool: str, classification: str) -> None:
    """Fail-fast on an out-of-vocabulary caller-scope classification.

    A typo'd classification value (e.g. ``'orgonly'``) would otherwise silently
    fall through every denial leg of the resolver and be treated as 'any'
    (unrestricted). Raised at import time for every pinned entry and again at
    classification time as a defence-in-depth backstop.
    """
    if classification not in VALID_CALLER_SCOPE_CLASSIFICATIONS:
        raise MCPConfigurationError(
            f"Misconfigured caller-scope classification for '{tool}': "
            f"'{classification}' is not one of {sorted(VALID_CALLER_SCOPE_CLASSIFICATIONS)}",
        )


def classify_caller_scope(tool: str, permission_key: str | None) -> str:
    """Classify a tool's caller-scope requirement (pure; one of the 3 values).

    Order: an explicit ``CALLER_SCOPE_REQUIREMENTS`` entry wins; then the
    ``.self`` permission-key suffix derives caller-scoped; then the read-only
    allowlist classifies 'any'; everything else is org-only (preserving
    today's default where unmapped mutating tools deny by default). An
    explicit out-of-vocabulary entry raises ``MCPConfigurationError`` instead
    of being implicitly treated as 'any'.
    """
    mapped = CALLER_SCOPE_REQUIREMENTS.get(tool)
    if mapped is not None:
        _validate_caller_scope_classification(tool, mapped)
        return mapped
    if permission_key is not None and permission_key.endswith(_CALLER_SCOPED_SUFFIX):
        return _CALLER_SCOPE_CALLER
    if tool in READ_ONLY_TOOLS:
        return _CALLER_SCOPE_ANY
    return _CALLER_SCOPE_ORG_ONLY


def resolve_tool_access(
    tool: str,
    action: str | None,
    role: str | None,
    key_scope: str | None,
    auth_type: str | None,
    allowed_tools: set[str] | None,
    kill_switch: bool,
) -> tuple[bool, str]:
    """Pure tool-access decision for one MCP call (FAR-620).

    Args:
        tool: lower-cased base tool name (the caller sanitises).
        action: optional lower-cased action (e.g. ``review_hitl`` claim).
        role: the caller's effective (live-clamped) org role.
        key_scope: the credential's caller scope — ``'org'`` (org-wide,
            team-scoped and run-scoped keys), ``'user'`` (a user-scoped key,
            a regular JWT session, or an OAuth token), or ``None`` (unset —
            fails closed).
        auth_type: the credential class — ``'api_key'``, ``'jwt'`` (regular
            JWT/Remy session), ``'oauth'``, or ``None`` (unset).
        allowed_tools: normalised node-level allow-list (``None`` =
            UNRESTRICTED; an explicit empty set is deny-by-default).
        kill_switch: True when the org's authz-enforce kill switch is ON
            (hierarchy enforced); False fail-opens ONLY the role leg.

    Returns ``(allowed, permission_key)``. ``permission_key`` is the resolved
    key for error/log detail, or ``""`` when the tool could not be resolved.

    Composes four deny-only legs in a fixed order (never widen):

    1. node allowed_tools narrowing (FAR-418/436 semantics preserved exactly).
    2. resolution — the tool/action must map to a permission key
       (``TOOL_SCOPE_REQUIREMENTS`` / ``READ_ONLY_TOOLS``) or deny.
    3. caller-scope leg — KILL-SWITCH-INELIGIBLE (the tenant-boundary
       precedent): caller-scoped tools require ``key_scope == 'user'`` (a
       user-scoped key, JWT session, or OAuth token); org-only tools deny
       user-scoped API KEYS (an org-wide key must never be mintable from a
       user-scoped credential, and a user-scoped key is not an org-level
       machine identity). ``None`` key scope fails closed on caller-scoped
       tools. Identity-bound JWT/OAuth sessions keep today's access to
       org-only tools.
    4. role leg — kill-switch-ELIGIBLE: the org-role hierarchy check; the
       kill switch OFF bypasses the comparison (never the identity checks,
       which deny above).
    """
    # Leg 1: node-level allowed_tools narrowing. When the node's
    # capability_scope declares an allow-list it is an ADDITIONAL filter —
    # the role must still permit the tool. Absent (None) = UNRESTRICTED;
    # an explicit EMPTY set is deny-by-default.
    if allowed_tools is not None and tool not in allowed_tools:
        return False, ""

    # Leg 2: resolution. Unmapped + not read-only ⇒ deny (deny-by-default).
    permission_key = _permission_key_for(tool, action)
    if permission_key is None:
        return False, ""

    # Leg 3: caller-scope (kill-switch-ineligible).
    classification = classify_caller_scope(tool, permission_key)
    if classification == _CALLER_SCOPE_CALLER and key_scope != "user":
        return False, permission_key
    if classification == _CALLER_SCOPE_ORG_ONLY and key_scope == "user" and auth_type == "api_key":
        return False, permission_key

    # Leg 4: role hierarchy (kill-switch-eligible). Fail-closed on a
    # missing/unknown role regardless of the kill switch, mirroring
    # ``assert_org_role``'s identity checks.
    required = resolve_required(permission_key)
    if role is None or not isinstance(role, str) or not role:
        return False, permission_key
    actual_level = ORG_ROLE_HIERARCHY.get(role.strip().lower())
    if actual_level is None:
        return False, permission_key
    if kill_switch and actual_level < ORG_ROLE_HIERARCHY[required]:
        return False, permission_key
    return True, permission_key


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

# FAR-620 fail-fast: every explicit caller-scope classification must be in
# vocabulary. A typo'd value would otherwise fall through every denial leg and
# be silently treated as 'any' (unrestricted) - refuse the import instead.
for _tool, _classification in _CALLER_SCOPE_REQUIREMENTS.items():
    _validate_caller_scope_classification(_tool, _classification)


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
    key_scope: str | None = None,
    auth_type: str | None = None,
) -> None:
    """Single tool-dispatch chokepoint (delegates to ``resolve_tool_access``).

    FAR-620: the DECISION is made by the pure resolver; this wrapper keeps the
    input validation, the ContextVar wiring (node allowed-tools fallback, the
    request-scoped authz-enforce kill switch) and the per-leg denial messages.
    ``key_scope`` / ``auth_type`` arrive from the MCP middleware's ContextVars
    via ``_check_agent_tool_scope``; direct callers that omit them keep
    today's behaviour (key_scope None fails closed on caller-scoped tools —
    e.g. the FAR-614 ``.self`` tools ``get_hitl_email_alerts`` /
    ``set_hitl_email_alerts`` — and leaves the role/org legs untouched).
    """
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

    act: str | None = None
    if action is not None:
        if not isinstance(action, str):
            _log.error("Scope check failed: action is not a string (type=%s)", type(action).__name__)
            raise MCPAuthorizationError("Action must be a string")
        act = _sanitize(action, name="action")

    allowed_set: set[str] | None = None
    if allowed_tools is not None:
        allowed_set = {_sanitize(t, name="allowed_tool") for t in allowed_tools}

    allowed, _permission_key = resolve_tool_access(
        tool=normalized,
        action=act,
        role=current_role,
        key_scope=key_scope,
        auth_type=auth_type,
        allowed_tools=allowed_set,
        kill_switch=authz_enforce_enabled(),
    )
    if not allowed:
        message = _denial_message(
            tool_name=tool_name,
            normalized=normalized,
            action=action,
            act=act,
            current_role=current_role,
            key_scope=key_scope,
            auth_type=auth_type,
            allowed_set=allowed_set,
            kill_switch=authz_enforce_enabled(),
        )
        _log.warning("Scope check failed: %s", message)
        raise MCPAuthorizationError(message)


def _denial_message(
    *,
    tool_name: str,
    normalized: str,
    action: str | None,
    act: str | None,
    current_role: str,
    key_scope: str | None,
    auth_type: str | None,
    allowed_set: set[str] | None,
    kill_switch: bool,
) -> str:
    """Re-derive the specific denial message for a resolver denial.

    The pure resolver returns a bare False; the legacy per-leg error messages
    (pinned by the unit tests) are re-derived here in the same leg order the
    resolver evaluated them. Single-fault inputs make the first failing leg
    deterministic.
    """
    # Leg 1: node allowed_tools narrowing.
    if allowed_set is not None and normalized not in allowed_set:
        return f"Tool '{tool_name}' is outside the node's allowed_tools scope"

    # Leg 2: resolution.
    permission_key = _permission_key_for(normalized, act)
    if permission_key is None:
        if act is not None:
            return f"Unknown action '{action}' for tool '{tool_name}'"
        return f"Tool '{tool_name}' is not registered in the scope policy"

    # Leg 3: caller-scope (kill-switch-ineligible).
    classification = classify_caller_scope(normalized, permission_key)
    if classification == _CALLER_SCOPE_CALLER and key_scope != "user":
        return (
            f"Tool '{tool_name}' is caller-scoped and requires a user-scoped "
            f"credential; this caller's key scope is '{key_scope or 'unset'}'"
        )
    if classification == _CALLER_SCOPE_ORG_ONLY and key_scope == "user" and auth_type == "api_key":
        return f"Tool '{tool_name}' is org-scoped and cannot be called with a user-scoped API key"

    # Leg 4: role hierarchy — reuse ``assert_org_role`` so the pinned
    # "Insufficient scope ... requires ... got ..." message is identical.
    required = resolve_required(permission_key)
    try:
        assert_org_role(current_role, required, subject=f"MCP tool '{tool_name}'")
    except PermissionDenied as exc:
        return str(exc)
    return f"Tool '{tool_name}' access denied"
