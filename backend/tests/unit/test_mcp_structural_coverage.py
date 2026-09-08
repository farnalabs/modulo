"""MCP structural coverage — every registered tool is scoped (ADR 017).

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

from modulo.core.mcp.scope_validator import READ_ONLY_TOOLS, TOOL_SCOPE_REQUIREMENTS

_EXPECTED_TOOLS = frozenset(
    {
        "list_pipelines",
        "create_pipeline",
        "list_runs",
        "get_pipeline_graph",
        "update_pipeline_graph",
        "bind_connector_to_node",
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
        "list_hitl_gates",
        "get_hitl_gate",
        "get_pipeline_gates",
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
