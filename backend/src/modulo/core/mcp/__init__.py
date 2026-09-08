"""Model Context Protocol (MCP) support for Modulo.

Provides scope validation and MCP server integration for agent-tool communication.
"""

from modulo.core.mcp.scope_validator import ScopeValidator

__all__ = [
    "ScopeValidator",
]
