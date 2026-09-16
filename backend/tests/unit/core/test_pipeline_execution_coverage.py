"""Coverage boost for ``modulo.core.pipeline_execution`` (FAR-835).

Targets uncovered paths identified by the baseline coverage report.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

import modulo.core.pipeline_execution as pe

# -----------------------------------------------------------------------
# _resolve_claim_stale_seconds — settings path (line 177)
# -----------------------------------------------------------------------


def test_resolve_claim_stale_seconds_settings_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When stale_seconds is None, the settings value is used."""
    monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=500))
    assert pe._resolve_claim_stale_seconds(stale_seconds=None) == 500


def test_resolve_claim_stale_seconds_explicit_override() -> None:
    """An explicit stale_seconds overrides settings."""
    assert pe._resolve_claim_stale_seconds(stale_seconds=999) == 999


# -----------------------------------------------------------------------
# _maybe_alert_retry_storm — full happy path + CancelledError
# -----------------------------------------------------------------------


class TestMaybeAlertRetryStorm:
    async def test_happy_path_emits_alert(self) -> None:
        """When claim_count exceeds threshold, emit_saq_retry_storm_alert is called."""
        row_result = MagicMock()
        row_result.first.return_value = (5,)

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        emit_mock = AsyncMock()
        with patch("modulo.core.error_tracking.emit_saq_retry_storm_alert", emit_mock):
            await pe._maybe_alert_retry_storm(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        emit_mock.assert_awaited_once()
        assert emit_mock.await_args.args[3] == 5

    async def test_row_none_returns_early(self) -> None:
        """When the run row is not found, no alert is emitted."""
        row_result = MagicMock()
        row_result.first.return_value = None

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        emit_mock = AsyncMock()
        with patch("modulo.core.error_tracking.emit_saq_retry_storm_alert", emit_mock):
            await pe._maybe_alert_retry_storm(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        emit_mock.assert_not_awaited()

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError must not be swallowed."""

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                raise asyncio.CancelledError

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        with pytest.raises(asyncio.CancelledError):
            await pe._maybe_alert_retry_storm(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]


# -----------------------------------------------------------------------
# claim_run_async — exception handler (lines 329-333)
# -----------------------------------------------------------------------


class TestClaimRunAsyncException:
    async def test_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the claim DB call raises, claim_run_async returns None."""
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> None:
                raise RuntimeError("db down")

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        result = await pe.claim_run_async(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert result is None


# -----------------------------------------------------------------------
# load_and_setup — full path (lines 355-375)
# -----------------------------------------------------------------------


class TestLoadAndSetup:
    async def test_sets_rls_and_loads_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_and_setup sets RLS, loads the run, and creates an executor."""
        fake_run = MagicMock()

        async def _fake_get_run(session: Any, rid: object) -> Any:
            return fake_run

        async def _fake_set_rls(session: Any, oid: object) -> None:
            pass

        factory_cm = AsyncMock()
        factory_cm.__aenter__ = AsyncMock(return_value=factory_cm)
        factory_cm.__aexit__ = AsyncMock(return_value=False)
        factory_cm.begin = MagicMock(return_value=factory_cm)

        monkeypatch.setattr(pe, "set_rls_org", _fake_set_rls)
        monkeypatch.setattr(pe, "get_run", _fake_get_run)
        monkeypatch.setattr(pe, "async_sessionmaker", MagicMock(return_value=lambda: factory_cm))
        monkeypatch.setattr(
            pe,
            "get_settings",
            MagicMock(
                return_value=MagicMock(
                    database_url="postgresql+asyncpg://localhost/test",
                    fernet_key="a" * 32,
                )
            ),
        )

        with (
            patch("modulo.core.pipeline_engine.executor.PipelineExecutor", autospec=True),
            patch("modulo.core.notifier.Notifier", autospec=True),
        ):
            run, executor = await pe.load_and_setup(MagicMock(), uuid.uuid4(), uuid.uuid4())  # type: ignore[arg-type]
        assert run is fake_run
        assert executor is not None

    async def test_missing_run_returns_none_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the run is not found, load_and_setup returns (None, None)."""

        async def _fake_get_run(session: Any, rid: object) -> Any:
            return None

        async def _fake_set_rls(session: Any, oid: object) -> None:
            pass

        factory_cm = AsyncMock()
        factory_cm.__aenter__ = AsyncMock(return_value=factory_cm)
        factory_cm.__aexit__ = AsyncMock(return_value=False)
        factory_cm.begin = MagicMock(return_value=factory_cm)

        monkeypatch.setattr(pe, "set_rls_org", _fake_set_rls)
        monkeypatch.setattr(pe, "get_run", _fake_get_run)
        monkeypatch.setattr(pe, "async_sessionmaker", MagicMock(return_value=lambda: factory_cm))
        monkeypatch.setattr(
            pe,
            "get_settings",
            MagicMock(
                return_value=MagicMock(
                    database_url="postgresql+asyncpg://localhost/test",
                    fernet_key="a" * 32,
                )
            ),
        )

        run, executor = await pe.load_and_setup(MagicMock(), uuid.uuid4(), uuid.uuid4())  # type: ignore[arg-type]
        assert run is None
        assert executor is None

    async def test_notifier_init_failure_still_returns_executor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Notifier init failure is caught -- the executor still runs."""
        fake_run = MagicMock()

        async def _fake_get_run(session: Any, rid: object) -> Any:
            return fake_run

        async def _fake_set_rls(session: Any, oid: object) -> None:
            pass

        factory_cm = AsyncMock()
        factory_cm.__aenter__ = AsyncMock(return_value=factory_cm)
        factory_cm.__aexit__ = AsyncMock(return_value=False)
        factory_cm.begin = MagicMock(return_value=factory_cm)

        monkeypatch.setattr(pe, "set_rls_org", _fake_set_rls)
        monkeypatch.setattr(pe, "get_run", _fake_get_run)
        monkeypatch.setattr(pe, "async_sessionmaker", MagicMock(return_value=lambda: factory_cm))
        monkeypatch.setattr(
            pe,
            "get_settings",
            MagicMock(
                return_value=MagicMock(
                    database_url="postgresql+asyncpg://localhost/test",
                    fernet_key="bad",
                )
            ),
        )

        def _fail_notifier(*args: object, **kwargs: object) -> None:
            raise RuntimeError("fernet bad")

        with (
            patch("modulo.core.notifier.Notifier", side_effect=_fail_notifier),
            patch("modulo.core.pipeline_engine.executor.PipelineExecutor", autospec=True),
        ):
            run, executor = await pe.load_and_setup(MagicMock(), uuid.uuid4(), uuid.uuid4())  # type: ignore[arg-type]
        assert run is fake_run
        assert executor is not None


# -----------------------------------------------------------------------
# _read_current_claim_token (lines 380-387)
# -----------------------------------------------------------------------


class TestReadCurrentClaimToken:
    async def test_returns_token_when_present(self) -> None:
        row_result = MagicMock()
        row_result.first.return_value = ("tok-abc",)

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        result = await pe._read_current_claim_token(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert result == "tok-abc"

    async def test_returns_none_when_no_token(self) -> None:
        row_result = MagicMock()
        row_result.first.return_value = (None,)

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        result = await pe._read_current_claim_token(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert result is None


# -----------------------------------------------------------------------
# heartbeat_loop — settings path and claim_token fallback
# -----------------------------------------------------------------------


class TestHeartbeatLoopPaths:
    async def test_uses_settings_interval_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When interval_seconds is None, the loop reads from settings."""
        heartbeat_mock = AsyncMock()
        monkeypatch.setattr(pe, "heartbeat_once", heartbeat_mock)
        monkeypatch.setattr(pe, "_read_current_claim_token", AsyncMock(return_value="tok-x"))
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_heartbeat_seconds=42))
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock(side_effect=[None, KeyboardInterrupt()]))
        with pytest.raises(KeyboardInterrupt):
            await pe.heartbeat_loop(MagicMock(), "run-1", "org-1", claim_token=None)  # type: ignore[arg-type]
        assert pe.asyncio.sleep.await_args.args[0] == 42

    async def test_reads_claim_token_from_db_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When claim_token is None, the loop reads it from the DB."""
        heartbeat_mock = AsyncMock()
        read_token = AsyncMock(return_value="tok-from-db")
        monkeypatch.setattr(pe, "heartbeat_once", heartbeat_mock)
        monkeypatch.setattr(pe, "_read_current_claim_token", read_token)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock(side_effect=[None, KeyboardInterrupt()]))
        engine = MagicMock()
        with pytest.raises(KeyboardInterrupt):
            await pe.heartbeat_loop(engine, "run-1", "org-1", interval_seconds=1, claim_token=None)
        read_token.assert_awaited_once()
        heartbeat_mock.assert_awaited_with(engine, "run-1", "org-1", job=None, claim_token="tok-from-db")


# -----------------------------------------------------------------------
# _heartbeat_round — CancelledError propagation (line 512)
# -----------------------------------------------------------------------


class TestHeartbeatRoundCancelled:
    async def test_cancelled_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CancelledError from heartbeat_once must propagate."""

        async def _cancelled(*_a: object, **_kw: object) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(pe, "heartbeat_once", _cancelled)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock())
        with pytest.raises(asyncio.CancelledError):
            await pe._heartbeat_round(
                MagicMock(),
                "run-1",
                "org-1",
                interval_seconds=1,
                job=None,
                claim_token="tok",
                superseded=None,
                health_failed=None,
                consecutive_failures=0,
            )


# -----------------------------------------------------------------------
# _is_transient_db_error — DBAPIError with None orig (line 664)
# -----------------------------------------------------------------------


class TestIsTransientDbError:
    def test_dbapi_error_with_none_orig_is_not_transient(self) -> None:
        """A DBAPIError whose orig is None is classified non-transient."""
        exc = DBAPIError("stmt", {}, None)
        assert pe._is_transient_db_error(exc) is False

    def test_operational_error_is_transient(self) -> None:
        """OperationalError is always transient."""
        exc = OperationalError("stmt", {}, Exception("conn refused"))
        assert pe._is_transient_db_error(exc) is True

    def test_non_dbapi_error_is_not_transient(self) -> None:
        """A plain Exception is not transient."""
        assert pe._is_transient_db_error(ValueError("nope")) is False


# -----------------------------------------------------------------------
# _fail_run_terminal_with_retry — CancelledError path (line 702)
# -----------------------------------------------------------------------


class TestFailRunTerminalRetryCancelled:
    async def test_cancelled_error_propagates_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CancelledError must not be retried."""
        monkeypatch.setattr(pe, "_TERMINAL_WRITE_RETRY_BACKOFF_SECONDS", 0.0)

        async def _cancelled(*args: Any, **kwargs: Any) -> bool:
            raise asyncio.CancelledError

        with patch.object(pe, "fail_run_terminal", _cancelled), pytest.raises(asyncio.CancelledError):
            await pe._fail_run_terminal_with_retry(
                MagicMock(),  # type: ignore[arg-type]
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                error_code="executor_heartbeat_lost",
                error_detail="boom",
            )


# -----------------------------------------------------------------------
# _maybe_watchdog_retry — exception path (lines 793-797)
# -----------------------------------------------------------------------


class TestMaybeWatchdogRetry:
    async def test_hook_exception_returns_false(self) -> None:
        """When the retry hook raises, returns False (fail closed)."""

        async def _broken_hook(status: str, code: str) -> bool:
            raise RuntimeError("hook boom")

        result = await pe._maybe_watchdog_retry(
            _broken_hook, "run-1", final_status="stalled", error_code="executor_stalled"
        )
        assert result is False

    async def test_none_hook_returns_false(self) -> None:
        """When retry_hook is None, returns False immediately."""
        result = await pe._maybe_watchdog_retry(None, "run-1", final_status="stalled", error_code="executor_stalled")
        assert result is False

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError from the hook must not be caught."""

        async def _cancelled_hook(status: str, code: str) -> bool:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await pe._maybe_watchdog_retry(
                _cancelled_hook, "run-1", final_status="stalled", error_code="executor_stalled"
            )


# -----------------------------------------------------------------------
# _await_first_node — event clearing (lines 912-914)
# -----------------------------------------------------------------------


class TestAwaitFirstNode:
    async def test_node_started_clears_event_and_returns_true(self) -> None:
        """When node_started_event fires, it is cleared and True is returned."""
        started = asyncio.Event()
        done = asyncio.Event()
        exec_task = asyncio.create_task(asyncio.sleep(999))

        started.set()
        result = await pe._await_first_node(started, done, exec_task)
        assert result is True
        assert not started.is_set()
        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task

    async def test_run_done_returns_false(self) -> None:
        """When run_done_event fires, False is returned."""
        started = asyncio.Event()
        done = asyncio.Event()
        exec_task = asyncio.create_task(asyncio.sleep(999))

        done.set()
        result = await pe._await_first_node(started, done, exec_task)
        assert result is False
        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task

    async def test_exec_task_done_returns_false(self) -> None:
        """When exec_task finishes, False is returned."""
        started = asyncio.Event()
        done = asyncio.Event()
        exec_task = asyncio.create_task(asyncio.sleep(0))
        await exec_task

        result = await pe._await_first_node(started, done, exec_task)
        assert result is False


# -----------------------------------------------------------------------
# _read_run_status (lines 1141-1148)
# -----------------------------------------------------------------------


class TestReadRunStatus:
    async def test_returns_status_string(self) -> None:
        row_result = MagicMock()
        row_result.first.return_value = ("running",)

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        result = await pe._read_run_status(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert result == "running"

    async def test_returns_none_when_no_row(self) -> None:
        row_result = MagicMock()
        row_result.first.return_value = None

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        result = await pe._read_run_status(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert result is None


# -----------------------------------------------------------------------
# _kill_sandbox_best_effort (lines 1157-1176)
# -----------------------------------------------------------------------


class TestKillSandboxBestEffort:
    async def test_no_sandbox_id_returns_early(self) -> None:
        """When the run has no sandbox_id, nothing happens."""
        row_result = MagicMock()
        row_result.first.return_value = (None,)

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        await pe._kill_sandbox_best_effort(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError must not be swallowed."""

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                raise asyncio.CancelledError

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        with pytest.raises(asyncio.CancelledError):
            await pe._kill_sandbox_best_effort(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]

    async def test_sandbox_kill_failure_is_swallowed(self) -> None:
        """A failed sandbox kill is logged and swallowed."""
        row_result = MagicMock()
        row_result.first.return_value = ("sb-123",)

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                return row_result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        async def _fail_connect(*args: object, **kwargs: object) -> None:
            raise RuntimeError("sandbox connect failed")

        with patch("e2b.AsyncSandbox.connect", _fail_connect):
            await pe._kill_sandbox_best_effort(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]


# -----------------------------------------------------------------------
# _cancel_and_await_tasks — exec_task cancel (lines 1625-1627)
# -----------------------------------------------------------------------


class TestCancelAndAwaitTasks:
    async def test_cancels_exec_task_when_not_done(self) -> None:
        """When exec_task is not done, it is cancelled and awaited."""
        exec_task = asyncio.create_task(asyncio.sleep(999))
        heartbeat_task = asyncio.create_task(asyncio.sleep(0))
        await heartbeat_task
        node_deadline_task = asyncio.create_task(asyncio.sleep(0))
        await node_deadline_task

        await pe._cancel_and_await_tasks(None, None, node_deadline_task, exec_task, heartbeat_task)
        assert exec_task.cancelled()

    async def test_skips_exec_task_when_done(self) -> None:
        """When exec_task is already done, it is not cancelled."""
        exec_task = asyncio.create_task(asyncio.sleep(0))
        await exec_task
        heartbeat_task = asyncio.create_task(asyncio.sleep(0))
        await heartbeat_task
        node_deadline_task = asyncio.create_task(asyncio.sleep(0))
        await node_deadline_task

        await pe._cancel_and_await_tasks(None, None, node_deadline_task, exec_task, heartbeat_task)
        assert not exec_task.cancelled()


# -----------------------------------------------------------------------
# _resolve_result_status — CancelledError path (line 1653)
# -----------------------------------------------------------------------


class TestResolveResultStatus:
    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError from _read_run_status must propagate."""
        exec_result = MagicMock(status=None)
        with (
            patch.object(pe, "_read_run_status", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await pe._resolve_result_status(MagicMock(), "run-1", "org-1", exec_result, uuid.uuid4())  # type: ignore[arg-type]

    async def test_status_from_exec_result(self) -> None:
        """When exec_result has a status attribute, it is used directly."""
        exec_result = MagicMock(status="complete")
        status = await pe._resolve_result_status(MagicMock(), "run-1", "org-1", exec_result, uuid.uuid4())  # type: ignore[arg-type]
        assert status == "complete"

    async def test_fallback_to_db_when_pending(self) -> None:
        """When exec_result.status is 'pending', falls back to DB read."""
        exec_result = MagicMock(status="pending")
        with patch.object(pe, "_read_run_status", new_callable=AsyncMock, return_value="running"):
            status = await pe._resolve_result_status(MagicMock(), "run-1", "org-1", exec_result, uuid.uuid4())  # type: ignore[arg-type]
        assert status == "running"


# -----------------------------------------------------------------------
# _re_dispatch_capacity_blocked (lines 1682-1691)
# -----------------------------------------------------------------------


class TestReDispatchCapacityBlocked:
    async def test_returns_outcome_string(self) -> None:
        """_re_dispatch_capacity_blocked returns the outcome string from dispatch_run."""
        with patch(
            "modulo.core.dispatch.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-123"),
        ):
            outcome = await pe._re_dispatch_capacity_blocked("run-1", "org-1")
        assert outcome == "enqueued"

    async def test_exception_returns_failed(self) -> None:
        """When dispatch_run raises, returns 'failed'."""
        with patch(
            "modulo.core.dispatch.dispatch_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch boom"),
        ):
            outcome = await pe._re_dispatch_capacity_blocked("run-1", "org-1")
        assert outcome == "failed"

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError from dispatch_run must propagate."""
        with (
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await pe._re_dispatch_capacity_blocked("run-1", "org-1")


# -----------------------------------------------------------------------
# stale_run_recovery_sweep — no-orgs early return (line 1927)
# -----------------------------------------------------------------------


class TestStaleRunRecoverySweepEdgeCases:
    async def test_no_orgs_returns_zero_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When there are no orgs, the sweep returns zero counts immediately."""

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                result = MagicMock()
                result.all.return_value = []
                return result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        monkeypatch.setattr(
            pe,
            "get_settings",
            lambda: MagicMock(
                saq_never_dispatched_window=300,
                saq_worker_lost_window=600,
            ),
        )
        result = await pe.stale_run_recovery_sweep(_Engine())  # type: ignore[arg-type]
        assert result["never_dispatched_swept"] == 0
        assert result["worker_lost_swept"] == 0
        assert result["capacity_timeout_swept"] == 0
        assert result["stranded_capacity_redispatched"] == 0

    async def test_cancelled_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CancelledError from the org enumeration must propagate."""

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                raise asyncio.CancelledError

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        monkeypatch.setattr(
            pe,
            "get_settings",
            lambda: MagicMock(
                saq_never_dispatched_window=300,
                saq_worker_lost_window=600,
            ),
        )
        with pytest.raises(asyncio.CancelledError):
            await pe.stale_run_recovery_sweep(_Engine())  # type: ignore[arg-type]


# -----------------------------------------------------------------------
# build_resume_claim_update / _resume_claim_params (lines 2068-2070, 2080-2083)
# -----------------------------------------------------------------------


class TestResumeClaimUpdate:
    def test_with_claim_token_returns_token_sql(self) -> None:
        """When claim_token is given, the SQL includes claim_token=:tok."""
        stmt = pe.build_resume_claim_update(_stale_seconds=450, claim_token="tok-abc")
        sql = str(stmt)
        assert "claim_token=:tok" in sql
        assert "status='running'" in sql
        assert "RETURNING id" in sql

    def test_without_claim_token_returns_plain_sql(self) -> None:
        """When claim_token is None, the SQL does not include claim_token=:tok."""
        stmt = pe.build_resume_claim_update(_stale_seconds=450)
        sql = str(stmt)
        assert "claim_token=:tok" not in sql
        assert "status='running'" in sql

    def test_resume_claim_params_includes_token(self) -> None:
        """_resume_claim_params includes 'tok' key when token is given."""
        params = pe._resume_claim_params("run-1", "org-1", 450, 20, claim_token="tok-x")
        assert params["tok"] == "tok-x"
        assert params["rid"] == "run-1"
        assert params["oid"] == "org-1"

    def test_resume_claim_params_excludes_token_when_none(self) -> None:
        """_resume_claim_params omits 'tok' key when token is None."""
        params = pe._resume_claim_params("run-1", "org-1", 450, 20)
        assert "tok" not in params


# -----------------------------------------------------------------------
# claim_resume_run_async (lines 2109-2131)
# -----------------------------------------------------------------------


class TestClaimResumeRunAsync:
    async def test_successful_claim_returns_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the claim UPDATE matches, a fresh token is returned."""
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                result = MagicMock()
                result.fetchone.return_value = ("id",)
                return result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        token = await pe.claim_resume_run_async(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is not None
        assert isinstance(token, str)
        assert len(token) == 32  # uuid4 hex

    async def test_unclaimable_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the claim UPDATE matches zero rows, returns None."""
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                result = MagicMock()
                result.fetchone.return_value = None
                return result

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        token = await pe.claim_resume_run_async(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is None

    async def test_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the DB call raises, returns None."""
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> None:
                raise RuntimeError("db down")

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        token = await pe.claim_resume_run_async(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is None


# -----------------------------------------------------------------------
# resume_run — load_and_setup failure, run missing, job update failure
# -----------------------------------------------------------------------


class TestResumeRunEdgeCases:
    async def test_load_and_setup_failure_terminal_fails(self) -> None:
        """When load_and_setup raises, the run is terminal-failed."""
        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok"),
            patch.object(pe, "load_and_setup", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail,
        ):
            result = await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
            )
        assert result["status"] == "setup_failed"
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_setup_failed"

    async def test_run_not_found_returns_missing(self) -> None:
        """When load_and_setup returns (None, None), returns missing."""
        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok"),
            patch.object(pe, "load_and_setup", new_callable=AsyncMock, return_value=(None, None)),
        ):
            result = await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
            )
        assert result["status"] == "missing"

    async def test_job_kwargs_stamp_failure_is_swallowed(self) -> None:
        """When job.update(kwargs=...) raises, the warning is logged and resume continues."""
        job = MagicMock()
        job.kwargs = {}
        job.update = AsyncMock(side_effect=RuntimeError("job boom"))

        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok"),
            patch.object(pe, "load_and_setup", new_callable=AsyncMock) as load,
            patch.object(pe, "mark_complete", new_callable=AsyncMock),
            patch.object(pe, "run_executor_with_watchdog", new_callable=AsyncMock, return_value={"status": "complete"}),
            patch.object(pe, "_log") as log,
        ):
            run_obj = MagicMock()
            executor_obj = MagicMock()
            executor_obj.resume = AsyncMock()
            load.return_value = (run_obj, executor_obj)
            result = await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                job=job,
            )
        assert result["status"] == "complete"
        log.warning.assert_called_once()
        assert "job kwargs stamp failed" in log.warning.call_args.args[0]

    async def test_mark_complete_called_on_successful_resume(self) -> None:
        """When resume completes successfully, mark_complete is called with the claim token."""

        async def _pass_through(*args: Any, **kwargs: Any) -> dict[str, str]:
            return {"status": "complete"}

        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok-fresh"),
            patch.object(pe, "load_and_setup", new_callable=AsyncMock) as load,
            patch.object(pe, "mark_complete", new_callable=AsyncMock) as complete,
            patch.object(pe, "run_executor_with_watchdog", side_effect=_pass_through),
        ):
            run_obj = MagicMock()
            executor_obj = MagicMock()
            executor_obj.resume = AsyncMock()
            load.return_value = (run_obj, executor_obj)
            result = await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                job=None,
            )
        assert result["status"] == "complete"
        complete.assert_awaited_once()
        assert complete.await_args.kwargs["claim_token"] == "tok-fresh"

    async def test_load_and_setup_cancelled_error_propagates(self) -> None:
        """CancelledError from load_and_setup must propagate."""
        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok"),
            patch.object(pe, "load_and_setup", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
            )

    async def test_job_update_cancelled_error_propagates(self) -> None:
        """CancelledError from job.update must propagate."""
        job = MagicMock()
        job.kwargs = {}
        job.update = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok"),
            pytest.raises(asyncio.CancelledError),
        ):
            await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                job=job,
            )


# -----------------------------------------------------------------------
# claim_run_async — CancelledError path (line 330)
# -----------------------------------------------------------------------


class TestClaimRunAsyncCancelled:
    async def test_cancelled_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CancelledError in claim_run_async must propagate, not be swallowed."""
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> None:
                raise asyncio.CancelledError

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        with pytest.raises(asyncio.CancelledError):
            await pe.claim_run_async(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]


# -----------------------------------------------------------------------
# node_deadline_watchdog — settings path (line 1064)
# -----------------------------------------------------------------------


class TestNodeDeadlineWatchdogSettings:
    @pytest.mark.asyncio
    async def test_uses_settings_default_timeout_when_none(self) -> None:
        """When default_timeout is None, the watchdog reads from settings."""
        exec_task = asyncio.create_task(asyncio.sleep(999))
        started = asyncio.Event()
        completed = asyncio.Event()
        done = asyncio.Event()
        deadlines: dict[str, tuple[float, int]] = {}

        with (
            patch.object(pe, "get_settings", return_value=MagicMock(saq_node_default_timeout_seconds=999)),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail,
        ):
            # Set run done immediately so the watchdog stands down.
            done.set()
            await pe.node_deadline_watchdog(  # type: ignore[arg-type]
                MagicMock(),
                "run-1",
                "org-1",
                exec_task=exec_task,
                stall_requested=asyncio.Event(),
                node_started_event=started,
                node_completed_event=completed,
                run_done_event=done,
                node_deadlines=deadlines,
                default_timeout=None,
            )
        fail.assert_not_awaited()
        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task


# -----------------------------------------------------------------------
# _await_watchdog_bounded — task.done() path (line 1492)
# -----------------------------------------------------------------------


class TestAwaitWatchdogBounded:
    @pytest.mark.asyncio
    async def test_done_task_returns_immediately(self) -> None:
        """When the watchdog task is already done, returns immediately."""
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task  # ensure it's done

        await pe._await_watchdog_bounded(
            done_task,
            stall_requested=asyncio.Event(),
            health_failed=asyncio.Event(),
            superseded=asyncio.Event(),
            label="test",
            rid=uuid.uuid4(),
        )
        # No error, no hang — returned immediately.

    @pytest.mark.asyncio
    async def test_none_task_returns_immediately(self) -> None:
        """When the watchdog task is None, returns immediately."""
        await pe._await_watchdog_bounded(
            None,
            stall_requested=asyncio.Event(),
            health_failed=asyncio.Event(),
            superseded=asyncio.Event(),
            label="test",
            rid=uuid.uuid4(),
        )
