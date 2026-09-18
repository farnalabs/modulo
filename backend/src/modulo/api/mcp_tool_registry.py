"""MCP tool registry — single source of truth for tool definitions.

Generates OpenAI-compatible tool definitions from the FastMCP tool registry,
eliminating the hand-maintained mirror in remy.py.

Usage:
    from modulo.api.mcp_tool_registry import build_tool_registry, get_mcp_tool_definitions
    await build_tool_registry()
    tools = get_mcp_tool_definitions()
"""

from typing import Any

_tool_definitions: list[dict[str, Any]] | None = None
_registry_built = False


async def build_tool_registry() -> None:
    """Build the MCP tool registry from FastMCP's registered tools."""
    global _tool_definitions, _registry_built
    if _registry_built:
        return

    from modulo.api.mcp_server import mcp

    tool_defs: list[dict[str, Any]] = []
    for name, tool in mcp._tool_manager._tools.items():
        params = tool.parameters or {"type": "object", "properties": {}}
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        tool_def = {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.description or "",
                "parameters": params,
            },
        }
        tool_defs.append(tool_def)

    _tool_definitions = tool_defs
    _registry_built = True


def get_mcp_tool_definitions() -> list[dict[str, Any]]:
    """Return cached OpenAI-compatible MCP tool definitions."""
    return list(_tool_definitions or [])
