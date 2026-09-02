"""Unit tests for Remy UI tools — tool definitions, permission resolution, session approvals."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from modulo.api.routes.remy import (
    _get_all_tool_definitions,
    _has_destructive_pattern,
    _is_approved_for_session,
    _reconstruct_tool_calls,
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
    def test_all_tools_defined(self):
        assert len(_UI_TOOLS) == 13

    def test_tool_names_match_dict(self):
        assert set(_UI_TOOLS.keys()) == UI_TOOL_NAMES

    def test_required_tools_exist(self):
        required = {
            "navigate",
            "click",
            "fill",
            "select",
            "extract",
            "extract_all",
            "get_page_interactables",
            "wait",
            "go_back",
            "get_url",
            "press",
            "get_manifest",
            "undo_last_action",
        }
        assert required == UI_TOOL_NAMES

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
    def test_read_tools(self):
        assert {"extract", "extract_all", "get_page_interactables", "get_url"} == READ_TOOLS

    def test_nav_tools(self):
        assert {"navigate", "go_back"} == NAV_TOOLS

    def test_write_tools(self):
        assert {"click", "fill", "select", "press"} == WRITE_TOOLS

    def test_sets_are_disjoint(self):
        assert READ_TOOLS.isdisjoint(NAV_TOOLS)
        assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
        assert NAV_TOOLS.isdisjoint(WRITE_TOOLS)

    def test_all_tools_covered_by_categories(self):
        categorized = READ_TOOLS | NAV_TOOLS | WRITE_TOOLS
        uncategorized = UI_TOOL_NAMES - categorized
        assert uncategorized == {"wait", "get_manifest", "undo_last_action"}, (
            f"Unexpected uncategorized tools: {uncategorized}"
        )

    def test_destructive_patterns(self):
        assert "delete" in DESTRUCTIVE_PATTERNS
        assert "remove" in DESTRUCTIVE_PATTERNS
        assert "destroy" in DESTRUCTIVE_PATTERNS


class TestDestructivePatternDetection:
    @pytest.mark.parametrize(
        "selector",
        [
            pytest.param("[data-testid='delete-btn']", id="delete_btn"),
            pytest.param(".remove-item", id="remove_item"),
            pytest.param("destroy-all", id="destroy_all"),
            pytest.param("archive-project", id="archive_project"),
            pytest.param("suspend-user", id="suspend_user"),
            pytest.param("ban-account", id="ban_account"),
            pytest.param("[data-testid='message-deleted']", id="message_deleted"),
            pytest.param(".message-deleted-badge", id="message_deleted_badge"),
            pytest.param("notification-deleted-label", id="notification_deleted_label"),
            pytest.param("DELETE-button", id="delete_uppercase"),
            pytest.param("RemoveItem", id="remove_camelcase"),
            pytest.param("ARCHIVE", id="archive_uppercase"),
        ],
    )
    def test_destructive_selectors(self, selector: str):
        assert _has_destructive_pattern(selector)

    @pytest.mark.parametrize(
        "selector",
        [
            pytest.param("[data-testid='save-btn']", id="save_btn"),
            pytest.param(".create-new", id="create_new"),
            pytest.param("input[name='email']", id="email_input"),
            pytest.param(".search-box", id="search_box"),
            pytest.param(".edit-profile", id="edit_profile"),
            pytest.param("[data-testid='add-user']", id="add_user"),
            pytest.param(".view-details", id="view_details"),
            pytest.param(".export-csv", id="export_csv"),
        ],
    )
    def test_safe_selectors(self, selector: str):
        assert not _has_destructive_pattern(selector)

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


class TestRemyConfigDefaults:
    def test_default_tool_permissions_empty(self):
        config = RemyConfig()
        assert not config.tool_permissions

    def test_default_permission_mode_is_safe(self):
        config = RemyConfig()
        assert config.permission_mode == "safe"

    def test_schema_version_bumped_to_3(self):
        config = RemyConfig()
        assert config.schema_version == 3

    def test_tool_permissions_defaults_are_independent(self):
        config1 = RemyConfig()
        config2 = RemyConfig()
        config1.tool_permissions["click"] = "disabled"
        assert "click" not in config2.tool_permissions


class TestPermissionModePresets:
    def test_full_auto_preset(self):
        preset = PERMISSION_MODE_PRESETS["full_auto"]
        for tool_name in UI_TOOL_NAMES:
            assert preset[tool_name] == "always_allowed"

    def test_safe_preset(self):
        preset = PERMISSION_MODE_PRESETS["safe"]
        assert preset["press"] == "requires_approval"
        assert "click" not in preset

    def test_locked_down_preset(self):
        preset = PERMISSION_MODE_PRESETS["locked_down"]
        for tool_name in READ_TOOLS:
            assert preset[tool_name] == "always_allowed"
        assert preset["navigate"] == "always_allowed"
        for tool_name in ("click", "fill", "select", "go_back", "press"):
            assert preset[tool_name] == "requires_approval"

    @pytest.mark.parametrize(
        ("mode", "overrides", "expected_checks"),
        [
            pytest.param("full_auto", None, {"click": "always_allowed", "press": "always_allowed"}, id="full_auto"),
            pytest.param("safe", None, {"press": "requires_approval", "click_missing": True}, id="safe"),
            pytest.param(
                "locked_down", None, {"click": "requires_approval", "navigate": "always_allowed"}, id="locked_down"
            ),
            pytest.param("custom", {"click": "disabled"}, {"click": "disabled"}, id="custom_with_overrides"),
            pytest.param("nonexistent", None, {}, id="unknown_mode_returns_empty"),
            pytest.param("custom", None, {}, id="custom_without_overrides_returns_empty"),
        ],
    )
    def test_apply_preset(self, mode: str, overrides: dict | None, expected_checks: dict):
        result = apply_permission_mode_preset(mode, overrides or {})
        for key, expected in expected_checks.items():
            if key.endswith("_missing"):
                tool = key.replace("_missing", "")
                assert tool not in result, f"{tool} should not be in result"
            else:
                assert result.get(key) == expected, f"{key}: expected {expected}, got {result.get(key)}"


class TestPermissionResolution:
    """Tests for _resolve_tool_permission logic via RemyConfig."""

    @pytest.mark.parametrize(
        ("mode", "tool", "selector", "expected"),
        [
            pytest.param("safe", "click", ".save-btn", "always_allowed", id="safe_click_safe_selector"),
            pytest.param("safe", "fill", "input[name=email]", "always_allowed", id="safe_fill_safe_selector"),
            pytest.param("safe", "click", ".delete-btn", "requires_approval", id="safe_click_destructive"),
            pytest.param("safe", "fill", "#remove-field", "requires_approval", id="safe_fill_destructive"),
            pytest.param("safe", "press", None, "requires_approval", id="safe_press_requires_approval"),
            pytest.param("full_auto", "click", ".save-btn", "always_allowed", id="full_auto_click_safe"),
            pytest.param("full_auto", "press", None, "always_allowed", id="full_auto_press_allowed"),
            pytest.param("full_auto", "click", "delete-btn", "requires_approval", id="full_auto_click_destructive"),
            pytest.param("locked_down", "click", ".save-btn", "requires_approval", id="locked_down_click_blocked"),
            pytest.param("locked_down", "press", None, "requires_approval", id="locked_down_press_blocked"),
            pytest.param("locked_down", "extract", None, "always_allowed", id="locked_down_read_allowed"),
            pytest.param("locked_down", "navigate", None, "always_allowed", id="locked_down_nav_allowed"),
        ],
    )
    def test_permission_for_mode(self, mode: str, tool: str, selector: str | None, expected: str):
        config = RemyConfig(permission_mode=mode)
        kwargs = {"selector": selector} if selector else {}
        assert _resolve_tool_permission(config, tool, kwargs) == expected

    def test_safe_mode_allows_read_tools(self):
        config = RemyConfig()
        for tool in READ_TOOLS:
            assert _resolve_tool_permission(config, tool, {}) == "always_allowed"

    def test_safe_mode_allows_nav_tools(self):
        config = RemyConfig()
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


class TestGetAllToolDefinitions:
    def test_returns_list_of_dicts(self):
        result = _get_all_tool_definitions()
        assert isinstance(result, list)
        assert len(result) >= 13

    def test_includes_all_ui_tools(self):
        result = _get_all_tool_definitions()
        names = {entry["function"]["name"] for entry in result}
        assert names >= UI_TOOL_NAMES

    def test_each_entry_has_type_function(self):
        for entry in _get_all_tool_definitions():
            assert entry["type"] == "function"

    def test_each_entry_has_function_with_name_description_parameters(self):
        for entry in _get_all_tool_definitions():
            fn = entry["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn
            assert fn["parameters"]["type"] == "object"
            assert "properties" in fn["parameters"]

    def test_include_ui_tools_false_excludes_ui_tools(self):
        result = _get_all_tool_definitions(include_ui_tools=False)
        names = {entry["function"]["name"] for entry in result}
        assert names.isdisjoint(UI_TOOL_NAMES)


class TestBuildToolDefinitionsForText:
    def test_returns_non_empty_string(self):
        result = build_tool_definitions_for_text()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_header(self):
        result = build_tool_definitions_for_text()
        assert "## Browser Tools Available (Text Mode)" in result

    def test_includes_all_tools(self):
        result = build_tool_definitions_for_text()
        for name in UI_TOOL_NAMES:
            assert name in result

    def test_includes_descriptions(self):
        result = build_tool_definitions_for_text()
        for schema in _UI_TOOLS.values():
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
    def setup_method(self) -> None:
        _session_approvals.clear()
        self._registry_patcher = patch("modulo.api.routes.remy._get_registry", return_value=None)
        self._registry_patcher.start()

    def teardown_method(self) -> None:
        self._registry_patcher.stop()

    async def test_approved_same_page_not_expired(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) + timedelta(minutes=30),
            },
        }
        assert await _is_approved_for_session("session-1", "click", "/admin/users")

    async def test_different_page_not_approved(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) + timedelta(minutes=30),
            },
        }
        assert not await _is_approved_for_session("session-1", "click", "/admin/settings")

    async def test_expired_approval_returns_false(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            },
        }
        assert not await _is_approved_for_session("session-1", "click", "/admin/users")

    async def test_expired_approval_is_cleaned_up(self) -> None:
        _session_approvals["session-1"] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            },
        }
        await _is_approved_for_session("session-1", "click", "/admin/users")
        assert "session-1" not in _session_approvals

    async def test_no_session_returns_false(self) -> None:
        assert not await _is_approved_for_session("nonexistent", "click", "/admin/users")

    async def test_tool_not_in_session_returns_false(self) -> None:
        _session_approvals["session-1"] = {}
        assert not await _is_approved_for_session("session-1", "click", "/admin/users")


# ── Agentic Loop Routing ────────────────────────────────────────────────


class TestAgenticLoopRouting:
    def test_ui_tools_separated_from_mcp_tools(self):
        ui_tool_calls = [{"name": n, "id": f"call_{n}", "args": {}} for n in UI_TOOL_NAMES]
        mcp_tool_calls = [{"name": "list_pipelines", "id": "call_mcp", "args": {}}]

        all_calls = ui_tool_calls + mcp_tool_calls
        separated_ui = [tc for tc in all_calls if tc["name"] in UI_TOOL_NAMES]
        separated_mcp = [tc for tc in all_calls if tc["name"] not in UI_TOOL_NAMES]

        assert len(separated_ui) == len(UI_TOOL_NAMES)
        assert len(separated_mcp) == 1
        assert separated_mcp[0]["name"] == "list_pipelines"

    def test_mcp_tools_are_never_in_ui_tool_set(self):
        mcp_tools = {
            "list_pipelines",
            "get_pipeline",
            "trigger_run",
            "search_agents",
            "list_schemas",
            "read_audit_log",
        }
        intersection = mcp_tools & UI_TOOL_NAMES
        assert intersection == set(), f"MCP tool names leaked into UI tools: {intersection}"

    def test_tool_param_includes_ui_definitions_for_tool_model(self):
        tools = _get_all_tool_definitions(include_ui_tools=True)
        names = {entry["function"]["name"] for entry in tools}
        assert "navigate" in names
        assert "get_manifest" in names

    def test_tool_param_omits_ui_definitions_for_non_tool_model(self):
        tools = _get_all_tool_definitions(include_ui_tools=False)
        names = {entry["function"]["name"] for entry in tools}
        assert names.isdisjoint(UI_TOOL_NAMES)

    def test_agentic_loop_continues_when_tool_calls_exist(self):
        tool_calls = _reconstruct_tool_calls({0: {"id": "call_1", "name": "navigate", "args": '{"path": "/admin"}'}})
        assert len(tool_calls) > 0

    def test_agentic_loop_exits_when_no_tool_calls(self):
        tool_calls = _reconstruct_tool_calls({})
        assert tool_calls == []

    def test_agentic_loop_continues_for_mixed_ui_and_mcp(self):
        tool_calls = _reconstruct_tool_calls(
            {
                0: {"id": "call_1", "name": "navigate", "args": '{"path": "/admin"}'},
                1: {"id": "call_2", "name": "list_pipelines", "args": "{}"},
            }
        )
        assert len(tool_calls) == 2

    def test_agentic_loop_with_empty_tool_call_buffers(self):
        buffers: dict[int, dict] = {}
        result = _reconstruct_tool_calls(buffers)
        assert result == []

    def test_agentic_loop_reconstructs_single_tool_call(self):
        buffers = {
            0: {"id": "call_abc", "name": "navigate", "args": '{"path": "/admin"}'},
        }
        result = _reconstruct_tool_calls(buffers)
        assert len(result) == 1
        assert result[0]["id"] == "call_abc"
        assert result[0]["name"] == "navigate"
        assert result[0]["args"] == {"path": "/admin"}

    def test_agentic_loop_reconstructs_multiple_tool_calls(self):
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
        buffers = {
            0: {"id": "call_x", "name": "click", "args": "not valid json"},
        }
        result = _reconstruct_tool_calls(buffers)
        assert not result[0]["args"]

    def test_reconstruct_handles_empty_and_none_args(self):
        buffers = {
            0: {"id": "call_1", "name": "navigate", "args": ""},
            1: {"id": "call_2", "name": "click", "args": None},
        }
        result = _reconstruct_tool_calls(buffers)
        assert not result[0]["args"]
        assert not result[1]["args"]

    def test_reconstruct_sorts_buffers_by_index(self):
        buffers = {
            2: {"id": "call_3", "name": "fill", "args": '{"value": "x"}'},
            0: {"id": "call_1", "name": "navigate", "args": '{"path": "/admin"}'},
            1: {"id": "call_2", "name": "click", "args": '{"selector": ".btn"}'},
        }
        result = _reconstruct_tool_calls(buffers)
        assert [tc["name"] for tc in result] == ["navigate", "click", "fill"]

    def test_tool_param_is_omitted_for_non_tool_backend_definition(self):
        tools = _get_all_tool_definitions()
        assert isinstance(tools, list)
        assert len(tools) >= 13
        for t in tools:
            assert t["type"] == "function"
            assert "function" in t
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert t["function"]["parameters"]["type"] == "object"
            assert "properties" in t["function"]["parameters"]
