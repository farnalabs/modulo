"""MCP structural coverage — every registered tool is scoped (ADR 047).

Walks the FastMCP tool registry and asserts:
1. The registered tool-name set equals a pinned fixed list.
2. Every registered tool is either in ``TOOL_SCOPE_REQUIREMENTS`` (mutating,
   permission-key mapped) or on the explicit read-only allowlist (pinned at
   viewer).

The ``mcp`` package is pinned at ``>=1.28.1`` in ``pyproject.toml``; the
tool-name set is asserted equal to a fixed list so upgrades cannot silently
register unscoped tools.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from modulo.core.mcp.scope_validator import READ_ONLY_TOOLS, TOOL_SCOPE_REQUIREMENTS

_EXPECTED_TOOLS = frozenset(
    {
        "list_pipelines",
        "create_pipeline",
        "list_runs",
        "get_pipeline_graph",
        "update_pipeline_graph",
        "bind_connector_to_node",
        "set_pipeline_circuit_breaker",
        "set_pipeline_owners",
        # FAR-1175: manual autonomy demote/promote.
        "demote_pipeline_autonomy",
        "promote_pipeline_autonomy",
        "update_pipeline",
        "trigger_pipeline",
        "get_run_status",
        "get_run_output",
        "get_run_evals",
        "list_eval_definitions",
        "create_eval_definition",
        "update_eval_definition",
        "delete_eval_definition",
        "cancel_run",
        "list_pending_hitl",
        "list_hitl_reviews",
        "get_hitl_review",
        "get_pipeline_reviews",
        "review_hitl",
        "copy_library_primitive",
        "search_library",
        "list_trigger_events",
        "list_triggers",
        "get_trigger",
        "update_trigger",
        "delete_trigger",
        "set_org_triggers_paused",
        "create_model_backend",
        "create_connector",
        "create_trigger",
        "delete_pipeline",
        "delete_connector",
        "create_secret",
        "list_secrets",
        "delete_secret",
        "create_api_key",
        "list_api_keys",
        "revoke_api_key",
        "get_hitl_email_alerts",
        "set_hitl_email_alerts",
        "create_agent",
        "create_schema",
        "search_documentation",
        "get_integration_status",
        "get_org_config",
        "get_available_features",
        "list_schemas",
        "infer_schema",
        "validate_payload",
        "list_housekeeping",
        "perform_housekeeping",
        "query_analytics",
        "query_analytics_concurrency",
        # FAR-695: build-time entity read/list tools.
        "list_agents",
        "get_agent",
        "list_connectors",
        "get_connector",
        "list_connector_types",
        "list_model_backends",
        "get_model_backend",
        "list_environment_profiles",
        "list_parameter_schemas",
        "create_parameter_schema",
        "get_parameter_schema",
        "update_parameter_schema",
        "delete_parameter_schema",
        "restore_parameter_schema",
        "get_parameter_schema_references",
        "validate_parameter_schema",
        "list_parameter_sets",
        "create_parameter_set",
        "get_parameter_set",
        "update_parameter_set",
        "delete_parameter_set",
        "restore_parameter_set",
        # FAR-696: governance-entity read/list tools.
        "list_teams",
        "list_users",
        "list_feature_flags",
        "get_audit_log",
        "list_notification_deliveries",
        "list_lifecycle_maps",
        "get_lifecycle_map",
        "list_plugins",
        "list_sso_providers",
        "list_eval_suites",
        "get_eval_suite",
        "get_schema_versions",
        "get_pipeline_diff",
    }
)


def _registered_tool_names() -> set[str]:
    from modulo.api.mcp_server import mcp

    return set(mcp._tool_manager._tools.keys())


def test_registered_tool_names_equal_pinned_list() -> None:
    assert _registered_tool_names() == _EXPECTED_TOOLS


def test_every_registered_tool_is_scoped_or_read_only() -> None:
    registered = _registered_tool_names()
    for tool in registered:
        assert tool in TOOL_SCOPE_REQUIREMENTS or tool in READ_ONLY_TOOLS, (
            f"registered tool '{tool}' is neither permission-key mapped "
            "nor on the read-only allowlist — it would be deny-by-default"
        )


def test_mutating_tools_all_have_permission_keys() -> None:
    mutating = _EXPECTED_TOOLS - READ_ONLY_TOOLS
    for tool in mutating:
        assert tool in TOOL_SCOPE_REQUIREMENTS, f"mutating tool '{tool}' is unmapped"


def test_read_only_tools_not_in_requirements() -> None:
    for tool in READ_ONLY_TOOLS:
        assert tool not in TOOL_SCOPE_REQUIREMENTS, (
            f"read-only tool '{tool}' should live on the allowlist, not the requirement map"
        )


def test_scope_requirements_only_reference_registered_tools() -> None:
    registered = _registered_tool_names()
    for tool_key in TOOL_SCOPE_REQUIREMENTS:
        base = tool_key.split(":", 1)[0]
        assert base in registered, f"TOOL_SCOPE_REQUIREMENTS references unregistered tool '{base}'"


# ---------------------------------------------------------------------------
# FAR-620: caller-scope classification invariants
# ---------------------------------------------------------------------------

_CALLER_SCOPED = "caller-scoped"
_ORG_ONLY = "org-only"
_ANY = "any"
_VALID_CLASSIFICATIONS = {_ORG_ONLY, _CALLER_SCOPED, _ANY}


def _classified_caller_scope(tool: str) -> str:
    from modulo.core.mcp.scope_validator import classify_caller_scope

    permission_key = TOOL_SCOPE_REQUIREMENTS.get(tool)
    if permission_key is None and tool in READ_ONLY_TOOLS:
        permission_key = "resource.read_only"
    return classify_caller_scope(tool, permission_key)


def test_every_registered_tool_has_a_valid_caller_scope_classification() -> None:
    registered = _registered_tool_names()
    for tool in registered:
        classification = _classified_caller_scope(tool)
        assert classification in _VALID_CLASSIFICATIONS, (
            f"registered tool '{tool}' classified '{classification}' — must be one of {sorted(_VALID_CLASSIFICATIONS)}"
        )


def test_no_mutating_tool_classified_any() -> None:
    mutating = _EXPECTED_TOOLS - READ_ONLY_TOOLS
    for tool in mutating:
        classification = _classified_caller_scope(tool)
        assert classification != _ANY, (
            f"mutating tool '{tool}' classified '{_ANY}' — a mutating tool must never be callable by every caller scope"
        )


def test_caller_scope_requirements_reference_registered_tools() -> None:
    from modulo.core.mcp.scope_validator import CALLER_SCOPE_REQUIREMENTS

    registered = _registered_tool_names()
    for tool in CALLER_SCOPE_REQUIREMENTS:
        assert tool in registered, f"CALLER_SCOPE_REQUIREMENTS references unregistered tool '{tool}'"


def test_caller_scope_requirements_values_are_valid() -> None:
    from modulo.core.mcp.scope_validator import CALLER_SCOPE_REQUIREMENTS

    for tool, classification in CALLER_SCOPE_REQUIREMENTS.items():
        assert classification in _VALID_CLASSIFICATIONS, (
            f"CALLER_SCOPE_REQUIREMENTS['{tool}'] = '{classification}' is not a valid classification"
        )


def test_self_suffix_tools_are_registered_caller_scoped() -> None:
    """Stage 2 (FAR-620/FAR-614): every ``.self`` permission key belongs to a
    REGISTERED tool that classifies caller-scoped purely through the suffix
    derivation — no parallel classification set to keep in sync, and no
    dangling ``.self`` mapping without its tool."""
    from modulo.core.mcp.scope_validator import _CALLER_SCOPED_SUFFIX, classify_caller_scope

    for tool_key in TOOL_SCOPE_REQUIREMENTS:
        if not TOOL_SCOPE_REQUIREMENTS[tool_key].endswith(_CALLER_SCOPED_SUFFIX):
            continue
        base = tool_key.split(":", 1)[0]
        registered = _registered_tool_names()
        assert base in registered, f".self tool '{base}' mapped but not registered"
        assert classify_caller_scope(base, TOOL_SCOPE_REQUIREMENTS[tool_key]) == _CALLER_SCOPED, (
            f".self tool '{tool_key}' must classify caller-scoped via the suffix derivation"
        )


# ---------------------------------------------------------------------------
# FAR-620: the Account.preferences single-writer invariants (source scan)
#
# The account preferences blob is serialised ONLY by the row-locked helpers
# in db/crud/account.py. These structural scans keep the discipline
# enforceable: a new locked Account read, a new direct ``.preferences``
# write, or a new ``hitl_email`` literal outside the owner/reader pair fails
# here instead of resurfacing as a lost-update race in production.
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "modulo"

# The ONLY src files allowed to lock the Account row
# (``session.get(Account, ..., with_for_update=True)``):
# - db/crud/account.py: the shared preferences helpers (the lock IS the
#   preferences serialisation point);
# - api/routes/api_keys.py: the user-key quota read - LOCK-ONLY (it counts
#   keys and never touches ``Account.preferences``), whitelisted explicitly.
_ACCOUNT_LOCK_ALLOWED = frozenset({Path("db", "crud", "account.py"), Path("api", "routes", "api_keys.py")})

# The ONLY src files allowed to carry direct ``.preferences =`` writes: the
# two row-locked helpers in the db layer. me.py, in_app_notifications.py and
# the MCP surface all route through them.
_PREFERENCES_WRITE_ALLOWED = frozenset({Path("db", "crud", "account.py")})

# The ONLY src files allowed to mention the quoted ``"hitl_email"`` literal:
# the db layer (owns PREFERENCE_KEY + the single writer) and the core reader
# (re-imports the constant; its occurrence is the module docstring's shape
# example). Any OTHER literal site is a would-be writer that bypassed the
# helper contract.
_HITL_EMAIL_KEY_ALLOWED = frozenset({Path("db", "crud", "account.py"), Path("core", "hitl_email_alerts.py")})


def _iter_source_files() -> Iterator[Path]:
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def test_account_row_lock_only_in_approved_sites() -> None:
    """A locked Account read (session.get(Account, ..., with_for_update=True))
    appears ONLY in the shared preferences helper and the lock-only quota read.
    Any other locked Account read is a preferences-writer candidate that
    bypasses the single-writer discipline (FAR-620 spec item 14)."""
    pattern = re.compile(r"session\.get\(\s*Account\b[^)]*with_for_update\s*=\s*True")
    found: set[Path] = set()
    for path in _iter_source_files():
        if pattern.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(_SRC_ROOT))
    assert found == _ACCOUNT_LOCK_ALLOWED, (
        f"locked Account reads found outside the approved set: {sorted(map(str, found - _ACCOUNT_LOCK_ALLOWED))}; "
        f"approved sites missing: {sorted(map(str, _ACCOUNT_LOCK_ALLOWED - found))}"
    )


def test_account_preferences_writes_only_in_shared_helper() -> None:
    """Every direct ``.preferences =`` write lives ONLY in db/crud/account.py —
    the REST /me routes, the notifications dashboard writer and the MCP
    surface all mutate the blob through the row-locked helpers, never by
    assigning the column directly."""
    pattern = re.compile(r"\.preferences\s*=")
    found: set[Path] = set()
    for path in _iter_source_files():
        if pattern.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(_SRC_ROOT))
    assert found == _PREFERENCES_WRITE_ALLOWED, (
        f"direct Account.preferences writes found outside the shared helper: "
        f"{sorted(map(str, found - _PREFERENCES_WRITE_ALLOWED))}"
    )


def test_hitl_email_key_literal_confined_to_owner_and_reader() -> None:
    """The quoted ``"hitl_email"`` key literal appears ONLY in db/crud/account.py
    (the constant owner + single writer) and core/hitl_email_alerts.py (the
    re-importing reader). me.py routes through the helper - it (and every
    other surface) must never re-declare the key."""
    found: set[Path] = set()
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        if '"hitl_email"' in text or "'hitl_email'" in text:
            found.add(path.relative_to(_SRC_ROOT))
    assert found == _HITL_EMAIL_KEY_ALLOWED, (
        f"'hitl_email' literal found outside the owner/reader pair: {sorted(map(str, found - _HITL_EMAIL_KEY_ALLOWED))}"
    )


def test_create_api_key_classified_org_only() -> None:
    """Minting is an org-level operation: under a user-scoped key the
    ``create_api_key`` MCP tool is denied (user minting is REST-JWT-only)."""
    assert _classified_caller_scope("create_api_key") == _ORG_ONLY


# ---------------------------------------------------------------------------
# FAR-1930: the system-admin requirement is DECLARED once and ENFORCED by the
# shared resolver — never re-implemented per tool.
#
# Before centralisation each system-admin-gated tool carried its own in-body
# ``if not _ctx_is_system_admin_val(): return ...`` check. The requirement now
# lives in ``scope_validator.SYSTEM_ADMIN_TOOLS`` and the resolver enforces it,
# so a tool cannot ship the declaration without the gate. These guards walk the
# declaration and prove the gate is live; the can-fail test proves the guard
# itself is not vacuous.
# ---------------------------------------------------------------------------

_EXPECTED_SYSTEM_ADMIN_TOOLS = frozenset(
    {
        "list_feature_flags",
        "list_housekeeping",
        "perform_housekeeping",
    }
)


def _system_admin_declared_tools() -> frozenset[str]:
    """Read the declaration through the MODULE (so a monkeypatch is visible)
    rather than a bound import."""
    import modulo.core.mcp.scope_validator as sv

    return sv.SYSTEM_ADMIN_TOOLS


def _unenforced_system_admin_declarations(declared: frozenset[str]) -> list[str]:
    """Return declared tools whose system-admin requirement is NOT enforced.

    A live gate means: for a caller whose other legs all pass (an ``admin``
    org role, an org-wide JWT credential), the resolver ALLOWS the tool when
    the claim is present and DENIES it when the claim is absent. A declared
    tool that is unmapped — or whose resolver leg was removed — cannot satisfy
    the first condition, so it is reported.
    """
    from modulo.core.mcp.scope_validator import resolve_tool_access

    unenforced: list[str] = []
    for tool in sorted(declared):
        with_claim, _ = resolve_tool_access(
            tool=tool,
            action=None,
            role="admin",
            key_scope="org",
            auth_type="jwt",
            allowed_tools=None,
            kill_switch=True,
            is_system_admin=True,
        )
        without_claim, _ = resolve_tool_access(
            tool=tool,
            action=None,
            role="admin",
            key_scope="org",
            auth_type="jwt",
            allowed_tools=None,
            kill_switch=True,
            is_system_admin=False,
        )
        if not with_claim or without_claim:
            unenforced.append(tool)
    return unenforced


def test_system_admin_declaration_is_pinned() -> None:
    """The declared set is pinned so adding a system-admin-gated tool is a
    deliberate, reviewed change (not an accidental omission)."""
    assert _system_admin_declared_tools() == _EXPECTED_SYSTEM_ADMIN_TOOLS


def test_every_declared_system_admin_tool_is_registered() -> None:
    registered = _registered_tool_names()
    for tool in _system_admin_declared_tools():
        assert tool in registered, f"SYSTEM_ADMIN_TOOLS declares unregistered tool '{tool}'"


def test_every_declared_system_admin_tool_is_enforced_by_the_shared_resolver() -> None:
    """Every declared tool's requirement is enforced by ``resolve_tool_access``.

    A declaration with no live gate (the tool is unmapped, or the resolver leg
    was removed) fails here — the structural guard against shipping the
    declaration without the enforcement.
    """
    unenforced = _unenforced_system_admin_declarations(_system_admin_declared_tools())
    assert not unenforced, (
        f"declared system-admin tools with NO live enforcement in the shared "
        f"resolver: {unenforced} — declare the tool in SYSTEM_ADMIN_TOOLS only "
        f"when resolve_tool_access actually denies it without the claim"
    )


def test_structural_guard_catches_a_declaration_without_a_live_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the guard can fail: a declared tool that the resolver cannot gate
    (here, unmapped) is reported by ``_unenforced_system_admin_declarations``.

    Without this, the guard could pass vacuously for every declaration.
    """
    import modulo.core.mcp.scope_validator as sv

    monkeypatch.setattr(
        sv,
        "SYSTEM_ADMIN_TOOLS",
        sv.SYSTEM_ADMIN_TOOLS | {"not_a_real_tool"},
    )
    assert _unenforced_system_admin_declarations(sv.SYSTEM_ADMIN_TOOLS) == ["not_a_real_tool"]


def test_system_admin_flag_is_inert_for_every_undeclared_tool() -> None:
    """FAR-1930 blast-radius proof: the system-admin leg cannot change any tool
    OUTSIDE the declaration.

    The resolver is on the path of every MCP tool, so a centralised gate has to
    leave every other tool's effective access bit-identical. This enumerates
    every registered tool NOT in ``SYSTEM_ADMIN_TOOLS`` across the role/
    credential matrix and asserts the decision (allow/deny AND the resolved
    permission key) is identical whether the claim is absent or present.
    """
    from modulo.core.mcp.scope_validator import resolve_tool_access

    declared = _system_admin_declared_tools()
    undeclared = _registered_tool_names() - declared
    assert undeclared, "expected many tools outside the system-admin declaration"

    for tool in sorted(undeclared):
        for role in ("viewer", "runner", "operator", "admin"):
            for key_scope, auth_type in (("org", "api_key"), ("user", "jwt"), (None, None)):
                with_claim = resolve_tool_access(
                    tool=tool,
                    action=None,
                    role=role,
                    key_scope=key_scope,
                    auth_type=auth_type,
                    allowed_tools=None,
                    kill_switch=True,
                    is_system_admin=True,
                )
                without_claim = resolve_tool_access(
                    tool=tool,
                    action=None,
                    role=role,
                    key_scope=key_scope,
                    auth_type=auth_type,
                    allowed_tools=None,
                    kill_switch=True,
                    is_system_admin=False,
                )
                assert with_claim == without_claim, (
                    f"tool '{tool}' decision changed with the system-admin flag "
                    f"(role={role}, key_scope={key_scope}, auth_type={auth_type}); "
                    f"it does not declare a requirement and must be unaffected"
                )


def test_system_admin_flag_is_read_only_by_the_shared_dispatch_wrapper() -> None:
    """The ``_ctx_is_system_admin_val()`` ContextVar is read at exactly ONE
    call site in the MCP server — the shared dispatch wrapper
    ``_check_agent_tool_scope`` (plus its definition).

    A per-tool in-body gate (the duplicated shape FAR-1930 centralised away)
    reintroduces a second reader and fails here.
    """
    path = _SRC_ROOT / "api" / "mcp_server.py"
    call_sites = len(re.findall(r"_ctx_is_system_admin_val\(\)", path.read_text(encoding="utf-8")))
    assert call_sites == 2, (
        f"expected exactly one _ctx_is_system_admin_val() call site (the shared "
        f"wrapper) plus its definition in api/mcp_server.py, found {call_sites} — "
        f"a per-tool system-admin gate has crept back in; enforce the "
        f"requirement through SYSTEM_ADMIN_TOOLS instead"
    )
