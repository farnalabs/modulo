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


def test_create_api_key_classified_org_only() -> None:
    """Minting is an org-level operation: under a user-scoped key the
    ``create_api_key`` MCP tool is denied (user minting is REST-JWT-only)."""
    assert _classified_caller_scope("create_api_key") == _ORG_ONLY
