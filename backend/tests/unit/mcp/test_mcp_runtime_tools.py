"""Unit tests for the pipeline/HITL/library MCP tools without dedicated coverage."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import (
    copy_library_primitive,
    create_pipeline,
    delete_pipeline,
    get_pipeline_graph_tool,
    list_pending_hitl,
    list_pipelines_tool,
    review_hitl,
)
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    DecisionPayloadError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    NotTeamMemberError,
)
from modulo.core.mcp.scope_validator import MCPAuthorizationError

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_run_lookup_result(run: MagicMock | None = None) -> MagicMock:
    """A session.execute() result that resolves a run for the get_run boundary check."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = run if run is not None else MagicMock()
    return result


def _make_page_result(items: list, *, total: int | None = None, next_cursor=None, has_more: bool = False) -> MagicMock:
    result = MagicMock()
    result.items = items
    result.total = total if total is not None else len(items)
    result.next_cursor = next_cursor
    result.has_more = has_more
    return result


class _AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    def _set_role_operator(self) -> None:
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")


def _make_mock_edge() -> MagicMock:
    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = uuid.uuid4()
    edge.target_node_id = uuid.uuid4()
    edge.edge_type = "normal"
    return edge


# ---------------------------------------------------------------------------
# get_pipeline_graph
# ---------------------------------------------------------------------------


class TestGetPipelineGraph(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_pipeline_graph_tool(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_pipeline_graph_tool(pipeline_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"
        mock_session.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.get_pipeline_graph")
    @patch("modulo.api.mcp_server._session")
    async def test_pipeline_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline_graph: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_pipeline_graph.return_value = None

        pipeline_id = str(uuid.uuid4())
        result = await get_pipeline_graph_tool(pipeline_id=pipeline_id)

        assert result == {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.get_pipeline_graph")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_graph_shape(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline_graph: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        edges = [_make_mock_edge(), _make_mock_edge()]
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_pipeline_graph.return_value = ([{"id": "node-1", "node_type": "agent"}], edges)

        pipeline_id = str(uuid.uuid4())
        result = await get_pipeline_graph_tool(pipeline_id=pipeline_id)

        assert result["pipeline_id"] == pipeline_id
        assert result["nodes"] == [{"id": "node-1", "node_type": "agent"}]
        assert result["node_count"] == 1
        assert result["edge_count"] == 2
        assert result["edges"][0]["id"] == str(edges[0].id)
        assert result["edges"][0]["source_node_id"] == str(edges[0].source_node_id)
        assert result["edges"][0]["target_node_id"] == str(edges[0].target_node_id)
        assert result["edges"][0]["edge_type"] == "normal"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.get_pipeline_graph")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline_graph: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_pipeline_graph.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await get_pipeline_graph_tool(pipeline_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# list_pipelines
# ---------------------------------------------------------------------------


class TestListPipelines(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_pipelines_tool()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.list_pipelines")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_summary_shape(
        self,
        mock_session: AsyncMock,
        mock_list_pipelines: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline = MagicMock()
        pipeline.id = uuid.uuid4()
        pipeline.name = "my-pipeline"
        pipeline.visibility = "org"
        page = _make_page_result([pipeline], total=1, has_more=False)

        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_list_pipelines.return_value = page

        result = await list_pipelines_tool(limit=50)

        assert result["data"][0]["id"] == str(pipeline.id)
        assert result["data"][0]["name"] == "my-pipeline"
        assert result["data"][0]["visibility"] == "org"
        assert result["total"] == 1
        assert result["has_more"] is False
        mock_list_pipelines.assert_awaited_once()
        assert mock_list_pipelines.await_args.kwargs["page_size"] == 50

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.list_pipelines")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_list_pipelines: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_list_pipelines.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await list_pipelines_tool()

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# create_pipeline
# ---------------------------------------------------------------------------


class TestCreatePipeline(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        self._set_role_operator()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_pipeline(name="test")
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await create_pipeline(name="test")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_folder_id_returns_invalid_folder_id(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_pipeline(name="test", folder_id="not-a-uuid")
        assert result["error"] == "invalid_folder_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.create_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_created_pipeline_shape(
        self,
        mock_session: AsyncMock,
        mock_db_create_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        pipeline = MagicMock()
        pipeline.id = uuid.uuid4()
        pipeline.name = "test"
        pipeline.description = "desc"
        pipeline.visibility = "org"
        pipeline.max_concurrent_runs = 5
        pipeline.default_autonomy_level = "manual_approval"
        pipeline.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_create_pipeline.return_value = pipeline

        result = await create_pipeline(name="test", description="desc")

        assert result["id"] == str(pipeline.id)
        assert result["name"] == "test"
        assert result["description"] == "desc"
        assert result["visibility"] == "org"
        assert result["max_concurrent_runs"] == 5
        assert result["default_autonomy_level"] == "manual_approval"
        assert result["created_at"] == pipeline.created_at.isoformat()
        mock_db_create_pipeline.assert_awaited_once()
        assert mock_db_create_pipeline.await_args.kwargs["name"] == "test"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.create_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_db_create_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_create_pipeline.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await create_pipeline(name="test")

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# delete_pipeline
# ---------------------------------------------------------------------------


class TestDeletePipeline(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        self._set_role_operator()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await delete_pipeline(pipeline_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"
        mock_session.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.soft_delete_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_pipeline_not_found(
        self,
        mock_session: AsyncMock,
        mock_soft_delete_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_soft_delete_pipeline.return_value = None

        pipeline_id = str(uuid.uuid4())
        result = await delete_pipeline(pipeline_id=pipeline_id)

        assert result == {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.soft_delete_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_success_returns_deleted(
        self,
        mock_session: AsyncMock,
        mock_soft_delete_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_soft_delete_pipeline.return_value = MagicMock()

        pipeline_id = str(uuid.uuid4())
        result = await delete_pipeline(pipeline_id=pipeline_id)

        assert result == {"status": "deleted", "pipeline_id": pipeline_id}
        mock_soft_delete_pipeline.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.pipeline.soft_delete_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_soft_delete_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_soft_delete_pipeline.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# list_pending_hitl
# ---------------------------------------------------------------------------


def _make_hitl_gate(
    *,
    gate_id: str = "gate-1",
    account_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    required_team_id: uuid.UUID | None = None,
) -> MagicMock:
    gate = MagicMock()
    gate.run_id = uuid.uuid4()
    gate.gate_id = gate_id
    gate.pipeline_id = uuid.uuid4()
    gate.account_id = account_id
    gate.expires_at = expires_at
    gate.required_team_id = required_team_id
    return gate


class TestListPendingHitl(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_pending_hitl()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_pending_hitl()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_pending_gates(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        expires_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
        gates = [
            _make_hitl_gate(gate_id="gate-1", account_id=None, expires_at=expires_at, required_team_id=None),
            _make_hitl_gate(gate_id="gate-2", account_id=uuid.uuid4(), expires_at=None, required_team_id=uuid.uuid4()),
        ]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        gates_result = MagicMock()
        gates_result.scalars.return_value = gates

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[count_result, gates_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["page"] == 1
        assert result["page_size"] == 20
        assert result["total"] == 2
        assert result["has_more"] is False
        assert result["gates"][0]["run_id"] == str(gates[0].run_id)
        assert result["gates"][0]["gate_id"] == "gate-1"
        assert result["gates"][0]["pipeline_id"] == str(gates[0].pipeline_id)
        assert result["gates"][0]["claimed_by"] is None
        assert result["gates"][0]["expires_at"] == expires_at.isoformat()
        assert result["gates"][0]["required_team_id"] is None
        assert result["gates"][1]["claimed_by"] == str(gates[1].account_id)
        assert result["gates"][1]["expires_at"] is None
        assert result["gates"][1]["required_team_id"] == str(gates[1].required_team_id)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_empty_pending_returns_no_gates(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        gates_result = MagicMock()
        gates_result.scalars.return_value = []

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[count_result, gates_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert not result["gates"]
        assert result["total"] == 0
        assert result["has_more"] is False

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_has_more_true_when_total_exceeds_page(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        gates = [_make_hitl_gate(gate_id=f"gate-{i}") for i in range(20)]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 25
        gates_result = MagicMock()
        gates_result.scalars.return_value = gates

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[count_result, gates_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["total"] == 25
        assert len(result["gates"]) == 20
        assert result["has_more"] is True

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("boom")))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# review_hitl
# ---------------------------------------------------------------------------


class TestReviewHitl(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="claim")
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_id_returns_invalid_id(self, mock_validate_auth: AsyncMock) -> None:
        result = await review_hitl(run_id="not-a-uuid", gate_id="gate-1", action="claim")

        assert result["error"] == "invalid_id"
        assert result["field"] == "run_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_invalid_action(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="bogus")

        assert result["error"] == "invalid_action"
        mock_manager_cls.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope_for_claim(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="claim")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_approve_requires_claim_token(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="approve")

        assert result["error"] == "claim_token_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_reject_requires_claim_token(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="reject")

        assert result["error"] == "claim_token_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_deliver_manual_requires_claim_token(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="deliver_manual")

        assert result["error"] == "claim_token_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_deliver_manual_requires_output(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(
            run_id=str(uuid.uuid4()),
            gate_id="gate-1",
            action="deliver_manual",
            claim_token="tok",
        )

        assert result["error"] == "output_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_claim_success(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        claimed = MagicMock()
        claimed.claim_token = "tok-123"
        claimed.expires_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
        manager = MagicMock()
        manager.claim = AsyncMock(return_value=claimed)

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, gate_id="gate-1", action="claim")

        assert result["status"] == "claimed"
        assert result["claim_token"] == "tok-123"
        assert result["expires_at"] == claimed.expires_at.isoformat()
        manager.claim.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_approve_success(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_row_result = MagicMock()
        gate_row_result.scalar_one_or_none.return_value = None

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_row_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, gate_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "gate_id": "gate-1"}
        manager.approve.assert_awaited_once()
        # FAR-541: the persisted decision payload is stamped with the gate id.
        assert manager.approve.await_args.kwargs["decision_payload"] == {
            "action": "approved",
            "gate_id": "gate-1",
        }

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_reject_success(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.reject = AsyncMock()

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, gate_id="gate-1", action="reject", claim_token="tok-123")

        assert result == {"status": "rejected", "gate_id": "gate-1"}
        manager.reject.assert_awaited_once()
        # FAR-541: the persisted decision payload is stamped with the gate id.
        assert manager.reject.await_args.kwargs["decision_payload"] == {
            "action": "rejected",
            "gate_id": "gate-1",
        }

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_deliver_manual_success(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.deliver_manual = AsyncMock()

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(
            run_id=run_id,
            gate_id="gate-1",
            action="deliver_manual",
            claim_token="tok-123",
            output={"result": "ok"},
        )

        assert result == {"status": "delivered_manual", "gate_id": "gate-1"}
        manager.deliver_manual.assert_awaited_once()
        # FAR-541: the persisted decision payload is stamped with the gate id.
        assert manager.deliver_manual.await_args.kwargs["decision_payload"] == {
            "action": "deliver_manual",
            "gate_id": "gate-1",
            "output": {"result": "ok"},
        }

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_approve_blocks_human_only_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()

        gate_row = MagicMock()
        gate_row.pipeline_id = uuid.uuid4()
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_row_result = MagicMock()
        gate_row_result.scalar_one_or_none.return_value = gate_row
        edge = MagicMock()
        edge.hitl_gate_config = {"human_only": True}
        edge_result = MagicMock()
        edge_result.scalars.return_value.first.return_value = edge

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_row_result, edge_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, gate_id="gate-1", action="approve", claim_token="tok-123")

        assert result["error"] == "human_only_gate"
        manager.approve.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_gate_not_found(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run_id_uuid = uuid.uuid4()
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=GateNotFoundError(run_id_uuid, "gate-1"))

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(run_id_uuid)
        result = await review_hitl(run_id=run_id, gate_id="gate-1", action="claim")

        assert result["error"] == "gate_not_found"
        assert result["run_id"] == run_id
        assert result["gate_id"] == "gate-1"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_already_claimed(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=AlreadyClaimedError(uuid.uuid4(), "gate-1"))

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="claim")

        assert result["error"] == "already_claimed"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_not_team_member(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=NotTeamMemberError(uuid.uuid4(), "gate-1", uuid.uuid4(), uuid.uuid4()))

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="claim")

        assert result["error"] == "not_team_member"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_claim_token_invalid(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=ClaimTokenInvalidError())

        mock_sesh = AsyncMock()
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="approve", claim_token="bad")

        assert result["error"] == "claim_token_invalid"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_claim_token_expired(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.reject = AsyncMock(side_effect=ClaimTokenExpiredError())

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="reject", claim_token="stale")

        assert result["error"] == "claim_token_expired"
        assert "Re-claim" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_already_decided(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=GateAlreadyDecidedError(uuid.uuid4(), "gate-1"))

        mock_sesh = AsyncMock()
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="approve", claim_token="tok")

        assert result["error"] == "already_decided"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_decision_payload_refusal_maps_to_error_response(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-541 (iteration 4): a ``_decide`` refusal (DecisionPayloadError)
        surfaces as the MCP error shape (``invalid_decision_payload``) instead
        of an unhandled exception — mirroring the HTTP API's 422."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=DecisionPayloadError("decision_payload must be a JSON object"))

        mock_sesh = AsyncMock()
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(
            run_id=str(uuid.uuid4()),
            gate_id="gate-1",
            action="approve",
            claim_token="tok",
            output={"not": "relevant"},
        )

        assert result["error"] == "invalid_decision_payload"
        assert "must be a JSON object" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("boom")))

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="claim")

        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_internal_error(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=RuntimeError("boom"))

        mock_sesh = AsyncMock()
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), gate_id="gate-1", action="approve", claim_token="tok")

        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# copy_library_primitive
# ---------------------------------------------------------------------------


class TestCopyLibraryPrimitive(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_insufficient_scope(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await copy_library_primitive(primitive_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "primitive_id"
        mock_session.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.library_copy_to_adapt")
    @patch("modulo.api.mcp_server._session")
    async def test_not_found_when_primitive_missing(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_library_copy.side_effect = LookupError("missing")

        primitive_id = str(uuid.uuid4())
        result = await copy_library_primitive(primitive_id=primitive_id)

        assert result == {"error": "not_found", "primitive_id": primitive_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.library_copy_to_adapt")
    @patch("modulo.api.mcp_server._session")
    async def test_success_returns_copied_shape(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        copied = MagicMock()
        copied.id = uuid.uuid4()
        copied.name = "my-agent"
        copied.slug = "my-agent"
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_library_copy.return_value = copied

        primitive_id = str(uuid.uuid4())
        result = await copy_library_primitive(primitive_id=primitive_id)

        assert result["status"] == "copied"
        assert result["primitive_id"] == str(copied.id)
        assert result["name"] == "my-agent"
        assert result["slug"] == "my-agent"
        mock_library_copy.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.library_copy_to_adapt")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_library_copy.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        primitive_id = str(uuid.uuid4())
        result = await copy_library_primitive(primitive_id=primitive_id)

        assert result["error"] == "migration_required"
