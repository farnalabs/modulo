"""Unit tests for the modulo://library MCP resource."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import resource_library
from modulo.db.crud.base import PageResult

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_mock_primitive(
    pid: str | None = None,
    name: str = "Test Primitive",
    primitive_type: str = "agent",
    version: str = "1.0",
    description: str = "A test primitive",
    tags: list[str] | None = None,
    average_rating: float | None = 4.2,
) -> MagicMock:
    p = MagicMock()
    p.id = uuid.UUID(pid) if pid else uuid.uuid4()
    p.name = name
    p.primitive_type = primitive_type
    p.version = version
    p.description = description
    p.tags = tags or []
    p.average_rating = average_rating
    return p


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestResourceLibraryAuth:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await resource_library()
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestResourceLibrarySuccess:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.list_primitives")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_all_primitives(
        self,
        mock_session: AsyncMock,
        mock_list_primitives: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        primitives = [
            _make_mock_primitive(
                pid="00000000-0000-0000-0000-000000000010",
                name="Schema One",
                primitive_type="schema",
                version="1.0",
                description="First schema",
                tags=["schema", "product"],
                average_rating=4.5,
            ),
            _make_mock_primitive(
                pid="00000000-0000-0000-0000-000000000020",
                name="Agent One",
                primitive_type="agent",
                version="2.0",
                description="First agent",
                tags=["agent", "llm"],
                average_rating=None,
            ),
        ]
        mock_list_primitives.return_value = PageResult(
            items=primitives,
            total=2,
            page=1,
            page_size=50,
        )

        result = await resource_library()

        assert "Library (2 primitives):" in result
        assert "Schema One" in result
        assert "type=schema" in result
        assert "v1.0" in result
        assert "tags=[schema, product]" in result
        assert "rating=4.5" in result
        assert "First schema" in result
        assert "Agent One" in result
        assert "type=agent" in result
        assert "v2.0" in result
        assert "tags=[agent, llm]" in result
        assert "rating=N/A" in result
        mock_list_primitives.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.list_primitives")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_empty_when_no_items(
        self,
        mock_session: AsyncMock,
        mock_list_primitives: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        mock_list_primitives.return_value = PageResult(
            items=[],
            total=0,
            page=1,
            page_size=50,
        )

        result = await resource_library()

        assert result == "Library is empty."
        mock_list_primitives.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.list_primitives")
    @patch("modulo.api.mcp_server._session")
    async def test_error_returns_generic_message(
        self,
        mock_session: AsyncMock,
        mock_list_primitives: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        mock_list_primitives.side_effect = Exception("DB failure")

        result = await resource_library()

        assert "error:" in result.lower()
