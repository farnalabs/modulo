"""Unit tests for the modulo://library/{type}/{slug} MCP resource."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import resource_library_detail

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_mock_primitive(
    *,
    pid: str | None = None,
    name: str = "Test Primitive",
    primitive_type: str = "agent",
    slug: str = "test-primitive",
    version: str = "1.0",
    description: str = "A test primitive",
    author: str = "modulo",
    tags: list[str] | None = None,
    average_rating: float | None = 4.2,
    download_count: int | None = 42,
    content_json: dict | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = uuid.UUID(pid) if pid else uuid.uuid4()
    p.name = name
    p.primitive_type = primitive_type
    p.slug = slug
    p.version = version
    p.description = description
    p.author = author
    p.tags = tags or []
    p.average_rating = average_rating
    p.download_count = download_count
    p.content_json = content_json or {"key": "value"}
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


class TestResourceLibraryDetailAuth:
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
        result = await resource_library_detail(primitive_type="agent", slug="test-primitive")
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestResourceLibraryDetailSuccess:
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
    @patch("modulo.api.mcp_server.get_primitive_by_slug")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_primitive_detail(
        self,
        mock_session: AsyncMock,
        mock_get_primitive_by_slug: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        p = _make_mock_primitive(
            pid="00000000-0000-0000-0000-000000000010",
            name="Schema One",
            primitive_type="schema",
            slug="schema-one",
            version="1.0",
            description="First schema",
            author="modulo",
            tags=["schema", "product"],
            average_rating=4.5,
            download_count=100,
            content_json={"fields": [{"name": "title", "type": "string"}]},
        )
        mock_get_primitive_by_slug.return_value = p

        result = await resource_library_detail(primitive_type="schema", slug="schema-one")

        assert "Name: Schema One" in result
        assert "00000000-0000-0000-0000-000000000010" in result
        assert "Type: schema" in result
        assert "Version: 1.0" in result
        assert "Author: modulo" in result
        assert "Tags: [schema, product]" in result
        assert "Average Rating: 4.50" in result
        assert "Download Count: 100" in result
        assert "Description: First schema" in result
        assert "Content Summary:" in result
        assert "title" in result
        mock_get_primitive_by_slug.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_primitive_by_slug")
    @patch("modulo.api.mcp_server._session")
    async def test_handles_null_rating_and_downloads(
        self,
        mock_session: AsyncMock,
        mock_get_primitive_by_slug: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        p = _make_mock_primitive(
            pid="00000000-0000-0000-0000-000000000020",
            name="Agent One",
            primitive_type="agent",
            slug="agent-one",
            version="2.0",
            description="First agent",
            author="modulo",
            tags=["agent", "llm"],
            average_rating=None,
            download_count=None,
            content_json={},
        )
        mock_get_primitive_by_slug.return_value = p

        result = await resource_library_detail(primitive_type="agent", slug="agent-one")

        assert "Average Rating: N/A" in result
        assert "Download Count: 0" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_primitive_by_slug")
    @patch("modulo.api.mcp_server._session")
    async def test_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_primitive_by_slug: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        mock_get_primitive_by_slug.return_value = None

        result = await resource_library_detail(primitive_type="agent", slug="nonexistent")

        assert "not found" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_primitive_by_slug")
    @patch("modulo.api.mcp_server._session")
    async def test_error_returns_generic_message(
        self,
        mock_session: AsyncMock,
        mock_get_primitive_by_slug: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        mock_get_primitive_by_slug.side_effect = Exception("DB failure")

        result = await resource_library_detail(primitive_type="agent", slug="test-primitive")

        assert "error:" in result.lower()
