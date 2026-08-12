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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    _ctx_team_id,
    _ctx_team_id_val,
    _team_scope_error,
    _team_scoped_key_mismatch,
    cancel_run,
    get_pipeline_graph_tool,
    get_run_evals,
    get_run_output,
    get_run_status,
    trigger_pipeline,
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


def _make_run(*, pipeline_id: uuid.UUID, status: str = "pending") -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = pipeline_id
    run.status = status
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
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)) as mock_owner,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4())
            mock_session.return_value = _make_session_context(session)
            result = await get_run_status(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"
        mock_owner.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_run_status_allowed_for_own_team_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        run = _make_run(pipeline_id=uuid.uuid4(), status="complete")
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_A)),
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
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)),
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4())
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
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)) as mock_owner,
            patch("modulo.db.crud.eval_run.get_run_evals", AsyncMock()) as mock_evals,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4())
            mock_session.return_value = _make_session_context(session)
            result = await get_run_evals(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"
        mock_owner.assert_awaited_once()
        mock_evals.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_cancel_run_blocked_for_other_teams_run(self, mock_validate_auth: AsyncMock) -> None:
        _ctx_team_id.set(_TEAM_A)
        run_id = uuid.uuid4()
        session = AsyncMock()
        with (
            patch("modulo.db.crud.run.get_run") as mock_get_run,
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)) as mock_owner,
            patch("modulo.db.crud.run.request_cancellation", AsyncMock()) as mock_cancel,
        ):
            mock_get_run.return_value = _make_run(pipeline_id=uuid.uuid4())
            mock_session.return_value = _make_session_context(session)
            result = await cancel_run(run_id=str(run_id))

        assert result["error"] == "team_boundary_violation"
        mock_owner.assert_awaited_once()
        mock_cancel.assert_not_awaited()
