"""FAR-436: node ``capability_scope`` wiring into the live MCP agent tool-call loop.

Covers the run-scoped-key resolution path and the ``_check_agent_tool_scope``
chokepoint wrapper that threads a node's ``allowed_tools`` into ``check_tool_scope``
(deny-by-default within the scope; absent scope = legacy role-only behaviour).
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api import mcp_server
from modulo.api.mcp_server import (
    _check_agent_tool_scope,
    _ctx_node_allowed_tools,
    _ctx_role,
    _extract_node_id_from_key_name,
)
from modulo.core.mcp.scope_validator import MCPAuthorizationError


class TestExtractNodeIdFromKeyName:
    """Parse the per-node sandbox key ``name`` (``run:<runid>:node:<nodeid>``)."""

    def test_run_scoped_key_parses_node_id(self) -> None:
        assert _extract_node_id_from_key_name("run:12345:node:sbx-1") == "sbx-1"

    def test_non_run_key_returns_none(self) -> None:
        assert _extract_node_id_from_key_name("some-user-org-key") is None

    def test_empty_or_missing_name_returns_none(self) -> None:
        assert _extract_node_id_from_key_name("") is None
        assert _extract_node_id_from_key_name(None) is None


class TestCheckAgentToolScope:
    """The ``_check_agent_tool_scope`` wrapper threads node allowed_tools."""

    @staticmethod
    def _with_scope(role: str, allowed: list[str] | None):
        """Context manager that sets + restores the request-scoped contextvars."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            t_role = _ctx_role.set(role)
            t_scope = _ctx_node_allowed_tools.set(allowed)
            try:
                yield
            finally:
                _ctx_node_allowed_tools.reset(t_scope)
                _ctx_role.reset(t_role)

        return _cm()

    def test_no_node_scope_is_role_only(self) -> None:
        with self._with_scope("admin", None):
            assert _check_agent_tool_scope("create_pipeline") is None

    def test_in_scope_tool_passes(self) -> None:
        with self._with_scope("admin", ["create_pipeline", "delete_pipeline"]):
            assert _check_agent_tool_scope("create_pipeline") is None

    def test_out_of_scope_tool_rejected_despite_valid_role(self) -> None:
        with (
            self._with_scope("admin", ["create_agent"]),
            pytest.raises(MCPAuthorizationError, match="allowed_tools scope"),
        ):
            _check_agent_tool_scope("create_pipeline")

    def test_empty_allow_list_denies_all(self) -> None:
        with self._with_scope("admin", []), pytest.raises(MCPAuthorizationError, match="allowed_tools scope"):
            _check_agent_tool_scope("create_pipeline")


class TestNodeAllowedToolsForKey:
    """Resolve a run-scoped key's node ``capability_scope.allowed_tools``."""

    @pytest.mark.asyncio
    async def test_non_run_key_returns_none(self) -> None:
        assert (
            await mcp_server._node_allowed_tools_for_key(
                org_id=uuid.uuid4(),
                run_id=None,
                key_name="some-user-org-key",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_run_scoped_key_resolves_node_allowlist(self) -> None:
        node = {"id": "sbx-1", "capability_scope": {"allowed_tools": ["bind_connector_to_node"]}}
        await self._assert_resolve(node, expected=["bind_connector_to_node"])

    @pytest.mark.asyncio
    async def test_scopeless_node_resolves_to_none(self) -> None:
        await self._assert_resolve({"id": "sbx-1"}, expected=None)

    @pytest.mark.asyncio
    async def test_empty_allowlist_returns_empty_list(self) -> None:
        await self._assert_resolve({"id": "sbx-1", "capability_scope": {"allowed_tools": []}}, expected=[])

    async def _assert_resolve(self, node: dict, *, expected: list[str] | None) -> None:
        snapshot = MagicMock()
        snapshot.graph_json = {"nodes": [node]}
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value="snapshot-1")))
        session.get = AsyncMock(return_value=snapshot)

        @asynccontextmanager
        async def _session_cm(org_id: uuid.UUID):
            del org_id
            yield session

        with patch.object(mcp_server, "_session", _session_cm):
            tools = await mcp_server._node_allowed_tools_for_key(
                org_id=uuid.uuid4(),
                run_id=uuid.uuid4(),
                key_name="run:abc:node:sbx-1",
            )
        assert tools == expected
