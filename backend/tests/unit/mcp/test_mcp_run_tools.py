"""Unit tests for the run/evals MCP tools (get_run_status, cancel_run, get_run_evals, list_eval_definitions)."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import cancel_run, get_run_evals, get_run_status, list_eval_definitions
from modulo.core.mcp.scope_validator import MCPAuthorizationError

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_mock_run(
    *,
    status: str = "pending",
    trigger_type: str = "manual",
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    node_token_usage: dict | None = None,
    outputs_json: dict | None = None,
    node_telemetry_json: dict | None = None,
    cost_breakdown: list | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.status = status
    run.trigger_type = trigger_type
    run.created_at = created_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    run.started_at = started_at
    run.completed_at = completed_at
    run.error_code = error_code
    run.error_detail = error_detail
    run.node_token_usage = node_token_usage
    run.outputs_json = outputs_json
    run.node_telemetry_json = node_telemetry_json
    run.cost_breakdown = cost_breakdown
    return run


_UNSET = object()


def _make_mock_eval(
    *,
    node_id: uuid.UUID | None = None,
    passed: bool = True,
    score: float | None = None,
    detail: str | None = None,
    evaluated_at: datetime | object | None = _UNSET,
) -> MagicMock:
    eval_result = MagicMock()
    eval_result.id = uuid.uuid4()
    eval_result.eval_id = uuid.uuid4()
    eval_result.node_id = node_id
    eval_result.passed = passed
    eval_result.score = score
    eval_result.detail = detail
    eval_result.evaluated_at = evaluated_at if evaluated_at is not _UNSET else datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    return eval_result


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


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


class TestGetRunStatus(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_run_status(run_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_run_status(run_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "run_id"
        mock_session.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_run_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = None

        run_id = str(uuid.uuid4())
        result = await get_run_status(run_id=run_id)

        assert result == {"error": "run_not_found", "run_id": run_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_full_run_shape(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(
            status="complete",
            started_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
            error_code="timeout",
        )
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run

        result = await get_run_status(run_id=str(run.id))

        assert result["run_id"] == str(run.id)
        assert result["pipeline_id"] == str(run.pipeline_id)
        assert result["status"] == "complete"
        assert result["trigger_type"] == "manual"
        assert result["created_at"] == run.created_at.isoformat()
        assert result["started_at"] == run.started_at.isoformat()
        assert result["completed_at"] == run.completed_at.isoformat()
        assert result["error_code"] == "timeout"
        assert "nodes" not in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_failed_run_includes_error_detail(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(status="failed", error_code="task_failure", error_detail="boom: worker crashed")
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run

        result = await get_run_status(run_id=str(run.id))

        assert result["error_code"] == "task_failure"
        assert result["error_detail"] == "boom: worker crashed"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_error_detail_absent_when_none(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(status="complete", error_code=None, error_detail=None)
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run

        result = await get_run_status(run_id=str(run.id))

        assert "error_detail" not in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_error_detail_redacts_secrets(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(
            status="failed",
            error_code="task_failure",
            error_detail="openai call failed: sk-abcdefghijkl1234, auth Bearer xyz123abc",
        )
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run

        result = await get_run_status(run_id=str(run.id))

        assert result["error_code"] == "task_failure"
        assert "sk-abcdefghijkl1234" not in json.dumps(result)
        assert "Bearer xyz123abc" not in json.dumps(result)
        assert "<redacted>" in result["error_detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_detail_includes_per_node_breakdown(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(
            status="running",
            node_token_usage={
                "node_a": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": 0.02},
                "node_c": {"input_tokens": 2, "output_tokens": 5, "total_tokens": 7, "cost_usd": 0.0},
            },
            outputs_json={"node_a": {"result": "ok"}, "node_b": {"result": "nope"}},
        )
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run

        result = await get_run_status(run_id=str(run.id), detail=True)

        assert result["nodes"] == [
            {
                "node_id": "node_a",
                "status": "completed",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.02,
                "has_output": True,
            },
            {
                "node_id": "node_b",
                "status": "completed",
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0,
                "has_output": True,
            },
            {
                "node_id": "node_c",
                "status": "processed",
                "input_tokens": 2,
                "output_tokens": 5,
                "total_tokens": 7,
                "cost_usd": 0.0,
                "has_output": False,
            },
        ]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_detail_derives_status_from_telemetry(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(
            status="running",
            node_token_usage={},
            outputs_json={"node_a": {"summary": "ok"}},
            node_telemetry_json={
                "node_a": {"status": "completed"},
                "node_b": {"status": "failed", "summary": "boom", "agent_stderr": "traceback", "error_message": "boom"},
                "node_c": {"status": "completed"},
            },
        )
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run

        result = await get_run_status(run_id=str(run.id), detail=True)

        by_id = {n["node_id"]: n for n in result["nodes"]}
        assert by_id["node_a"]["status"] == "completed"
        assert by_id["node_a"]["has_output"] is True
        assert by_id["node_b"]["status"] == "failed"
        assert by_id["node_b"]["has_output"] is True
        assert by_id["node_c"]["status"] == "processed"
        assert by_id["node_c"]["has_output"] is True
        # Response field set stays bounded: no raw telemetry dump.
        assert all("agent_stderr" not in n and "error_message" not in n for n in result["nodes"])
        assert "traceback" not in json.dumps(result)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await get_run_status(run_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_internal_error(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.side_effect = RuntimeError("boom")

        result = await get_run_status(run_id=str(uuid.uuid4()))

        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# cancel_run
# ---------------------------------------------------------------------------


class TestCancelRun(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await cancel_run(run_id=str(uuid.uuid4()))
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
            result = await cancel_run(run_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await cancel_run(run_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "run_id"
        mock_session.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.run.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_run_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = None

        run_id = str(uuid.uuid4())
        result = await cancel_run(run_id=run_id)

        assert result == {"error": "run_not_found", "run_id": run_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.run.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_terminal_status_returns_cannot_cancel(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = _make_mock_run(status="complete")

        run_id = str(uuid.uuid4())
        result = await cancel_run(run_id=run_id)

        assert result["error"] == "cannot_cancel"
        assert result["run_id"] == run_id
        assert "terminal" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.run.get_run")
    @patch("modulo.db.crud.run.request_cancellation")
    @patch("modulo.api.mcp_server.finalize_cancelled_run")
    @patch("modulo.api.mcp_server._session")
    async def test_run_not_found_when_request_cancellation_returns_none(
        self,
        mock_session: AsyncMock,
        mock_finalize_cancelled: AsyncMock,
        mock_request_cancellation: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = _make_mock_run(status="running")
        mock_request_cancellation.return_value = None

        run_id = str(uuid.uuid4())
        result = await cancel_run(run_id=run_id)

        assert result == {"error": "run_not_found", "run_id": run_id}
        mock_request_cancellation.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.run.get_run")
    @patch("modulo.db.crud.run.request_cancellation")
    @patch("modulo.api.mcp_server.finalize_cancelled_run")
    @patch("modulo.api.mcp_server._session")
    async def test_success_requests_cancellation(
        self,
        mock_session: AsyncMock,
        mock_finalize_cancelled: AsyncMock,
        mock_request_cancellation: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(status="running")
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run
        mock_request_cancellation.return_value = run

        result = await cancel_run(run_id=str(run.id))

        assert result == {"run_id": str(run.id), "cancellation_requested": True}
        mock_request_cancellation.assert_awaited_once()
        # A STREAMED (non-paused) run is routed through finalize_cost.
        mock_finalize_cancelled.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.run.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await cancel_run(run_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# get_run_evals
# ---------------------------------------------------------------------------


class TestGetRunEvals(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_run_evals(run_id=str(uuid.uuid4()))
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
            result = await get_run_evals(run_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_run_evals(run_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "run_id"
        mock_session.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_run_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = None

        run_id = str(uuid.uuid4())
        result = await get_run_evals(run_id=run_id)

        assert result == {"error": "run_not_found", "run_id": run_id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.db.crud.eval_run.get_run_evals")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_evals_shape(
        self,
        mock_session: AsyncMock,
        mock_db_get_run_evals: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_mock_run(status="complete")
        evals = [
            _make_mock_eval(node_id=None, passed=True, score=0.95, detail="good"),
            _make_mock_eval(node_id=uuid.uuid4(), passed=False, score=0.4, detail="bad", evaluated_at=None),
        ]
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = run
        mock_db_get_run_evals.return_value = evals

        result = await get_run_evals(run_id=str(run.id))

        assert result["run_id"] == str(run.id)
        assert result["status"] == "complete"
        assert result["eval_count"] == 2
        assert result["evals"][0]["id"] == str(evals[0].id)
        assert result["evals"][0]["eval_id"] == str(evals[0].eval_id)
        assert result["evals"][0]["node_id"] is None
        assert result["evals"][0]["passed"] is True
        assert result["evals"][0]["score"] == 0.95
        assert result["evals"][0]["evaluated_at"] == evals[0].evaluated_at.isoformat()
        assert result["evals"][1]["node_id"] == str(evals[1].node_id)
        assert result["evals"][1]["evaluated_at"] is None
        mock_db_get_run_evals.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.db.crud.eval_run.get_run_evals")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_db_get_run_evals: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = _make_mock_run(status="complete")
        mock_db_get_run_evals.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await get_run_evals(run_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# list_eval_definitions
# ---------------------------------------------------------------------------


class TestListEvalDefinitions(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_eval_definitions()
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
            result = await list_eval_definitions()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.eval_definition.list_eval_definitions")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_definitions_shape(
        self,
        mock_session: AsyncMock,
        mock_db_list_eval_definitions: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        definition = MagicMock()
        definition.id = uuid.uuid4()
        definition.name = "quality-eval"
        definition.eval_type = "json_schema"
        definition.pipeline_id = uuid.uuid4()
        definition.failure_behaviour = "block"
        definition.pass_threshold = None
        definition.suite_id = None
        page = _make_page_result([definition], total=1)

        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_list_eval_definitions.return_value = page

        result = await list_eval_definitions()

        assert result["data"][0]["id"] == str(definition.id)
        assert result["data"][0]["name"] == "quality-eval"
        assert result["data"][0]["type"] == "json_schema"
        assert result["data"][0]["pipeline_id"] == str(definition.pipeline_id)
        assert result["data"][0]["failure_behaviour"] == "block"
        assert result["data"][0]["pass_threshold"] is None
        assert result["total"] == 1
        assert result["next_cursor"] is None
        assert result["has_more"] is False

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.eval_definition.list_eval_definitions")
    @patch("modulo.api.mcp_server._session")
    async def test_passes_pipeline_filter(
        self,
        mock_session: AsyncMock,
        mock_db_list_eval_definitions: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_list_eval_definitions.return_value = _make_page_result([])

        pid = uuid.uuid4()
        await list_eval_definitions(pipeline_id=str(pid))

        mock_db_list_eval_definitions.assert_awaited_once()
        assert mock_db_list_eval_definitions.await_args.kwargs["pipeline_id"] == pid

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.db.crud.eval_definition.list_eval_definitions")
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_when_programming_error(
        self,
        mock_session: AsyncMock,
        mock_db_list_eval_definitions: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_list_eval_definitions.side_effect = ProgrammingError("stmt", {}, Exception("boom"))

        result = await list_eval_definitions()

        assert result["error"] == "migration_required"
