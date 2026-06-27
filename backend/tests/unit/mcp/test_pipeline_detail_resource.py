"""Unit tests for the modulo://pipelines/{id} MCP resource."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import resource_pipeline_detail

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_PIPELINE_ID = uuid.uuid4()


def _make_mock_pipeline(
    *,
    name: str = "Test Pipeline",
    description: str | None = "A test pipeline",
    graph_nodes_json: list | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.name = name
    p.description = description
    p.visibility = "org"
    p.graph_nodes_json = graph_nodes_json or []
    p.created_at = MagicMock()
    p.created_at.isoformat.return_value = "2026-01-15T10:00:00+00:00"
    return p


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _mock_scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _mock_scalar_one_or_none(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestResourcePipelineDetailAuth:
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
        result = await resource_pipeline_detail(pipeline_id=str(uuid.uuid4()))
        assert "revoked" in result.lower() or "expired" in result.lower()


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestResourcePipelineDetailSuccess:
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
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_pipeline_detail(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        pipeline = _make_mock_pipeline(graph_nodes_json=[{"id": "n1"}, {"id": "n2"}])
        mock_get_pipeline.return_value = pipeline

        session.execute.side_effect = [
            _mock_scalar_result(3),  # edge count
            _mock_scalar_result(5),  # snapshot count
            _mock_scalar_one_or_none(  # last run at
                MagicMock(isoformat=lambda: "2026-06-20T14:30:00+00:00")
            ),
        ]

        result = await resource_pipeline_detail(pipeline_id=str(_PIPELINE_ID))

        assert "Test Pipeline" in result
        assert str(_PIPELINE_ID) in result
        assert "A test pipeline" in result
        assert "active" in result
        assert "org" in result
        assert "2026-01-15T10:00:00+00:00" in result
        assert "Node count: 2" in result
        assert "Edge count: 3" in result
        assert "Snapshot count: 5" in result
        assert "2026-06-20T14:30:00+00:00" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_inactive_when_no_nodes(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        pipeline = _make_mock_pipeline(graph_nodes_json=[])
        mock_get_pipeline.return_value = pipeline

        session.execute.side_effect = [
            _mock_scalar_result(0),
            _mock_scalar_result(0),
            _mock_scalar_one_or_none(None),
        ]

        result = await resource_pipeline_detail(pipeline_id=str(_PIPELINE_ID))

        assert "inactive" in result
        assert "Node count: 0" in result
        assert "Edge count: 0" in result
        assert "Snapshot count: 0" in result
        assert "Last run" not in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_none_description(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        pipeline = _make_mock_pipeline(description=None)
        mock_get_pipeline.return_value = pipeline

        session.execute.side_effect = [
            _mock_scalar_result(0),
            _mock_scalar_result(0),
            _mock_scalar_one_or_none(None),
        ]

        result = await resource_pipeline_detail(pipeline_id=str(_PIPELINE_ID))

        assert "(none)" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_pipeline_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        mock_get_pipeline.return_value = None

        result = await resource_pipeline_detail(pipeline_id=str(_PIPELINE_ID))

        assert "not found" in result
