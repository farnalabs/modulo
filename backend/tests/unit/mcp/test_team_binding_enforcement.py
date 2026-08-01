"""Unit tests for cross-team connector binding enforcement in MCP tools (PRD §9.3)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import bind_connector_to_node, update_pipeline_graph
from modulo.core.team_visibility import CONNECTOR_TEAM_MISMATCH

pytestmark = pytest.mark.asyncio(loop_scope="module")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _set_ctx() -> None:
    from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

    _ctx_org_id.set(_ORG_ID)
    _ctx_role.set("admin")
    _ctx_auth_token.set(_API_KEY)
    _ctx_auth_type.set("api_key")


def _clear_ctx() -> None:
    from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

    _ctx_org_id.set(None)
    _ctx_role.set(None)
    _ctx_auth_token.set(None)
    _ctx_auth_type.set(None)


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _valid_graph_node(connector_id: uuid.UUID | None = None) -> dict:
    node: dict = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(uuid.uuid4()),
        "position": {"x": 10, "y": 20},
    }
    if connector_id is not None:
        node["connector_binding"] = {"type": "test", "instance_id": str(connector_id)}
    return node


# ---------------------------------------------------------------------------
# update_pipeline_graph
# ---------------------------------------------------------------------------


class TestUpdatePipelineGraphEnforcement:
    def setup_method(self) -> None:
        _set_ctx()

    def teardown_method(self) -> None:
        _clear_ctx()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    async def test_blocks_cross_team_binding(
        self,
        mock_get_pipeline: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline_id = uuid.uuid4()
        connector_id = uuid.uuid4()
        pipeline = MagicMock(id=pipeline_id, owner_team_id=_TEAM_A)
        mock_get_pipeline.return_value = pipeline
        mock_find_mismatches.return_value = [
            MagicMock(
                connector_id=connector_id,
                connector_name="eng-db",
                connector_owner_team_id=_TEAM_B,
                pipeline_owner_team_id=_TEAM_A,
                node_id="n1",
            )
        ]
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_valid_graph_node(connector_id)],
            edges=[],
        )

        assert result["error"] == CONNECTOR_TEAM_MISMATCH
        assert "eng-db" in result.get("detail", "")
        assert mock_find_mismatches.await_args.kwargs["pipeline_owner_team_id"] == _TEAM_A

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_same_team_binding_proceeds(
        self,
        mock_replace_graph: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline_id = uuid.uuid4()
        connector_id = uuid.uuid4()
        mock_get_pipeline.return_value = MagicMock(id=pipeline_id, owner_team_id=_TEAM_A)
        mock_find_mismatches.return_value = []
        mock_replace_graph.return_value = (_valid_graph_node(connector_id), [])
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_valid_graph_node(connector_id)],
            edges=[],
        )

        assert "error" not in result
        assert result["pipeline_id"] == str(pipeline_id)
        mock_replace_graph.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    async def test_missing_pipeline_returns_not_found(
        self,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline_id = uuid.uuid4()
        mock_get_pipeline.return_value = None
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_valid_graph_node(uuid.uuid4())],
            edges=[],
        )

        assert result["error"] == "pipeline_not_found"


# ---------------------------------------------------------------------------
# bind_connector_to_node
# ---------------------------------------------------------------------------


class TestBindConnectorToNodeEnforcement:
    def setup_method(self) -> None:
        _set_ctx()

    def teardown_method(self) -> None:
        _clear_ctx()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.get_connector_instance")
    async def test_blocks_cross_team_binding(
        self,
        mock_get_connector: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline_id = uuid.uuid4()
        node_id = uuid.uuid4()
        connector_id = uuid.uuid4()
        connector = MagicMock(
            id=connector_id,
            organisation_id=_ORG_ID,
            name="eng-db",
            visibility="team",
            owner_team_id=_TEAM_B,
        )
        mock_get_connector.return_value = connector

        session = AsyncMock()
        pipeline = MagicMock(id=pipeline_id, owner_team_id=_TEAM_A, graph_nodes_json=[])
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = pipeline
        session.execute = AsyncMock(return_value=execute_result)
        mock_session.return_value.__aenter__.return_value = session

        result = await bind_connector_to_node(
            pipeline_id=str(pipeline_id),
            node_id=str(node_id),
            connector_type="test",
            connector_instance_id=str(connector_id),
        )

        assert result["error"] == CONNECTOR_TEAM_MISMATCH
        assert "eng-db" in result.get("detail", "")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.get_connector_instance")
    async def test_same_team_binding_proceeds(
        self,
        mock_get_connector: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline_id = uuid.uuid4()
        node_id = uuid.uuid4()
        connector_id = uuid.uuid4()
        connector = MagicMock(
            id=connector_id,
            organisation_id=_ORG_ID,
            name="eng-db",
            visibility="team",
            owner_team_id=_TEAM_A,
        )
        mock_get_connector.return_value = connector

        session = AsyncMock()
        pipeline = MagicMock(
            id=pipeline_id,
            owner_team_id=_TEAM_A,
            graph_nodes_json=[{"id": str(node_id)}],
        )
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = pipeline
        session.execute = AsyncMock(return_value=execute_result)
        mock_session.return_value.__aenter__.return_value = session

        result = await bind_connector_to_node(
            pipeline_id=str(pipeline_id),
            node_id=str(node_id),
            connector_type="test",
            connector_instance_id=str(connector_id),
        )

        assert result.get("status") == "bound"
        assert result.get("error") is None
