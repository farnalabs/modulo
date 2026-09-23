"""FAR-1129 cap-mcp: MCP reachable over a REAL JSON-RPC stdio transport.

The repo ships an MCP server (``modulo.api.mcp_server``) but no consumer ever
talks to an MCP server at the wire level — the capability is only proven by
unit tests that mock ``mcp`` internals. Here we run a real ``FastMCP`` server
from the repo's own ``mcp`` dependency as a subprocess and drive it through the
real ``mcp.client.stdio`` transport: initialize, ``tools/list``, ``tools/call``,
and a protocol-level error path. The client and server are distinct processes,
so the boundary under test is the actual MCP JSON-RPC pipe, not an in-memory
mock.

Loopback subprocess only — no container, no external service.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.integration

_SERVER_SRC = """\
import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("far1129-loopback")


@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b


@mcp.tool()
def whoami() -> str:
    return "far1129-loopback"


@mcp.tool()
def explode(subject: str) -> str:
    raise RuntimeError(f"exploded: {subject}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
"""


@pytest.fixture(scope="module")
def mcp_server_script(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    script = tmp_path_factory.mktemp("far1129_mcp") / "server.py"
    script.write_text(_SERVER_SRC)
    return script


def _connect(script: pathlib.Path):
    params = StdioServerParameters(command=sys.executable, args=[str(script)])
    return stdio_client(params)


async def test_mcp_stdio_handshake_lists_real_server_tools(mcp_server_script: pathlib.Path) -> None:
    async with _connect(mcp_server_script) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {"add", "whoami", "explode"} <= names


async def test_mcp_stdio_call_over_real_transport(mcp_server_script: pathlib.Path) -> None:
    async with _connect(mcp_server_script) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("add", {"a": 20, "b": 22})
        assert result.isError is False
        assert result.content[0].text == "42"
        echo = await session.call_tool("whoami", {})
        assert echo.isError is False
        assert echo.content[0].text == "far1129-loopback"


async def test_mcp_stdio_tool_error_surfaces_over_protocol(mcp_server_script: pathlib.Path) -> None:
    async with _connect(mcp_server_script) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("explode", {"subject": "boom"})
        assert result.isError is True
        joined = "\n".join(str(c.text) for c in result.content)
        assert "boom" in joined


async def test_mcp_stdio_unknown_tool_rejected(mcp_server_script: pathlib.Path) -> None:
    async with _connect(mcp_server_script) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("no_such_tool", {})
        assert result.isError is True
