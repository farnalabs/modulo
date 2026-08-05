"""Unit tests for modulo.core.saq_worker (plan F1/F2/F5).

Covers: functions lists wired (runs + system), fail-closed auth, explicit cron
knobs, worker metadata hostname, and the SAQ execute/resume wrappers.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp.web
import pytest

import modulo.core.saq_worker as sw


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_redis_pool_size": 50,
        "redis_url": "redis://localhost:6379/0",
        "database_url": "postgresql+asyncpg://localhost/test",
        "modulo_db": "postgres",
        "saq_worker_db_pool_size": 2,
        "saq_auth_password": "pw",
        "saq_auth_username": "admin",
        "fernet_key": "x" * 44,
    }
    base.update(overrides)
    return MagicMock(**base)


class TestFunctionsWiring:
    def test_runs_functions_registered_under_dispatch_names(self) -> None:
        names = [f[0] for f in sw._runs_functions()]
        assert "modulo.core.saq_worker.execute_run" in names
        assert "modulo.core.saq_worker.resume_run" in names
        assert "modulo.core.saq_worker.fire_cron_trigger" in names
        assert "modulo.core.saq_worker.fire_polling_trigger" in names
        assert "modulo.core.saq_worker.fire_report_trigger" in names

    def test_system_functions_registered_under_qualname(self) -> None:
        names = [f.__name__ for f in sw._system_functions()]
        assert "fire_due_triggers" in names
        assert "dispatcher_reconcile" in names
        assert "claim_expiry" in names
        assert "retention_cleanup" in names
        assert "webhook_dedup_cleanup" in names
        assert "stale_run_recovery" in names

    def test_system_cron_knobs_explicit(self) -> None:
        jobs = {c.function.__name__: c for c in sw._system_cron_jobs()}
        assert set(jobs) == {
            "fire_due_triggers",
            "dispatcher_reconcile",
            "claim_expiry",
            "retention_cleanup",
            "webhook_dedup_cleanup",
            "stale_run_recovery",
            "cost_probe",
        }
        # fire_due_triggers: every 60s (croniter parses 5-field cron), timeout=300, retries=3 (F1).
        fdt = jobs["fire_due_triggers"]
        assert fdt.cron == "* * * * *"
        assert fdt.timeout == 300
        assert fdt.retries == 3
        assert fdt.heartbeat == 30
        assert fdt.ttl == 300
        assert fdt.unique is True
        # dispatcher_reconcile: timeout=120 (F1), every 60s.
        dr = jobs["dispatcher_reconcile"]
        assert dr.timeout == 120
        assert dr.cron == "* * * * *"
        assert dr.unique is True

    def test_settings_after_process_and_metadata(self) -> None:
        with patch.object(sw, "get_settings", return_value=_settings()):
            settings = sw.runs_settings()
        assert settings["after_process"] is not None
        assert "hostname" in settings["metadata"]


class TestFailClosedAuth:
    def test_system_settings_refuses_boot_without_password(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings(saq_auth_password="")),
            pytest.raises(RuntimeError, match="SAQ_AUTH_PASSWORD"),
        ):
            sw.system_settings()

    def test_system_settings_refuses_boot_without_username(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings(saq_auth_username=None)),
            pytest.raises(RuntimeError, match="SAQ_AUTH_USERNAME"),
        ):
            sw.system_settings()

    def test_system_settings_boots_when_auth_configured(self) -> None:
        with patch.object(sw, "get_settings", return_value=_settings()):
            settings = sw.system_settings()
        assert settings["queue"].name == "system"
        assert settings["cron_jobs"]

    def test_staging_system_settings_refuses_boot_without_auth(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings(saq_auth_password="")),
            pytest.raises(RuntimeError),
        ):
            sw.staging_system_settings()


class TestStagingQueueNames:
    def test_staging_workers_use_dedicated_queues(self) -> None:
        # Staging configures SAQ_RUNS_QUEUE=staging-runs; the workers derive
        # their queue names from it (never a hardcoded literal).
        with patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="staging-runs")):
            assert sw.staging_runs_settings()["queue"].name == "staging-runs"
            assert sw.staging_system_settings()["queue"].name == "staging-system"


class TestQueueDerivation:
    def test_non_default_saq_runs_queue_used_by_workers(self) -> None:
        # A non-default SAQ_RUNS_QUEUE must drive the runs worker (dispatch /
        # fire_due_triggers / health enqueue to the same name).
        with patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="my-runs")):
            assert sw.runs_settings()["queue"].name == "my-runs"

    def test_system_queue_derives_from_runs_queue(self) -> None:
        # health._configured_queues derives the system queue as
        # runs_queue.replace("runs", "system") — the worker must match.
        with patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="my-runs")):
            assert sw.system_settings()["queue"].name == "my-system"
        with patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="runs")):
            assert sw.system_settings()["queue"].name == "system"

    def test_system_queue_falls_back_without_runs_substring(self) -> None:
        with patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="queue-alpha")):
            assert sw.system_settings()["queue"].name == "system"


class TestMaxConcurrentOps:
    """``max_concurrent_ops`` must always leave reserve connections (FAR-88).

    The old ``max(pool_size - 5, 5)`` clamp gave zero reserve at pool 5
    (max_ops == pool) and could exceed the pool below 5; the semaphore must
    always stay strictly below the connection budget.
    """

    @pytest.mark.parametrize(
        ("pool_size", "expected"),
        [
            (1, 1),
            (3, 2),
            (5, 4),
            (20, 15),
            (50, 45),
        ],
    )
    def test_reserve_clamp(self, pool_size: int, expected: int) -> None:
        assert sw._max_concurrent_ops(pool_size) == expected

    def test_never_exhausts_pool(self) -> None:
        # For every pool in the settings' valid range (ge=1, le=50) the
        # semaphore must never equal or exceed the connection budget.
        for pool_size in range(1, 51):
            assert sw._max_concurrent_ops(pool_size) <= pool_size
        for pool_size in range(2, 51):
            assert sw._max_concurrent_ops(pool_size) < pool_size


class TestSystemWebRunner:
    """run_system_web must bind 127.0.0.1 AND map auth into AUTH_PASSWORD/AUTH_USER."""

    def _runner_patches(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AUTH_PASSWORD", raising=False)
        monkeypatch.delenv("AUTH_USER", raising=False)
        worker = MagicMock()
        worker.queue = MagicMock()
        loop = MagicMock()

        def _fake_create_task(coro: object) -> MagicMock:
            coro.close()  # type: ignore[union-attr]
            return MagicMock()

        loop.create_task.side_effect = _fake_create_task
        run_app = MagicMock()
        return (
            patch.object(sw, "Worker", return_value=worker),
            patch("modulo.core.saq_worker.asyncio.new_event_loop", return_value=loop),
            patch.object(aiohttp.web, "run_app", run_app),
            run_app,
        )

    def test_run_system_web_binds_127_0_0_1_and_maps_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker_patch, loop_patch, run_app_patch, run_app = self._runner_patches(monkeypatch)
        with (
            worker_patch,
            loop_patch,
            run_app_patch,
            patch.object(sw, "get_settings", return_value=_settings()),
        ):
            sw.run_system_web()
        assert os.environ["AUTH_PASSWORD"] == "pw"
        assert os.environ["AUTH_USER"] == "admin"
        _, kwargs = run_app.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8081

    def test_run_system_web_app_has_basicauth_middleware(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker_patch, loop_patch, run_app_patch, run_app = self._runner_patches(monkeypatch)
        with (
            worker_patch,
            loop_patch,
            run_app_patch,
            patch.object(sw, "get_settings", return_value=_settings()),
        ):
            sw.run_system_web()
        app = run_app.call_args.args[0]
        middleware_names = {type(m).__name__ for m in app.middlewares}
        assert "BasicAuthMiddleware" in middleware_names

    def test_run_system_web_fails_closed_without_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker_patch, loop_patch, run_app_patch, _ = self._runner_patches(monkeypatch)
        with (
            worker_patch,
            loop_patch,
            run_app_patch,
            patch.object(sw, "get_settings", return_value=_settings(saq_auth_password="")),
            pytest.raises(RuntimeError, match="SAQ_AUTH_PASSWORD"),
        ):
            sw.run_system_web()

    def test_run_system_web_fails_closed_without_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker_patch, loop_patch, run_app_patch, _ = self._runner_patches(monkeypatch)
        with (
            worker_patch,
            loop_patch,
            run_app_patch,
            patch.object(sw, "get_settings", return_value=_settings(saq_auth_username=None)),
            pytest.raises(RuntimeError, match="SAQ_AUTH_USERNAME"),
        ):
            sw.run_system_web()


class TestExecuteResumeWrappers:
    @pytest.mark.asyncio
    async def test_execute_run_claims_and_completes(self) -> None:
        ctx: dict = {"job": MagicMock()}
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=True) as claim,
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock) as load,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch("modulo.core.pipeline_execution.heartbeat_loop", new_callable=AsyncMock),
        ):
            run = MagicMock()
            run.input_payload = {"a": 1}
            executor = MagicMock()
            executor.execute = AsyncMock()
            load.return_value = (run, executor)
            result = await sw.execute_run(
                ctx, run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f", org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
            )

        assert result == {"status": "complete"}
        claim.assert_awaited_once()
        executor.execute.assert_awaited_once()
        complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_run_not_claimed_returns_early(self) -> None:
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=False),
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
        ):
            result = await sw.execute_run(
                {}, run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f", org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
            )
        assert result == {"status": "not_claimed"}
        complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_run_delegates(self) -> None:
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.pipeline_execution.resume_run", new_callable=AsyncMock, return_value={"status": "complete"}
            ) as core,
        ):
            result = await sw.resume_run(
                {},
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                resume_data={"action": "approved"},
            )
        assert result == {"status": "complete"}
        core.assert_awaited_once()
        assert core.await_args.kwargs["resume_data"] == {"action": "approved"}


class TestFireWrappersDispatchRuns:
    @pytest.mark.asyncio
    async def test_fire_cron_trigger_dispatches_created_run(self) -> None:
        fired = {"status": "fired", "run_id": "run-9"}
        with (
            patch("modulo.core.cron_helpers.fire_cron_trigger", new_callable=AsyncMock, return_value=fired) as ch,
            patch(
                "modulo.core.dispatch.dispatch_run", new_callable=AsyncMock, return_value=("enqueued", "job-1")
            ) as dispatch,
            patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="runs")),
        ):
            result = await sw.fire_cron_trigger(
                {},
                trigger_id="11111111-1111-4111-8111-111111111111",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                pipeline_id="22222222-2222-4222-8222-222222222222",
                cron_expression="* * * * *",
            )
        assert result["status"] == "fired"
        assert result["dispatch"] == "enqueued"
        dispatch.assert_awaited_once_with("run-9", "8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70", queue="runs")
        ch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fire_cron_trigger_not_fired_no_dispatch(self) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.fire_cron_trigger", new_callable=AsyncMock, return_value={"status": "skipped"}
            ),
            patch("modulo.core.dispatch.dispatch_run", new_callable=AsyncMock) as dispatch,
        ):
            result = await sw.fire_cron_trigger(
                {},
                trigger_id="11111111-1111-4111-8111-111111111111",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                pipeline_id="22222222-2222-4222-8222-222222222222",
                cron_expression="* * * * *",
            )
        assert result["status"] == "skipped"
        dispatch.assert_not_awaited()
