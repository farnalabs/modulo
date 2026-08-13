"""Team-boundary enforcement for team-scoped API keys on MCP pipeline/run tools.

PRD 9.3: a team-scoped API key (non-null ``OrgApiKey.team_id``) must be
restricted to resources accessible to that team under the key's embedded
role, while an org-wide key (NULL ``team_id``) keeps the org-level role with
no team boundary.

``McpAuthMiddleware`` now propagates the key's ``team_id`` into the request
context via ``_ctx_team_id``; tool handlers enforce the boundary by resolving
each resource's ``owner_team_id`` and rejecting access to pipelines/runs owned
by a different team with a ``team_boundary_violation`` error.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    _ctx_team_id,
    _ctx_team_id_val,
    _team_scope_error,
    _team_scoped_key_mismatch,
    bind_connector_to_node,
    cancel_run,
    create_trigger,
    delete_pipeline,
    delete_trigger,
    get_pipeline_graph_tool,
    get_run_evals,
    get_run_output,
    get_run_status,
    list_pending_hitl,
    list_pipelines_tool,
    list_runs,
    list_trigger_events,
    list_triggers,
    query_analytics,
    resource_hitl_gate,
    resource_pipeline_detail,
    resource_pipeline_runs,
    resource_pipeline_snapshot_detail,
    resource_pipeline_snapshots,
    resource_pipelines,
    resource_run,
    review_hitl,
    trigger_pipeline,
    update_pipeline_graph,
    update_trigger,
)

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PLACEHOLDER_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_execute_result(value: Any) -> AsyncMock:
    """A session.execute() result whose sync scalar_one_or_none() returns *value*."""
    result = AsyncMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _make_run(
    *,
    pipeline_id: uuid.UUID,
    status: str = "pending",
    owner_team_id: uuid.UUID | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = pipeline_id
    run.status = status
    run.owner_team_id = owner_team_id
    run.trigger_type = "manual"
    run.created_at = datetime.now(UTC)
    run.started_at = None
    run.completed_at = None
    run.error_code = None
    run.outputs_json = {}
    run.node_telemetry_json = None
    run.node_token_usage = {}
    run.cost_breakdown = None
    return run


class _AuthContext:
    """Set/teardown the MCP ContextVars the tool handlers read for enforcement."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_key_id,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_key_id.set(_PLACEHOLDER_KEY_ID)
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")
        _ctx_team_id.set(None)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_key_id,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_key_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_team_id.set(None)


class TestTeamScopeHelpers:
    def test_team_id_ctxvar_defaults_to_none(self) -> None:
        ctx = pytest.importorskip("contextvars").Context()
        assert ctx.run(_ctx_team_id_val) is None

    def test_no_mismatch_for_org_wide_key(self) -> None:
        token = _ctx_team_id.set(None)
        try:
            assert _team_scoped_key_mismatch(_TEAM_A) is False
            assert _team_scoped_key_mismatch(None) is False
        finally:
            _ctx_team_id.reset(token)

    def test_no_mismatch_when_owner_team_matches(self) -> None:
        token = _ctx_team_id.set(_TEAM_A)
        try:
            assert _team_scoped_key_mismatch(_TEAM_A) is False
        finally:
            _ctx_team_id.reset(token)

    def test_no_mismatch_for_org_level_resource(self) -> None:
        # An org-level pipeline (no owner team) is accessible to any team key.
        token = _ctx_team_id.set(_TEAM_A)
        try:
            assert _team_scoped_key_mismatch(None) is False
        finally:
            _ctx_team_id.reset(token)

    def test_mismatch_when_different_team(self) -> None:
        token = _ctx_team_id.set(_TEAM_A)
        try:
            assert _team_scoped_key_mismatch(_TEAM_B) is True
        finally:
            _ctx_team_id.reset(token)

    def test_team_scope_error_surfaces_boundary(self) -> None:
        token = _ctx_team_id.set(_TEAM_A)
        try:
            err = _team_scope_error("pipeline", "abc")
            assert err["error"] == "team_boundary_violation"
            assert str(_TEAM_A) in err["detail"]
            assert "pipeline abc" in err["detail"]
        finally:
            _ctx_team_id.reset(token)


class TestTriggerPipelineTeamScope(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_cannot_trigger_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_B
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_pipeline") as mock_get_pipeline,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph") as mock_snapshot,
            patch("modulo.db.crud.run.create_run") as mock_create_run,
            patch("modulo.api.mcp_server.dispatch_run") as mock_dispatch,
        ):
            mock_get_pipeline.return_value = mock_pipeline
            mock_session.return_value = _make_session_context(session)
            result = await trigger_pipeline(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"
        mock_snapshot.assert_not_called()
        mock_create_run.assert_not_called()
        mock_dispatch.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_can_trigger_own_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_A
        session = AsyncMock()
        mock_snap = MagicMock()
        mock_snap.graph_json = {"nodes": {"n1": {}}}
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_run.langgraph_thread_id = str(uuid.uuid4())
        with (
            patch("modulo.api.mcp_server.get_pipeline") as mock_get_pipeline,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph") as mock_snapshot,
            patch("modulo.db.crud.run.create_run") as mock_create_run,
            patch("modulo.api.mcp_server.dispatch_run") as mock_dispatch,
        ):
            mock_get_pipeline.return_value = mock_pipeline
            mock_snapshot.return_value = mock_snap
            mock_create_run.return_value = mock_run
            mock_session.return_value = _make_session_context(session)
            result = await trigger_pipeline(pipeline_id=str(pipeline_id))

        assert result["run_id"] == str(mock_run.id)
        mock_dispatch.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_org_wide_key_has_no_team_boundary(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(None)
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_B  # other team — still allowed for org-wide key
        session = AsyncMock()
        mock_snap = MagicMock()
        mock_snap.graph_json = {"nodes": {"n1": {}}}
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_run.langgraph_thread_id = str(uuid.uuid4())
        with (
            patch("modulo.api.mcp_server.get_pipeline") as mock_get_pipeline,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph") as mock_snapshot,
            patch("modulo.db.crud.run.create_run") as mock_create_run,
            patch("modulo.api.mcp_server.dispatch_run") as mock_dispatch,
        ):
            mock_get_pipeline.return_value = mock_pipeline
            mock_snapshot.return_value = mock_snap
            mock_create_run.return_value = mock_run
            mock_session.return_value = _make_session_context(session)
            result = await trigger_pipeline(pipeline_id=str(pipeline_id))

        assert result["run_id"] == str(mock_run.id)
        mock_dispatch.assert_awaited_once()


class TestPipelineGraphTeamScope(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_cannot_read_other_teams_graph(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)) as mock_owner,
            patch("modulo.db.crud.pipeline.get_pipeline_graph", AsyncMock()) as mock_graph,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await get_pipeline_graph_tool(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"
        mock_owner.assert_awaited_once()
        mock_graph.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_org_wide_key_reads_graph_regardless_of_owner(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(None)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
            patch("modulo.db.crud.pipeline.get_pipeline_graph", AsyncMock(return_value=([], []))),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await get_pipeline_graph_tool(pipeline_id=str(pipeline_id))

        assert result["node_count"] == 0
        assert result["edges"] == []


class TestRunToolsTeamScope(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_status_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await get_run_status(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_status_allowed_for_own_team_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        run = _make_run(pipeline_id=uuid.uuid4(), status="complete", owner_team_id=_TEAM_A)
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_get_run.return_value = run
            mock_session.return_value = _make_session_context(session)
            result = await get_run_status(run_id=str(run_id), detail=False)

        assert result["run_id"] == str(run.id)
        assert result["status"] == "complete"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_output_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await get_run_output(run_id=str(run_id), node_id="n1")

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_evals_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.eval_run.get_run_evals", AsyncMock()) as mock_evals,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await get_run_evals(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"
        mock_evals.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_cancel_run_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.db.crud.run.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.run.request_cancellation", AsyncMock()) as mock_cancel,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await cancel_run(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"
        mock_cancel.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_status_blocked_when_run_owner_unstamped_but_pipeline_other_team(
        self, mock_validate_auth: AsyncMock
    ) -> None:
        # Pre-stamp production state: Run.owner_team_id is NULL (never written
        # by any code path before the create_run inheritance fix). The guard
        # must NOT treat a NULL stamp as org-level — it falls back to the
        # pipeline's owner team and still blocks the cross-team read.
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)) as mock_owner,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=None)
            mock_session.return_value = _make_session_context(session)
            result = await get_run_status(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"
        mock_owner.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_status_allowed_when_run_owner_unstamped_and_pipeline_org_level(
        self, mock_validate_auth: AsyncMock
    ) -> None:
        # An unstamped run whose pipeline is org-level (no owner team) stays
        # accessible to a team-scoped key — matches the org-level semantics.
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        run = _make_run(pipeline_id=uuid.uuid4(), status="complete", owner_team_id=None)
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=None)) as mock_owner,
        ):
            mock_get_run.return_value = run
            mock_session.return_value = _make_session_context(session)
            result = await get_run_status(run_id=str(run_id), detail=False)

        assert result["run_id"] == str(run.id)
        assert result["status"] == "complete"
        mock_owner.assert_awaited_once()


class _OperatorAuthContext(_AuthContext):
    """Auth context with an operator-role key (mutating tools need operator)."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_role

        super().setup_method()
        _ctx_role.set("operator")


class TestUpdatePipelineGraphTeamScope(_OperatorAuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_cannot_update_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_B
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.routes.pipelines._is_privileged", return_value=False),
            patch("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=mock_pipeline)) as mock_get_pipeline,
            patch("modulo.core.team_visibility.find_connector_team_mismatches", AsyncMock(return_value=[])),
            patch("modulo.db.crud.pipeline.replace_pipeline_graph", AsyncMock()) as mock_replace,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await update_pipeline_graph(pipeline_id=str(pipeline_id), nodes=[], edges=[])

        assert result["error"] == "team_boundary_violation"
        mock_get_pipeline.assert_awaited_once()
        mock_replace.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_can_update_own_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_A
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.routes.pipelines._is_privileged", return_value=False),
            patch("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=mock_pipeline)) as mock_get_pipeline,
            patch("modulo.core.team_visibility.find_connector_team_mismatches", AsyncMock(return_value=[])),
            patch(
                "modulo.db.crud.pipeline.replace_pipeline_graph",
                AsyncMock(return_value=([], [])),
            ) as mock_replace,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await update_pipeline_graph(pipeline_id=str(pipeline_id), nodes=[], edges=[])

        assert result["node_count"] == 0
        mock_get_pipeline.assert_awaited_once()
        mock_replace.assert_awaited_once()


class TestBindConnectorToNodeTeamScope(_OperatorAuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_cannot_bind_to_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_B
        session = AsyncMock()
        connector = MagicMock()
        connector.organisation_id = _PLACEHOLDER_ORG_ID
        session.execute.return_value = _make_execute_result(mock_pipeline)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.db.crud.connector_instance.get_connector_instance",
                AsyncMock(return_value=connector),
            ) as mock_connector,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await bind_connector_to_node(
                pipeline_id=str(pipeline_id),
                node_id=str(uuid.uuid4()),
                connector_type="github",
                connector_instance_id=str(uuid.uuid4()),
            )

        assert result["error"] == "team_boundary_violation"
        mock_connector.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_scoped_key_can_bind_to_own_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_pipeline.owner_team_id = _TEAM_A
        mock_pipeline.graph_nodes_json = []
        session = AsyncMock()
        connector = MagicMock()
        connector.organisation_id = _PLACEHOLDER_ORG_ID
        connector.visibility = "org"
        connector.owner_team_id = None
        connector.name = "github-conn"
        session.execute.return_value = _make_execute_result(mock_pipeline)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.db.crud.connector_instance.get_connector_instance",
                AsyncMock(return_value=connector),
            ),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await bind_connector_to_node(
                pipeline_id=str(pipeline_id),
                node_id=str(node_id),
                connector_type="github",
                connector_instance_id=str(uuid.uuid4()),
            )

        assert result["error"] == "node_not_found"


class TestListDeletePipelineTeamScope(_OperatorAuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_delete_pipeline_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
            patch("modulo.db.crud.pipeline.soft_delete_pipeline", AsyncMock()) as mock_delete,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await delete_pipeline(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"
        mock_delete.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_pipelines_passes_team_id_for_team_scoped_key(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        mock_page = MagicMock()
        mock_page.items = []
        mock_page.total = 0
        mock_page.next_cursor = None
        mock_page.has_more = False
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.pipeline.list_pipelines", AsyncMock(return_value=mock_page)) as mock_list,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_pipelines_tool()

        assert result["total"] == 0
        mock_list.assert_awaited_once()
        assert mock_list.await_args.kwargs["team_id"] == _TEAM_A

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_pipelines_passes_none_team_id_for_org_wide_key(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(None)
        session = AsyncMock()
        mock_page = MagicMock()
        mock_page.items = []
        mock_page.total = 0
        mock_page.next_cursor = None
        mock_page.has_more = False
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.pipeline.list_pipelines", AsyncMock(return_value=mock_page)) as mock_list,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_pipelines_tool()

        assert result["total"] == 0
        mock_list.assert_awaited_once()
        assert mock_list.await_args.kwargs["team_id"] is None

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_runs_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_runs(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_runs_passes_team_id_when_no_pipeline_filter(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        mock_page = MagicMock()
        mock_page.items = []
        mock_page.total = 0
        mock_page.next_cursor = None
        mock_page.has_more = False
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.run.list_runs", AsyncMock(return_value=mock_page)) as mock_list,
            patch("modulo.db.crud.run.get_child_run_rollup", AsyncMock(return_value={})),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_runs()

        assert result["total"] == 0
        mock_list.assert_awaited_once()
        assert mock_list.await_args.kwargs["team_id"] == _TEAM_A


class TestListPendingHitlTeamScope(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_pending_hitl_filters_by_team_for_team_scoped_key(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one.return_value = 0
        gates_result = MagicMock()
        gates_result.scalars.return_value = []
        session.execute.side_effect = [total_result, gates_result]
        with patch("modulo.api.mcp_server._session") as mock_session:
            mock_session.return_value = _make_session_context(session)
            result = await list_pending_hitl()

        assert result["total"] == 0
        assert result["gates"] == []

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_pending_hitl_org_wide_key_no_team_filter(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(None)
        session = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one.return_value = 1
        gates_result = MagicMock()
        gates_result.scalars.return_value = [MagicMock()]
        session.execute.side_effect = [total_result, gates_result]
        with patch("modulo.api.mcp_server._session") as mock_session:
            mock_session.return_value = _make_session_context(session)
            result = await list_pending_hitl()

        assert result["total"] == 1


class TestTriggerTeamScope(_OperatorAuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_create_trigger_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await create_trigger(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_triggers_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_triggers(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_triggers_passes_team_id_when_no_pipeline_filter(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        mock_page = MagicMock()
        mock_page.items = []
        mock_page.total = 0
        mock_page.next_cursor = None
        mock_page.has_more = False
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.trigger.list_triggers", AsyncMock(return_value=mock_page)) as mock_list,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_triggers()

        assert result["total"] == 0
        mock_list.assert_awaited_once()
        assert mock_list.await_args.kwargs["team_id"] == _TEAM_A

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_update_trigger_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        trigger_id = uuid.uuid4()
        trigger = MagicMock()
        trigger.pipeline_id = uuid.uuid4()
        session = AsyncMock()
        session.execute.return_value = _make_execute_result(trigger)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await update_trigger(trigger_id=str(trigger_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_delete_trigger_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        trigger_id = uuid.uuid4()
        trigger = MagicMock()
        trigger.pipeline_id = uuid.uuid4()
        session = AsyncMock()
        session.execute.return_value = _make_execute_result(trigger)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
            patch("modulo.db.crud.trigger.soft_delete_trigger", AsyncMock()) as mock_delete,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await delete_trigger(trigger_id=str(trigger_id))

        assert result["error"] == "team_boundary_violation"
        mock_delete.assert_not_awaited()


class TestQueryAnalyticsTeamScope(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_query_analytics_passes_team_id_for_team_scoped_key(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.get_settings", return_value=MagicMock()),
            patch("modulo.db.crud.organisation.get_organisation", AsyncMock(return_value=MagicMock())),
            patch(
                "modulo.core.feature_flags.resolve_plan_context",
                AsyncMock(
                    return_value=MagicMock(
                        feature_enabled=MagicMock(return_value=True),
                    )
                ),
            ),
            patch("modulo.api.mcp_server.run_analytics_query", AsyncMock(return_value={"buckets": []})) as mock_query,
            patch("modulo.api.mcp_server._get_session_factory", return_value=MagicMock()),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await query_analytics()

        assert result["buckets"] == []
        mock_query.assert_awaited_once()
        assert mock_query.await_args.kwargs["params"].team_id == _TEAM_A


class TestReviewHitlTeamScope(_OperatorAuthContext):
    """review_hitl must reject cross-team gate mutations (PR #1117 review finding 1)."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_claim_blocked_for_other_teams_run(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await review_hitl(run_id=str(run_id), gate_id="gate-1", action="claim")

        assert result["error"] == "team_boundary_violation"
        mock_manager_cls.return_value.claim.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_claim_blocked_for_org_level_gate_on_other_teams_run(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        # The common case: an org-level gate (required_team_id IS NULL) sits on
        # team-B's run; a team-A key must still be blocked. required_team_id is
        # a different concept from the run's owner.
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await review_hitl(run_id=str(run_id), gate_id="gate-1", action="approve", claim_token="tok")

        assert result["error"] == "team_boundary_violation"
        mock_manager_cls.return_value.approve.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_claim_blocked_when_run_unstamped_but_pipeline_other_team(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        # Pre-stamp runs (NULL Run.owner_team_id) must fall back to the
        # pipeline owner — a NULL stamp never widens the boundary.
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=None)
            mock_session.return_value = _make_session_context(session)
            result = await review_hitl(run_id=str(run_id), gate_id="gate-1", action="claim")

        assert result["error"] == "team_boundary_violation"
        mock_manager_cls.return_value.claim.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    async def test_claim_allowed_for_own_team_run(
        self,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        claimed = MagicMock()
        claimed.claim_token = "tok-123"
        claimed.expires_at = None
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_A)
            mock_session.return_value = _make_session_context(session)
            mock_manager_cls.return_value.claim = AsyncMock(return_value=claimed)
            result = await review_hitl(run_id=str(run_id), gate_id="gate-1", action="claim")

        assert result["status"] == "claimed"
        mock_manager_cls.return_value.claim.assert_awaited_once()


class TestListTriggerEventsTeamScope(_AuthContext):
    """list_trigger_events must apply the same team boundary as list_triggers."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_cross_team_pipeline_filter_blocked(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_trigger_events(pipeline_id=str(pipeline_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_cross_team_trigger_filter_blocked(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        trigger_id = uuid.uuid4()
        trigger = MagicMock()
        trigger.pipeline_id = uuid.uuid4()
        session = AsyncMock()
        session.execute.return_value = _make_execute_result(trigger)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_trigger_events(trigger_id=str(trigger_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_soft_deleted_trigger_blocked_for_team_scoped_key(self, mock_validate_auth: AsyncMock) -> None:
        """A soft-deleted/unresolvable trigger fails closed for a team-scoped key.

        The ownership lookup filters ``Trigger.deleted_at.is_(None)``, so a
        soft-deleted cross-team trigger resolves to None and is treated as out
        of the key's team boundary instead of falling through to an unfiltered
        event listing.
        """
        _ctx_team_id.set(_TEAM_A)
        trigger_id = uuid.uuid4()
        session = AsyncMock()
        session.execute.return_value = _make_execute_result(None)
        with patch("modulo.api.mcp_server._session") as mock_session:
            mock_session.return_value = _make_session_context(session)
            result = await list_trigger_events(trigger_id=str(trigger_id))

        assert result["error"] == "team_boundary_violation"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_trigger_id_query_carries_team_clause(self, mock_validate_auth: AsyncMock) -> None:
        """The deleted-trigger query must carry the team clause for a team-scoped key."""
        from sqlalchemy.dialects import postgresql

        _ctx_team_id.set(_TEAM_A)
        trigger_id = uuid.uuid4()
        trigger = MagicMock()
        trigger.pipeline_id = uuid.uuid4()
        session = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one_or_none.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        captured: list = []

        async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
            captured.append(stmt)
            s = str(stmt).lower()
            if "count(" in s:
                return total_result
            if "from triggers" in s and "trigger_events" not in s:
                return _make_execute_result(trigger)
            return rows_result

        session.execute = fake_execute
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_A)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await list_trigger_events(trigger_id=str(trigger_id))

        assert result["total"] == 0
        sqls = [str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})) for s in captured]
        # The ownership lookup must exclude soft-deleted triggers.
        assert any("triggers.deleted_at IS NULL" in s for s in sqls)
        # The event query must carry the team clause even when filtered by trigger_id.
        assert any(f"pipelines.owner_team_id = '{_TEAM_A}'" in s for s in sqls)
        assert any("pipelines.owner_team_id IS NULL" in s for s in sqls)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_filter_applied_in_sql_when_no_filter(self, mock_validate_auth: AsyncMock) -> None:
        from sqlalchemy.dialects import postgresql

        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one_or_none.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        captured: list = []

        async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
            captured.append(stmt)
            if "count(" in str(stmt).lower():
                return total_result
            return rows_result

        session.execute = fake_execute
        with patch("modulo.api.mcp_server._session") as mock_session:
            mock_session.return_value = _make_session_context(session)
            result = await list_trigger_events()

        assert result["total"] == 0
        sqls = [str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})) for s in captured]
        assert any("triggers.pipeline_id" in s for s in sqls), "a team filter must join triggers"
        assert any(f"pipelines.owner_team_id = '{_TEAM_A}'" in s for s in sqls)
        assert any("pipelines.owner_team_id IS NULL" in s for s in sqls)


class TestResourceTeamScope(_AuthContext):
    """MCP resource surface must enforce the same boundary as the tools."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_pipelines_passes_team_id(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        mock_page = MagicMock()
        mock_page.items = []
        mock_page.total = 0
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.pipeline.list_pipelines", AsyncMock(return_value=mock_page)) as mock_list,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await resource_pipelines()

        assert "Pipelines (0 total)" in result
        assert mock_list.await_args.kwargs["team_id"] == _TEAM_A

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_pipeline_runs_blocked_for_other_teams_pipeline(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        pipeline = MagicMock()
        pipeline.owner_team_id = _TEAM_B
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.get_pipeline", AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.run.list_runs", AsyncMock()) as mock_list_runs,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await resource_pipeline_runs(pipeline_id=str(pipeline_id))

        assert "team_boundary_violation" in result
        mock_list_runs.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_pipeline_detail_blocked_for_other_teams_pipeline(
        self, mock_validate_auth: AsyncMock
    ) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        pipeline = MagicMock()
        pipeline.owner_team_id = _TEAM_B
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.get_pipeline", AsyncMock(return_value=pipeline)),
        ):
            mock_session.return_value = _make_session_context(session)
            result = await resource_pipeline_detail(pipeline_id=str(pipeline_id))

        assert "team_boundary_violation" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_pipeline_snapshots_blocked_for_other_teams_pipeline(
        self, mock_validate_auth: AsyncMock
    ) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
            patch("modulo.db.crud.pipeline_snapshot_versioning.list_snapshots", AsyncMock()) as mock_list,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await resource_pipeline_snapshots(pipeline_id=str(pipeline_id))

        assert "team_boundary_violation" in result
        mock_list.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_pipeline_snapshot_detail_blocked_for_other_teams_pipeline(
        self, mock_validate_auth: AsyncMock
    ) -> None:
        _ctx_team_id.set(_TEAM_A)
        pipeline_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
            patch("modulo.db.crud.pipeline_snapshot_versioning.get_snapshot_detail", AsyncMock()) as mock_detail,
        ):
            mock_session.return_value = _make_session_context(session)
            result = await resource_pipeline_snapshot_detail(
                pipeline_id=str(pipeline_id), snapshot_id=str(uuid.uuid4())
            )

        assert "team_boundary_violation" in result
        mock_detail.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_run_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.db.crud.run.get_child_run_rollup", AsyncMock()) as mock_rollup,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await resource_run(run_id=str(run_id))

        assert "team_boundary_violation" in result
        mock_rollup.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_run_blocked_when_unstamped_but_pipeline_other_team(
        self, mock_validate_auth: AsyncMock
    ) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=None)
            mock_session.return_value = _make_session_context(session)
            result = await resource_run(run_id=str(run_id))

        assert "team_boundary_violation" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_resource_hitl_gate_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        gate = MagicMock()
        gate.pipeline_id = uuid.uuid4()
        gate.required_team_id = None
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = gate
        session = AsyncMock()
        session.execute = AsyncMock(return_value=gate_result)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4(), owner_team_id=_TEAM_B)
            mock_session.return_value = _make_session_context(session)
            result = await resource_hitl_gate(run_id=str(run_id), gate_id="gate-1")

        assert "team_boundary_violation" in result


class TestListPendingHitlTeamFilterSQL(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_team_filter_compiles_to_sql(self, mock_validate_auth: AsyncMock) -> None:
        from sqlalchemy.dialects import postgresql

        _ctx_team_id.set(_TEAM_A)
        session = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one.return_value = 0
        gates_result = MagicMock()
        gates_result.scalars.return_value = []
        captured: list = []

        async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
            captured.append(stmt)
            if "count(" in str(stmt).lower():
                return total_result
            return gates_result

        session.execute = fake_execute
        with patch("modulo.api.mcp_server._session") as mock_session:
            mock_session.return_value = _make_session_context(session)
            result = await list_pending_hitl()

        assert result["total"] == 0
        sql = str(captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert "COALESCE" in sql.upper(), "the effective owner must coalesce run + pipeline owner"
        assert "runs.owner_team_id" in sql
        assert "pipelines.owner_team_id" in sql
        assert "IS NULL" in sql
        assert f"'{_TEAM_A}'" in sql, "the key's team must appear as the boundary bound value"


class TestCrudTeamFilterSQL:
    """Compile-and-assert the CRUD team filters so they cannot silently no-op.

    Mirrors the analytics builder test pattern (test_analytics_builder.py):
    build the real statement, compile it for the postgres dialect, and assert
    on the WHERE clause — a mocked ``session.execute`` alone never proves the
    filter exists in SQL.
    """

    @staticmethod
    def _compile(stmt: Any) -> str:
        from sqlalchemy.dialects import postgresql

        return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    async def test_db_list_pipelines_applies_team_and_folder_filters_in_count(self) -> None:
        from modulo.db.crud.pipeline import list_pipelines as db_list_pipelines

        team = uuid.uuid4()
        folder = uuid.uuid4()
        session = AsyncMock()
        captured: list = []
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value = []

        async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
            captured.append(stmt)
            if "count(" in str(stmt).lower():
                return count_result
            return rows_result

        session.execute = fake_execute

        page = await db_list_pipelines(session, page=1, page_size=20, folder_id=folder, team_id=team)

        assert page.total == 0
        count_sql = self._compile(captured[0])
        assert f"pipelines.folder_id = '{folder}'" in count_sql, "folder filter must be in the count query"
        assert "pipelines.owner_team_id IS NULL" in count_sql, "org-level pipelines must stay visible"
        assert f"pipelines.owner_team_id = '{team}'" in count_sql, "own-team pipelines must be visible"

    async def test_db_list_runs_applies_effective_owner_coalesce_filter(self) -> None:
        from modulo.db.crud.run import list_runs as db_list_runs

        team = uuid.uuid4()
        session = AsyncMock()
        captured: list = []
        count_result = MagicMock()
        count_result.scalar_one_or_none.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value = []

        async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
            captured.append(stmt)
            if "count(" in str(stmt).lower():
                return count_result
            return rows_result

        session.execute = fake_execute

        page = await db_list_runs(session, page=1, page_size=20, team_id=team)

        assert page.total == 0
        count_sql = self._compile(captured[0]).lower()
        assert "coalesce(runs.owner_team_id, pipelines.owner_team_id)" in count_sql, (
            "the boundary must use the run snapshot owner with pipeline fallback"
        )
        assert f"'{team}'" in count_sql, "the key's team must be the boundary bound value"

    async def test_db_list_triggers_applies_team_filter(self) -> None:
        from modulo.db.crud.trigger import list_triggers as db_list_triggers

        team = uuid.uuid4()
        session = AsyncMock()
        captured: list = []
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []

        async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
            captured.append(stmt)
            return rows_result

        session.execute = fake_execute

        page = await db_list_triggers(session, _PLACEHOLDER_ORG_ID, team_id=team)

        assert page.total == 0
        sql = self._compile(captured[0])
        assert "pipelines.owner_team_id IS NULL" in sql, "org-level pipelines must stay visible"
        assert f"pipelines.owner_team_id = '{team}'" in sql, "own-team pipelines must be visible"
