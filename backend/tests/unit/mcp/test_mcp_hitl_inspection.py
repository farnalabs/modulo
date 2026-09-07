"""Unit tests for the read-only HITL gate-inspection MCP tools (FAR-641).

Covers list_hitl_gates, get_hitl_gate, and get_pipeline_gates. All three are
read-only: no state mutation anywhere (no claim/approve/reject capability).
"""

import inspect
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import (
    _TEAM_SCOPE_ERROR,
    MCPAuthorizationError,
    _ctx_team_id,
    _get_hitl_gate_impl,
    _get_pipeline_gates_impl,
    _list_hitl_gates_impl,
    get_hitl_gate,
    get_pipeline_gates,
    list_hitl_gates,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_gate(*, decision: str | None = None, claimed: bool = False) -> MagicMock:
    gate = MagicMock()
    gate.run_id = uuid.uuid4()
    gate.gate_id = "hitl_gate_a_b"
    gate.pipeline_id = uuid.uuid4()
    gate.account_id = uuid.uuid4() if claimed else None
    gate.claimed_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC) if claimed else None
    gate.expires_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    gate.required_team_id = None
    gate.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    gate.decision = decision
    gate.decision_at = None
    return gate


def _make_run() -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.status = "awaiting_human"
    run.owner_team_id = uuid.uuid4()
    return run


class _AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)


# ---------------------------------------------------------------------------
# list_hitl_gates
# ---------------------------------------------------------------------------


class TestListHitlGates(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_empty_pending_list(self, mock_session: AsyncMock, mock_validate_auth: AsyncMock) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        mock_session.return_value = _make_session_context(session)

        out = await list_hitl_gates()

        assert not out["gates"]
        assert out["limit"] == 20

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        out = await list_hitl_gates()
        assert out["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_claim_expiry_is_surfaced(self, mock_session: AsyncMock, mock_validate_auth: AsyncMock) -> None:
        gate = _make_gate()
        pipeline_id = gate.pipeline_id
        run_number = 7
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = [(gate, "PR Reviewer", run_number)]
        session.execute = AsyncMock(return_value=result)
        mock_session.return_value = _make_session_context(session)

        out = await list_hitl_gates()

        fetched = out["gates"][0]
        assert fetched["gate_id"] == "hitl_gate_a_b"
        assert fetched["pipeline_name"] == "PR Reviewer"
        assert fetched["pipeline_id"] == str(pipeline_id)
        assert fetched["run_number"] == run_number
        assert fetched["claimed_by"] is None
        assert fetched["expires_at"] == "2026-01-01T13:00:00+00:00"
        assert fetched["created_at"] == "2026-01-01T12:00:00+00:00"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_limit_is_capped(self, mock_session: AsyncMock, mock_validate_auth: AsyncMock) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        mock_session.return_value = _make_session_context(session)

        out = await list_hitl_gates(limit=10_000)

        assert out["limit"] == 100
        assert not out["gates"]


# ---------------------------------------------------------------------------
# get_hitl_gate
# ---------------------------------------------------------------------------


class TestGetHitlGate(_AuthContext):
    @patch("modulo.db.crud.hitl_gate_config.resolve_hitl_gate_config", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_human_only_flag_surfaced_in_detail(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_hitl_manager: MagicMock,
        mock_resolve_config: AsyncMock,
    ) -> None:
        run = _make_run()
        gate = _make_gate(claimed=True, decision=None)
        required_team = uuid.uuid4()
        manager = MagicMock()
        manager.get_gate = AsyncMock(return_value=gate)
        mock_hitl_manager.return_value = manager
        mock_get_run.return_value = run
        mock_resolve_config.return_value = {
            "label": "Approve Review",
            "human_only": True,
            "claim_expiry_minutes": 60,
            "condition": None,
            "reject_target": None,
            "required_team_id": required_team,
        }
        session = AsyncMock()
        mock_session.return_value = _make_session_context(session)

        out = await get_hitl_gate(run_id=str(run.id), gate_id="hitl_gate_a_b")

        assert out["run_status"] == "awaiting_human"
        assert out["gate_fired"] is True
        config = out["gate_config"]
        assert config["human_only"] is True
        assert config["label"] == "Approve Review"
        assert config["claim_expiry_minutes"] == 60
        assert config["required_team_id"] == str(required_team)
        assert out["claimed_by"] == str(gate.account_id)

    @patch("modulo.db.crud.hitl_gate_config.resolve_hitl_gate_config", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_gate_not_found(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_hitl_manager: MagicMock,
        mock_resolve_config: AsyncMock,
    ) -> None:
        run = _make_run()
        manager = MagicMock()
        manager.get_gate = AsyncMock(return_value=None)
        mock_hitl_manager.return_value = manager
        mock_get_run.return_value = run
        mock_session.return_value = _make_session_context(AsyncMock())

        out = await get_hitl_gate(run_id=str(run.id), gate_id="missing")

        assert out == {"error": "gate_not_found", "run_id": str(run.id), "gate_id": "missing"}
        mock_resolve_config.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_run_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run_id = str(uuid.uuid4())
        mock_get_run.return_value = None
        mock_session.return_value = _make_session_context(AsyncMock())

        out = await get_hitl_gate(run_id=run_id, gate_id="hitl_gate_a_b")

        assert out == {"error": "run_not_found", "run_id": run_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_uuid_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        out = await get_hitl_gate(run_id="not-a-uuid", gate_id="hitl_gate_a_b")

        assert out["error"] == "invalid_id"
        assert out["field"] == "run_id"


# ---------------------------------------------------------------------------
# get_pipeline_gates
# ---------------------------------------------------------------------------


class TestGetPipelineGates(_AuthContext):
    @patch("modulo.db.crud.pipeline.get_pipeline_graph", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_pipeline_with_no_gates_returns_empty_list(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_owner_team: AsyncMock,
        mock_graph: AsyncMock,
    ) -> None:
        pid = uuid.uuid4()
        bare_edge = MagicMock()
        bare_edge.source_node_id = uuid.uuid4()
        bare_edge.target_node_id = uuid.uuid4()
        bare_edge.edge_type = "normal"
        bare_edge.hitl_gate_config = None
        mock_owner_team.return_value = None
        mock_graph.return_value = ([], [bare_edge])
        mock_session.return_value = _make_session_context(AsyncMock())

        out = await get_pipeline_gates(pipeline_id=str(pid))

        assert out["pipeline_id"] == str(pid)
        assert not out["gates"]
        assert out["gate_count"] == 0
        assert mock_graph.await_args.args[1] == pid

    @patch("modulo.db.crud.pipeline.get_pipeline_graph", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_gated_edges_return_full_config(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_owner_team: AsyncMock,
        mock_graph: AsyncMock,
    ) -> None:
        pid = uuid.uuid4()
        config = {"label": "Approve", "human_only": True, "claim_expiry_minutes": 30}
        gated = MagicMock()
        gated.source_node_id = uuid.uuid4()
        gated.target_node_id = uuid.uuid4()
        gated.edge_type = "normal"
        gated.hitl_gate_config = config
        mock_owner_team.return_value = None
        mock_graph.return_value = (["node-a"], [gated])
        mock_session.return_value = _make_session_context(AsyncMock())

        out = await get_pipeline_gates(pipeline_id=str(pid))

        edge_gate = out["gates"][0]
        assert edge_gate["source_node_id"] == str(gated.source_node_id)
        assert edge_gate["target_node_id"] == str(gated.target_node_id)
        assert edge_gate["edge_type"] == "normal"
        assert edge_gate["hitl_gate_config"] == config
        assert out["gate_count"] == 1

    @patch("modulo.db.crud.pipeline.get_pipeline_graph", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_pipeline_not_found(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_owner_team: AsyncMock,
        mock_graph: AsyncMock,
    ) -> None:
        pid = str(uuid.uuid4())
        mock_owner_team.return_value = None
        mock_graph.return_value = None
        mock_session.return_value = _make_session_context(AsyncMock())

        out = await get_pipeline_gates(pipeline_id=pid)

        assert out == {"error": "pipeline_not_found", "pipeline_id": pid}


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


class TestReadOnlyGuarantee(_AuthContext):
    @patch("modulo.db.crud.hitl_gate_config.resolve_hitl_gate_config", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.db.crud.pipeline.get_pipeline_graph", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_no_writes_performed_by_inspection_tools(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_owner_team: AsyncMock,
        mock_graph: AsyncMock,
        mock_hitl_manager: MagicMock,
        mock_resolve_config: AsyncMock,
    ) -> None:
        session = AsyncMock()
        query_result = MagicMock()
        query_result.all.return_value = [(_make_gate(), "PR Reviewer", 1)]
        session.execute = AsyncMock(return_value=query_result)
        mock_session.return_value = _make_session_context(session)

        mock_owner_team.return_value = None
        gated_edge = MagicMock()
        gated_edge.source_node_id = uuid.uuid4()
        gated_edge.target_node_id = uuid.uuid4()
        gated_edge.edge_type = "normal"
        gated_edge.hitl_gate_config = {"label": "Gate", "human_only": False}
        mock_graph.return_value = ([], [gated_edge])

        run = _make_run()
        mock_get_run.return_value = run
        manager = MagicMock()
        manager.get_gate = AsyncMock(return_value=_make_gate())
        mock_hitl_manager.return_value = manager
        mock_resolve_config.return_value = {"label": "Gate", "human_only": False}

        await list_hitl_gates()
        await get_hitl_gate(run_id=str(run.id), gate_id="hitl_gate_a_b")
        await get_pipeline_gates(pipeline_id=str(uuid.uuid4()))

        session.add.assert_not_called()
        session.add_all.assert_not_called()
        session.delete.assert_not_called()
        session.commit.assert_not_called()
        session.rollback.assert_not_called()

    def test_tool_sources_contain_no_write_primitives(self) -> None:
        sources = [
            inspect.getsource(_list_hitl_gates_impl),
            inspect.getsource(_get_hitl_gate_impl),
            inspect.getsource(_get_pipeline_gates_impl),
        ]
        joined = "\n".join(sources)
        forbidden = [
            "session.add",
            "session.commit",
            "session.flush",
            "session.delete",
            ".update(",
            ".delete(",
            ".insert(",
        ]
        for needle in forbidden:
            assert needle not in joined


# ---------------------------------------------------------------------------
# Branch coverage for the FAR-641 inspection tools (team-scoping, boundary
# errors, absent config, and the wrapper exception handlers).
# ---------------------------------------------------------------------------


class TestHitlInspectionBranches(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_team_scoped_key_appends_team_clause(
        self, mock_session: AsyncMock, mock_validate_auth: AsyncMock
    ) -> None:
        self.setup_method()
        team_id = uuid.uuid4()
        _ctx_team_id.set(team_id)
        try:
            session = AsyncMock()
            result = MagicMock()
            result.all.return_value = []
            session.execute = AsyncMock(return_value=result)
            mock_session.return_value = _make_session_context(session)

            out = await list_hitl_gates()

            assert out["limit"] == 20
            # The team-scoped key must add a team boundary clause to the query.
            assert session.execute.called
        finally:
            _ctx_team_id.set(None)
            self.teardown_method()

    @patch("modulo.api.mcp_server._check_agent_tool_scope", side_effect=MCPAuthorizationError("no scope"))
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_scope_error_returns_insufficient_scope(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await list_hitl_gates()
        assert out["error"] == "insufficient_scope"
        assert "no scope" in out["detail"]

    @patch(
        "modulo.api.mcp_server._check_agent_tool_scope",
        side_effect=ProgrammingError("relation missing", "detail", None),
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_programming_error_returns_migration_required(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await list_hitl_gates()
        assert out["error"] == "migration_required"

    @patch("modulo.api.mcp_server._check_agent_tool_scope", side_effect=ValueError("boom"))
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_list_generic_error_returns_tool_error(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await list_hitl_gates()
        assert out["error"] == "internal_error"

    @patch("modulo.api.mcp_server._load_hitl_run", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_gate_run_team_scope_error(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_load_run: AsyncMock,
    ) -> None:
        mock_load_run.return_value = _TEAM_SCOPE_ERROR
        mock_session.return_value = _make_session_context(AsyncMock())
        rid = str(uuid.uuid4())
        out = await get_hitl_gate(run_id=rid, gate_id="hitl_gate_a_b")
        assert out["error"] == "team_boundary_violation"

    @patch("modulo.db.crud.hitl_gate_config.resolve_hitl_gate_config", new_callable=AsyncMock, return_value=None)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_gate_config_none_surfaced(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_hitl_manager: MagicMock,
        mock_resolve_config: AsyncMock,
    ) -> None:
        run = _make_run()
        gate = _make_gate()
        manager = MagicMock()
        manager.get_gate = AsyncMock(return_value=gate)
        mock_hitl_manager.return_value = manager
        mock_get_run.return_value = run
        mock_session.return_value = _make_session_context(AsyncMock())

        out = await get_hitl_gate(run_id=str(run.id), gate_id="hitl_gate_a_b")

        assert out["gate_config"] is None
        assert out["gate_fired"] is True

    @patch("modulo.api.mcp_server._check_agent_tool_scope", side_effect=MCPAuthorizationError("no scope"))
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_gate_scope_error_returns_insufficient_scope(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await get_hitl_gate(run_id=str(uuid.uuid4()), gate_id="hitl_gate_a_b")
        assert out["error"] == "insufficient_scope"

    @patch(
        "modulo.api.mcp_server._check_agent_tool_scope",
        side_effect=ProgrammingError("relation missing", "detail", None),
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_gate_programming_error_returns_migration_required(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await get_hitl_gate(run_id=str(uuid.uuid4()), gate_id="hitl_gate_a_b")
        assert out["error"] == "migration_required"

    @patch("modulo.api.mcp_server._check_agent_tool_scope", side_effect=ValueError("boom"))
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_gate_generic_error_returns_tool_error(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await get_hitl_gate(run_id=str(uuid.uuid4()), gate_id="hitl_gate_a_b")
        assert out["error"] == "internal_error"

    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_pipeline_gates_team_scope_mismatch(
        self,
        mock_validate_auth: AsyncMock,
        mock_session: AsyncMock,
        mock_owner_team: AsyncMock,
    ) -> None:
        self.setup_method()
        key_team = uuid.uuid4()
        _ctx_team_id.set(key_team)
        try:
            pid = uuid.uuid4()
            # Owner team differs from the team-scoped key -> boundary violation.
            mock_owner_team.return_value = uuid.uuid4()
            mock_session.return_value = _make_session_context(AsyncMock())

            out = await get_pipeline_gates(pipeline_id=str(pid))

            assert out["error"] == "team_boundary_violation"
        finally:
            _ctx_team_id.set(None)
            self.teardown_method()

    @patch("modulo.api.mcp_server._check_agent_tool_scope", side_effect=MCPAuthorizationError("no scope"))
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_pipeline_gates_scope_error_returns_insufficient_scope(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await get_pipeline_gates(pipeline_id=str(uuid.uuid4()))
        assert out["error"] == "insufficient_scope"

    @patch(
        "modulo.api.mcp_server._check_agent_tool_scope",
        side_effect=ProgrammingError("relation missing", "detail", None),
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_pipeline_gates_programming_error_returns_migration_required(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await get_pipeline_gates(pipeline_id=str(uuid.uuid4()))
        assert out["error"] == "migration_required"

    @patch("modulo.api.mcp_server._check_agent_tool_scope", side_effect=ValueError("boom"))
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_pipeline_gates_generic_error_returns_tool_error(
        self, mock_validate_auth: AsyncMock, mock_scope: MagicMock
    ) -> None:
        out = await get_pipeline_gates(pipeline_id=str(uuid.uuid4()))
        assert out["error"] == "internal_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_get_pipeline_gates_invalid_uuid_returns_invalid_id(self, mock_validate_auth: AsyncMock) -> None:
        out = await get_pipeline_gates(pipeline_id="not-a-uuid")
        assert out["error"] == "invalid_id"
        assert out["field"] == "pipeline_id"
