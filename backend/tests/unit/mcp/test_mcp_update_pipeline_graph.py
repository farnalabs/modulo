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


def _hitl_node(node_id: uuid.UUID, hitl_config: dict | None) -> dict:
    return {
        "id": str(node_id),
        "node_type": "hitl",
        "hitl_config": hitl_config,
        "position": {"x": 0, "y": 0},
    }


class TestUpdatePipelineGraphHitlDescription:
    """Review iteration 1, MAJOR-4 (FAR-613): the MCP graph-write path
    bypasses the REST Pydantic contract for node-level ``hitl_config`` (a
    plain ``dict[str, Any]``) and never runs the full graph validator, so the
    HITL gate-description requirement is enforced explicitly before the
    write. An agent-authored gate is exactly where an unexplained gate most
    needs its decision briefing."""

    def setup_method(self) -> None:
        _set_ctx(role=None)

    def teardown_method(self) -> None:
        _clear_ctx()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_undescribed_node_level_hitl_gate_rejected(
        self,
        mock_replace_graph: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A node-level ``hitl_config`` without a usable human description is
        rejected with the ``validation_failed`` error shape BEFORE the write —
        ``replace_pipeline_graph`` is never awaited, so no undescribed gate
        can reach persistence through the MCP authoring path."""
        _set_ctx(role="operator")
        node_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        mock_get_pipeline.return_value = MagicMock(id=pipeline_id, owner_team_id=None)
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_hitl_node(node_id, {"label": "Review"})],
            edges=[],
        )

        assert result["error"] == "validation_failed", result
        assert "human-provided description" in result["detail"]
        mock_replace_graph.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_described_node_level_hitl_gate_succeeds(
        self,
        mock_replace_graph: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A node-level gate whose description meets the minimum passes the
        MCP check and the write proceeds — the enforcement is scoped to the
        description rule only."""
        _set_ctx(role="operator")
        pipeline_id = uuid.uuid4()
        node_id = uuid.uuid4()
        described_config = {"label": "Review", "description": "Reviewer confirms the refund amount before payout."}
        mock_get_pipeline.return_value = MagicMock(id=pipeline_id, owner_team_id=None)
        mock_replace_graph.return_value = ([_hitl_node(node_id, described_config)], [])
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id=str(pipeline_id),
            nodes=[_hitl_node(node_id, described_config)],
            edges=[],
        )

        assert "error" not in result, result
        assert result["pipeline_id"] == str(pipeline_id)
        mock_replace_graph.assert_awaited_once()


# ---------------------------------------------------------------------------
# FAR-1181 — masking parity on the MCP read/write surfaces
# ---------------------------------------------------------------------------

_GCP_SECRET = "AIza" + "0123456789abcdef" * 3
_MASK_SECRET_NODE = {
    "id": "2c7e9a10-8f3a-4d61-9b2c-4a5e6f809010",
    "node_type": "agent",
    "agent_id": "11111111-1111-1111-1111-111111111111",
    "position": {"x": 0, "y": 0},
    "env_vars": {"GOOGLE_API_KEY": _GCP_SECRET, "APP_URL": "https://example.com"},
}


class TestMcpGraphMaskingParity:
    """FAR-1181: the MCP get_pipeline_graph / update_pipeline_graph tools must
    apply the SAME masking parity as the REST graph endpoints — a read never
    surfaces a raw node credential, and a write round-tripping the masked read
    never persists mask literals over the stored values."""

    def setup_method(self) -> None:
        _set_ctx(role="operator")

    def teardown_method(self) -> None:
        _clear_ctx()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None))
    @patch("modulo.db.crud.pipeline.get_pipeline_graph")
    async def test_get_pipeline_graph_masks_node_env(
        self,
        mock_get_graph: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import get_pipeline_graph_tool
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        mock_session.return_value.__aenter__.return_value = AsyncMock()
        mock_get_graph.return_value = ([{**_MASK_SECRET_NODE}], [])

        result = await get_pipeline_graph_tool(pipeline_id="2c7e9a10-8f3a-4d61-9b2c-4a5e6f809099")

        assert "error" not in result, result
        env = result["nodes"][0]["env_vars"]
        assert env["GOOGLE_API_KEY"] == SENSITIVE_VALUE_MASK
        # Plain values pass through untouched (no over-masking).
        assert env["APP_URL"] == "https://example.com"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows")
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_update_resolves_masked_echo_and_masks_response(
        self,
        mock_replace_graph: AsyncMock,
        mock_guardrail_rows: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import update_pipeline_graph
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        secret_node = {**_MASK_SECRET_NODE}
        mock_get_pipeline.return_value = MagicMock(
            id=uuid.uuid4(), owner_team_id=None, graph_nodes_json=[{**secret_node}]
        )
        mock_replace_graph.return_value = ([{**secret_node}], [])
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        masked_node = {
            **_MASK_SECRET_NODE,
            "env_vars": {
                "GOOGLE_API_KEY": SENSITIVE_VALUE_MASK,
                "APP_URL": "https://example.com",
            },
        }
        result = await update_pipeline_graph(
            pipeline_id="2c7e9a10-8f3a-4d61-9b2c-4a5e6f809099",
            nodes=[masked_node],
            edges=[],
        )

        assert "error" not in result, result
        written = mock_replace_graph.await_args.kwargs["nodes"]
        # The mask echo was resolved against the stored graph BEFORE the write.
        assert written[0]["env_vars"]["GOOGLE_API_KEY"] == _GCP_SECRET
        # The tool response re-masked the node.
        assert result["nodes"][0]["env_vars"]["GOOGLE_API_KEY"] == SENSITIVE_VALUE_MASK

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.pipeline.get_pipeline")
    @patch("modulo.core.team_visibility.find_connector_team_mismatches", return_value=[])
    @patch("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows")
    @patch("modulo.db.crud.pipeline.replace_pipeline_graph")
    async def test_update_drops_masked_echo_without_stored_counterpart(
        self,
        mock_replace_graph: AsyncMock,
        mock_guardrail_rows: AsyncMock,
        mock_find_mismatches: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import update_pipeline_graph
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        mock_get_pipeline.return_value = MagicMock(
            id=uuid.uuid4(), owner_team_id=None, graph_nodes_json=[{**_MASK_SECRET_NODE}]
        )
        mock_replace_graph.return_value = ([{**_MASK_SECRET_NODE}], [])
        mock_session.return_value.__aenter__.return_value = AsyncMock()

        result = await update_pipeline_graph(
            pipeline_id="2c7e9a10-8f3a-4d61-9b2c-4a5e6f809099",
            nodes=[
                {
                    **_MASK_SECRET_NODE,
                    "env_vars": {
                        "RABBITMQ_URL": SENSITIVE_VALUE_MASK,  # no stored counterpart
                    },
                }
            ],
            edges=[],
        )

        assert "error" not in result, result
        written = mock_replace_graph.await_args.kwargs["nodes"]
        # The unverifiable mask echo is dropped, never persisted as a value.
        assert "RABBITMQ_URL" not in written[0]["env_vars"]
