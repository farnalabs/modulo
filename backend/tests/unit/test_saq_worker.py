"""Unit tests for modulo.core.saq_worker (plan F1/F2/F5).

Covers: functions lists wired (runs + system), fail-closed auth, explicit cron
knobs, worker metadata hostname, the SAQ execute/resume wrappers, startup
health checks (engine, Redis probe, DB probe), fire wrappers, system job
delegates, and claim expiry.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import aiohttp.web
import pytest
import redis.exceptions

import modulo.core.saq_worker as sw

_UUID_1 = "11111111-1111-4111-8111-111111111111"
_UUID_2 = "22222222-2222-4222-8222-222222222222"
_UUID_3 = "33333333-3333-4333-8333-333333333333"
_UUID_4 = "44444444-4444-4444-8444-444444444444"
_UUID_ORG = "8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_redis_pool_size": 50,
        "saq_worker_concurrency": 5,
        "redis_url": "redis://localhost:6379/0",
        "database_url": "postgresql+asyncpg://localhost/test",
        "modulo_db": "postgres",
        "saq_worker_db_pool_size": 2,
        "saq_auth_password": "pw",
        "saq_auth_username": "admin",
        "fernet_key": "x" * 44,
        "modulo_library_sync_interval_seconds": 300,
    }
    base.update(overrides)
    return MagicMock(**base)


# Minimal env so ``modulo.db.session`` can be imported in a worktree (no .env):
# its module-level ``engine = _build_engine()`` constructs ``Settings()``, which
# requires DATABASE_URL/SECRET_KEY/FERNET_KEY. On main these come from .env.
_MIN_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://localhost/test",
    "SECRET_KEY": "s" * 44,
    "FERNET_KEY": "f" * 44,
}


def _make_retention_factory() -> tuple[MagicMock, MagicMock]:
    """Return a sessionmaker mock usable as ``async with factory() as session,
    session.begin():`` plus its session.

    ``retention_cleanup`` enters a system session (no ``set_rls_org``) and a
    ``session.begin()`` transaction, then calls both batch deletes against it.
    """
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = session_cm
    return factory, session


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
        assert "hitl_overdue" in names
        assert "retention_cleanup" in names
        assert "webhook_dedup_cleanup" in names
        assert "trigger_events_cleanup" in names
        assert "stale_run_recovery" in names
        assert "journey_reconcile" in names
        assert "check_missed_fire_alerts_cron" in names
        assert "library_sync" in names

    def test_system_cron_knobs_explicit(self) -> None:
        # _system_cron_jobs derives the library_sync cadence from settings, so
        # the settings factory is patched (real Settings() requires env vars).
        with patch.object(sw, "get_settings", return_value=_settings()):
            jobs = {c.function.__name__: c for c in sw._system_cron_jobs()}
        assert set(jobs) == {
            "fire_due_triggers",
            "dispatcher_reconcile",
            "claim_expiry",
            "hitl_overdue",
            "retention_cleanup",
            "webhook_dedup_cleanup",
            "trigger_events_cleanup",
            "stale_run_recovery",
            "cost_probe",
            "analytics_facts_maintenance",
            "journey_reconcile",
            "check_missed_fire_alerts_cron",
            "library_sync",
            "metrics_dump",
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
        # hitl_overdue: every 5 minutes (overdue thresholds are hour-scale),
        # unique so overlapping ticks cannot double-dispatch.
        ho = jobs["hitl_overdue"]
        assert ho.cron == "*/5 * * * *"
        assert ho.timeout == 120
        assert ho.retries == 2
        assert ho.heartbeat == 30
        assert ho.ttl == 300
        assert ho.unique is True
        # check_missed_fire_alerts: hourly, 5-field form (NOT 6-field — the bug
        # class #680 croniter seconds-field misparse), unique so overlaps are
        # impossible (the probe has its own in-memory cooldown).
        mf = jobs["check_missed_fire_alerts_cron"]
        assert mf.cron == "0 * * * *"
        assert mf.timeout == 300
        assert mf.retries == 2
        assert mf.heartbeat == 30
        assert mf.ttl == 300
        assert mf.unique is True
        # journey_reconcile: hourly (bounded + idempotent — overlapping ticks
        # cannot double-advance), unique so ticks cannot interleave.
        jr = jobs["journey_reconcile"]
        assert jr.cron == "0 * * * *"
        assert jr.timeout == 300
        assert jr.retries == 2
        assert jr.heartbeat == 30
        assert jr.ttl == 300
        assert jr.unique is True
        # library_sync: cadence derives from modulo_library_sync_interval_seconds
        # (default 300s -> */5 * * * *), fail-open (retries=1, never raises).
        ls = jobs["library_sync"]
        assert ls.cron == "*/5 * * * *"
        assert ls.timeout == 300
        assert ls.retries == 1
        assert ls.heartbeat == 30
        assert ls.ttl == 300
        assert ls.unique is True

        # metrics_dump: ticks every 10 minutes (*/10 * * * *), aligned to the
        # in-job jitter gate (FAR-356 review — the gate only ever fires when the
        # cron cadence matches its grid). unique=True, long timeout (full org
        # scan), single retry, generous ttl. Per-instance jitter spreads real
        # execution across a 6-hour window via an in-job gate.
        md = jobs["metrics_dump"]
        assert md.cron == "*/10 * * * *"
        assert md.timeout == 600
        assert md.heartbeat == 60
        assert md.retries == 1
        assert md.ttl == 900
        assert md.unique is True

    def test_sync_interval_to_cron_maps_seconds_to_5_field_cron(self) -> None:
        assert sw._sync_interval_to_cron(300) == "*/5 * * * *"
        assert sw._sync_interval_to_cron(60) == "* * * * *"
        assert sw._sync_interval_to_cron(30) == "* * * * *"
        assert sw._sync_interval_to_cron(3600) == "0 */1 * * *"
        assert sw._sync_interval_to_cron(7200) == "0 */2 * * *"
        assert sw._sync_interval_to_cron(86400) == "0 0 * * *"

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


class TestEffectiveRedisPoolSize:
    """The effective pool must always exceed concurrency (2026-08-10 wedge).

    SAQ's blocking ``dequeue()`` (``blmove``) holds one pool connection per
    concurrent ``_process`` task regardless of ``max_concurrent_ops``. A pool
    <= concurrency starves the Upkeep tasks (``schedule``/``sweep``/``abort``/
    ``worker_info``) of a connection — ``ConnectionError: Too many connections``
    kills the heartbeats and the worker wedges silently. The effective pool is
    ``max(configured_pool, concurrency + 5)``.
    """

    @pytest.mark.parametrize(
        ("pool_size", "concurrency", "expected"),
        [
            (50, 20, 50),  # pool already sufficient, unchanged
            (20, 20, 25),  # equal -> raised to concurrency + 5
            (5, 20, 25),  # too small -> raised (the exact incident config)
            (2, 20, 25),
            (20, 5, 20),  # default config, unchanged
            (1, 1, 6),
            (50, 50, 55),  # max concurrency raises above the settings cap; the
            # worker accepts it (le=50 bounds the configured value, the
            # effective pool is a runtime override)
        ],
    )
    def test_effective_pool_never_below_concurrency_plus_reserve(
        self, pool_size: int, concurrency: int, expected: int
    ) -> None:
        assert sw._effective_redis_pool_size(pool_size, concurrency) == expected

    def test_effective_pool_always_exceeds_concurrency(self) -> None:
        # For every pool and concurrency in the settings' valid ranges the
        # effective pool must always be strictly larger than concurrency.
        for pool in range(1, 51):
            for concurrency in range(1, 51):
                assert sw._effective_redis_pool_size(pool, concurrency) > concurrency

    def test_build_queue_uses_effective_pool_and_clamps_max_ops(self, caplog: pytest.LogCaptureFixture) -> None:
        # A configured pool of 5 with concurrency 20 (the incident config) must
        # produce a client pool of 25 and a max_concurrent_ops of 20 (25-5),
        # and log a warning that the pool was raised.
        with (
            patch.object(sw, "get_settings", return_value=_settings(saq_redis_pool_size=5, saq_worker_concurrency=20)),
            patch("modulo.core.saq_worker.aioredis.from_url") as from_url,
            patch("modulo.core.saq_worker.RedisQueue") as redis_queue,
            caplog.at_level(logging.WARNING, logger="modulo.core.saq_worker"),
        ):
            client = MagicMock()
            client.ping.return_value = True
            from_url.return_value = client
            sw._build_queue("runs")

        assert from_url.call_args.kwargs["max_connections"] == 25
        assert redis_queue.call_args.kwargs["max_concurrent_ops"] == 20
        assert redis_queue.call_args.kwargs["name"] == "runs"
        assert "pool_raised" in caplog.text

    def test_build_queue_keeps_configured_pool_when_sufficient(self, caplog: pytest.LogCaptureFixture) -> None:
        # A pool of 50 with concurrency 20 is already sufficient — no raise, no
        # warning, and max_concurrent_ops clamped to 45 (50-5).
        with (
            patch.object(sw, "get_settings", return_value=_settings(saq_redis_pool_size=50, saq_worker_concurrency=20)),
            patch("modulo.core.saq_worker.aioredis.from_url") as from_url,
            patch("modulo.core.saq_worker.RedisQueue") as redis_queue,
            caplog.at_level(logging.WARNING, logger="modulo.core.saq_worker"),
        ):
            client = MagicMock()
            client.ping.return_value = True
            from_url.return_value = client
            sw._build_queue("runs")

        assert from_url.call_args.kwargs["max_connections"] == 50
        assert redis_queue.call_args.kwargs["max_concurrent_ops"] == 45
        assert "pool_raised" not in caplog.text


class TestEffectiveDbPoolSize:
    """The effective DB pool must size for multiple connections per run.

    Each concurrent run draws several DB connections (the
    ``load_and_setup`` session, the executor connection, and the watchdog's
    ``fail_run_terminal`` write), so the floor is ``concurrency *
    CONNS_PER_RUN + reserve`` — not ``concurrency + 5``. With concurrency 20
    and a pool sized for 1 connection/run the worker exhausts its pool in
    pre-node setup and the watchdog cannot terminalize — the run rides to the
    35-min ``dispatcher_reconcile`` backstop (the agent.stall nodeless zombie).
    The effective pool is ``max(configured_pool, concurrency * CONNS_PER_RUN +
    reserve)``.
    """

    @pytest.mark.parametrize(
        ("pool_size", "concurrency", "expected"),
        [
            (30, 5, 30),  # pool already sufficient, unchanged
            (20, 20, 65),  # equal -> raised to concurrency*CONNS_PER_RUN + 5
            (5, 20, 65),  # too small -> raised (the incident config)
            (2, 20, 65),
            (20, 5, 20),  # default config, unchanged
            (1, 1, 8),  # 1*3 + 5 reserve
            (50, 50, 155),  # max concurrency raises above the settings cap; the
            # worker accepts it (le=200 bounds the configured value, the
            # effective pool is a runtime override)
        ],
    )
    def test_effective_db_pool_never_below_concurrency_plus_reserve(
        self, pool_size: int, concurrency: int, expected: int
    ) -> None:
        assert sw._effective_db_pool_size(pool_size, concurrency) == expected

    def test_effective_db_pool_always_exceeds_concurrency(self) -> None:
        # For every pool and concurrency in the settings' valid ranges the
        # effective pool must always be strictly larger than concurrency.
        for pool in range(1, 201):
            for concurrency in range(1, 51):
                assert sw._effective_db_pool_size(pool, concurrency) > concurrency


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
        job = MagicMock()
        job.update = AsyncMock()
        ctx: dict = {"job": job}

        async def _pass_through(aeng: Any, **kwargs: Any) -> dict[str, str]:
            # Faithfully execute the executor body (the real watchdog wiring is
            # covered by pipeline_execution tests) so the assertions below see
            # the executor.execute call and its return path.
            await kwargs["execute_fn"]()
            return {"status": "complete"}

        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value="tok-claim"
            ) as claim,
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock) as load,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch(
                "modulo.core.pipeline_execution.run_executor_with_watchdog",
                side_effect=_pass_through,
            ) as watchdog,
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
        assert executor.execute.await_args.kwargs["claim_token"] == "tok-claim"
        complete.assert_awaited_once()
        assert complete.await_args.kwargs["claim_token"] == "tok-claim"
        watchdog.assert_awaited_once()
        assert watchdog.await_args.kwargs["job"] is not None
        assert watchdog.await_args.kwargs["claim_token"] == "tok-claim"
        # The claim token is stamped into the job hash so the after_process
        # task_failure hook can fence its terminal write (A1).
        job.update.assert_awaited_once()
        assert job.update.await_args.kwargs["kwargs"]["claim_token"] == "tok-claim"

    @pytest.mark.asyncio
    async def test_execute_run_failed_outcome_skips_mark_complete(self) -> None:
        """An honest ``failed`` outcome must NOT run mark_complete (A2) — a
        silent wrong-success write is never attempted after a failure."""
        job = MagicMock()
        job.update = AsyncMock()
        ctx: dict = {"job": job}

        async def _fail_through(aeng: Any, **kwargs: Any) -> dict[str, str]:
            await kwargs["execute_fn"]()
            return {"status": "failed"}

        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value="tok-claim"),
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock) as load,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch(
                "modulo.core.pipeline_execution.run_executor_with_watchdog",
                side_effect=_fail_through,
            ),
        ):
            run = MagicMock()
            run.input_payload = {}
            executor = MagicMock()
            executor.execute = AsyncMock()
            load.return_value = (run, executor)
            result = await sw.execute_run(
                ctx, run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f", org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
            )

        assert result == {"status": "failed"}
        complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_run_not_claimed_returns_early(self) -> None:
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=None),
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
        ):
            result = await sw.execute_run(
                {}, run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f", org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
            )
        assert result == {"status": "not_claimed"}
        complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_run_setup_failure_fails_run_terminal(self) -> None:
        """A run claimed but whose load_and_setup raises (e.g. a DB
        OperationalError during checkpointer setup) must be terminal-failed —
        never left 'running' with no worker — and the SAQ job returns cleanly."""
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=True),
            patch(
                "modulo.core.pipeline_execution.load_and_setup",
                new_callable=AsyncMock,
                side_effect=RuntimeError("OperationalError: FK corrupt"),
            ),
            patch(
                "modulo.core.pipeline_execution.fail_run_terminal",
                new_callable=AsyncMock,
                return_value=True,
            ) as fail,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
        ):
            result = await sw.execute_run(
                {}, run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f", org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
            )
        assert result == {"status": "setup_failed"}
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_setup_failed"
        complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_run_setup_failure_records_terminal_facts(self) -> None:
        """FAR-162 (P6'): the executor_setup_failed terminal path routes through
        the REAL ``fail_run_terminal``, which records a compensating daily fact
        for the failed run — a setup-failed run must be visible in analytics."""

        class _FakeConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _FakeConn:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
                result = MagicMock()
                result.fetchone.return_value = ("id",)
                return result

        class _FakeEngine:
            def connect(self) -> _FakeConn:
                return _FakeConn()

        with (
            patch.object(sw, "_get_async_engine", return_value=_FakeEngine()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=True),
            patch(
                "modulo.core.pipeline_execution.load_and_setup",
                new_callable=AsyncMock,
                side_effect=RuntimeError("OperationalError: FK corrupt"),
            ),
            patch("modulo.core.pipeline_execution._advance_journeys_from_stored_refs", new_callable=AsyncMock),
            patch(
                "modulo.core.pipeline_execution._record_fact_for_terminal_failed_run", new_callable=AsyncMock
            ) as record_facts,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
        ):
            result = await sw.execute_run(
                {}, run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f", org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
            )

        assert result == {"status": "setup_failed"}
        complete.assert_not_awaited()
        # The REAL fail_run_terminal runs (not patched) and records the fact
        # after the terminal UPDATE.
        assert record_facts.await_count == 1

    @pytest.mark.asyncio
    async def test_execute_run_setup_hang_meets_setup_timeout(self) -> None:
        """FIX A (FAR-372): a load_and_setup that NEVER returns (a wedged
        pre-node setup — DB connection exhaustion, graph-compile hang) must
        fail fast at the setup grace instead of riding to the 35-min
        agent.stall backstop. The run is terminal-failed with
        executor_setup_failed and execute_run returns promptly (well under the
        backstop). Without the asyncio.wait_for wrap this test hangs until the
        35-min dispatcher_reconcile backstop — proving the fix."""

        async def _hang(*a: Any, **k: Any) -> tuple[MagicMock, MagicMock]:
            # Never returns: simulates a run wedged in pre-node setup.
            await asyncio.sleep(10_000)
            return (MagicMock(), MagicMock())

        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.pipeline_execution.claim_run_async",
                new_callable=AsyncMock,
                return_value="tok-claim",
            ),
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock, side_effect=_hang),
            patch(
                "modulo.core.pipeline_execution.fail_run_terminal",
                new_callable=AsyncMock,
                return_value=True,
            ) as fail,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch.object(sw, "get_settings", return_value=_settings(saq_setup_grace_seconds=1)),
        ):
            # The outer guard fails the test fast (instead of hanging 35 min)
            # if the inner setup-grace wrap is missing.
            result = await asyncio.wait_for(
                sw.execute_run(
                    {},
                    run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                    org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                ),
                timeout=10,
            )

        assert result == {"status": "setup_failed"}
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_setup_failed"
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

    @pytest.mark.asyncio
    async def test_execute_run_accepts_stale_claim_token_on_saq_retry(self) -> None:
        """SAQ retries re-invoke the job function with **job.kwargs, which since
        PR #1003 includes the stamped claim_token. The wrapper must ACCEPT the
        stale kwarg (no TypeError) and still claim with a FRESH token, which is
        what gets re-stamped into the job hash."""
        job = MagicMock()
        job.update = AsyncMock()
        ctx: dict = {"job": job}

        async def _pass_through(aeng: Any, **kwargs: Any) -> dict[str, str]:
            await kwargs["execute_fn"]()
            return {"status": "complete"}

        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value="tok-fresh"
            ) as claim,
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock) as load,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch(
                "modulo.core.pipeline_execution.run_executor_with_watchdog",
                side_effect=_pass_through,
            ) as watchdog,
        ):
            run = MagicMock()
            run.input_payload = {"a": 1}
            executor = MagicMock()
            executor.execute = AsyncMock()
            load.return_value = (run, executor)
            result = await sw.execute_run(
                ctx,
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                claim_token="stale-token-from-previous-attempt",
            )

        assert result == {"status": "complete"}
        claim.assert_awaited_once()
        executor.execute.assert_awaited_once()
        assert executor.execute.await_args.kwargs["claim_token"] == "tok-fresh"
        complete.assert_awaited_once()
        assert complete.await_args.kwargs["claim_token"] == "tok-fresh"
        watchdog.assert_awaited_once()
        assert watchdog.await_args.kwargs["claim_token"] == "tok-fresh"
        # The FRESH token is stamped into the job hash, not the stale retry kwarg.
        job.update.assert_awaited_once()
        assert job.update.await_args.kwargs["kwargs"]["claim_token"] == "tok-fresh"

    @pytest.mark.asyncio
    async def test_resume_run_accepts_stale_claim_token_on_saq_retry(self) -> None:
        """Analogous to the execute_run retry: the resume wrapper must accept the
        stale claim_token kwarg SAQ passes back on a retry and forward to the
        core without TypeError."""
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
                _claim_token="stale-token-from-previous-attempt",
            )
        assert result == {"status": "complete"}
        core.assert_awaited_once()
        assert core.await_args.kwargs["resume_data"] == {"action": "approved"}
        # The stale kwarg is not forwarded to the core as a claim token.
        assert "claim_token" not in core.await_args.kwargs


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


class TestRetentionCleanup:
    @pytest.mark.asyncio
    async def test_purges_terminal_runs_and_langgraph_checkpoints(self) -> None:
        """retention_cleanup deletes old terminal runs AND old checkpoint rows,
        reporting both counts in its return dict."""
        factory, session = _make_retention_factory()

        with (
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.db.crud.run.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=7,
            ) as runs_delete,
            patch(
                "modulo.db.crud.org_deletion.batch_delete_langgraph_checkpoints",
                new_callable=AsyncMock,
                return_value=12,
            ) as ckpt_delete,
        ):
            result = await sw.retention_cleanup({})

        assert result == {"deleted": 7, "checkpoints_deleted": 12}
        runs_delete.assert_awaited_once()
        ckpt_delete.assert_awaited_once()
        assert runs_delete.await_args.args[0] is session
        assert ckpt_delete.await_args.args[0] is session

    @pytest.mark.asyncio
    async def test_zero_deletions_returns_zero_counts(self) -> None:
        """A clean pass must still report both zero counts (no log line)."""
        factory, session = _make_retention_factory()

        with (
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.db.crud.run.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=0,
            ) as runs_delete,
            patch(
                "modulo.db.crud.org_deletion.batch_delete_langgraph_checkpoints",
                new_callable=AsyncMock,
                return_value=0,
            ) as ckpt_delete,
        ):
            result = await sw.retention_cleanup({})

        assert result == {"deleted": 0, "checkpoints_deleted": 0}
        runs_delete.assert_awaited_once()
        ckpt_delete.assert_awaited_once()
        assert runs_delete.await_args.args[0] is session
        assert ckpt_delete.await_args.args[0] is session

    @pytest.mark.asyncio
    async def test_missing_checkpoint_schema_does_not_fail_job(self) -> None:
        """Major 1: a missing saver schema must not roll back the runs purge.

        The system worker's cron can fire before the app boot creates the
        checkpoint tables / ``created_at`` columns. The checkpoint purge then
        raises ``ProgrammingError`` (the SQLAlchemy wrapper for the DBAPI's
        missing-table/column errors); the job must catch it, log a warning,
        and still report the runs purge (``checkpoints_deleted`` = 0) — the
        runs purge already committed in its own transaction.
        """
        from sqlalchemy.exc import ProgrammingError

        factory, session = _make_retention_factory()

        with (
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.db.crud.run.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=7,
            ) as runs_delete,
            patch(
                "modulo.db.crud.org_deletion.batch_delete_langgraph_checkpoints",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("statement", {}, Exception("checkpoints does not exist")),
            ) as ckpt_delete,
        ):
            result = await sw.retention_cleanup({})

        assert result == {"deleted": 7, "checkpoints_deleted": 0}
        runs_delete.assert_awaited_once()
        ckpt_delete.assert_awaited_once()
        assert runs_delete.await_args.args[0] is session


class TestGetAsyncEngine:
    """``_get_async_engine`` delegates to the SHARED per-process engine factory
    (db.session.get_shared_engine), passing the per-worker pool budget for
    Postgres (D4)."""

    def test_uses_shared_engine_with_per_worker_pool_override(self) -> None:
        # A configured pool of 30 with concurrency 5 is already sufficient — no
        # raise, the effective pool equals the configured value.
        with (
            patch.dict(os.environ, _MIN_ENV),
            patch.object(
                sw,
                "get_settings",
                return_value=_settings(
                    database_url="postgresql+asyncpg://localhost/test",
                    saq_worker_db_pool_size=30,
                    saq_worker_concurrency=5,
                ),
            ),
            patch.object(sw, "_ASYNC_ENGINE", None),
            patch("modulo.db.session.get_shared_engine", return_value=MagicMock()) as shared,
        ):
            engine = sw._get_async_engine()

        assert engine is shared.return_value
        shared.assert_called_once_with(pool_size=30, max_overflow=0)

    def test_uses_effective_db_pool_raised_for_high_concurrency(self, caplog: pytest.LogCaptureFixture) -> None:
        # A configured pool of 5 with concurrency 20 (the incident config) must
        # produce an engine with pool_size 65 (concurrency * CONNS_PER_RUN + 5
        # reserve) and log a warning that the pool was raised.
        with (
            patch.object(
                sw,
                "get_settings",
                return_value=_settings(
                    database_url="postgresql+asyncpg://localhost/test",
                    saq_worker_db_pool_size=5,
                    saq_worker_concurrency=20,
                ),
            ),
            patch.object(sw, "_ASYNC_ENGINE", None),
            patch("modulo.db.session.get_shared_engine", return_value=MagicMock()) as shared,
            caplog.at_level(logging.WARNING, logger="modulo.core.saq_worker"),
        ):
            engine = sw._get_async_engine()

        assert engine is shared.return_value
        shared.assert_called_once_with(pool_size=65, max_overflow=0)
        assert "db_pool_raised" in caplog.text

    def test_uses_plain_shared_engine_for_non_postgres_backend(self) -> None:
        with (
            patch.dict(os.environ, _MIN_ENV),
            patch.object(
                sw,
                "get_settings",
                return_value=_settings(database_url="sqlite+aiosqlite:///./test.db", modulo_db="sqlite"),
            ),
            patch.object(sw, "_ASYNC_ENGINE", None),
            patch("modulo.db.session.get_shared_engine", return_value=MagicMock()) as shared,
        ):
            sw._get_async_engine()

        shared.assert_called_once_with()

    def test_caches_engine_across_calls(self) -> None:
        with (
            patch.dict(os.environ, _MIN_ENV),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_ASYNC_ENGINE", None),
            patch("modulo.db.session.get_shared_engine", return_value=MagicMock()) as shared,
        ):
            first = sw._get_async_engine()
            second = sw._get_async_engine()

        assert first is second
        shared.assert_called_once()


class TestRedisConnectionCheck:
    """``_check_redis_connection`` pings a SEPARATE SYNC client (never the async
    SAQ client — redis.asyncio pools are loop-affine) with exponential backoff
    and FAILS OPEN after ``max_retries`` — the worker boots even if Redis is
    down (recovery is possible before the first job)."""

    def test_ping_success_returns_immediately(self, caplog: pytest.LogCaptureFixture) -> None:
        sync_client = MagicMock()

        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("redis.Redis.from_url", return_value=sync_client) as from_url,
            patch("modulo.core.saq_worker.time.sleep", return_value=None) as mock_sleep,
            caplog.at_level(logging.INFO, logger="modulo.core.saq_worker"),
        ):
            sw._check_redis_connection(MagicMock())

        sync_client.ping.assert_called_once()
        assert from_url.call_args.kwargs["socket_connect_timeout"] == 5
        mock_sleep.assert_not_called()
        assert "Redis connection validated" in caplog.text

    def test_retries_with_exponential_backoff_then_succeeds(self, caplog: pytest.LogCaptureFixture) -> None:
        sync_client = MagicMock()
        sync_client.ping.side_effect = [
            redis.exceptions.ConnectionError("down"),
            redis.exceptions.TimeoutError("t"),
            True,
        ]

        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("redis.Redis.from_url", return_value=sync_client),
            patch("modulo.core.saq_worker.time.sleep", return_value=None) as mock_sleep,
            caplog.at_level(logging.WARNING, logger="modulo.core.saq_worker"),
        ):
            sw._check_redis_connection(MagicMock())

        assert sync_client.ping.call_count == 3
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0].args == (2,)
        assert mock_sleep.call_args_list[1].args == (4,)
        assert "retrying in 2s" in caplog.text

    def test_gives_up_after_max_retries_fail_open(self, caplog: pytest.LogCaptureFixture) -> None:
        sync_client = MagicMock()
        sync_client.ping.side_effect = OSError("connection refused")

        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("redis.Redis.from_url", return_value=sync_client),
            patch("modulo.core.saq_worker.time.sleep", return_value=None) as mock_sleep,
            caplog.at_level(logging.ERROR, logger="modulo.core.saq_worker"),
        ):
            sw._check_redis_connection(MagicMock())

        assert sync_client.ping.call_count == 3
        assert mock_sleep.call_count == 2
        assert "Redis unreachable after 3 attempts" in caplog.text


class TestProbeDatabase:
    """``_probe_database`` runs a non-fatal ``SELECT 1`` on worker startup."""

    def test_success_runs_select_one(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = MagicMock()
        conn = MagicMock()
        connect_cm = engine.connect.return_value
        connect_cm.__enter__.return_value = conn
        connect_cm.__exit__.return_value = False

        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("sqlalchemy.create_engine", return_value=engine) as create,
            caplog.at_level(logging.INFO, logger="modulo.core.saq_worker"),
        ):
            sw._probe_database()

        assert create.call_args.args[0] == "postgresql+psycopg://localhost/test"
        assert create.call_args.kwargs["pool_pre_ping"] is True
        conn.execute.assert_called_once()
        assert "Database connection probe passed" in caplog.text

    def test_failure_is_non_fatal_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("sqlalchemy.create_engine", side_effect=RuntimeError("conn refused")),
            caplog.at_level(logging.WARNING, logger="modulo.core.saq_worker"),
        ):
            sw._probe_database()

        assert "Database probe failed (non-fatal)" in caplog.text


class TestBaseWorkerSettings:
    def test_timers_dict_is_copied_not_shared(self) -> None:
        """Mutating a returned settings' timers must not leak into the module globals."""
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_build_queue", return_value=MagicMock()),
        ):
            settings = sw._base_worker_settings("runs", [])

        settings["timers"]["schedule"] = 999
        assert sw._TIMERS["schedule"] == 5

    @pytest.mark.asyncio
    async def test_after_process_hook_delegates(self) -> None:
        ctx = {"job": MagicMock()}

        with patch("modulo.core.error_tracking.saq_hooks.after_process", new_callable=AsyncMock) as hook:
            await sw._after_process_hook(ctx)

        hook.assert_awaited_once_with(ctx)


class TestFireWrappersExtended:
    @pytest.mark.asyncio
    async def test_fire_cron_trigger_dispatch_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A dispatch failure must not lose the fired run — the fire result is
        returned as-is and the error is logged."""
        with (
            patch(
                "modulo.core.cron_helpers.fire_cron_trigger",
                new_callable=AsyncMock,
                return_value={"status": "fired", "run_id": "run-9"},
            ),
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                side_effect=RuntimeError("queue down"),
            ),
            patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="runs")),
            caplog.at_level(logging.ERROR, logger="modulo.core.saq_worker"),
        ):
            result = await sw.fire_cron_trigger(
                {},
                trigger_id=_UUID_1,
                org_id=_UUID_ORG,
                pipeline_id=_UUID_2,
                cron_expression="* * * * *",
            )

        assert result["status"] == "fired"
        assert "dispatch" not in result
        assert "fire_cron_trigger: dispatch failed" in caplog.text

    @pytest.mark.asyncio
    async def test_fire_polling_trigger_dispatches_created_run(self) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.fire_polling_trigger",
                new_callable=AsyncMock,
                return_value={"status": "fired", "run_id": "run-5"},
            ) as ch,
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                return_value=("enqueued", "job-2"),
            ) as dispatch,
            patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="runs")),
        ):
            result = await sw.fire_polling_trigger(
                {},
                trigger_id=_UUID_1,
                org_id=_UUID_ORG,
                pipeline_id=_UUID_2,
                connector_instance_id=_UUID_3,
                poll_query="is:open",
            )

        assert result["status"] == "fired"
        assert result["dispatch"] == "enqueued"
        assert result["job_id"] == "job-2"
        dispatch.assert_awaited_once_with("run-5", _UUID_ORG, queue="runs")
        ch.assert_awaited_once()
        assert ch.await_args.kwargs["poll_query"] == "is:open"

    @pytest.mark.asyncio
    async def test_fire_polling_trigger_not_fired_no_dispatch(self) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.fire_polling_trigger",
                new_callable=AsyncMock,
                return_value={"status": "skipped"},
            ),
            patch("modulo.core.dispatch.dispatch_run", new_callable=AsyncMock) as dispatch,
        ):
            result = await sw.fire_polling_trigger(
                {},
                trigger_id=_UUID_1,
                org_id=_UUID_ORG,
                pipeline_id=_UUID_2,
                connector_instance_id=_UUID_3,
                poll_query="is:open",
            )

        assert result["status"] == "skipped"
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fire_polling_trigger_dispatch_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.fire_polling_trigger",
                new_callable=AsyncMock,
                return_value={"status": "fired", "run_id": "run-5"},
            ),
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                side_effect=RuntimeError("queue down"),
            ),
            patch.object(sw, "get_settings", return_value=_settings(saq_runs_queue="runs")),
            caplog.at_level(logging.ERROR, logger="modulo.core.saq_worker"),
        ):
            result = await sw.fire_polling_trigger(
                {},
                trigger_id=_UUID_1,
                org_id=_UUID_ORG,
                pipeline_id=_UUID_2,
                connector_instance_id=_UUID_3,
                poll_query="is:open",
            )

        assert result["status"] == "fired"
        assert "dispatch" not in result
        assert "fire_polling_trigger: dispatch failed" in caplog.text

    @pytest.mark.asyncio
    async def test_fire_report_trigger_delegates(self) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.fire_report_trigger",
                new_callable=AsyncMock,
                return_value={"status": "delivered", "report_id": "report-1"},
            ) as ch,
        ):
            result = await sw.fire_report_trigger({}, report_id=_UUID_1, org_id=_UUID_ORG)

        assert result == {"status": "delivered", "report_id": "report-1"}
        ch.assert_awaited_once()
        assert ch.await_args.kwargs["report_id"] == UUID(_UUID_1)
        assert ch.await_args.kwargs["org_id"] == UUID(_UUID_ORG)


class TestSystemJobDelegates:
    """Every remaining system-worker job is a thin delegate — these pin the
    delegation target so a refactor cannot silently drop the wiring."""

    @pytest.mark.asyncio
    async def test_fire_due_triggers_delegates(self) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.fire_due_triggers",
                new_callable=AsyncMock,
                return_value={"fired": 2, "enqueued": 2},
            ) as ch,
        ):
            result = await sw.fire_due_triggers({})

        assert result == {"fired": 2, "enqueued": 2}
        ch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatcher_reconcile_delegates(self) -> None:
        with (
            patch(
                "modulo.core.cron_helpers.dispatcher_reconcile",
                new_callable=AsyncMock,
                return_value={"redispatched": 1},
            ) as ch,
        ):
            result = await sw.dispatcher_reconcile({})

        assert result == {"redispatched": 1}
        ch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_run_recovery_delegates(self) -> None:
        redis_client = AsyncMock()
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.pipeline_execution.stale_run_recovery_sweep",
                new_callable=AsyncMock,
                return_value=3,
            ) as sweep,
            patch("redis.asyncio.Redis.from_url", return_value=redis_client) as from_url,
        ):
            result = await sw.stale_run_recovery({})

        assert result == 3
        sweep.assert_awaited_once()
        # The wrapper persists a stats dict to the shared Redis key (D1) so
        # /healthz/ready can detect a silently dead sweep.
        from_url.assert_called_once()
        redis_client.set.assert_awaited_once()
        assert redis_client.set.await_args.args[0] == sw.STALE_RUN_RECOVERY_STATS_KEY
        import json as _json

        stats = _json.loads(redis_client.set.await_args.args[1])
        assert stats["recovered"] == 3
        assert stats["last_run_at"]

    @pytest.mark.asyncio
    async def test_stale_run_recovery_persist_failure_does_not_break_sweep(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stats-persistence failure must never fail the sweep itself."""
        redis_client = AsyncMock()
        redis_client.set.side_effect = RuntimeError("redis down")
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.pipeline_execution.stale_run_recovery_sweep",
                new_callable=AsyncMock,
                return_value=5,
            ) as sweep,
            patch("redis.asyncio.Redis.from_url", return_value=redis_client),
            caplog.at_level(logging.WARNING, logger="modulo.core.saq_worker"),
        ):
            result = await sw.stale_run_recovery({})

        assert result == 5
        sweep.assert_awaited_once()
        assert "stale_run_recovery_stats_persist_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_cost_probe_delegates(self) -> None:
        factory = MagicMock()
        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cost_controller.probe.run_probe",
                new_callable=AsyncMock,
                return_value={"status": "ok", "orgs": 1},
            ) as probe,
        ):
            result = await sw.cost_probe({})

        assert result == {"status": "ok", "orgs": 1}
        probe.assert_awaited_once_with(factory)

    @pytest.mark.asyncio
    async def test_analytics_facts_maintenance_delegates(self) -> None:
        factory = MagicMock()
        with (
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.core.analytics.maintenance.run_maintenance",
                new_callable=AsyncMock,
                return_value={"maintained": 4},
            ) as maint,
        ):
            result = await sw.analytics_facts_maintenance({})

        assert result == {"maintained": 4}
        maint.assert_awaited_once_with(factory)

    @pytest.mark.asyncio
    async def test_journey_reconcile_delegates(self) -> None:
        """journey_reconcile opens a system session and runs the sweep."""
        factory, session = _make_retention_factory()
        with (
            patch.object(sw, "_make_system_session_factory", return_value=factory),
            patch(
                "modulo.core.lifecycle_map.reconcile.reconcile_journeys",
                new_callable=AsyncMock,
                return_value=7,
            ) as reconcile,
        ):
            result = await sw.journey_reconcile({})

        assert result == {"advanced": 7}
        reconcile.assert_awaited_once()
        assert reconcile.await_args.args[0] is session

    @pytest.mark.asyncio
    async def test_check_missed_fire_alerts_cron_delegates_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.error_tracking.check_missed_fire_alerts",
                new_callable=AsyncMock,
                return_value=2,
            ) as check,
            caplog.at_level(logging.INFO, logger="modulo.core.saq_worker"),
        ):
            result = await sw.check_missed_fire_alerts_cron({})

        assert result == {"emitted": 2}
        check.assert_awaited_once()
        assert "check_missed_fire_alerts.emitted" in caplog.text

    @pytest.mark.asyncio
    async def test_check_missed_fire_alerts_cron_no_emission_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.error_tracking.check_missed_fire_alerts",
                new_callable=AsyncMock,
                return_value=0,
            ) as check,
            caplog.at_level(logging.INFO, logger="modulo.core.saq_worker"),
        ):
            result = await sw.check_missed_fire_alerts_cron({})

        assert result == {"emitted": 0}
        check.assert_awaited_once()
        assert "check_missed_fire_alerts.emitted" not in caplog.text

    @pytest.mark.asyncio
    async def test_library_sync_noop_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        """Empty MODULO_LIBRARY_ENDPOINT must short-circuit to a disabled no-op."""
        with (
            patch.object(sw, "get_settings", return_value=_settings(modulo_library_endpoint="")),
            patch.object(sw, "_make_session_factory") as factory,
            caplog.at_level(logging.INFO, logger="modulo.core.saq_worker"),
        ):
            result = await sw.library_sync({})

        assert result == {"status": "disabled"}
        factory.assert_not_called()
        assert "saq.library_sync.disabled" in caplog.text

    @pytest.mark.asyncio
    async def test_library_sync_reports_result(self) -> None:
        session = MagicMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock()
        factory.return_value = session_cm
        result_obj = MagicMock(success=True, entries_count=3, revoked_count=1, error=None)
        with (
            patch.object(
                sw, "get_settings", return_value=_settings(modulo_library_endpoint="https://library.modulo.run")
            ),
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch("modulo.core.library_sync.sync_library", new_callable=AsyncMock, return_value=result_obj) as sync,
        ):
            result = await sw.library_sync({})

        assert result == {"status": "ok", "entries_count": 3, "revoked_count": 1, "error": None}
        sync.assert_awaited_once()
        assert sync.await_args.args[0] is session

    @pytest.mark.asyncio
    async def test_library_sync_fail_open_on_cron_failure(self) -> None:
        """A session/DB failure in the cron wrapper must never raise (fail-open)."""
        with (
            patch.object(
                sw, "get_settings", return_value=_settings(modulo_library_endpoint="https://library.modulo.run")
            ),
            patch.object(sw, "_make_session_factory", side_effect=RuntimeError("redis down")),
        ):
            result = await sw.library_sync({})

        assert result == {"status": "failed", "error": "unexpected cron failure"}


class TestClaimExpiry:
    @staticmethod
    def _make_factory() -> MagicMock:
        session = AsyncMock()
        factory = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        factory.return_value = context
        return factory

    @pytest.mark.asyncio
    async def test_expires_claims_with_notifier(self) -> None:
        factory = self._make_factory()
        engine = MagicMock()
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch.object(sw, "_get_async_engine", return_value=engine),
            patch("modulo.core.notifier.Notifier") as notifier_cls,
            patch(
                "modulo.core.hitl_manager.expiry_job.expire_stale_claims",
                new_callable=AsyncMock,
                return_value=["claim-1", "claim-2"],
            ) as expire,
        ):
            result = await sw.claim_expiry({})

        assert result == {"expired": 2}
        expire.assert_awaited_once()
        assert expire.await_args.kwargs["notifier"] is notifier_cls.return_value
        notifier_cls.assert_called_once_with(engine, _settings().fernet_key)

    @pytest.mark.asyncio
    async def test_notifier_init_failure_does_not_block_expiry(self, caplog: pytest.LogCaptureFixture) -> None:
        """A Notifier init failure must be swallowed — DB expiry still runs with
        ``notifier=None`` (claim_expiry is the SOLE writer, expiry must not
        depend on the alerting side)."""
        factory = self._make_factory()
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.notifier.Notifier", side_effect=RuntimeError("fernet boom")),
            patch(
                "modulo.core.hitl_manager.expiry_job.expire_stale_claims",
                new_callable=AsyncMock,
                return_value=["claim-1"],
            ) as expire,
            caplog.at_level(logging.ERROR, logger="modulo.core.saq_worker"),
        ):
            result = await sw.claim_expiry({})

        assert result == {"expired": 1}
        expire.assert_awaited_once()
        assert expire.await_args.kwargs["notifier"] is None
        assert "claim_expiry: notifier init failed" in caplog.text


class TestHitlOverdue:
    @staticmethod
    def _make_factory() -> MagicMock:
        session = AsyncMock()
        factory = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        factory.return_value = context
        return factory

    @pytest.mark.asyncio
    async def test_dispatches_overdue_notifications_with_notifier(self) -> None:
        factory = self._make_factory()
        engine = MagicMock()
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch.object(sw, "_get_async_engine", return_value=engine),
            patch("modulo.core.notifier.Notifier") as notifier_cls,
            patch(
                "modulo.core.hitl_manager.overdue_warning.dispatch_overdue_notifications",
                new_callable=AsyncMock,
                return_value=["claim-1", "claim-2"],
            ) as dispatch,
        ):
            result = await sw.hitl_overdue({})

        assert result == {"dispatched": 2}
        dispatch.assert_awaited_once()
        assert dispatch.await_args.kwargs["notifier"] is notifier_cls.return_value
        notifier_cls.assert_called_once_with(engine, _settings().fernet_key)

    @pytest.mark.asyncio
    async def test_notifier_init_failure_does_not_block_dispatch(self, caplog: pytest.LogCaptureFixture) -> None:
        """A Notifier init failure must be swallowed — overdue dispatch still
        runs with ``notifier=None`` (the sweep must not depend on alerting)."""
        factory = self._make_factory()
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.notifier.Notifier", side_effect=RuntimeError("fernet boom")),
            patch(
                "modulo.core.hitl_manager.overdue_warning.dispatch_overdue_notifications",
                new_callable=AsyncMock,
                return_value=["claim-1"],
            ) as dispatch,
            caplog.at_level(logging.ERROR, logger="modulo.core.saq_worker"),
        ):
            result = await sw.hitl_overdue({})

        assert result == {"dispatched": 1}
        dispatch.assert_awaited_once()
        assert dispatch.await_args.kwargs["notifier"] is None
        assert "hitl_overdue: notifier init failed" in caplog.text


class TestCancellationPropagation:
    """Cancellation must always propagate (never swallowed) so SAQ can abort a
    stuck job cleanly — assert each wrapper re-raises ``CancelledError``."""

    @pytest.mark.asyncio
    async def test_execute_run_cancellation_propagates(self) -> None:
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=True),
            patch(
                "modulo.core.pipeline_execution.load_and_setup",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            patch("modulo.core.pipeline_execution.fail_run_terminal", new_callable=AsyncMock) as fail,
            pytest.raises(asyncio.CancelledError),
        ):
            await sw.execute_run({}, run_id=_UUID_1, org_id=_UUID_ORG)

        fail.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fire_cron_trigger_cancellation_propagates(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.cron_helpers.fire_cron_trigger",
                new_callable=AsyncMock,
                return_value={"status": "fired", "run_id": "run-9"},
            ),
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await sw.fire_cron_trigger(
                {},
                trigger_id=_UUID_1,
                org_id=_UUID_ORG,
                pipeline_id=_UUID_2,
                cron_expression="* * * * *",
            )

    @pytest.mark.asyncio
    async def test_fire_polling_trigger_cancellation_propagates(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.cron_helpers.fire_polling_trigger",
                new_callable=AsyncMock,
                return_value={"status": "fired", "run_id": "run-5"},
            ),
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await sw.fire_polling_trigger(
                {},
                trigger_id=_UUID_1,
                org_id=_UUID_ORG,
                pipeline_id=_UUID_2,
                connector_instance_id=_UUID_3,
                poll_query="is:open",
            )


class TestExecuteRunMissingRun:
    @pytest.mark.asyncio
    async def test_execute_run_missing_run_returns_early(self) -> None:
        """A claimed run whose ``load_and_setup`` yields no run row must return
        ``missing`` without touching the watchdog or completion path."""
        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=True),
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock, return_value=(None, None)),
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch("modulo.core.pipeline_execution.run_executor_with_watchdog", new_callable=AsyncMock) as watchdog,
        ):
            result = await sw.execute_run({}, run_id=_UUID_1, org_id=_UUID_ORG)

        assert result == {"status": "missing"}
        complete.assert_not_awaited()
        watchdog.assert_not_awaited()


class TestMakeSessionFactory:
    def test_returns_configured_sessionmaker(self) -> None:
        engine = MagicMock()
        with (
            patch.object(sw, "_get_async_engine", return_value=engine),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker") as sessionmaker,
        ):
            result = sw._make_session_factory()

        assert result is sessionmaker.return_value
        sessionmaker.assert_called_once_with(engine, expire_on_commit=False, autobegin=False)


class TestMakeSystemSessionFactory:
    def test_uses_system_engine_when_url_set(self) -> None:
        system_engine = MagicMock()
        with (
            patch.object(sw, "_get_system_async_engine", return_value=system_engine),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker") as sessionmaker,
        ):
            result = sw._make_system_session_factory()

        assert result is sessionmaker.return_value
        sessionmaker.assert_called_once_with(system_engine, expire_on_commit=False, autobegin=False)

    def test_system_factory_uses_system_engine_not_regular(self) -> None:
        regular_engine = MagicMock()
        system_engine = MagicMock()
        with (
            patch.object(sw, "_get_async_engine", return_value=regular_engine),
            patch.object(sw, "_get_system_async_engine", return_value=system_engine),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker") as sessionmaker,
        ):
            sw._make_system_session_factory()

        sessionmaker.assert_called_once_with(system_engine, expire_on_commit=False, autobegin=False)


class TestGetSystemAsyncEngine:
    def test_creates_engine_from_system_url(self) -> None:
        mock_settings = _settings(modulo_system_database_url="postgresql+asyncpg://sys:pass@db:5432/modulo")
        sw._SYSTEM_ASYNC_ENGINE = None  # reset singleton
        try:
            with (
                patch.object(sw, "get_settings", return_value=mock_settings),
                patch("sqlalchemy.ext.asyncio.create_async_engine") as create_engine,
            ):
                result = sw._get_system_async_engine()

            assert result is create_engine.return_value
            create_engine.assert_called_once()
            _, kwargs = create_engine.call_args
            assert kwargs["connect_args"] == {"ssl": False, "statement_cache_size": 0}
        finally:
            sw._SYSTEM_ASYNC_ENGINE = None

    def test_falls_back_to_regular_engine_when_url_empty(self) -> None:
        regular_engine = MagicMock()
        mock_settings = _settings(modulo_system_database_url="")
        sw._SYSTEM_ASYNC_ENGINE = None  # reset singleton
        try:
            with (
                patch.object(sw, "get_settings", return_value=mock_settings),
                patch.object(sw, "_get_async_engine", return_value=regular_engine),
            ):
                result = sw._get_system_async_engine()

            assert result is regular_engine
        finally:
            sw._SYSTEM_ASYNC_ENGINE = None

    def test_caches_engine_singleton(self) -> None:
        regular_engine = MagicMock()
        mock_settings = _settings(modulo_system_database_url="")
        sw._SYSTEM_ASYNC_ENGINE = None  # reset singleton
        try:
            with (
                patch.object(sw, "get_settings", return_value=mock_settings),
                patch.object(sw, "_get_async_engine", return_value=regular_engine),
            ):
                first = sw._get_system_async_engine()
                second = sw._get_system_async_engine()

            assert first is second
        finally:
            sw._SYSTEM_ASYNC_ENGINE = None


class TestFireSuiteRunTriggerEnqueueFailure:
    """``fire_suite_run_trigger`` must not strand a ``pending`` SuiteRun when the
    ``execute_suite_run`` job cannot be enqueued (Redis/SAQ down).

    The run is already committed ``pending`` by ``cron_helpers.fire_suite_run_trigger``
    before enqueue; if enqueue fails and we swallow it, the run sits ``pending``
    forever (nothing reconciles stuck ``pending`` suite_runs). The fix terminalises
    it to ``failed`` via ``_fail_run`` (FAR-377 reviewer finding).
    """

    @pytest.mark.asyncio
    async def test_enqueue_failure_terminalises_pending_suite_run(self) -> None:
        suite_run_id = _UUID_1
        org_id = _UUID_ORG
        trigger_id = _UUID_2
        pipeline_id = _UUID_3

        fired = {"status": "fired", "suite_run_id": suite_run_id, "trigger_id": trigger_id}

        enqueue_calls: list[tuple[str, str]] = []

        async def fake_enqueue(sid: str, oid: str) -> None:
            enqueue_calls.append((sid, oid))
            raise RuntimeError("redis down")

        # Session double for the terminalise path (the run is found + failed).
        run = MagicMock()
        run.organisation_id = UUID(org_id)
        term_session = MagicMock()
        term_session.get = AsyncMock(return_value=run)
        # ``session.begin()`` must return an async context manager (not a bare
        # coroutine) for ``async with session.begin():`` to work under mock.
        # ``MagicMock(return_value=...)`` (not ``AsyncMock``) so calling it
        # returns the inner async-CM directly instead of wrapping it in a
        # coroutine.
        term_session.begin = MagicMock(return_value=AsyncMock())
        term_cm = AsyncMock()
        term_cm.__aenter__.return_value = term_session
        term_cm.__aexit__.return_value = False
        factory = MagicMock(return_value=term_cm)

        fail_calls: list[tuple[object, str]] = []

        async def fake_fail_run(session: object, r: object, detail: str) -> None:
            fail_calls.append((r, detail))

        with (
            patch(
                "modulo.core.cron_helpers.fire_suite_run_trigger",
                new_callable=AsyncMock,
                return_value=fired,
            ),
            patch.object(sw, "_enqueue_suite_run_execution", side_effect=fake_enqueue),
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch("modulo.core.eval_engine.execute_suite_run._fail_run", side_effect=fake_fail_run),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
        ):
            result = await sw.fire_suite_run_trigger(
                {},
                trigger_id=trigger_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
            )

        assert result["status"] == "fired"
        assert result["dispatched"] == "enqueue_failed"
        assert enqueue_calls == [(suite_run_id, org_id)]
        # The committed ``pending`` run must be terminalised (never stranded).
        assert fail_calls, "pending SuiteRun must be terminalised on enqueue failure"
        assert fail_calls[0][0] is run
        assert "enqueue" in fail_calls[0][1].lower()

    @pytest.mark.asyncio
    async def test_enqueue_success_marks_dispatched(self) -> None:
        """Happy path: a successful enqueue is recorded as ``enqueued``."""
        suite_run_id = _UUID_1
        org_id = _UUID_ORG
        trigger_id = _UUID_2
        pipeline_id = _UUID_3

        fired = {"status": "fired", "suite_run_id": suite_run_id, "trigger_id": trigger_id}
        enqueue_calls = []

        async def fake_enqueue(sid: str, oid: str) -> None:
            enqueue_calls.append((sid, oid))

        with (
            patch(
                "modulo.core.cron_helpers.fire_suite_run_trigger",
                new_callable=AsyncMock,
                return_value=fired,
            ),
            patch.object(sw, "_enqueue_suite_run_execution", side_effect=fake_enqueue),
        ):
            result = await sw.fire_suite_run_trigger(
                {},
                trigger_id=trigger_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
            )

        assert result["dispatched"] == "enqueued"
        assert enqueue_calls == [(suite_run_id, org_id)]
