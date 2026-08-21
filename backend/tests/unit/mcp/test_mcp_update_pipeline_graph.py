"""Unit tests for FAR-309 PR A guardrail-binding strip enforcement in the
MCP ``update_pipeline_graph`` tool.

Mirrors the REST graph-save enforcement (``test_nonadmin_cannot_strip_guardrail_binding``):
a non-admin MCP caller may not strip a guardrail binding by removing a
guardrail-bound node from the graph; an admin may; unrelated graph changes
are unaffected.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import update_pipeline_graph

pytestmark = pytest.mark.asyncio(loop_scope="module")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _set_ctx(role: str | None) -> None:
    from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

    _ctx_org_id.set(_ORG_ID)
    _ctx_role.set(role)
    _ctx_user_id.set(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    _ctx_auth_token.set(_API_KEY)
    _ctx_auth_type.set("api_key")


def _clear_ctx() -> None:
    from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

    _ctx_org_id.set(None)
    _ctx_role.set(None)
    _ctx_user_id.set(None)
    _ctx_auth_token.set(None)
    _ctx_auth_type.set(None)


def _guardrail_row(node_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        node_id=node_id,
        name="no-aws-keys",
        eval_type="guardrail",
    )


def _graph_node(node_id: uuid.UUID) -> dict:
    return {"id": str(node_id), "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}


class TestUpdatePipelineGraphGuardrailStrip:
    def setup_method(self) -> None:
        _set_ctx(role=None)

    def teardown_method(self) -> None:
        _clear_ctx()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows")
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_nonadmin_cannot_strip_guardrail_binding(
        self,
        mock_replace_graph: AsyncMock,
        mock_guardrail_rows: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-309 PR A prove-the-fix: a NON-ADMIN MCP caller stripping a
        guardrail-bound node is denied. The enforcement lives in the SERVICE
        LAYER (``replace_pipeline_graph``, under the row lock); the MCP tool
        translates the ``GuardrailBindingStripDenied`` it raises into
        ``guardrail_strip_forbidden``. Without the service-layer guard this
        save would proceed (the bound node's guardrail would silently drop)."""
        from modulo.db.crud.hitl_gate_guard import GuardrailBindingStripDenied

        _set_ctx(role="operator")
        pipeline_id = uuid.uuid4()
        bound_node_id = uuid.uuid4()
        kept_node_id = uuid.uuid4()
        mock_get_pipeline.return_value = MagicMock(id=pipeline_id, owner_team_id=None)
        mock_guardrail_rows.return_value = [_guardrail_row(bound_node_id)]
        mock_session.return_value.__aenter__.return_value = AsyncMock()
        mock_replace_graph.side_effect = GuardrailBindingStripDenied(
            stripped_node_ids=[str(bound_node_id)],
            detail=(
                "Non-admin cannot strip a guardrail binding: removing node(s) "
                + str(bound_node_id)
                + " from the graph would drop a node-bound guardrail. Only an "
                "admin can remove a node that has a bound guardrail."
            ),
        )

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_graph_node(kept_node_id)],
            edges=[],
        )

        assert result["error"] == "guardrail_strip_forbidden", result
        assert "strip a guardrail binding" in result["detail"]
        assert str(bound_node_id) in result["detail"]
        mock_replace_graph.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows")
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_admin_can_strip_guardrail_binding(
        self,
        mock_replace_graph: AsyncMock,
        mock_guardrail_rows: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """An ADMIN MCP caller may remove a guardrail-bound node (admin owns
        guardrail management via ``guardrail.manage``)."""
        _set_ctx(role="admin")
        pipeline_id = uuid.uuid4()
        bound_node_id = uuid.uuid4()
        kept_node_id = uuid.uuid4()
        mock_get_pipeline.return_value = MagicMock(id=pipeline_id, owner_team_id=None)
        mock_guardrail_rows.return_value = [_guardrail_row(bound_node_id)]
        mock_replace_graph.return_value = ([_graph_node(kept_node_id)], [])
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_graph_node(kept_node_id)],
            edges=[],
        )

        assert "error" not in result, result
        assert result["pipeline_id"] == str(pipeline_id)
        mock_replace_graph.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows")
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_nonadmin_unrelated_graph_changes_allowed(
        self,
        mock_replace_graph: AsyncMock,
        mock_guardrail_rows: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A NON-ADMIN MCP caller making unrelated graph changes while KEEPING
        the guardrail-bound node is allowed — only guardrail-binding removal
        is protected."""
        _set_ctx(role="operator")
        pipeline_id = uuid.uuid4()
        bound_node_id = uuid.uuid4()
        kept_node_id = uuid.uuid4()
        mock_get_pipeline.return_value = MagicMock(id=pipeline_id, owner_team_id=None)
        mock_guardrail_rows.return_value = [_guardrail_row(bound_node_id)]
        nodes = [_graph_node(bound_node_id), _graph_node(kept_node_id)]
        mock_replace_graph.return_value = (nodes, [])
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=nodes,
            edges=[],
        )

        assert "error" not in result, result
        assert result["pipeline_id"] == str(pipeline_id)
        mock_replace_graph.assert_awaited_once()
