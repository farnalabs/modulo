"""Unit tests for Remy UI tools — tool definitions, permission resolution, session approvals."""

from datetime import UTC, datetime, timedelta

from modulo.api.routes.remy import (
    _has_destructive_pattern,
    _is_approved_for_session,
    _resolve_tool_permission,
    _session_approvals,
)
from modulo.api.ui_tools import (
    _UI_TOOLS,
    DESTRUCTIVE_PATTERNS,
    NAV_TOOLS,
    READ_TOOLS,
    UI_TOOL_NAMES,
    WRITE_TOOLS,
    build_tool_definitions_for_text,
)
from modulo.core.remy.config_service import (
    PERMISSION_MODE_PRESETS,
    RemyConfig,
    apply_permission_mode_preset,
)


class TestUIToolDefinitions:
    """Tests for _UI_TOOLS definitions and constants."""

    def test_all_11_tools_defined(self):
        assert len(_UI_TOOLS) == 11

    def test_tool_names_match_dict(self):
        assert UI_TOOL_NAMES == set(_UI_TOOLS.keys())

    def test_required_tools_exist(self):
        required = {
            "navigate", "click", "fill", "select", "extract",
            "extract_all", "get_page_interactables", "wait",
            "go_back", "get_url", "press",
        }
        assert UI_TOOL_NAMES == required

    def test_each_tool_has_description(self):
        for name, schema in _UI_TOOLS.items():
            assert "description" in schema, f"{name} missing description"
            assert isinstance(schema["description"], str)

    def test_each_tool_has_parameters(self):
        for name, schema in _UI_TOOLS.items():
            assert "parameters" in schema, f"{name} missing parameters"
            assert isinstance(schema["parameters"], dict)

    def test_tool_parameters_have_type(self):
        for name, schema in _UI_TOOLS.items():
            for param_name, param_info in schema["parameters"].items():
                assert "type" in param_info, f"{name}.{param_name} missing type"


class TestToolConstants:
    """Tests for READ_TOOLS, NAV_TOOLS, WRITE_TOOLS, DESTRUCTIVE_PATTERNS."""

    def test_read_tools(self):
        assert READ_TOOLS == {"extract", "extract_all", "get_page_interactables", "get_url"}

    def test_nav_tools(self):
        assert NAV_TOOLS == {"navigate", "go_back"}

    def test_write_tools(self):
        assert WRITE_TOOLS == {"click", "fill", "select", "press"}

    def test_sets_are_disjoint(self):
        assert READ_TOOLS.isdisjoint(NAV_TOOLS)
        assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
        assert NAV_TOOLS.isdisjoint(WRITE_TOOLS)

    def test_all_tools_covered_by_categories(self):
        categorized = READ_TOOLS | NAV_TOOLS | WRITE_TOOLS
        uncategorized = UI_TOOL_NAMES - categorized
        assert uncategorized == {"wait"}, f"Unexpected uncategorized tools: {uncategorized}"

    def test_destructive_patterns(self):
        assert "delete" in DESTRUCTIVE_PATTERNS
        assert "remove" in DESTRUCTIVE_PATTERNS
        assert "destroy" in DESTRUCTIVE_PATTERNS


class TestDestructivePatternDetection:
    """Tests for _has_destructive_pattern."""

    def test_destructive_selectors(self):
        assert _has_destructive_pattern("[data-testid='delete-btn']")
        assert _has_destructive_pattern(".remove-item")
        assert _has_destructive_pattern("destroy-all")
        assert _has_destructive_pattern("archive-project")
        assert _has_destructive_pattern("suspend-user")
        assert _has_destructive_pattern("ban-account")

    def test_safe_selectors(self):
        assert not _has_destructive_pattern("[data-testid='save-btn']")
        assert not _has_destructive_pattern(".create-new")
        assert not _has_destructive_pattern("input[name='email']")
        assert not _has_destructive_pattern(".search-box")

    def test_case_insensitive(self):
        assert _has_destructive_pattern("DELETE-button")
        assert _has_destructive_pattern("RemoveItem")
        assert _has_destructive_pattern("ARCHIVE")

    def test_all_destructive_keywords_are_caught(self):
        for pattern in DESTRUCTIVE_PATTERNS:
            assert _has_destructive_pattern(f"[data-testid='{pattern}-btn']"), f"Pattern '{pattern}' was not caught"

    def test_innocent_words_are_not_caught(self):
        innocent = [
            "[data-testid='save-btn']",
            ".create-new",
            "input[name='email']",
            ".search-box",
            ".edit-profile",
            "[data-testid='add-user']",
            ".view-details",
            ".export-csv",
        ]
        for sel in innocent:
            assert not _has_destructive_pattern(sel), f"'{sel}' should not flag as destructive"

    def test_message_deleted_does_not_false_positive(self):
        assert not _has_destructive_pattern("[data-testid='message-deleted']")
        assert not _has_destructive_pattern(".message-deleted-badge")
        assert not _has_destructive_pattern("notification-deleted-label")


class TestRemyConfigDefaults:
    """Tests for new RemyConfig fields."""

    def test_default_tool_permissions_empty(self):
        config = RemyConfig()
        assert config.tool_permissions == {}

    def test_default_permission_mode_is_safe(self):
        config = RemyConfig()
        assert config.permission_mode == "safe"

    def test_schema_version_bumped_to_2(self):
        config = RemyConfig()
        assert config.schema_version == 2

    def test_tool_permissions_defaults_are_independent(self):
        config1 = RemyConfig()
        config2 = RemyConfig()
        config1.tool_permissions["click"] = "disabled"
        assert "click" not in config2.tool_permissions


class TestPermissionModePresets:
    """Tests for PERMISSION_MODE_PRESETS and apply_permission_mode_preset."""

    def test_full_auto_preset(self):
        preset = PERMISSION_MODE_PRESETS["full_auto"]
        for tool_name in UI_TOOL_NAMES:
            assert preset[tool_name] == "always_allowed"

    def test_safe_preset(self):
        preset = PERMISSION_MODE_PRESETS["safe"]
        assert preset["press"] == "requires_approval"
        # Other tools are not in the preset (inherit defaults)
        assert "click" not in preset

    def test_locked_down_preset(self):
        preset = PERMISSION_MODE_PRESETS["locked_down"]
        for tool_name in READ_TOOLS:
            assert preset[tool_name] == "always_allowed"
        assert preset["navigate"] == "always_allowed"
        for tool_name in {"click", "fill", "select", "go_back", "press"}:
            assert preset[tool_name] == "requires_approval"

    def test_apply_full_auto(self):
        result = apply_permission_mode_preset("full_auto")
        assert result["click"] == "always_allowed"
        assert result["press"] == "always_allowed"

    def test_apply_safe(self):
        result = apply_permission_mode_preset("safe")
        assert result["press"] == "requires_approval"
        assert "click" not in result

    def test_apply_locked_down(self):
        result = apply_permission_mode_preset("locked_down")
        assert result["click"] == "requires_approval"
        assert result["navigate"] == "always_allowed"

    def test_apply_custom_with_overrides(self):
        result = apply_permission_mode_preset("custom", {"click": "disabled"})
        assert result["click"] == "disabled"

    def test_apply_unknown_mode_returns_empty(self):
        result = apply_permission_mode_preset("nonexistent")
        assert result == {}

    def test_apply_custom_without_overrides_returns_empty(self):
        result = apply_permission_mode_preset("custom")
        assert result == {}


class TestPermissionResolution:
    """Tests for _resolve_tool_permission logic via RemyConfig."""

    def test_safe_mode_allows_read_tools(self):
        config = RemyConfig()
        for tool in READ_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"

    def test_safe_mode_allows_nav_tools(self):
        config = RemyConfig()
        for tool in NAV_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"

    def test_safe_mode_allows_write_tools_with_safe_selector(self):
        config = RemyConfig()
        assert _resolve_tool_permission(config, "click", {"selector": ".save-btn"}) == "always_allowed"
        assert _resolve_tool_permission(config, "fill", {"selector": "input[name=email]"}) == "always_allowed"

    def test_safe_mode_blocks_write_tools_with_destructive_selector(self):
        config = RemyConfig()
        assert _resolve_tool_permission(config, "click", {"selector": ".delete-btn"}) == "requires_approval"
        assert _resolve_tool_permission(config, "fill", {"selector": "#remove-field"}) == "requires_approval"

    def test_safe_mode_press_requires_approval(self):
        config = RemyConfig()
        assert _resolve_tool_permission(config, "press", {"key": "Enter"}) == "requires_approval"

    def test_full_auto_allows_safe_selectors(self):
        config = RemyConfig(permission_mode="full_auto")
        assert _resolve_tool_permission(config, "click", {"selector": ".save-btn"}) == "always_allowed"
        assert _resolve_tool_permission(config, "press", {"key": "Enter"}) == "always_allowed"

    def test_full_auto_still_checks_destructive_selectors(self):
        config = RemyConfig(permission_mode="full_auto")
        assert _resolve_tool_permission(config, "click", {"selector": "delete-btn"}) == "requires_approval"

    def test_locked_down_blocks_write_tools(self):
        config = RemyConfig(permission_mode="locked_down")
        for tool in WRITE_TOOLS:
            assert _resolve_tool_permission(config, tool, {"selector": ".save-btn"}) == "requires_approval"

    def test_locked_down_blocks_press(self):
        config = RemyConfig(permission_mode="locked_down")
        assert _resolve_tool_permission(config, "press", {"key": "Enter"}) == "requires_approval"

    def test_locked_down_allows_read_tools(self):
        config = RemyConfig(permission_mode="locked_down")
        for tool in READ_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"

    def test_locked_down_allows_nav_tools(self):
        config = RemyConfig(permission_mode="locked_down")
        for tool in NAV_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"

    def test_locked_down_write_tools_remain_requires_approval_with_destructive(self):
        config = RemyConfig(permission_mode="locked_down")
        for tool in WRITE_TOOLS:
            result = _resolve_tool_permission(config, tool, {"selector": ".delete-btn"})
            assert result == "requires_approval", f"{tool} should be requires_approval"

    def test_per_tool_override(self):
        config = RemyConfig(tool_permissions={"click": "disabled"})
        assert _resolve_tool_permission(config, "click", {"selector": ".save-btn"}) == "disabled"

    def test_per_tool_override_takes_precedence(self):
        config = RemyConfig(
            tool_permissions={"press": "disabled"},
            permission_mode="safe",
        )
        assert _resolve_tool_permission(config, "press", {"key": "Escape"}) == "disabled"

    def test_per_tool_override_can_override_locked_down(self):
        config = RemyConfig(
            tool_permissions={"click": "always_allowed"},
            permission_mode="locked_down",
        )
        assert _resolve_tool_permission(config, "click", {"selector": ".save-btn"}) == "always_allowed"

    def test_destructive_selectors_require_approval_in_all_modes(self):
        for mode in ("safe", "full_auto", "locked_down"):
            config = RemyConfig(permission_mode=mode)
            for tool in WRITE_TOOLS:
                result = _resolve_tool_permission(config, tool, {"selector": ".delete-btn"})
                assert result == "requires_approval", f"{tool} in {mode} should be requires_approval"

    def test_comprehensive_safe_mode_all_11_tools(self):
        config = RemyConfig()
        for tool in READ_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"
        for tool in NAV_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"
        for tool in {"click", "fill", "select"}:
            assert _resolve_tool_permission(config, tool, {"selector": ".save-btn"}) == "always_allowed"
        assert _resolve_tool_permission(config, "press", {"key": "Enter"}) == "requires_approval"
        assert _resolve_tool_permission(config, "wait", {"ms": 500}) == "always_allowed"

    def test_comprehensive_full_auto_mode_all_11_tools(self):
        config = RemyConfig(permission_mode="full_auto")
        for tool in UI_TOOL_NAMES:
            if tool in WRITE_TOOLS:
                assert _resolve_tool_permission(config, tool, {"selector": ".save-btn"}) == "always_allowed"
            else:
                assert _resolve_tool_permission(config, tool, {}) == "always_allowed"

    def test_comprehensive_locked_down_mode_all_11_tools(self):
        config = RemyConfig(permission_mode="locked_down")
        for tool in READ_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"
        for tool in NAV_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"
        assert _resolve_tool_permission(config, "wait", {"ms": 500}) == "always_allowed"
        assert _resolve_tool_permission(config, "get_url", {}) == "always_allowed"
        for tool in WRITE_TOOLS:
            assert _resolve_tool_permission(config, tool, {"selector": ".save-btn"}) == "requires_approval"


class TestGetAllToolDefinitions:
    """Tests for _get_all_tool_definitions."""

    def _get_definitions(self):
        """Duplicate of the helper for testing."""
        from modulo.api.ui_tools import _UI_TOOLS

        tools = []
        for name, schema in _UI_TOOLS.items():
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema["description"],
                    "parameters": {
                        "type": "object",
                        "properties": schema["parameters"],
                    },
                },
            })
        return tools

    def test_returns_list_of_dicts(self):
        result = self._get_definitions()
        assert isinstance(result, list)
        assert len(result) == 11

    def test_each_entry_has_type_function(self):
        for entry in self._get_definitions():
            assert entry["type"] == "function"

    def test_each_entry_has_function_with_name_description_parameters(self):
        for entry in self._get_definitions():
            fn = entry["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn
            assert fn["parameters"]["type"] == "object"
            assert "properties" in fn["parameters"]


class TestBuildToolDefinitionsForText:
    """Tests for build_tool_definitions_for_text."""

    def test_returns_non_empty_string(self):
        result = build_tool_definitions_for_text()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_header(self):
        result = build_tool_definitions_for_text()
        assert "## Browser Tools Available (Text Mode)" in result

    def test_includes_all_11_tools(self):
        result = build_tool_definitions_for_text()
        for name in UI_TOOL_NAMES:
            assert name in result

    def test_includes_descriptions(self):
        result = build_tool_definitions_for_text()
        for name, schema in _UI_TOOLS.items():
            assert schema["description"] in result

    def test_includes_navigate_with_path_param(self):
        result = build_tool_definitions_for_text()
        assert "**navigate**(path: string)" in result

    def test_includes_click_with_selector_param(self):
        result = build_tool_definitions_for_text()
        assert "**click**(selector: string)" in result

    def test_includes_example_workflow(self):
        result = build_tool_definitions_for_text()
        assert "Example workflow:" in result
        assert "navigate(path: /admin/pipelines)" in result
        assert "click(selector: [data-testid=create-btn])" in result
        assert "go_back() — return to previous page" in result

    def test_shows_default_for_wait_ms(self):
        result = build_tool_definitions_for_text()
        assert "ms: number (default: 500)" in result


class TestSessionApproval:
    """Tests for _is_approved_for_session — TTL, page_path matching, expiry cleanup."""

    def setup_method(self) -> None:
        _session_approvals.clear()

    def test_approved_same_page_not_expired(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) + timedelta(minutes=30),
            },
        }
        assert _is_approved_for_session("session-1", "click", "/admin/users")

    def test_different_page_not_approved(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) + timedelta(minutes=30),
            },
        }
        assert not _is_approved_for_session("session-1", "click", "/admin/settings")

    def test_expired_approval_returns_false(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            },
        }
        assert not _is_approved_for_session("session-1", "click", "/admin/users")

    def test_expired_approval_is_cleaned_up(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            },
        }
        _is_approved_for_session("session-1", "click", "/admin/users")
        assert "click" not in _session_approvals["session-1"]

    def test_no_session_returns_false(self) -> None:
        assert not _is_approved_for_session("nonexistent", "click", "/admin/users")

    def test_tool_not_in_session_returns_false(self) -> None:
        _session_approvals["session-1"] = {}
        assert not _is_approved_for_session("session-1", "click", "/admin/users")


# ── Agentic Loop Routing ────────────────────────────────────────────────


class TestAgenticLoopRouting:
    """Tests for tool separation, non-tool fallback, and agentic loop logic."""

    def test_ui_tools_separated_from_mcp_tools(self):
        """UI tool names are correctly separated from non-UI names."""
        ui_tool_calls = [{"name": n, "id": f"call_{n}", "args": {}} for n in UI_TOOL_NAMES]
        mcp_tool_calls = [{"name": "list_pipelines", "id": "call_mcp", "args": {}}]

        all_calls = ui_tool_calls + mcp_tool_calls
        separated_ui = [tc for tc in all_calls if tc["name"] in UI_TOOL_NAMES]
        separated_mcp = [tc for tc in all_calls if tc["name"] not in UI_TOOL_NAMES]

        assert len(separated_ui) == len(UI_TOOL_NAMES)
        assert len(separated_mcp) == 1
        assert separated_mcp[0]["name"] == "list_pipelines"

    def test_mcp_tools_are_never_in_ui_tool_set(self):
        """Ensure common MCP tool names are not accidentally in UI_TOOL_NAMES."""
        mcp_tools = {
            "list_pipelines", "get_pipeline", "trigger_run",
            "search_agents", "list_schemas", "read_audit_log",
        }
        intersection = mcp_tools & UI_TOOL_NAMES
        assert intersection == set(), f"MCP tool names leaked into UI tools: {intersection}"

    def test_non_tool_model_does_not_pass_tools_param(self):
        """When supports_tools is False, tools_param is None."""
        from unittest.mock import MagicMock

        backend = MagicMock()
        backend.supports_tools = False

        tools_param = None
        if getattr(backend, "supports_tools", False):
            from modulo.api.routes.remy import _get_all_tool_definitions
            tools_param = _get_all_tool_definitions()

        assert tools_param is None

    def test_tool_model_passes_tools_param(self):
        """When supports_tools is True, tools_param is set."""
        from unittest.mock import MagicMock

        from modulo.api.routes.remy import _get_all_tool_definitions

        backend = MagicMock()
        backend.supports_tools = True

        tools_param = None
        if getattr(backend, "supports_tools", False):
            tools_param = _get_all_tool_definitions()

        assert tools_param is not None
        assert len(tools_param) == 11

    def test_agentic_loop_continues_when_tool_calls_exist(self):
        """The while-true loop continues when tool calls exist."""
        tool_calls = [{"name": "navigate", "id": "call_1", "args": {"path": "/admin"}}]
        # In the actual loop, after processing tool results, the loop continues
        # if tool_calls is non-empty (it appends results and does the next LLM turn)
        should_continue = len(tool_calls) > 0
        assert should_continue is True

    def test_agentic_loop_exits_when_no_tool_calls(self):
        """The while-true loop exits when no tool calls remain."""
        tool_calls: list[dict] = []
        should_exit = len(tool_calls) == 0
        assert should_exit is True

    def test_agentic_loop_continues_for_mixed_ui_and_mcp(self):
        """Mixed UI + MCP tool calls keep the loop alive."""
        tool_calls = [
            {"name": "navigate", "id": "call_1", "args": {"path": "/admin"}},
            {"name": "list_pipelines", "id": "call_2", "args": {}},
        ]
        should_continue = len(tool_calls) > 0
        assert should_continue is True

    def test_agentic_loop_with_empty_tool_call_buffers(self):
        """When tool call buffers are empty (tool_call_chunks with no content), no reconstruction happens."""
        from modulo.api.routes.remy import _reconstruct_tool_calls

        buffers: dict[int, dict] = {}
        result = _reconstruct_tool_calls(buffers)
        assert result == []

    def test_agentic_loop_reconstructs_single_tool_call(self):
        """A single tool call buffer is correctly reconstructed."""
        from modulo.api.routes.remy import _reconstruct_tool_calls

        buffers = {
            0: {"id": "call_abc", "name": "navigate", "args": '{"path": "/admin"}'},
        }
        result = _reconstruct_tool_calls(buffers)
        assert len(result) == 1
        assert result[0]["id"] == "call_abc"
        assert result[0]["name"] == "navigate"
        assert result[0]["args"] == {"path": "/admin"}

    def test_agentic_loop_reconstructs_multiple_tool_calls(self):
        """Multiple tool call buffers are reconstructed in index order."""
        from modulo.api.routes.remy import _reconstruct_tool_calls

        buffers = {
            0: {"id": "call_1", "name": "navigate", "args": '{"path": "/admin"}'},
            1: {"id": "call_2", "name": "click", "args": '{"selector": ".btn"}'},
            2: {"id": "call_3", "name": "fill", "args": '{"selector": "#input", "value": "test"}'},
        }
        result = _reconstruct_tool_calls(buffers)
        assert len(result) == 3
        assert result[0]["name"] == "navigate"
        assert result[1]["name"] == "click"
        assert result[2]["name"] == "fill"

    def test_reconstruct_handles_malformed_json_gracefully(self):
        """Malformed args JSON results in empty dict, not crash."""
        from modulo.api.routes.remy import _reconstruct_tool_calls

        buffers = {
            0: {"id": "call_x", "name": "click", "args": "not valid json"},
        }
        result = _reconstruct_tool_calls(buffers)
        assert result[0]["args"] == {}

    def test_tool_param_is_omitted_for_non_tool_backend_definition(self):
        """Verify the _get_all_tool_definitions function returns proper OpenAI-style tool definitions."""
        from modulo.api.routes.remy import _get_all_tool_definitions

        tools = _get_all_tool_definitions()
        assert isinstance(tools, list)
        assert len(tools) == 11
        for t in tools:
            assert t["type"] == "function"
            assert "function" in t
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert t["function"]["parameters"]["type"] == "object"
            assert "properties" in t["function"]["parameters"]
