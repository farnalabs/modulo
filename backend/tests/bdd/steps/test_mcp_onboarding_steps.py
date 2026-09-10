"""BDD step definitions: MCP onboarding — discoverable tool surface (ADR 017).

The onboarding surface is two contracts:

- a discoverable tool inventory — the FastMCP tool manager (pinned by
  ``test_mcp_structural_coverage.py``) exposes every registered tool with a
  description and an input schema, which is what ``build_tool_registry()``
  mirrors for OpenAI-style tool definitions;
- a fail-closed authentication gate — ``McpAuthMiddleware`` rejects
  unauthenticated and invalid-credential requests with 401 before any tool
  handler runs.

This module is the executing BDD mirror of those two contracts. It replaces
the earlier ``mcp/onboarding.feature`` draft that described a public
``tools/list`` surface: the middleware now rejects unauthenticated
introspection fail-closed, so the draft could never execute against the
shipped server.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when
from starlette.requests import Request
from starlette.responses import JSONResponse

from tests.bdd.conftest import make_settings

scenarios("../features/mcp/onboarding.feature")


def _make_mcp_request(
    *,
    path: str = "/mcp/tools/list",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    """Build a minimal ASGI request scope (mirrors test_mcp_oauth_bdd)."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    return Request(scope, receive=receive)


def _dispatch_middleware(request: Request) -> int:
    """Run ``McpAuthMiddleware.dispatch`` and return the resulting status code."""

    async def call_next(_req: Request) -> JSONResponse:
        return JSONResponse({})

    with (
        patch("modulo.api.mcp_server.get_settings", return_value=make_settings()),
    ):
        from modulo.api.mcp_server import McpAuthMiddleware

        middleware = McpAuthMiddleware(app=MagicMock())
        return asyncio.run(middleware.dispatch(request, call_next)).status_code


@given("the MCP server is mounted at /mcp")
def mcp_mounted(request) -> None:
    request.node._mcp_path = "/mcp"


@given("a request with an invalid bearer token")
def invalid_bearer_request(request) -> None:
    request.node._mcp_request = _make_mcp_request(headers=[(b"authorization", b"Bearer invalid-token")])


@when("the MCP tool registry is inspected")
def mcp_registry_inspected(request) -> None:
    from modulo.api.mcp_server import mcp

    request.node._mcp_tools = dict(getattr(mcp._tool_manager, "_tools", {}))


@when("an unauthenticated request reaches the MCP auth middleware")
def unauth_reaches_middleware(request) -> None:
    request.node._mcp_status = _dispatch_middleware(_make_mcp_request())


@when("the request reaches the MCP auth middleware")
def request_reaches_middleware(request) -> None:
    request.node._mcp_status = _dispatch_middleware(getattr(request.node, "_mcp_request", None) or _make_mcp_request())


@then("the tool inventory contains definitions")
def inventory_contains_definitions(request) -> None:
    assert request.node._mcp_tools


@then(parsers.parse('the "{tool}" tool has a description and inputSchema'))
def tool_has_description_and_schema(tool: str, request) -> None:
    tool_obj = request.node._mcp_tools.get(tool)
    assert tool_obj is not None, f"tool {tool!r} is not advertised by the MCP server"
    assert getattr(tool_obj, "description", ""), f"tool {tool!r} has no description"
    parameters = getattr(tool_obj, "parameters", None)
    assert parameters is None or isinstance(parameters, dict), f"tool {tool!r} inputSchema is not a JSON schema object"


@then(parsers.parse("the tool inventory contains at least {count:d} tools"))
def inventory_has_at_least(count: int, request) -> None:
    assert len(request.node._mcp_tools) >= count


@then("every tool definition carries a name, a description and an inputSchema")
def every_tool_carries_contract(request) -> None:
    for name, tool_obj in request.node._mcp_tools.items():
        assert name
        assert isinstance(getattr(tool_obj, "description", None), str)
        parameters = getattr(tool_obj, "parameters", None)
        assert parameters is None or isinstance(parameters, dict)


@then(parsers.parse("the MCP request is rejected with status {status:d}"))
def mcp_request_rejected_with_status(status: int, request) -> None:
    assert getattr(request.node, "_mcp_status", None) == status
