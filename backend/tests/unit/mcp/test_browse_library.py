"""Unit tests for the browse_library MCP tool."""

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


class TestBrowseLibrary:
    pytestmark = pytest.mark.asyncio

    async def test_returns_formatted_items(self) -> None:
        from modulo.api.mcp_server import _ctx_org_id, browse_library

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

        _ctx_org_id.set("00000000-0000-0000-0000-000000000001")

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=page_result,
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await browse_library()

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
        from modulo.api.mcp_server import _ctx_org_id, browse_library

        page_result = PageResult(
            items=[],
            total=0,
            page=1,
            page_size=20,
            next_cursor=None,
            has_more=False,
        )

        _ctx_org_id.set("00000000-0000-0000-0000-000000000001")

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=page_result,
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await browse_library()

        mock_list.assert_called_once()
        assert result == {
            "items": [],
            "total": 0,
            "next_cursor": None,
            "has_more": False,
        }

    async def test_passes_filter_params(self) -> None:
        from modulo.api.mcp_server import _ctx_org_id, browse_library

        _ctx_org_id.set("00000000-0000-0000-0000-000000000001")

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            await browse_library(
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
        from modulo.api.mcp_server import _ctx_org_id, browse_library

        _ctx_org_id.set("00000000-0000-0000-0000-000000000001")

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            await browse_library()

        _, kwargs = mock_list.call_args
        assert kwargs["page_size"] == 20

    async def test_rejects_expired_token(self) -> None:
        from modulo.api.mcp_server import _ctx_org_id, browse_library

        _ctx_org_id.set("00000000-0000-0000-0000-000000000001")

        with patch("modulo.api.mcp_server.validate_current_auth", return_value=False):
            result = await browse_library()

        assert result == {
            "error": "auth_expired",
            "detail": "Token revoked or expired - re-authenticate",
        }

    async def test_no_scope_check_needed(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import browse_library

        _role.set(None)

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await browse_library()

        assert "insufficient_scope" not in result

    async def test_error_handling(self) -> None:
        from modulo.api.mcp_server import _ctx_org_id, browse_library

        _ctx_org_id.set("00000000-0000-0000-0000-000000000001")

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.api.mcp_server.list_primitives",
                side_effect=RuntimeError("DB connection lost"),
            ),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await browse_library()

        assert result == {
            "error": "internal_error",
            "detail": "Failed to search library",
        }
