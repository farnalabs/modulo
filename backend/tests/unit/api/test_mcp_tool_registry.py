"""Unit tests for modulo.api.mcp_tool_registry — MCP tool-definition cache.

QA lens pass (correctness, bugs, maintainability, deps) on the module that is
the single source of truth for OpenAI-compatible tool definitions. The remy
agentic loop calls ``await build_tool_registry()`` then
``get_mcp_tool_definitions()`` on every stream request so the tool schema sent
to the model is generated once from FastMCP's registered tools instead of a
hand-maintained mirror.

These tests lock the generation contract (function-typed, name/description/
parameters passthrough), the two defensive fallbacks (``None`` description and
non-dict parameters), the once-only idempotent build, the defensive copy on
read, and the empty-before-build contract. Because the module caches in
module-level globals, every test resets the cache so ordering and import state
cannot leak across tests.
"""

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import modulo.api.mcp_tool_registry as registry
from modulo.api.mcp_tool_registry import build_tool_registry, get_mcp_tool_definitions


def _make_tool(
    *,
    name: str = "list_pipelines",
    description: str | None = "List pipelines in the org",
    parameters: Any = None,
) -> SimpleNamespace:
    if parameters is None:
        parameters = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    return SimpleNamespace(name=name, description=description, parameters=parameters)


@pytest.fixture(autouse=True)
def _reset_registry_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level cache so tests are independent of import state."""
    monkeypatch.setattr(registry, "_tool_definitions", None)
    monkeypatch.setattr(registry, "_registry_built", False)


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Inject a fake ``mcp_server`` module whose ``mcp`` mimics the FastMCP tool manager.

    ``build_tool_registry`` imports ``mcp`` lazily via
    ``from modulo.api.mcp_server import mcp``, so seeding ``sys.modules`` with a
    stub keeps the test hermetic (no real FastMCP server, tools, or DB deps).
    """
    fake_server = ModuleType("modulo.api.mcp_server")
    fake_mcp = SimpleNamespace()
    fake_mcp._tool_manager = SimpleNamespace(_tools={})
    fake_server.mcp = fake_mcp
    monkeypatch.setitem(sys.modules, "modulo.api.mcp_server", fake_server)
    return fake_mcp


class TestBuildToolRegistry:
    async def test_build_generates_function_typed_definitions(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {
            "list_pipelines": _make_tool(),
            "cancel_run": _make_tool(
                name="cancel_run",
                description="Cancel a running pipeline run.",
                parameters={"type": "object", "properties": {"run_id": {"type": "string"}}},
            ),
        }
        await build_tool_registry()
        defs = get_mcp_tool_definitions()
        assert [d["type"] for d in defs] == ["function", "function"]
        assert {d["function"]["name"] for d in defs} == {"list_pipelines", "cancel_run"}
        assert defs[0]["function"]["description"] == "List pipelines in the org"
        assert defs[0]["function"]["parameters"] == {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }

    async def test_build_passes_parameters_through_unchanged(self, fake_mcp: SimpleNamespace) -> None:
        params = {"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"]}
        fake_mcp._tool_manager._tools = {"list_pipelines": _make_tool(parameters=params)}
        await build_tool_registry()
        assert get_mcp_tool_definitions()[0]["function"]["parameters"] is params

    async def test_none_description_defaults_to_empty_string(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {"foo": _make_tool(description=None)}
        await build_tool_registry()
        assert not get_mcp_tool_definitions()[0]["function"]["description"]

    async def test_empty_description_stays_empty(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {"foo": _make_tool(description="")}
        await build_tool_registry()
        assert not get_mcp_tool_definitions()[0]["function"]["description"]

    async def test_non_dict_parameters_fallback_to_default_object(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {
            "foo": _make_tool(parameters="not-a-dict"),
            "bar": SimpleNamespace(name="bar", description="B", parameters=None),
        }
        await build_tool_registry()
        defs = {d["function"]["name"]: d["function"]["parameters"] for d in get_mcp_tool_definitions()}
        assert defs["foo"] == {"type": "object", "properties": {}}
        assert defs["bar"] == {"type": "object", "properties": {}}

    async def test_build_is_idempotent_and_ignores_new_tools(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {"foo": _make_tool(name="foo")}
        await build_tool_registry()
        fake_mcp._tool_manager._tools["bar"] = _make_tool(name="bar")
        await build_tool_registry()
        names = [d["function"]["name"] for d in get_mcp_tool_definitions()]
        assert names == ["foo"]

    async def test_build_is_awaitable_and_returns_none(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {"foo": _make_tool(name="foo")}
        result = await build_tool_registry()
        assert result is None

    async def test_rebuild_after_cache_reset_reflects_new_tools(
        self, fake_mcp: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mcp._tool_manager._tools = {"foo": _make_tool(name="foo")}
        await build_tool_registry()
        assert [d["function"]["name"] for d in get_mcp_tool_definitions()] == ["foo"]
        monkeypatch.setattr(registry, "_registry_built", False)
        monkeypatch.setattr(registry, "_tool_definitions", None)
        fake_mcp._tool_manager._tools["bar"] = _make_tool(name="bar")
        await build_tool_registry()
        names = [d["function"]["name"] for d in get_mcp_tool_definitions()]
        assert names == ["foo", "bar"]

    async def test_empty_tool_registry_yields_empty_definitions(self, fake_mcp: SimpleNamespace) -> None:
        await build_tool_registry()
        assert not get_mcp_tool_definitions()


class TestGetMcpToolDefinitions:
    async def test_returns_empty_list_before_build(self, fake_mcp: SimpleNamespace) -> None:
        assert not get_mcp_tool_definitions()

    async def test_returns_defensive_copy(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {"foo": _make_tool(name="foo")}
        await build_tool_registry()
        first = get_mcp_tool_definitions()
        first.append({"type": "function", "function": {"name": "injected", "description": "", "parameters": {}}})
        assert [d["function"]["name"] for d in get_mcp_tool_definitions()] == ["foo"]

    async def test_returns_ordered_definitions_matching_fastmcp_registry(self, fake_mcp: SimpleNamespace) -> None:
        fake_mcp._tool_manager._tools = {
            "a": _make_tool(name="a", description="A"),
            "b": _make_tool(name="b", description="B"),
            "c": _make_tool(name="c", description="C"),
        }
        await build_tool_registry()
        assert [d["function"]["name"] for d in get_mcp_tool_definitions()] == ["a", "b", "c"]

    async def test_async_usage_from_routes(self, fake_mcp: SimpleNamespace) -> None:
        """Mirror the remy agentic-loop call pattern: await build, then read."""
        fake_mcp._tool_manager._tools = {"foo": _make_tool(name="foo")}
        await build_tool_registry()
        tools_param = get_mcp_tool_definitions()
        assert [t["function"]["name"] for t in tools_param] == ["foo"]
