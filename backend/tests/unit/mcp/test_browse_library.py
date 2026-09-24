"""Unit tests for the search_library MCP tool (formerly browse_library)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.base import PageResult


def _make_mock_primitive(
    pid: str,
    name: str = "Test Primitive",
    description: str | None = "A test primitive",
    primitive_type: str = "schema",
    version: str = "1.0",
    average_rating: float | None = 4.5,
    tags: list[str] | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.name = name
    p.description = description
    p.primitive_type = primitive_type
    p.version = version
    p.average_rating = average_rating
    p.tags = tags or []
    return p


class TestSearchLibrary:
    pytestmark = pytest.mark.asyncio

    def _auth_ctx(
        self,
        org_id: str = "00000000-0000-0000-0000-000000000001",
        role: str = "viewer",
        node_allowed_tools: list[str] | None = None,
    ) -> None:
        """Seed the request ContextVars an authenticated MCP handler reads.

        Auth re-validation is patched separately per test; here we mirror the
        middleware's context so the REAL ``_check_agent_tool_scope`` gate runs
        against a live role + node ``capability_scope.allowed_tools`` (FAR-436).
        """
        from modulo.api.mcp_server import _ctx_node_allowed_tools as _node_tools
        from modulo.api.mcp_server import _ctx_org_id as _org
        from modulo.api.mcp_server import _ctx_role as _role

        _org.set(org_id)
        _role.set(role)
        _node_tools.set(node_allowed_tools)

    async def test_returns_formatted_items(self) -> None:
        from modulo.api.mcp_server import search_library

        mock_items = [
            _make_mock_primitive(
                pid="00000000-0000-0000-0000-000000000001",
                name="PRD Input Schema",
                primitive_type="schema",
                tags=["schema", "prd"],
            ),
            _make_mock_primitive(
                pid="00000000-0000-0000-0000-000000000002",
                name="PRD Ingestion Agent",
                description="Ingests PRD documents",
                primitive_type="agent",
                average_rating=None,
                tags=["agent", "prd"],
            ),
        ]
        page_result = PageResult(
            items=mock_items,
            total=2,
            page=1,
            page_size=20,
            next_cursor=None,
            has_more=False,
        )

        self._auth_ctx()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=page_result,
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await search_library()

        mock_list.assert_called_once()
        assert result == {
            "items": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "name": "PRD Input Schema",
                    "description": "A test primitive",
                    "type": "schema",
                    "version": "1.0",
                    "average_rating": 4.5,
                    "tags": ["schema", "prd"],
                },
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "name": "PRD Ingestion Agent",
                    "description": "Ingests PRD documents",
                    "type": "agent",
                    "version": "1.0",
                    "average_rating": None,
                    "tags": ["agent", "prd"],
                },
            ],
            "total": 2,
            "next_cursor": None,
            "has_more": False,
        }

    async def test_returns_empty_list_when_no_results(self) -> None:
        from modulo.api.mcp_server import search_library

        page_result = PageResult(
            items=[],
            total=0,
            page=1,
            page_size=20,
            next_cursor=None,
            has_more=False,
        )

        self._auth_ctx()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=page_result,
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await search_library()

        mock_list.assert_called_once()
        assert result == {
            "items": [],
            "total": 0,
            "next_cursor": None,
            "has_more": False,
        }

    async def test_passes_filter_params(self) -> None:
        from modulo.api.mcp_server import search_library

        self._auth_ctx()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            await search_library(
                primitive_type="agent",
                search="test",
                cursor="abc123",
                limit=10,
            )

        mock_list.assert_called_once_with(
            mock_session.return_value.__aenter__.return_value,
            "00000000-0000-0000-0000-000000000001",
            primitive_type="agent",
            search="test",
            page=1,
            page_size=10,
            include_community=True,
            cursor="abc123",
        )

    async def test_uses_default_limit_of_20(self) -> None:
        from modulo.api.mcp_server import search_library

        self._auth_ctx()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            await search_library()

        _, kwargs = mock_list.call_args
        assert kwargs["page_size"] == 20

    async def test_rejects_expired_token(self) -> None:
        from modulo.api.mcp_server import search_library

        self._auth_ctx()
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=False):
            result = await search_library()

        assert result == {
            "error": "auth_expired",
            "detail": "Token revoked or expired - re-authenticate",
        }

    async def test_authenticated_viewer_browses(self) -> None:
        from modulo.api.mcp_server import search_library

        self._auth_ctx(role="viewer")
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await search_library()

        assert "insufficient_scope" not in result
        # The library-browse surface is a read at the viewer floor: the tool
        # must still perform its work — not just avoid raising.
        mock_list.assert_called_once()
        assert not result["items"]
        assert result["total"] == 0

    async def test_out_of_scope_node_denied_insufficient_scope(self) -> None:
        from modulo.api.mcp_server import search_library

        self._auth_ctx(
            role="runner",
            node_allowed_tools=["trigger_pipeline"],
        )
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await search_library()

        # The FAR-436 node capability_scope.allowed_tools narrowing excludes
        # the library-browse tool, so the centralized scope gate denies the call
        # at the handler before any DB read/seam is touched.
        assert result.get("error") == "insufficient_scope", result
        assert "search_library" in result.get("detail", "")
        mock_list.assert_not_called()

    async def test_error_handling(self) -> None:
        from modulo.api.mcp_server import search_library

        self._auth_ctx()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                side_effect=RuntimeError("DB connection lost"),
            ),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await search_library()

        assert result == {
            "error": "internal_error",
            "detail": "Failed to search library",
        }
