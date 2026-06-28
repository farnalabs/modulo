"""Unit tests for the modulo://schemas/{id}@{version} MCP resource."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import resource_schema_detail

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_SCHEMA_ID = uuid.uuid4()


def _make_mock_schema(*, name: str = "Test Schema") -> MagicMock:
    sc = MagicMock()
    sc.id = _SCHEMA_ID
    sc.name = name
    return sc


def _make_mock_schema_version(
    *,
    version: str = "1.0.0",
    definition_json: dict | None = None,
) -> MagicMock:
    sv = MagicMock()
    sv.version = version
    sv.definition_json = definition_json or {}
    sv.created_at = MagicMock()
    sv.created_at.isoformat.return_value = "2026-06-20T14:00:00+00:00"
    return sv


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _mock_scalar_one_or_none(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestResourceSchemaDetailAuth:
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
        result = await resource_schema_detail(schema_id=str(uuid.uuid4()), version="latest")
        assert "revoked" in result.lower() or "expired" in result.lower()


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestResourceSchemaDetailSuccess:
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
    @patch("modulo.api.mcp_server._session")
    async def test_returns_schema_version_detail_json_schema(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        schema = _make_mock_schema()
        sv = _make_mock_schema_version(
            definition_json={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["title"],
            },
        )

        session.get = AsyncMock(return_value=schema)

        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = sv
        session.execute = AsyncMock(return_value=scalar)

        result = await resource_schema_detail(schema_id=str(_SCHEMA_ID), version="1.0.0")

        assert "Test Schema" in result
        assert str(_SCHEMA_ID) in result
        assert "object" in result
        assert "1.0.0" in result
        assert "2026-06-20T14:00:00+00:00" in result
        assert "Fields (2):" in result
        assert "title: string (required)" in result
        assert "count: integer (optional)" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_latest_version(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        schema = _make_mock_schema()
        sv = _make_mock_schema_version(
            version="2.0.0",
            definition_json={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": [],
            },
        )

        session.get = AsyncMock(return_value=schema)
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = sv
        session.execute = AsyncMock(return_value=scalar)

        result = await resource_schema_detail(schema_id=str(_SCHEMA_ID), version="latest")

        assert "2.0.0" in result
        assert "name: string (optional)" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_handles_flat_fields_format(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        schema = _make_mock_schema()
        sv = _make_mock_schema_version(
            definition_json={
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "count", "type": "integer", "required": False},
                ],
            },
        )

        session.get = AsyncMock(return_value=schema)
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = sv
        session.execute = AsyncMock(return_value=scalar)

        result = await resource_schema_detail(schema_id=str(_SCHEMA_ID), version="1.0.0")

        assert "Fields (2):" in result
        assert "title: string (required)" in result
        assert "count: integer (optional)" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_empty_definition_json(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        schema = _make_mock_schema()
        sv = _make_mock_schema_version(definition_json={})

        session.get = AsyncMock(return_value=schema)
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = sv
        session.execute = AsyncMock(return_value=scalar)

        result = await resource_schema_detail(schema_id=str(_SCHEMA_ID), version="1.0.0")

        assert "Fields (0):" in result
        assert "object" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_schema_not_found(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        session.get = AsyncMock(return_value=None)

        result = await resource_schema_detail(schema_id=str(_SCHEMA_ID), version="latest")

        assert "not found" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_version_not_found(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        schema = _make_mock_schema()
        session.get = AsyncMock(return_value=schema)
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=scalar)

        result = await resource_schema_detail(schema_id=str(_SCHEMA_ID), version="99.0.0")

        assert "not found" in result
