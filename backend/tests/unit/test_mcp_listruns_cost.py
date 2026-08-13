"""Unit tests for the cost fields exposed by the MCP ``list_runs`` tool.

The MCP ``list_runs`` tool mirrors the REST ``GET /api/v1/runs`` surface: each
item carries ``total_cost_usd`` (own-run cost, may be None), plus the derived
``child_runs_cost_usd`` / ``child_runs_count`` rollup and the quantized
``aggregate_cost_usd`` = own + child. The Daily Watcher pipeline reads these
fields via MCP so its COSTS section can report real per-run costs.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import list_runs

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    total_cost_usd: Decimal | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    status: str = "complete",
) -> MagicMock:
    r = MagicMock()
    r.id = run_id or uuid.uuid4()
    r.pipeline_id = uuid.uuid4()
    r.status = status
    r.trigger_type = "manual"
    r.run_number = 1
    r.created_at = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    r.started_at = datetime(2026, 8, 1, 10, 0, 1, tzinfo=UTC)
    r.completed_at = datetime(2026, 8, 1, 10, 5, tzinfo=UTC)
    r.error_code = error_code
    r.error_detail = error_detail
    r.total_cost_usd = total_cost_usd
    return r


def _make_list_result(items: list[MagicMock], *, total: int, next_cursor: str | None, has_more: bool) -> MagicMock:
    result = MagicMock()
    result.items = items
    result.total = total
    result.page = 1
    result.page_size = len(items)
    result.next_cursor = next_cursor
    result.has_more = has_more
    return result


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class _AuthContext:
    """Set/teardown the MCP ContextVars so the tool handler reaches the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")
        _ctx_user_id.set(_PLACEHOLDER_ACCOUNT_ID)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_user_id.set(None)


class TestListRunsCost(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_includes_cost_fields(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(total_cost_usd=Decimal("1.230000"))
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {run.id: (Decimal("0.500000"), 2)}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        assert result["total"] == 1
        assert "error" not in result
        item = result["items"][0]
        assert item["total_cost_usd"] == 1.23
        assert item["child_runs_cost_usd"] == 0.5
        assert item["child_runs_count"] == 2
        assert item["aggregate_cost_usd"] == 1.73
        assert isinstance(item["total_cost_usd"], float)
        assert isinstance(item["child_runs_cost_usd"], float)
        assert isinstance(item["aggregate_cost_usd"], float)
        assert isinstance(item["child_runs_count"], int)
        mock_db_list_runs.assert_awaited_once()
        mock_child_rollup.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_aggregate_is_own_plus_child(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(total_cost_usd=Decimal("7.500000"))
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {run.id: (Decimal("0.125000"), 3)}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        item = result["items"][0]
        assert item["total_cost_usd"] == 7.5
        assert item["child_runs_cost_usd"] == 0.125
        assert item["child_runs_count"] == 3
        assert item["aggregate_cost_usd"] == 7.625

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_missing_cost_matches_rest_zero_shape(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(total_cost_usd=None)
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        item = result["items"][0]
        assert item["total_cost_usd"] is None
        assert item["child_runs_cost_usd"] == 0.0
        assert item["child_runs_count"] == 0
        assert item["aggregate_cost_usd"] == 0.0

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_child_rollup_skipped_when_page_empty(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_db_list_runs.return_value = _make_list_result([], total=0, next_cursor=None, has_more=False)
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        assert result["items"] == []
        assert result["total"] == 0
        mock_child_rollup.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_mixed_page_per_run_rollup(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run_a = _make_run(total_cost_usd=Decimal("1.000000"))
        run_b = _make_run(total_cost_usd=None)
        mock_db_list_runs.return_value = _make_list_result([run_a, run_b], total=2, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {run_a.id: (Decimal("0.250000"), 1)}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        assert len(result["items"]) == 2
        item_a = next(i for i in result["items"] if i["id"] == str(run_a.id))
        item_b = next(i for i in result["items"] if i["id"] == str(run_b.id))
        assert item_a["aggregate_cost_usd"] == 1.25
        assert item_a["child_runs_count"] == 1
        assert item_b["total_cost_usd"] is None
        assert item_b["child_runs_count"] == 0
        assert item_b["aggregate_cost_usd"] == 0.0
        mock_child_rollup.assert_awaited_once()


class TestListRunsErrorDetail(_AuthContext):
    """REST parity for ``error_detail`` (the Daily Watcher's hang-death signal)."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_returns_error_detail_alongside_error_code(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(error_code="node_cancelled", error_detail="run likely hung")
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        assert "error" not in result
        item = result["items"][0]
        assert item["error_code"] == "node_cancelled"
        assert item["error_detail"] == "run likely hung"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_error_detail_nullable_when_no_error(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(error_code=None, error_detail=None, status="complete")
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        item = result["items"][0]
        assert item["error_code"] is None
        assert item["error_detail"] is None

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_error_detail_preview_truncated_to_200(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(error_code="task_failure", error_detail="e" * 500)
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        item = result["items"][0]
        assert item["error_detail"].endswith("…")
        assert len(item["error_detail"]) == 201

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.db.crud.run.list_runs")
    async def test_error_detail_preview_redacts_secrets(
        self,
        mock_db_list_runs: AsyncMock,
        mock_child_rollup: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run = _make_run(error_code="task_failure", error_detail="openai call failed: sk-abcdefghijkl1234")
        mock_db_list_runs.return_value = _make_list_result([run], total=1, next_cursor=None, has_more=False)
        mock_child_rollup.return_value = {}
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await list_runs(limit=20)

        item = result["items"][0]
        assert "sk-abcdefghijkl1234" not in item["error_detail"]
        assert "<redacted>" in item["error_detail"]


class TestListRunsCostErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_runs()
        assert result["error"] == "auth_expired"
