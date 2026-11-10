"""Unit tests for the pipeline/HITL/library MCP tools without dedicated coverage."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from farna.api.mcp_server import (
    _resume_run_after_mcp_approval,
    copy_library_primitive,
    create_pipeline,
    delete_pipeline,
    get_pipeline_graph_tool,
    list_pending_hitl,
    list_pipelines_tool,
    review_hitl,
)
from farna.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    DecisionPayloadError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    NotTeamMemberError,
    RunNotAwaitingError,
)
from farna.core.mcp.scope_validator import MCPAuthorizationError
from farna.db.crud.hitl_review_config import EVENT_HUMAN_ONLY_DENIED
from farna.db.crud.hitl_review_config import MSG_HUMAN_ONLY_DENY as _MSG_HUMAN_ONLY_DENY
from tests.unit.api.mock_session import configure_mock_session, make_session_scope

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
    resolved_run = run if run is not None else MagicMock()
    if not isinstance(getattr(resolved_run, "claim_token", None), str):
        resolved_run.claim_token = "claim-current"
    result.scalar_one_or_none.return_value = resolved_run
    return result


def _stub_resume_executor(mock_build_resume_executor: MagicMock) -> MagicMock:
    """Wire ``mcp_server._build_resume_executor`` to a no-op executor (FAR-2328).

    Since FAR-2328 the MCP approve resumes the pipeline inline after the
    decision commits, so any test that reaches a successful approve must stub
    the executor rather than let it open a real DB connection.
    """
    executor = MagicMock()
    executor.resume = AsyncMock()
    mock_build_resume_executor.return_value = executor
    return executor


def _wire_restore_session(
    mock_sessionmaker: MagicMock,
    *,
    update_rowcount: int,
    row: tuple[str, str] | None,
) -> MagicMock:
    """Wire the restore helper's session: CAS UPDATE, then the F6a row re-read."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = _make_session_context(session)
    update_result = MagicMock(rowcount=update_rowcount)
    select_result = MagicMock()
    select_result.first.return_value = row
    session.execute = AsyncMock(side_effect=[update_result, select_result])
    mock_sessionmaker.return_value = MagicMock(return_value=session)
    return session


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
        from farna.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from farna.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    def _set_role_operator(self) -> None:
        from farna.api.mcp_server import _ctx_role

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
    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_pipeline_graph_tool(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_pipeline_graph_tool(pipeline_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"
        mock_session.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.get_pipeline_graph")
    @patch("farna.api.mcp_server._session")
    async def test_pipeline_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline_graph: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_pipeline_graph.return_value = None

        pipeline_id = str(uuid.uuid4())
        result = await get_pipeline_graph_tool(pipeline_id=pipeline_id)

        assert result == {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.get_pipeline_graph")
    @patch("farna.api.mcp_server._session")
    async def test_returns_graph_shape(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline_graph: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        edges = [_make_mock_edge(), _make_mock_edge()]
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
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

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.get_pipeline_graph")
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline_graph: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_pipeline_graph.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await get_pipeline_graph_tool(pipeline_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# list_pipelines
# ---------------------------------------------------------------------------


class TestListPipelines(_AuthContext):
    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_pipelines_tool()
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.list_pipelines")
    @patch("farna.api.mcp_server._session")
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

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
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

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.list_pipelines")
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_list_pipelines: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
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

    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_pipeline(name="test")
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(make_session_scope())
        with patch(
            "farna.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await create_pipeline(name="test")
        assert result["error"] == "insufficient_scope"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_folder_id_returns_invalid_folder_id(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_pipeline(name="test", folder_id="not-a-uuid")
        assert result["error"] == "invalid_folder_id"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.create_pipeline")
    @patch("farna.api.mcp_server._session")
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

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
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

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.create_pipeline")
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_db_create_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
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

    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(make_session_scope())
        with patch(
            "farna.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await delete_pipeline(pipeline_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"
        mock_session.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.soft_delete_pipeline")
    @patch("farna.api.mcp_server._session")
    async def test_pipeline_not_found(
        self,
        mock_session: AsyncMock,
        mock_soft_delete_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_soft_delete_pipeline.return_value = None

        pipeline_id = str(uuid.uuid4())
        result = await delete_pipeline(pipeline_id=pipeline_id)

        assert result == {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.soft_delete_pipeline")
    @patch("farna.api.mcp_server._session")
    async def test_success_returns_deleted(
        self,
        mock_session: AsyncMock,
        mock_soft_delete_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_soft_delete_pipeline.return_value = MagicMock()

        pipeline_id = str(uuid.uuid4())
        result = await delete_pipeline(pipeline_id=pipeline_id)

        assert result == {"status": "deleted", "pipeline_id": pipeline_id}
        mock_soft_delete_pipeline.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.db.crud.pipeline.soft_delete_pipeline")
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_soft_delete_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_soft_delete_pipeline.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# list_pending_hitl
# ---------------------------------------------------------------------------


def _make_hitl_review(
    *,
    review_id: str = "gate-1",
    account_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    required_team_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> MagicMock:
    gate = MagicMock()
    gate.run_id = run_id or uuid.uuid4()
    gate.review_id = review_id
    gate.pipeline_id = uuid.uuid4()
    gate.account_id = account_id
    gate.expires_at = expires_at
    gate.required_team_id = required_team_id
    gate.context_json = None
    return gate


class TestListPendingHitl(_AuthContext):
    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_pending_hitl()
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(make_session_scope())
        with patch(
            "farna.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_pending_hitl()
        assert result["error"] == "insufficient_scope"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_returns_pending_gates(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        expires_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
        shared_run_id = uuid.uuid4()
        gates = [
            _make_hitl_review(
                review_id="gate-1", account_id=None, expires_at=expires_at, required_team_id=None, run_id=shared_run_id
            ),
            _make_hitl_review(
                review_id="gate-2",
                account_id=uuid.uuid4(),
                expires_at=None,
                required_team_id=uuid.uuid4(),
                run_id=shared_run_id,
            ),
        ]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        gates_result = MagicMock()
        gates_result.scalars.return_value = gates
        # FAR-613/FAR-609: both pending-gate resolvers run two IN queries
        # (runs for snapshot ids, snapshots for graphs) after the gate page
        # loads. The fixture gates carry non-topology ids, so nothing resolves.
        run_rows_result = MagicMock()
        run_rows_result.all.return_value = [(shared_run_id, uuid.uuid4())]
        snapshot_rows_result = MagicMock()
        snapshot_rows_result.all.return_value = []

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                count_result,
                gates_result,
                run_rows_result,
                snapshot_rows_result,
                run_rows_result,
                snapshot_rows_result,
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["page"] == 1
        assert result["page_size"] == 20
        assert result["total"] == 2
        assert result["has_more"] is False
        assert result["reviews"][0]["run_id"] == str(gates[0].run_id)
        assert result["reviews"][0]["review_id"] == "gate-1"
        assert result["reviews"][0]["pipeline_id"] == str(gates[0].pipeline_id)
        assert result["reviews"][0]["claimed_by"] is None
        assert result["reviews"][0]["expires_at"] == expires_at.isoformat()
        assert result["reviews"][0]["required_team_id"] is None
        assert result["reviews"][1]["claimed_by"] == str(gates[1].account_id)
        assert result["reviews"][1]["expires_at"] is None
        assert result["reviews"][1]["required_team_id"] == str(gates[1].required_team_id)
        # FAR-613 briefing fields: unresolvable configs map to None.
        assert result["reviews"][0]["description"] is None
        assert result["reviews"][0]["context"] is None
        # FAR-609: an unresolvable gate-config falls back to the fail-safe
        # DEFAULT_HUMAN_ONLY (True) so MCP never under-reports a review.
        assert result["reviews"][0]["human_only"] is True
        assert result["reviews"][1]["human_only"] is True

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_pending_gates_carry_description_and_context(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-613: topology-shaped gates resolve their snapshot description;
        context comes from the claim row."""
        from farna.db.crud.hitl_review_config import make_review_id

        source, target = str(uuid.uuid4()), str(uuid.uuid4())
        resolved_review_id = make_review_id(source, target)
        context = {"trigger": "condition", "condition": "output.severity == 'high'"}
        gate = _make_hitl_review(review_id=resolved_review_id)
        gate.context_json = context
        graph = {
            "nodes": [],
            "edges": [
                {"source": source, "target": target, "hitl_review_config": {"description": "MCP briefing description."}}
            ],
        }
        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        gates_result = MagicMock()
        gates_result.scalars.return_value = [gate]
        snapshot_id = uuid.uuid4()
        run_rows_result = MagicMock()
        run_rows_result.all.return_value = [(gate.run_id, snapshot_id)]
        snapshot_rows_result = MagicMock()
        snapshot_rows_result.all.return_value = [(snapshot_id, graph)]

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                count_result,
                gates_result,
                run_rows_result,
                snapshot_rows_result,
                run_rows_result,
                snapshot_rows_result,
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["reviews"][0]["description"] == "MCP briefing description."
        assert result["reviews"][0]["context"] == context
        # FAR-609: a snapshot config without an explicit human_only flag falls
        # back to the fail-safe True default.
        assert result["reviews"][0]["human_only"] is True

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_empty_pending_returns_no_gates(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        gates_result = MagicMock()
        gates_result.scalars.return_value = []

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[count_result, gates_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert not result["reviews"]
        assert result["total"] == 0
        assert result["has_more"] is False

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_has_more_true_when_total_exceeds_page(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        shared_run_id = uuid.uuid4()
        gates = [_make_hitl_review(review_id=f"gate-{i}", run_id=shared_run_id) for i in range(20)]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 25
        gates_result = MagicMock()
        gates_result.scalars.return_value = gates
        run_rows_result = MagicMock()
        run_rows_result.all.return_value = [(shared_run_id, uuid.uuid4())]
        snapshot_rows_result = MagicMock()
        snapshot_rows_result.all.return_value = []

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                count_result,
                gates_result,
                run_rows_result,
                snapshot_rows_result,
                run_rows_result,
                snapshot_rows_result,
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["total"] == 25
        assert len(result["reviews"]) == 20
        assert result["has_more"] is True

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_pending_filter_matches_rest_actionable_run_statuses(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-612: the MCP pending list filters runs to the same actionable set
        (awaiting_human + claimed) as the REST org-wide queue — a plain
        not_in(TERMINAL_STATUSES) would still list gates on pending/running/
        unknown runs, which claim() refuses with run_not_awaiting."""
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        gates_result = MagicMock()
        gates_result.scalars.return_value = []

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[count_result, gates_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["total"] == 0
        sql = str(mock_sesh.execute.call_args_list[0][0][0].compile(compile_kwargs={"literal_binds": True}))
        assert "awaiting_human" in sql, f"pending filter must allow awaiting_human, got: {sql}"
        assert "claimed" in sql, f"pending filter must allow claimed, got: {sql}"
        assert "running" not in sql, f"pending filter must exclude non-actionable run statuses, got: {sql}"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("boom")))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_pending_hitl()

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# review_hitl
# ---------------------------------------------------------------------------


class TestReviewHitl(_AuthContext):
    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_id_returns_invalid_id(self, mock_validate_auth: AsyncMock) -> None:
        result = await review_hitl(run_id="not-a-uuid", review_id="gate-1", action="claim")

        assert result["error"] == "invalid_id"
        assert result["field"] == "run_id"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    async def test_invalid_action(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="bogus")

        assert result["error"] == "invalid_action"
        mock_manager_cls.assert_called_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_insufficient_scope_for_claim(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(make_session_scope())
        with patch(
            "farna.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")
        assert result["error"] == "insufficient_scope"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    async def test_approve_requires_claim_token(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="approve")

        assert result["error"] == "claim_token_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    async def test_reject_requires_claim_token(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="reject")

        assert result["error"] == "claim_token_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    async def test_deliver_manual_requires_claim_token(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="deliver_manual")

        assert result["error"] == "claim_token_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    async def test_deliver_manual_requires_output(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        result = await review_hitl(
            run_id=str(uuid.uuid4()),
            review_id="gate-1",
            action="deliver_manual",
            claim_token="tok",
        )

        assert result["error"] == "output_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
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

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="claim")

        assert result["status"] == "claimed"
        assert result["claim_token"] == "tok-123"
        assert result["expires_at"] == claimed.expires_at.isoformat()
        manager.claim.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_approve_success(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        events: list[str] = []
        manager = MagicMock()

        async def approve(*_args: Any, **_kwargs: Any) -> None:
            events.append("decision")

        manager.approve = AsyncMock(side_effect=approve)
        run = MagicMock()
        run.claim_token = "claim-current"
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = run
        gate_row_result = MagicMock()
        gate_row_result.scalar_one_or_none.return_value = None

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)

        async def close_session() -> None:
            events.append("commit")

        mock_sesh.commit = AsyncMock(side_effect=close_session)
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_row_result])
        session_context = _make_session_context(mock_sesh)

        async def exit_session(*_args: Any) -> bool:
            events.append("commit")
            return False

        session_context.__aexit__ = AsyncMock(side_effect=exit_session)
        mock_session.return_value = session_context
        mock_manager_cls.return_value = manager
        executor = _stub_resume_executor(mock_build_resume_executor)

        async def resume_after_commit(**_kwargs: Any) -> None:
            events.append("resume")

        executor.resume.side_effect = resume_after_commit

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "review_id": "gate-1", "run_id": run_id}
        manager.approve.assert_awaited_once()
        # FAR-541: the persisted decision payload is stamped with the gate id.
        assert manager.approve.await_args.kwargs["decision_payload"] == {
            "action": "approved",
            "review_id": "gate-1",
        }
        # FAR-2328: the approval resumes the pipeline inline, after the decision
        # commits, and the resumed payload is the committed decision itself.
        executor.resume.assert_awaited_once()
        assert executor.resume.await_args.kwargs["run_id"] == uuid.UUID(run_id)
        assert executor.resume.await_args.kwargs["resume_data"] == {"action": "approved", "review_id": "gate-1"}
        assert events == ["decision", "commit", "resume"]

    @patch("farna.api.routes.hitl._awaiting_human_has_committed_decision", new_callable=AsyncMock)
    @patch("farna.api.routes.hitl.async_sessionmaker")
    @patch("farna.api.mcp_server.get_or_create_engine")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_resume_failure_restores_run_for_reconcile(
        self,
        mock_build_resume_executor: MagicMock,
        mock_get_engine: MagicMock,
        mock_sessionmaker: MagicMock,
        mock_committed_decision: AsyncMock,
    ) -> None:
        """A post-claim resume failure restores a stale F6a candidate."""
        mock_committed_decision.return_value = True
        engine = MagicMock()
        mock_get_engine.return_value = engine
        executor = MagicMock()

        async def resume_after_claim(**_kwargs: Any) -> None:
            raise RuntimeError("graph compile failed")

        executor.resume = AsyncMock(side_effect=resume_after_claim)
        mock_build_resume_executor.return_value = executor
        session = _wire_restore_session(mock_sessionmaker, update_rowcount=1, row=("awaiting_human", "saq"))

        run_id = uuid.uuid4()
        result = await _resume_run_after_mcp_approval(
            org_id=_PLACEHOLDER_ORG_ID,
            run_id=run_id,
            review_id="gate-1",
            resume_data={"action": "approved", "review_id": "gate-1"},
            claim_token="claim-old",
        )

        assert result["resume_status"] == "failed"
        assert result["reconcile_status"] == "queued"
        assert "reconciliation sweep will retry" in result["detail"]
        executor.resume.assert_awaited_once()
        assert executor.resume.await_args.kwargs["claim_token"] == "claim-old"
        assert executor.resume.await_args.kwargs["expected_claim_status"] == "awaiting_human"
        assert session.execute.await_count == 2

        # The repair UPDATE is fenced to the exact executor lease it repairs,
        # and stamps the row back into the F6a-recoverable state.
        update_statement = session.execute.await_args_list[0].args[0]
        compiled = update_statement.compile()
        assert "claim-old" in compiled.params.values()
        assert "running" in compiled.params.values()
        assert compiled.params["status"] == "awaiting_human"
        assert compiled.params["dispatcher"] == "saq"
        assert compiled.params["completed_at"] is None
        assert compiled.params["heartbeat_at"] is not None

    @patch("farna.api.routes.hitl._awaiting_human_has_committed_decision", new_callable=AsyncMock)
    @patch("farna.api.routes.hitl.async_sessionmaker")
    @patch("farna.api.mcp_server.get_or_create_engine")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_resume_failure_before_claim_reports_reconcile_queued(
        self,
        mock_build_resume_executor: MagicMock,
        mock_get_engine: MagicMock,
        mock_sessionmaker: MagicMock,
        mock_committed_decision: AsyncMock,
    ) -> None:
        """A pre-claim failure leaves an already-recoverable row — not operator work.

        ``executor.resume`` can fail before its claim transaction commits (graph
        load, validation, connection loss). The repair UPDATE then matches no
        row, but the run is still ``awaiting_human`` with its committed decision
        and is therefore a valid F6a candidate. Recovery must be judged on that
        row state, never on the UPDATE's rowcount.
        """
        mock_committed_decision.return_value = True
        mock_get_engine.return_value = MagicMock()
        executor = MagicMock()
        executor.resume = AsyncMock(side_effect=RuntimeError("setup failed before claim"))
        mock_build_resume_executor.return_value = executor
        _wire_restore_session(mock_sessionmaker, update_rowcount=0, row=("awaiting_human", "saq"))

        result = await _resume_run_after_mcp_approval(
            org_id=_PLACEHOLDER_ORG_ID,
            run_id=uuid.uuid4(),
            review_id="gate-1",
            resume_data={"action": "approved", "review_id": "gate-1"},
            claim_token="claim-old",
        )

        assert result["resume_status"] == "failed"
        assert result["reconcile_status"] == "queued"
        assert "reconciliation sweep will retry" in result["detail"]
        assert "operator intervention" not in result["detail"]

    @patch("farna.api.routes.hitl._awaiting_human_has_committed_decision", new_callable=AsyncMock)
    @patch("farna.api.routes.hitl.async_sessionmaker")
    @patch("farna.api.mcp_server.get_or_create_engine")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_resume_failure_unrecoverable_reports_operator_intervention(
        self,
        mock_build_resume_executor: MagicMock,
        mock_get_engine: MagicMock,
        mock_sessionmaker: MagicMock,
        mock_committed_decision: AsyncMock,
    ) -> None:
        """Only a row that is genuinely not F6a-recoverable reports operator work."""
        mock_committed_decision.return_value = True
        mock_get_engine.return_value = MagicMock()
        executor = MagicMock()
        executor.resume = AsyncMock(side_effect=RuntimeError("successor owns the run"))
        mock_build_resume_executor.return_value = executor
        _wire_restore_session(mock_sessionmaker, update_rowcount=0, row=("running", "saq"))

        result = await _resume_run_after_mcp_approval(
            org_id=_PLACEHOLDER_ORG_ID,
            run_id=uuid.uuid4(),
            review_id="gate-1",
            resume_data={"action": "approved", "review_id": "gate-1"},
            claim_token="claim-old",
        )

        assert result["resume_status"] == "failed"
        assert result["reconcile_status"] == "failed"
        assert "operator intervention is required" in result["detail"]
        mock_committed_decision.assert_not_awaited()

    @patch("farna.api.mcp_server.get_or_create_engine")
    @patch("farna.api.mcp_server._build_resume_executor")
    @patch(
        "farna.api.mcp_server._restore_failed_mcp_resume_for_reconcile",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError("restore read failed"),
    )
    async def test_resume_failure_restore_error_fails_closed(
        self,
        mock_restore: AsyncMock,
        mock_build_resume_executor: MagicMock,
        mock_get_engine: MagicMock,
    ) -> None:
        """A failed repair read must report unrecoverable, never claim a retry."""
        mock_get_engine.return_value = MagicMock()
        executor = MagicMock()
        executor.resume = AsyncMock(side_effect=RuntimeError("legacy resume failed"))
        mock_build_resume_executor.return_value = executor

        result = await _resume_run_after_mcp_approval(
            org_id=_PLACEHOLDER_ORG_ID,
            run_id=uuid.uuid4(),
            review_id="gate-1",
            resume_data={"action": "approved", "review_id": "gate-1"},
            claim_token="claim-old",
        )

        assert result["reconcile_status"] == "failed"
        assert "operator intervention is required" in result["detail"]
        mock_restore.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    @patch(
        "farna.api.mcp_server._run_is_f6a_reconcile_candidate",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError("candidate read failed"),
    )
    async def test_approve_missing_claim_token_candidate_read_error_fails_closed(
        self,
        mock_candidate: AsyncMock,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A failed recoverability read must not claim the sweep will retry."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        run = MagicMock()
        run.claim_token = ""
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(run), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        _stub_resume_executor(mock_build_resume_executor)

        result = await review_hitl(
            run_id=str(uuid.uuid4()), review_id="gate-1", action="approve", claim_token="tok-123"
        )

        assert result["reconcile_status"] == "failed"
        assert "operator intervention is required" in result["detail"]
        mock_candidate.assert_awaited_once()

    @patch("farna.api.mcp_server.get_or_create_engine")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_resume_superseded_claim_reports_not_needed(
        self,
        mock_build_resume_executor: MagicMock,
        mock_get_engine: MagicMock,
    ) -> None:
        """A lost CAS reports superseded, not a failure needing reconcile."""
        from farna.core.pipeline_engine.errors import ResumeClaimSupersededError

        mock_get_engine.return_value = MagicMock()
        executor = MagicMock()
        run_id = uuid.uuid4()
        executor.resume = AsyncMock(side_effect=ResumeClaimSupersededError(run_id))
        mock_build_resume_executor.return_value = executor

        result = await _resume_run_after_mcp_approval(
            org_id=_PLACEHOLDER_ORG_ID,
            run_id=run_id,
            review_id="gate-1",
            resume_data={"action": "approved", "review_id": "gate-1"},
            claim_token="claim-old",
        )

        assert result == {
            "status": "approved",
            "review_id": "gate-1",
            "run_id": str(run_id),
            "resume_status": "superseded",
            "reconcile_status": "not_needed",
            "detail": "Approval recorded, but another resume claim won before inline execution started.",
        }
        executor.resume.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_approve_capacity_deferral_is_queued_success(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A committed approval survives a sandbox-capacity deferral."""
        from farna.core.pipeline_engine.executor import SandboxCapacityExceededError

        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        run = MagicMock()
        run.claim_token = "claim-current"
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(run), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        executor = _stub_resume_executor(mock_build_resume_executor)
        run_id = str(uuid.uuid4())
        executor.resume.side_effect = SandboxCapacityExceededError(_PLACEHOLDER_ORG_ID)

        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "queued", "run_id": run_id, "review_id": "gate-1"}
        manager.approve.assert_awaited_once()
        executor.resume.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_approve_parked_run_resumes_from_unparked_status(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A decision on a ``hitl_parked`` run resumes from the un-parked status."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        run = MagicMock()
        run.claim_token = "claim-current"
        run.status = "hitl_parked"
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(run), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        executor = _stub_resume_executor(mock_build_resume_executor)

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "review_id": "gate-1", "run_id": run_id}
        executor.resume.assert_awaited_once()
        assert executor.resume.await_args.kwargs["expected_claim_status"] == "awaiting_human"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    @patch("farna.api.mcp_server._run_is_f6a_reconcile_candidate", new_callable=AsyncMock)
    async def test_approve_missing_claim_token_uses_row_recoverability(
        self,
        mock_candidate: AsyncMock,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """Missing the captured token must not claim the run is unrecoverable.

        The row is already in the claimable HITL state with its committed
        decision, so dispatcher_reconcile will resume it. The MCP response must
        say so instead of demanding operator intervention.
        """
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        run = MagicMock()
        run.claim_token = ""
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(run), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        mock_candidate.return_value = True
        _stub_resume_executor(mock_build_resume_executor)

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result["status"] == "approved"
        assert result["run_id"] == run_id
        assert result["resume_status"] == "failed"
        assert result["reconcile_status"] == "queued"
        assert "operator intervention" not in result["detail"]
        mock_candidate.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    @patch("farna.api.mcp_server._run_is_f6a_reconcile_candidate", new_callable=AsyncMock)
    async def test_approve_missing_claim_token_unrecoverable_reports_operator(
        self,
        mock_candidate: AsyncMock,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A genuinely unreconcilable row still reports operator intervention."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        run = MagicMock()
        run.claim_token = ""
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(run), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        mock_candidate.return_value = False
        _stub_resume_executor(mock_build_resume_executor)

        result = await review_hitl(
            run_id=str(uuid.uuid4()), review_id="gate-1", action="approve", claim_token="tok-123"
        )

        assert result["reconcile_status"] == "failed"
        assert "operator intervention is required" in result["detail"]

    @patch("farna.api.mcp_server.validate_hitl_answer", new_callable=AsyncMock)
    async def test_dispatch_hitl_approve_without_resume_payload_uses_default(
        self,
        mock_validate_answer: AsyncMock,
    ) -> None:
        """The direct dispatch path (no inline resume payload) keeps the old payload."""
        from farna.api.mcp_server import _dispatch_hitl_approve

        mock_validate_answer.return_value = None
        manager = MagicMock()
        manager.approve = AsyncMock()
        run_id = uuid.uuid4()

        result = await _dispatch_hitl_approve(
            manager,
            AsyncMock(),
            rid=run_id,
            review_id="gate-1",
            org_id=_PLACEHOLDER_ORG_ID,
            claim_token="tok-123",
            actor_account_id=_PLACEHOLDER_USER_ID,
            answer=None,
            client_type="mcp",
        )

        assert result == {"status": "approved", "review_id": "gate-1"}
        assert manager.approve.await_args.kwargs["decision_payload"] == {
            "action": "approved",
            "review_id": "gate-1",
        }

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_reject_success(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.reject = AsyncMock()

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="reject", claim_token="tok-123")

        assert result == {"status": "rejected", "review_id": "gate-1"}
        manager.reject.assert_awaited_once()
        # FAR-541: the persisted decision payload is stamped with the gate id.
        assert manager.reject.await_args.kwargs["decision_payload"] == {
            "action": "rejected",
            "review_id": "gate-1",
        }

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_deliver_manual_success(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.deliver_manual = AsyncMock()

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        # 1: run lookup; 2: gate-config snapshot lookup (gate id "gate-1" is
        # not a hitl_review_* id, so the human_only check resolves None).
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(uuid.uuid4())
        result = await review_hitl(
            run_id=run_id,
            review_id="gate-1",
            action="deliver_manual",
            claim_token="tok-123",
            output={"result": "ok"},
        )

        assert result == {"status": "delivered_manual", "review_id": "gate-1"}
        manager.deliver_manual.assert_awaited_once()
        # FAR-541: the persisted decision payload is stamped with the gate id.
        assert manager.deliver_manual.await_args.kwargs["decision_payload"] == {
            "action": "deliver_manual",
            "review_id": "gate-1",
            "output": {"result": "ok"},
        }

    # -- FAR-611 review fix: audit actor / claimant attribution ---------------

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_approve_attributes_decision_to_account_id(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """The MCP approve's audit actor is the ACCOUNT id — AuditEvent.
        account_id is FK'd to accounts.id, so the org_api_keys row id (or the
        OAuth uuid(int=0) sentinel) would violate the FK and roll back the
        whole decision."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        _stub_resume_executor(mock_build_resume_executor)

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "review_id": "gate-1", "run_id": run_id}
        assert manager.approve.await_args.kwargs["actor_id"] == _PLACEHOLDER_USER_ID
        assert manager.approve.await_args.kwargs["client_type"] == "mcp"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_claim_claims_on_behalf_of_account_id(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """hitl_claims.account_id is FK'd to accounts.id — the claimant must be
        the account id, never the key id (REST passes principal.account_id)."""
        manager = MagicMock()
        manager.claim = AsyncMock(return_value=MagicMock(expires_at=None))
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")

        assert result["status"] == "claimed"
        assert manager.claim.await_args.kwargs["claimant_id"] == _PLACEHOLDER_USER_ID
        assert manager.claim.await_args.kwargs["client_type"] == "mcp"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_reject_and_deliver_manual_attribute_to_account_id(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """reject and deliver_manual carry the account id as the audit actor."""
        self._set_role_operator()
        manager = MagicMock()
        manager.reject = AsyncMock()
        manager.deliver_manual = AsyncMock()
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(),
                _make_run_lookup_result(),
                _make_run_lookup_result(None),
                _make_run_lookup_result(),
                _make_run_lookup_result(),
                _make_run_lookup_result(None),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result_reject = await review_hitl(
            run_id=str(uuid.uuid4()), review_id="gate-1", action="reject", claim_token="tok-123", reason="nope"
        )
        result_manual = await review_hitl(
            run_id=str(uuid.uuid4()),
            review_id="gate-1",
            action="deliver_manual",
            claim_token="tok-123",
            output={"result": "ok"},
        )

        assert result_reject == {"status": "rejected", "review_id": "gate-1"}
        assert manager.reject.await_args.kwargs["actor_id"] == _PLACEHOLDER_USER_ID
        assert result_manual == {"status": "delivered_manual", "review_id": "gate-1"}
        assert manager.deliver_manual.await_args.kwargs["actor_id"] == _PLACEHOLDER_USER_ID

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_oauth_sentinel_key_id_does_not_leak_into_audit(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """OAuth/JWT MCP sessions carry the uuid(int=0) key-id sentinel; the
        audit actor must STILL be the account id (the sentinel id exists in no
        accounts row, so writing it would FK-fail the decision)."""
        from farna.api.mcp_server import _ctx_key_id, _ctx_user_id

        self._set_role_operator()
        _ctx_key_id.set(uuid.UUID(int=0))
        account_id = uuid.uuid4()
        _ctx_user_id.set(account_id)
        manager = MagicMock()
        manager.approve = AsyncMock()
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        _stub_resume_executor(mock_build_resume_executor)

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "review_id": "gate-1", "run_id": run_id}
        assert manager.approve.await_args.kwargs["actor_id"] == account_id
        assert manager.approve.await_args.kwargs["actor_id"] != uuid.UUID(int=0)

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_approve_without_user_context_omits_actor(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A session with no account id (defensive: unreachable through the
        production auth paths, which all set one) omits the actor rather than
        writing a bogus id — the decision still succeeds."""
        from farna.api.mcp_server import _ctx_user_id

        self._set_role_operator()
        _ctx_user_id.set(None)
        manager = MagicMock()
        manager.approve = AsyncMock()
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(side_effect=[_make_run_lookup_result(), _make_run_lookup_result(None)])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        _stub_resume_executor(mock_build_resume_executor)

        run_id = str(uuid.uuid4())
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "review_id": "gate-1", "run_id": run_id}
        assert manager.approve.await_args.kwargs["actor_id"] is None

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_claim_without_user_context_fails_closed(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A claim cannot omit its claimant (hitl_claims.account_id defines
        claim ownership) and must never write a bogus id — it fails closed."""
        from farna.api.mcp_server import _ctx_user_id

        _ctx_user_id.set(None)
        manager = MagicMock()
        manager.claim = AsyncMock()
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")

        assert result["error"] == "no_user_context"
        manager.claim.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_approve_blocks_human_only_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-610: the gate config is resolved from the run's SNAPSHOT graph
        by edge topology (hitl_review_<source>_<target>), not from the first
        edge of the pipeline in arbitrary order."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result["error"] == "human_only_gate"
        manager.approve.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_approve_blocks_human_only_gate_when_first_edge_unconfigured(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-610 regression: the pipeline's FIRST edge has no
        hitl_review_config (the old ``.scalars().first()`` read exactly that edge
        and allowed the approve); the gated edge elsewhere in the graph IS
        human_only and must block."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                # First edge: no hitl_review_config — the pre-FAR-610 bug read
                # THIS edge's config and let the approve through.
                {"source": "n1", "target": "n2"},
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result["error"] == "human_only_gate"
        manager.approve.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_deliver_manual_blocks_human_only_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-610: deliver_manual is a decision like approve — human_only
        gates block it on MCP too."""
        self._set_role_operator()
        manager = MagicMock()
        manager.deliver_manual = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(
            run_id=str(run.id),
            review_id=review_id,
            action="deliver_manual",
            claim_token="tok-123",
            output={"result": "ok"},
        )

        assert result["error"] == "human_only_gate"
        manager.deliver_manual.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_claim_blocks_human_only_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-609: claim is human_only — an MCP client (always a
        non-browser credential) can neither CLAIM nor decide a human_only
        gate, so the claim action is denied like approve/deliver_manual."""
        self._set_role_operator()
        manager = MagicMock()
        manager.claim = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result["error"] == "human_only_gate"
        manager.claim.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_claim_blocks_gate_without_explicit_human_only(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-609 default flip: a gate config WITHOUT ``human_only`` is
        human-only, so an MCP claim is denied."""
        self._set_role_operator()
        manager = MagicMock()
        manager.claim = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"label": "Legacy"}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result["error"] == "human_only_gate"
        manager.claim.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_claim_allows_explicit_opt_out_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-609: an explicit ``human_only: false`` opts the gate out —
        the MCP claim proceeds (role permitting)."""
        self._set_role_operator()
        gate = MagicMock()
        gate.claim_token = "tok-claim"
        gate.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        manager = MagicMock()
        manager.claim = AsyncMock(return_value=gate)

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": False}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("status") == "claimed", result
        manager.claim.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_approve_unresolvable_fired_gate_fails_closed(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-610 review: the gate FIRED (claim row exists) but its config is
        unresolvable (no snapshot, no live edge, no live HITL-node config) —
        the human_only policy cannot be verified, so the approve is denied
        instead of silently allowed."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.owner_team_id = uuid.uuid4()  # team boundary check: no extra query
        run.snapshot_id = None
        run.pipeline_id = uuid.uuid4()

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        # 1: run lookup; 2: live-edge miss; 3: live graph_nodes_json miss;
        # 4: fail-closed claim lookup — the gate fired.
        # _make_run_lookup_result cannot produce a None row (it always yields a
        # truthy MagicMock), so the None results are built inline.
        none_result = MagicMock()
        none_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                none_result,
                none_result,
                none_result,
                _make_run_lookup_result(),  # truthy claim row
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result["error"] == "human_only_gate"
        assert "could not be resolved" in result["detail"]
        manager.approve.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    @patch("farna.api.mcp_server._build_resume_executor")
    async def test_approve_unresolvable_gate_without_claim_allowed(
        self,
        mock_build_resume_executor: MagicMock,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """No claim row → the gate never fired; the fail-closed check passes
        through and the manager validates the decision as before."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(return_value=MagicMock(status="approved"))

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.owner_team_id = uuid.uuid4()  # team boundary check: no extra query
        run.snapshot_id = None
        run.pipeline_id = uuid.uuid4()

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        # 1: run lookup; 2: live-edge miss; 3: live graph_nodes_json miss;
        # 4: fail-closed claim lookup — no claim row.
        none_result = MagicMock()
        none_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                none_result,
                none_result,
                none_result,
                none_result,
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager
        _stub_resume_executor(mock_build_resume_executor)

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result == {"status": "approved", "review_id": review_id, "run_id": str(run.id)}
        manager.approve.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_reject_denied_on_human_only_gate_without_human_linkage(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-609/FAR-2281: an unlinked MCP caller cannot reject a
        human_only gate. Reject is intercepted by the shared human_only gate
        check and also requires a claim token; human-linked authority is the
        explicit exception when the org flag is enabled."""
        self._set_role_operator()
        manager = MagicMock()
        manager.reject = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(
            run_id=str(run.id),
            review_id=review_id,
            action="reject",
            claim_token="tok-123",
            reason="not good",
        )

        assert result["error"] == "human_only_gate"
        manager.reject.assert_not_awaited()

    @patch("farna.core.audit_logger.append_audit_event", new_callable=AsyncMock)
    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_human_only_denial_appends_audit_event(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
        mock_append: AsyncMock,
    ) -> None:
        """FAR-634: every MCP human_only denial emits a warning log + the
        ``hitl.human_only_denied`` audit event with the denial fields."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run_id = uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run_id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result["error"] == "human_only_gate"
        mock_append.assert_awaited_once()
        kwargs = mock_append.await_args.kwargs
        assert kwargs["event_type"] == EVENT_HUMAN_ONLY_DENIED
        assert kwargs["resource_type"] == "run"
        assert kwargs["resource_id"] == run_id
        payload = kwargs["payload_json"]
        assert payload["run_id"] == str(run_id)
        assert payload["review_id"] == review_id
        assert payload["action"] == "approve"
        assert payload["surface"] == "mcp"
        assert payload["reason"] == _MSG_HUMAN_ONLY_DENY

    @patch(
        "farna.core.audit_logger.append_audit_event",
        new=AsyncMock(side_effect=RuntimeError("audit write boom")),
    )
    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_human_only_denial_survives_audit_failure(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-634 failure isolation: an audit write failure never changes the
        denial - the error dict still returns."""
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock()

        src = uuid.uuid4()
        tgt = uuid.uuid4()
        review_id = f"hitl_review_{src}_{tgt}"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.snapshot_id = uuid.uuid4()
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": True}},
            ],
        }

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634: the claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result["error"] == "human_only_gate"
        manager.approve.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_gate_not_found(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run_id_uuid = uuid.uuid4()
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=GateNotFoundError(run_id_uuid, "gate-1"))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        run_id = str(run_id_uuid)
        result = await review_hitl(run_id=run_id, review_id="gate-1", action="claim")

        assert result["error"] == "review_not_found"
        assert result["run_id"] == run_id
        assert result["review_id"] == "gate-1"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_already_claimed(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=AlreadyClaimedError(uuid.uuid4(), "gate-1"))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")

        assert result["error"] == "already_claimed"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_not_team_member(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=NotTeamMemberError(uuid.uuid4(), "gate-1", uuid.uuid4(), uuid.uuid4()))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")

        assert result["error"] == "not_team_member"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_claim_token_invalid(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=ClaimTokenInvalidError())

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="approve", claim_token="bad")

        assert result["error"] == "claim_token_invalid"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_claim_token_expired(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.reject = AsyncMock(side_effect=ClaimTokenExpiredError())

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="reject", claim_token="stale")

        assert result["error"] == "claim_token_expired"
        assert "Re-claim" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_already_decided(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=GateAlreadyDecidedError(uuid.uuid4(), "gate-1"))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="approve", claim_token="tok")

        assert result["error"] == "already_decided"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_run_not_awaiting(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-612: claiming a gate whose run is not ``awaiting_human`` surfaces
        as the MCP error shape ``run_not_awaiting`` with the run's actual status
        in the detail, mirroring the HTTP route's 409."""
        self._set_role_operator()
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=RunNotAwaitingError(uuid.uuid4(), "complete"))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")

        assert result["error"] == "run_not_awaiting"
        assert "complete" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
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

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(
            run_id=str(uuid.uuid4()),
            review_id="gate-1",
            action="approve",
            claim_token="tok",
            output={"not": "relevant"},
        )

        assert result["error"] == "invalid_decision_payload"
        assert "must be a JSON object" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        manager = MagicMock()
        manager.claim = AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("boom")))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_sesh.execute = AsyncMock(return_value=_make_run_lookup_result())
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="claim")

        assert result["error"] == "migration_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.HITLManager")
    @patch("farna.api.mcp_server._session")
    async def test_internal_error(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        self._set_role_operator()
        manager = MagicMock()
        manager.approve = AsyncMock(side_effect=RuntimeError("boom"))

        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = MagicMock()  # the run
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = None
        mock_sesh.execute = AsyncMock(side_effect=[run_result, gate_result])
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(uuid.uuid4()), review_id="gate-1", action="approve", claim_token="tok")

        assert result["error"] == "server_error"


# ---------------------------------------------------------------------------
# copy_library_primitive
# ---------------------------------------------------------------------------


class TestCopyLibraryPrimitive(_AuthContext):
    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_insufficient_scope(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with patch(
            "farna.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await copy_library_primitive(primitive_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "primitive_id"
        mock_session.assert_not_called()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.library_copy_to_adapt")
    @patch("farna.api.mcp_server._session")
    async def test_not_found_when_primitive_missing(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_library_copy.side_effect = LookupError("missing")

        primitive_id = str(uuid.uuid4())
        result = await copy_library_primitive(primitive_id=primitive_id)

        assert result == {"error": "not_found", "primitive_id": primitive_id}

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.get_primitive")
    @patch("farna.api.mcp_server.library_copy_to_adapt")
    @patch("farna.api.mcp_server._session")
    async def test_lifecycle_map_is_denied_when_dev_mode_off(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_get_primitive: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        primitive = MagicMock()
        primitive.primitive_type = "lifecycle_map"
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_primitive.return_value = primitive
        with patch("farna.api.mcp_server.get_settings", return_value=MagicMock(modulo_dev_mode=False)):
            result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"
        mock_library_copy.assert_not_awaited()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.get_primitive", return_value=None)
    @patch("farna.api.mcp_server.library_copy_to_adapt")
    @patch("farna.api.mcp_server._session")
    async def test_success_returns_copied_shape(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_get_primitive: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        copied = MagicMock()
        copied.id = uuid.uuid4()
        copied.name = "my-agent"
        copied.slug = "my-agent"
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_library_copy.return_value = copied

        primitive_id = str(uuid.uuid4())
        result = await copy_library_primitive(primitive_id=primitive_id)

        assert result["status"] == "copied"
        assert result["primitive_id"] == str(copied.id)
        assert result["name"] == "my-agent"
        assert result["slug"] == "my-agent"
        mock_library_copy.assert_awaited_once()

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    @patch("farna.api.mcp_server.library_copy_to_adapt")
    @patch("farna.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_library_copy: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_library_copy.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        primitive_id = str(uuid.uuid4())
        result = await copy_library_primitive(primitive_id=primitive_id)

        assert result["error"] == "migration_required"
