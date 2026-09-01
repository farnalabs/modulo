"""Unit tests for the MCP trigger_pipeline app-level rate limit (PRD §7.18, 60/min)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import trigger_pipeline
from modulo.core.rate_limiter import TokenBucketRegistry

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PLACEHOLDER_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class _AuthContext:
    """Set/teardown the MCP ContextVars so the tool handlers reach the rate limit + DB layer."""

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


class TestTriggerPipelineClientKey:
    def setup_method(self) -> None:
        self._ctx = _AuthContext()
        self._ctx.setup_method()

    def teardown_method(self) -> None:
        self._ctx.teardown_method()

    def test_api_key_keyed_by_org_and_key_id(self) -> None:
        from modulo.api.mcp_server import _trigger_pipeline_client_key

        assert _trigger_pipeline_client_key() == (
            f"trigger_pipeline:{_PLACEHOLDER_ORG_ID}:api_key:ak:{_PLACEHOLDER_KEY_ID}"
        )

    def test_oauth_keyed_by_org_and_user_id(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_type, _trigger_pipeline_client_key

        _ctx_auth_type.set("oauth")
        assert _trigger_pipeline_client_key() == (
            f"trigger_pipeline:{_PLACEHOLDER_ORG_ID}:oauth:user:{_PLACEHOLDER_USER_ID}"
        )

    def test_distinct_clients_get_distinct_keys(self) -> None:
        from modulo.api.mcp_server import _trigger_pipeline_client_key

        key_a = _trigger_pipeline_client_key()
        from modulo.api.mcp_server import _ctx_key_id

        _ctx_key_id.set(uuid.UUID("00000000-0000-0000-0000-000000000099"))
        key_b = _trigger_pipeline_client_key()
        assert key_a != key_b


class TestTriggerPipelineRateAllowed(_AuthContext):
    @patch(
        "modulo.api.mcp_server._trigger_pipeline_limiter",
        new_callable=lambda: TokenBucketRegistry(rate=1.0, burst=3),
    )
    async def test_allowed_within_burst(self, limiter: TokenBucketRegistry) -> None:
        from modulo.api.mcp_server import _trigger_pipeline_rate_allowed

        assert await _trigger_pipeline_rate_allowed() is True
        assert await _trigger_pipeline_rate_allowed() is True
        assert await _trigger_pipeline_rate_allowed() is True
        assert await _trigger_pipeline_rate_allowed() is False

    @patch(
        "modulo.api.mcp_server._trigger_pipeline_limiter",
        new_callable=lambda: TokenBucketRegistry(rate=1.0, burst=1),
    )
    async def test_denied_when_exhausted(self, limiter: TokenBucketRegistry) -> None:
        from modulo.api.mcp_server import _trigger_pipeline_client_key, _trigger_pipeline_rate_allowed

        assert await _trigger_pipeline_rate_allowed() is True
        assert await _trigger_pipeline_rate_allowed() is False
        assert limiter._buckets[_trigger_pipeline_client_key()]._tokens < 1.0


class TestTriggerPipelineToolRateLimit(_AuthContext):
    @patch(
        "modulo.api.mcp_server._trigger_pipeline_limiter",
        new_callable=lambda: TokenBucketRegistry(rate=1.0, burst=1),
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_returns_rate_limited_when_bucket_exhausted(
        self,
        mock_validate_auth: AsyncMock,
        limiter: TokenBucketRegistry,
    ) -> None:
        from modulo.api.mcp_server import _trigger_pipeline_client_key

        assert await limiter.consume(_trigger_pipeline_client_key()) is True

        with (
            patch("modulo.api.mcp_server.get_pipeline") as mock_get_pipeline,
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph") as mock_snapshot,
            patch("modulo.db.crud.run.create_run") as mock_create_run,
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            result = await trigger_pipeline(pipeline_id=str(uuid.uuid4()))

        assert result["error"] == "rate_limited"
        assert "60/min" in result["detail"]
        mock_get_pipeline.assert_not_called()
        mock_snapshot.assert_not_called()
        mock_create_run.assert_not_called()
        mock_session.assert_not_called()

    @patch(
        "modulo.api.mcp_server._trigger_pipeline_limiter",
        new_callable=lambda: TokenBucketRegistry(rate=1.0, burst=1),
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.dispatch_run")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph")
    @patch("modulo.db.crud.run.create_run")
    async def test_succeeds_within_limit(
        self,
        mock_create_run: AsyncMock,
        mock_snapshot: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_dispatch: AsyncMock,
        mock_validate_auth: AsyncMock,
        limiter: TokenBucketRegistry,
    ) -> None:
        pipeline_id = uuid.uuid4()
        mock_pipeline = MagicMock()
        mock_get_pipeline.return_value = mock_pipeline
        mock_snap = MagicMock()
        mock_snap.graph_json = {"nodes": {"n1": {}}}
        mock_snapshot.return_value = mock_snap
        run_id = uuid.uuid4()
        thread_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = run_id
        mock_run.langgraph_thread_id = thread_id
        mock_create_run.return_value = mock_run
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await trigger_pipeline(pipeline_id=str(pipeline_id))

        assert result["run_id"] == str(run_id)
        assert result["status"] == "pending"
        assert result["langgraph_thread_id"] == thread_id
        mock_get_pipeline.assert_awaited_once()
        mock_dispatch.assert_awaited_once()


class TestTriggerPipelineSnapshotLockBusy(_AuthContext):
    """FAR-527: a busy snapshot advisory lock used to return a fake
    {"status": "queued"} with no run_id and nothing persisted — a silent drop
    that told the caller a retry existed when none did. The tool must return
    an honest, actionable error instead."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.dispatch_run")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph")
    @patch("modulo.db.crud.run.create_run")
    async def test_returns_snapshot_lock_busy_error_not_queued(
        self,
        mock_create_run: AsyncMock,
        mock_snapshot: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_session: AsyncMock,
        mock_dispatch: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.exceptions import SnapshotLockNotAvailableError

        mock_snapshot.side_effect = SnapshotLockNotAvailableError("busy")
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await trigger_pipeline(pipeline_id=str(uuid.uuid4()))

        assert result["error"] == "snapshot_lock_busy"
        assert "retry" in result["detail"].lower()
        assert result.get("status") != "queued"
        assert "queued" not in result.get("detail", "")
        assert "run_id" not in result
        mock_create_run.assert_not_called()
        mock_dispatch.assert_not_called()
