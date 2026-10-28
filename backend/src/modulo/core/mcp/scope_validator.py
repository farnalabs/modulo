"""ViewModel-level scope validation for MCP tools.

Dual-layer enforcement:
1. Middleware (McpAuthMiddleware): validates API key / OAuth token at HTTP level,
   sets _ctx_role ContextVar.
2. ViewModel (this module): re-checks role against per-tool requirements at the
   business logic layer, preventing bypass if the middleware has a bug.

The per-tool requirement map references the centralized permission registry
(``modulo.auth.permissions.PERMISSIONS``) rather than duplicating roles — the
registry is the single source of truth (ADR 047).
"""

import contextvars
import types
from collections.abc import Sequence
from dataclasses import dataclass
from logging import getLogger

from modulo.auth.permissions import (
    PermissionConfigurationError,
    PermissionDenied,
    assert_org_role,
    authz_enforce_enabled,
    grants_permit,
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
    "SYSTEM_ADMIN_TOOLS",
    "TOOL_SCOPE_REQUIREMENTS",
    "MCPAuthorizationError",
    "MCPConfigurationError",
    "MCPSystemAdminRequiredError",
    "check_node_allowed_tools",
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


class MCPSystemAdminRequiredError(MCPAuthorizationError):
    """Raised when a system-admin-gated tool is called without the claim.

    FAR-1930: a subclass of ``MCPAuthorizationError`` so every existing
    ``except MCPAuthorizationError`` handler (the MCP tool handlers that build
    the ``insufficient_scope`` envelope) catches it unchanged. The MCP surface
    distinguishes it only to attach the call-site warning to that refusal; the
    decision itself is the resolver's.
    """


# ---------------------------------------------------------------------------
# FAR-1930: the SINGLE per-tool requirement registry
# ---------------------------------------------------------------------------
# One entry per registered MCP tool — plus a ``tool:action`` key for sub-scope
# actions — declares BOTH scope requirements together:
#
#   * ``permission_key`` — the org-role floor in ``PERMISSIONS``. ``None``
#     means the tool is on the read-only allowlist (pinned at
#     ``resource.read_only``).
#   * ``system_admin`` — the strict ``is_system_admin`` claim whose REST twin
#     is gated by ``require_system_permission`` (no org-role fall-through).
#
# The three public views — ``TOOL_SCOPE_REQUIREMENTS`` (mapped tools),
# ``READ_ONLY_TOOLS`` (the read-only pin) and ``SYSTEM_ADMIN_TOOLS`` (the
# claim) — are all DERIVED from this registry, so there is no second per-tool
# list to keep in sync: a system-admin tool is declared ONCE, and the resolver
# enforces it. The two gates stay independently visible as the two fields of
# each entry.


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    """The scope requirements declared for one MCP tool (or ``tool:action``).

    ``permission_key`` is the org-role permission key, or ``None`` when the
    tool is on the read-only allowlist (pinned at ``resource.read_only``).
    ``system_admin`` declares the FAR-1930 strict ``is_system_admin`` claim.
    """

    permission_key: str | None = None
    system_admin: bool = False


# Secret-management permission shared by the create/delete/list secret tools.
_SCOPE_SECRET_MANAGE = "secret.manage"  # nosec B105 — permission scope name, not a credential
_SCOPE_CONNECTOR_LIST = "connector.list"  # nosec B105 — permission scope name, not a credential
_SCOPE_PARAMETER_SCHEMA_LIST = "parameter_schema.list"  # nosec B105 — permission scope name, not a credential
_SCOPE_HITL_LIST = "hitl.list"  # nosec B105 — permission scope name, not a credential
_SCOPE_PIPELINE_UPDATE = "pipeline.update"  # nosec B105 — permission scope name, not a credential
_SCOPE_EVAL_LIST = "eval.list"  # nosec B105 — permission scope name, not a credential

_TOOL_REQUIREMENTS: dict[str, ToolRequirement] = {
    "trigger_pipeline": ToolRequirement("run.trigger"),
    "cancel_run": ToolRequirement("run.cancel"),
    "review_hitl": ToolRequirement("hitl.review"),
    "review_hitl:claim": ToolRequirement("hitl.claim"),
    "review_hitl:approve": ToolRequirement("hitl.approve"),
    "review_hitl:reject": ToolRequirement("hitl.reject"),
    "review_hitl:deliver_manual": ToolRequirement("hitl.deliver_manual"),
    "copy_library_primitive": ToolRequirement("library.copy"),
    "list_pending_hitl": ToolRequirement(_SCOPE_HITL_LIST),
    "list_hitl_reviews": ToolRequirement(_SCOPE_HITL_LIST),
    "get_hitl_review": ToolRequirement(_SCOPE_HITL_LIST),
    "get_pipeline_reviews": ToolRequirement("pipeline.graph.read"),
    "get_run_output": ToolRequirement("run.output"),
    "create_pipeline": ToolRequirement("pipeline.create"),
    "update_pipeline_graph": ToolRequirement("pipeline.graph.update"),
    "bind_connector_to_node": ToolRequirement("pipeline.bind_connector"),
    # FAR-1182: pipeline monthly spend circuit breaker threshold - same
    # permission key as the REST PATCH /pipelines/{id} that also sets it.
    "set_pipeline_circuit_breaker": ToolRequirement(_SCOPE_PIPELINE_UPDATE),
    # FAR-1161: accountability owners - same permission key as the REST
    # PATCH /pipelines/{id} that also sets them (eligibility runs in the
    # shared CRUD layer, identical to the REST path).
    "set_pipeline_owners": ToolRequirement(_SCOPE_PIPELINE_UPDATE),
    # FAR-1175 (ADR 043 S1): manual autonomy demote/promote - same permission
    # key as the REST POST /pipelines/{id}/autonomy/{demote,promote}; the
    # admin/owner gate is enforced in-handler on both surfaces.
    "demote_pipeline_autonomy": ToolRequirement(_SCOPE_PIPELINE_UPDATE),
    "promote_pipeline_autonomy": ToolRequirement(_SCOPE_PIPELINE_UPDATE),
    # FAR-1599: environment-profile binding - same permission key as the REST
    # PATCH /pipelines/{id} that also sets it (the shared FAR-1558 predicate
    # validates the binding on both surfaces).
    "update_pipeline": ToolRequirement(_SCOPE_PIPELINE_UPDATE),
    "create_model_backend": ToolRequirement("model_backend.create"),
    "list_runs": ToolRequirement("run.list"),
    "get_run_evals": ToolRequirement("run.evals"),
    "list_eval_definitions": ToolRequirement(_SCOPE_EVAL_LIST),
    "create_eval_definition": ToolRequirement("eval.definition.create"),
    "update_eval_definition": ToolRequirement("eval.definition.update"),
    "delete_eval_definition": ToolRequirement("eval.definition.delete"),
    "list_triggers": ToolRequirement("trigger.list"),
    "get_trigger": ToolRequirement("trigger.list"),
    "update_trigger": ToolRequirement("trigger.update"),
    "delete_trigger": ToolRequirement("trigger.delete"),
    "set_org_triggers_paused": ToolRequirement("org.triggers.pause.manage"),
    # FAR-1879/FAR-1930: both MCP housekeeping tools mirror their REST twins
    # (GET /api/v1/admin/housekeeping and POST /api/v1/admin/housekeeping/cleanup),
    # each gated by BOTH ``require_permission("housekeeping.manage")`` (admin
    # org-role floor) and ``require_system_permission(...)`` (a strict
    # is_system_admin gate with no org-role fall-through). Both requirements are
    # declared on these ONE entries; the resolver's system-admin leg enforces
    # the claim. A new system-admin-gated tool declares it on its single entry
    # here — never with an in-body check.
    "list_housekeeping": ToolRequirement("housekeeping.manage", system_admin=True),
    "perform_housekeeping": ToolRequirement("housekeeping.manage", system_admin=True),
    "create_connector": ToolRequirement("connector.create"),
    "delete_connector": ToolRequirement("connector.delete"),
    "create_trigger": ToolRequirement("trigger.create"),
    "delete_pipeline": ToolRequirement("pipeline.delete"),
    "create_agent": ToolRequirement("agent.create"),
    "create_schema": ToolRequirement("schema.create"),
    "infer_schema": ToolRequirement("schema.infer"),
    "create_secret": ToolRequirement(_SCOPE_SECRET_MANAGE),
    "delete_secret": ToolRequirement(_SCOPE_SECRET_MANAGE),
    "list_secrets": ToolRequirement(_SCOPE_SECRET_MANAGE),
    "create_api_key": ToolRequirement("api_key.create"),
    # Listing keys exposes the owning account's human display name/email, so it
    # uses the same operator floor as the REST list endpoint.
    "list_api_keys": ToolRequirement("api_key.list"),
    "revoke_api_key": ToolRequirement("api_key.revoke"),
    # FAR-614: the first caller-scoped (``.self``) MCP tools — get/set the
    # CALLER's own HITL email-alert preference. Target = the caller's account
    # by construction; no target parameter exists to misuse.
    "get_hitl_email_alerts": ToolRequirement("hitl_email.self"),
    "set_hitl_email_alerts": ToolRequirement("hitl_email.self"),
    "list_trigger_events": ToolRequirement("trigger.events.list"),
    "query_analytics": ToolRequirement("analytics.query"),
    "query_analytics_concurrency": ToolRequirement("analytics.query"),
    # FAR-695: read/list tools for the build-time entities that previously had
    # MCP write surfaces only. Each maps to the SAME permission key as the
    # corresponding REST route (agent.list / connector.list / etc.).
    "list_agents": ToolRequirement("agent.list"),
    "get_agent": ToolRequirement("agent.list"),
    "list_connectors": ToolRequirement(_SCOPE_CONNECTOR_LIST),
    "get_connector": ToolRequirement(_SCOPE_CONNECTOR_LIST),
    "list_connector_types": ToolRequirement(_SCOPE_CONNECTOR_LIST),
    "list_model_backends": ToolRequirement("model_backend.list"),
    "get_model_backend": ToolRequirement("model_backend.list"),
    "list_environment_profiles": ToolRequirement("environment_profile.list"),
    "list_parameter_schemas": ToolRequirement(_SCOPE_PARAMETER_SCHEMA_LIST),
    "create_parameter_schema": ToolRequirement("parameter_schema.create"),
    "get_parameter_schema": ToolRequirement(_SCOPE_PARAMETER_SCHEMA_LIST),
    "update_parameter_schema": ToolRequirement("parameter_schema.update"),
    "delete_parameter_schema": ToolRequirement("parameter_schema.delete"),
    "restore_parameter_schema": ToolRequirement("parameter_schema.update"),
    "get_parameter_schema_references": ToolRequirement(_SCOPE_PARAMETER_SCHEMA_LIST),
    "validate_parameter_schema": ToolRequirement("parameter_schema.validate"),
    "list_parameter_sets": ToolRequirement(_SCOPE_PARAMETER_SCHEMA_LIST),
    "create_parameter_set": ToolRequirement("parameter_schema.set.create"),
    "get_parameter_set": ToolRequirement(_SCOPE_PARAMETER_SCHEMA_LIST),
    "update_parameter_set": ToolRequirement("parameter_schema.set.update"),
    "delete_parameter_set": ToolRequirement("parameter_schema.set.delete"),
    "restore_parameter_set": ToolRequirement("parameter_schema.set.update"),
    # FAR-696: governance-entity read/list tools. Each maps to the SAME
    # permission key as the corresponding REST route (team.list /
    # user.list / audit.manage / admin.notification.manage /
    # lifecycle_map.list / plugin.list / sso.manage / eval.list /
    # schema.list / pipeline.list).
    "list_teams": ToolRequirement("team.list"),
    "list_users": ToolRequirement("user.list"),
    "get_audit_log": ToolRequirement("audit.manage"),
    "list_notification_deliveries": ToolRequirement("admin.notification.manage"),
    "list_lifecycle_maps": ToolRequirement("lifecycle_map.list"),
    "get_lifecycle_map": ToolRequirement("lifecycle_map.list"),
    "list_plugins": ToolRequirement("plugin.list"),
    "list_sso_providers": ToolRequirement("sso.manage"),
    "list_eval_suites": ToolRequirement(_SCOPE_EVAL_LIST),
    "get_eval_suite": ToolRequirement(_SCOPE_EVAL_LIST),
    "get_schema_versions": ToolRequirement("schema.list"),
    "get_pipeline_diff": ToolRequirement("pipeline.list"),
    # Explicit read-only tools (pinned at viewer via ``resource.read_only``).
    # Unmapped mutating tools FAIL under deny-by-default; an unmapped read-only
    # tool is declared here with NO permission key instead.
    "list_pipelines": ToolRequirement(),
    "get_pipeline_graph": ToolRequirement(),
    "get_run_status": ToolRequirement(),
    "search_library": ToolRequirement(),
    "search_documentation": ToolRequirement(),
    "get_integration_status": ToolRequirement(),
    # FAR-1942: ``get_org_config`` mirrors the SYSTEM-ADMIN-ONLY REST route
    # ``GET /api/v1/system-admin/config``, gated by
    # ``require_system_permission("system.config.manage")`` — a strict
    # ``is_system_admin`` claim with NO org-role fall-through. Pinning it
    # read-only at the role leg (like ``list_feature_flags``, which mirrors the
    # same permission) preserves that parity: adding the permission key would
    # impose an org-role floor REST does not have. The claim is enforced by the
    # resolver's system-admin leg; the handler routes through
    # ``_check_agent_tool_scope`` so the declaration is LIVE (FAR-1930).
    "get_org_config": ToolRequirement(system_admin=True),
    "get_available_features": ToolRequirement(),
    "list_schemas": ToolRequirement(),
    "validate_payload": ToolRequirement(),
    # FAR-696/FAR-1930: the REST route's real gate is the strict
    # ``require_system_permission`` check (no org-role fall-through). The tool
    # is pinned read-only at the role leg — mapping it to the
    # ``system.config.manage`` placeholder role would let an org admin through —
    # AND carries the system-admin claim; both declared on this ONE entry.
    "list_feature_flags": ToolRequirement(system_admin=True),
}

# Derived view: mapped tool/action key -> permission key. Kept as the public
# ``Mapping[str, str]`` so every existing consumer (capability_scope, the role
# parity tests, per-leg resolution) is unchanged.
_TOOL_SCOPE_REQUIREMENTS: dict[str, str] = {
    key: req.permission_key for key, req in _TOOL_REQUIREMENTS.items() if req.permission_key is not None
}
TOOL_SCOPE_REQUIREMENTS: types.MappingProxyType[str, str] = types.MappingProxyType(_TOOL_SCOPE_REQUIREMENTS)

# Derived view: tools with no permission key are the explicit read-only
# allowlist (pinned at viewer).
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    key for key, req in _TOOL_REQUIREMENTS.items() if req.permission_key is None
)

# Derived view (FAR-1930): tools whose REST twin is guarded by
# ``require_system_permission`` — a STRICT ``is_system_admin`` claim with NO
# org-role fall-through. ``resolve_tool_access`` enforces it as a deny-only,
# kill-switch-ineligible leg, so a tool cannot ship the requirement without the
# gate. Distinct from the org-role floor: ``list_housekeeping`` /
# ``perform_housekeeping`` carry BOTH fields, while ``list_feature_flags`` is
# read-only at the role leg and carries only the claim.
SYSTEM_ADMIN_TOOLS: frozenset[str] = frozenset(key for key, req in _TOOL_REQUIREMENTS.items() if req.system_admin)


def _tool_requires_system_admin(tool: str) -> bool:
    """True when ``tool``'s registry entry declares the system-admin claim (FAR-1930).

    Module-private: the declaration is read through ``SYSTEM_ADMIN_TOOLS`` (or
    the registry entry) and the authoritative access decision is made by
    ``resolve_tool_access`` — no external surface consumes this lookup.
    """
    return tool in SYSTEM_ADMIN_TOOLS


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


# FAR-1930: the resolver reports WHICH leg denied (or ``None`` when allowed),
# so the caller selects the exception subclass and log line from the decision
# itself — never by re-running the resolver (which would re-read live grant
# state and depend on an unstated leg-ordering invariant).
_LEG_ALLOWED_TOOLS = "allowed_tools"
_LEG_UNMAPPED = "unmapped"
_LEG_CALLER_SCOPE = "caller_scope"
_LEG_ROLE = "role"
_LEG_GRANTS = "grants"
_LEG_SYSTEM_ADMIN = "system_admin"


def _resolve_tool_access_detail(
    *,
    tool: str,
    action: str | None,
    role: str | None,
    key_scope: str | None,
    auth_type: str | None,
    allowed_tools: set[str] | None,
    kill_switch: bool,
    grants: frozenset[str] | None,
    is_system_admin: bool,
) -> tuple[bool, str, str | None]:
    """Resolve one MCP call and report the failing leg (FAR-1930).

    Returns ``(allowed, permission_key, denied_leg)``; ``denied_leg`` is
    ``None`` when allowed, otherwise one of the ``_LEG_*`` constants. This is
    the authoritative six-leg decision; ``resolve_tool_access`` is the
    two-value public wrapper and ``check_tool_scope`` reads the leg from HERE
    so it never re-resolves (and so never depends on the leg order).
    """
    # Leg 1: node-level allowed_tools narrowing. When the node's
    # capability_scope declares an allow-list it is an ADDITIONAL filter —
    # the role must still permit the tool. Absent (None) = UNRESTRICTED;
    # an explicit EMPTY set is deny-by-default.
    if allowed_tools is not None and tool not in allowed_tools:
        return False, "", _LEG_ALLOWED_TOOLS

    # Leg 2: resolution. Unmapped + not read-only ⇒ deny (deny-by-default).
    permission_key = _permission_key_for(tool, action)
    if permission_key is None:
        return False, "", _LEG_UNMAPPED

    # Leg 3: caller-scope (kill-switch-ineligible).
    classification = classify_caller_scope(tool, permission_key)
    if classification == _CALLER_SCOPE_CALLER and key_scope != "user":
        return False, permission_key, _LEG_CALLER_SCOPE
    if classification == _CALLER_SCOPE_ORG_ONLY and key_scope == "user" and auth_type == "api_key":
        return False, permission_key, _LEG_CALLER_SCOPE

    # Leg 4: role hierarchy (kill-switch-eligible). Fail-closed on a
    # missing/unknown role regardless of the kill switch, mirroring
    # ``assert_org_role``'s identity checks.
    required = resolve_required(permission_key)
    if role is None or not isinstance(role, str) or not role:
        return False, permission_key, _LEG_ROLE
    actual_level = ORG_ROLE_HIERARCHY.get(role.strip().lower())
    if actual_level is None:
        return False, permission_key, _LEG_ROLE
    if kill_switch and actual_level < ORG_ROLE_HIERARCHY[required]:
        return False, permission_key, _LEG_ROLE
    # Leg 5 (FAR-1477): grant-set membership + live non-delegable exclusion.
    if not grants_permit(grants, permission_key):
        return False, permission_key, _LEG_GRANTS
    # Leg 6 (FAR-1930): system-admin requirement. A strict identity claim with
    # no org-role fall-through, so the org-role kill switch never bypasses it.
    # Last, and gated on the per-tool declaration, so a tool outside
    # ``SYSTEM_ADMIN_TOOLS`` is wholly unaffected by the flag.
    if _tool_requires_system_admin(tool) and not is_system_admin:
        return False, permission_key, _LEG_SYSTEM_ADMIN
    return True, permission_key, None


def resolve_tool_access(
    tool: str,
    action: str | None,
    role: str | None,
    key_scope: str | None,
    auth_type: str | None,
    allowed_tools: set[str] | None,
    kill_switch: bool,
    grants: frozenset[str] | None = None,
    is_system_admin: bool = False,
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
            JWT/Assistant session), ``'oauth'``, or ``None`` (unset).
        allowed_tools: normalised node-level allow-list (``None`` =
            UNRESTRICTED; an explicit empty set is deny-by-default).
        kill_switch: True when the org's authz-enforce kill switch is ON
            (hierarchy enforced); False fail-opens ONLY the role leg.
        grants: FAR-1477 API-key grant-set (``None`` = legacy role bundle, no
            narrowing; empty = deny-all). A deny-only, kill-switch-INELIGIBLE
            leg: ``effective = grants INTERSECT bundle(live_role)``.
        is_system_admin: the caller's live ``is_system_admin`` claim
            (FAR-1930). Required for any tool declared in
            ``SYSTEM_ADMIN_TOOLS``; defaults False (fail-closed), so a caller
            that never resolved the claim cannot reach a system-admin tool.

    Returns ``(allowed, permission_key)``. ``permission_key`` is the resolved
    key for error/log detail, or ``""`` when the tool could not be resolved.
    Delegates to ``_resolve_tool_access_detail``, which additionally reports
    the failing leg.

    Composes six deny-only legs in a fixed order (never widen):

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
    5. grant-set leg (FAR-1477) — runs after the role leg:
       ``grants_permit`` requires the permission to be in the key's grant-set
       and still delegable (live exclusion read). KILL-SWITCH-INELIGIBLE: it
       applies even when the authz kill switch is OFF, because a grant-set
       only ever narrows. ``grants is None`` is a no-op.
    6. system-admin leg (FAR-1930) — runs LAST: a tool declared in
       ``SYSTEM_ADMIN_TOOLS`` requires ``is_system_admin`` (a strict identity
       claim, mirroring REST's ``require_system_permission``).
       KILL-SWITCH-INELIGIBLE (identity, like the caller-scope leg) and
       deny-only. It is last so a tool that also fails an earlier leg keeps
       that leg's existing denial reason; it fires only when every other leg
       passed. A tool NOT so declared ignores the flag entirely.
    """
    allowed, permission_key, _denied_leg = _resolve_tool_access_detail(
        tool=tool,
        action=action,
        role=role,
        key_scope=key_scope,
        auth_type=auth_type,
        allowed_tools=allowed_tools,
        kill_switch=kill_switch,
        grants=grants,
        is_system_admin=is_system_admin,
    )
    return allowed, permission_key


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
    grants: frozenset[str] | None = None,
    is_system_admin: bool = False,
) -> None:
    """Single tool-dispatch chokepoint (delegates to ``_resolve_tool_access_detail``).

    FAR-620: the DECISION is made by the pure resolver; this wrapper keeps the
    input validation, the ContextVar wiring (node allowed-tools fallback, the
    request-scoped authz-enforce kill switch) and the per-leg denial messages.
    ``key_scope`` / ``auth_type`` / ``is_system_admin`` arrive from the MCP
    middleware's ContextVars via ``_check_agent_tool_scope``; direct callers
    that omit them keep today's behaviour (key_scope None fails closed on
    caller-scoped tools — e.g. the FAR-614 ``.self`` tools
    ``get_hitl_email_alerts`` / ``set_hitl_email_alerts`` — the role/org legs
    are untouched, and ``is_system_admin`` defaults False so a system-admin
    tool is denied unless the caller passes the live claim).

    Raises ``MCPSystemAdminRequiredError`` (a subclass of
    ``MCPAuthorizationError``) when the ONLY failing leg is the FAR-1930
    system-admin requirement — so the MCP surface can attach its call-site
    warning while every existing ``except MCPAuthorizationError`` handler
    keeps working unchanged.
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

    kill_switch = authz_enforce_enabled()
    allowed, _permission_key, denied_leg = _resolve_tool_access_detail(
        tool=normalized,
        action=act,
        role=current_role,
        key_scope=key_scope,
        auth_type=auth_type,
        allowed_tools=allowed_set,
        kill_switch=kill_switch,
        grants=grants,
        is_system_admin=is_system_admin,
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
            grants=grants,
            is_system_admin=is_system_admin,
        )
        if denied_leg == _LEG_SYSTEM_ADMIN:
            # FAR-1930: the MCP adapter (``_check_agent_tool_scope``) owns the
            # single canonical warning for a system-admin refusal — it holds
            # the call-site context and emits the ``system_admin_required``
            # reason. The subclass selects that path; do NOT also log the
            # generic line here, or every denial double-logs.
            raise MCPSystemAdminRequiredError(message)
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
    grants: frozenset[str] | None = None,
    is_system_admin: bool = False,
) -> str:
    """Re-derive the specific denial message for a resolver denial.

    The pure resolver returns a bare False; the legacy per-leg error messages
    (pinned by the unit tests) are re-derived here in the same leg order the
    resolver evaluated them. Single-fault inputs make the first failing leg
    deterministic.
    """
    # Leg 1: node allowed_tools narrowing.
    if allowed_set is not None and normalized not in allowed_set:
        return _allowed_tools_denial_message(tool_name)

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
    # Leg 5: grant-set (FAR-1477).
    if not grants_permit(grants, permission_key):
        return f"Tool '{tool_name}' requires '{permission_key}', which is not granted to this API key"
    # Leg 6 (FAR-1930): system-admin requirement. Reached only when every other
    # leg passed; the detail matches the MCP refusal envelope verbatim.
    if _tool_requires_system_admin(normalized) and not is_system_admin:
        return f"{tool_name} requires system admin role"
    return f"Tool '{tool_name}' access denied"


def _allowed_tools_denial_message(tool_name: str) -> str:
    """The leg-1 (node ``allowed_tools`` narrowing) refusal message.

    Shared by ``_denial_message`` and ``check_node_allowed_tools`` so the two
    enforcement sites cannot diverge on the caller-facing text.
    """
    return f"Tool '{tool_name}' is outside the node's allowed_tools scope"


def check_node_allowed_tools(tool_name: str, allowed_tools: Sequence[str] | None = None) -> None:
    """Enforce ONLY the node ``allowed_tools`` leg of the tool-scope decision.

    FAR-1976: a handful of read-only MCP tools (the chokepoint-bypassing set
    pinned in ``tests/unit/test_mcp_structural_coverage.py``) declare no
    role/system-admin requirement, so their handlers never route through
    ``check_tool_scope`` — and therefore never reach its node-level
    ``allowed_tools`` narrowing leg either. Those handlers call this helper to
    apply EXACTLY that one leg, so a node's ``capability_scope.allowed_tools``
    bounds them too ("read-only" is not the same as "in scope").

    Deliberately does NOT re-derive the role, caller-scope, grant or
    system-admin legs: these tools declare none of them, and re-deriving them
    here would change their semantics.

    Args:
        tool_name: the tool name being dispatched (validated and normalised
            exactly as ``check_tool_scope`` does — strip + lower-case).
        allowed_tools: the resolved allow-list, or ``None`` to fall back to the
            request-scoped value (``get_request_allowed_tools()``) — the same
            source ``check_tool_scope`` reads. ``None`` (after the fallback) =
            UNRESTRICTED/legacy; an explicit empty sequence is
            deny-by-default.

    Raises:
        MCPAuthorizationError: when a node scope is present and ``tool_name``
            is not on it. The message is byte-identical to the chokepoint's
            leg-1 refusal.
    """
    if not isinstance(tool_name, str):
        raise MCPAuthorizationError("Tool name must be a string")

    normalized = _sanitize(tool_name, name="tool_name")

    if allowed_tools is None:
        allowed_tools = get_request_allowed_tools()
    if allowed_tools is None:
        return

    allowed_set = {_sanitize(t, name="allowed_tool") for t in allowed_tools}
    if normalized not in allowed_set:
        raise MCPAuthorizationError(_allowed_tools_denial_message(tool_name))
