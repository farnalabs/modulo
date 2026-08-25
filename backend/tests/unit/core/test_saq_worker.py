"""Unit tests for the modulo_system engine selection and worker-sizing math
in modulo.core.saq_worker.

Proves that _get_system_async_engine creates an engine from
MODULO_SYSTEM_DATABASE_URL when set, and logs a WARNING + falls back to the app
engine (modulo_app, NOBYPASSRLS) when it is not set — the silent RLS-zero-rows
data-loss posture. Also locks the pool-sizing helpers (_max_concurrent_ops,
_effective_redis_pool_size, _effective_db_pool_size), the cron-interval mapping,
queue-name derivation, fail-closed web auth, and the fail-open startup cron
reconciliation — the settings that govern the worker's Redis/DB connection
reserve and heartbeat health.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import MagicMock

import pytest

import modulo.core.saq_worker as sw


def _settings(system_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        modulo_system_database_url=system_url,
        modulo_db="postgres",
        saq_worker_db_pool_size=7,
        saq_worker_concurrency=2,
    )


@pytest.fixture
def reset_system_engine(monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level _SYSTEM_ASYNC_ENGINE singleton before and after."""
    monkeypatch.setattr(sw, "_SYSTEM_ASYNC_ENGINE", None)
    yield
    sw._SYSTEM_ASYNC_ENGINE = None


def test_creates_engine_with_system_url_when_configured(reset_system_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    system_url = "postgresql+asyncpg://modulo_system:s3cret@db.internal:5432/modulo"
    monkeypatch.setattr(sw, "get_settings", lambda: _settings(system_url))
    create_engine = MagicMock(return_value=MagicMock())
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", create_engine)

    engine = sw._get_system_async_engine()

    assert engine is create_engine.return_value
    create_engine.assert_called_once()
    assert create_engine.call_args.args[0] == system_url


def test_logs_warning_and_uses_app_engine_when_unset(reset_system_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    app_engine = MagicMock()
    monkeypatch.setattr(sw, "get_settings", lambda: _settings(system_url=""))
    monkeypatch.setattr(sw, "_get_async_engine", lambda: app_engine)
    warnings: list[tuple[object, object]] = []
    monkeypatch.setattr(sw._log, "warning", lambda msg, extra=None: warnings.append((msg, extra)))

    engine = sw._get_system_async_engine()

    assert engine is app_engine
    assert len(warnings) == 1
    msg, extra = warnings[0]
    assert msg == "saq_worker.system_engine_fallback"
    assert "MODULO_SYSTEM_DATABASE_URL not set" in extra["reason"]


# ---------------------------------------------------------------------------
# Cron-interval mapping (bug class #680: croniter parses 5-field cron, so the
# mapping must never emit a 6-field expression or a sub-minute interval).
# ---------------------------------------------------------------------------


class TestSyncIntervalToCron:
    def test_sub_minute_intervals_collapse_to_every_minute(self) -> None:
        for seconds in (0, 1, 30, 60):
            assert sw._sync_interval_to_cron(seconds) == "* * * * *"

    def test_minute_multiples_map_to_star_minutes(self) -> None:
        assert sw._sync_interval_to_cron(61) == "*/1 * * * *"
        assert sw._sync_interval_to_cron(90) == "*/1 * * * *"
        assert sw._sync_interval_to_cron(120) == "*/2 * * * *"
        assert sw._sync_interval_to_cron(600) == "*/10 * * * *"
        assert sw._sync_interval_to_cron(1800) == "*/30 * * * *"
        assert sw._sync_interval_to_cron(3599) == "*/59 * * * *"

    def test_hourly_multiples_map_to_hourly_expression(self) -> None:
        assert sw._sync_interval_to_cron(3600) == "0 */1 * * *"
        assert sw._sync_interval_to_cron(7200) == "0 */2 * * *"
        assert sw._sync_interval_to_cron(23 * 3600) == "0 */23 * * *"

    def test_daily_and_beyond_collapse_to_midnight(self) -> None:
        assert sw._sync_interval_to_cron(86400) == "0 0 * * *"
        assert sw._sync_interval_to_cron(48 * 3600) == "0 0 * * *"

    def test_never_emits_six_field_or_sub_minute_expression(self) -> None:
        for seconds in range(0, 20 * 3600, 61):
            cron = sw._sync_interval_to_cron(seconds)
            assert len(cron.split()) == 5, f"non-5-field cron for {seconds}s: {cron!r}"


# ---------------------------------------------------------------------------
# Pool-sizing math — the reserve guarantees that keep the worker from wedging.
# ---------------------------------------------------------------------------


class TestMaxConcurrentOps:
    def test_pool_of_one_gets_the_whole_single_connection(self) -> None:
        assert sw._max_concurrent_ops(1) == 1

    def test_small_pools_reserve_one_connection(self) -> None:
        assert sw._max_concurrent_ops(2) == 1
        assert sw._max_concurrent_ops(3) == 2
        assert sw._max_concurrent_ops(4) == 3
        assert sw._max_concurrent_ops(5) == 4

    def test_larger_pools_reserve_five_connections(self) -> None:
        assert sw._max_concurrent_ops(6) == 1
        assert sw._max_concurrent_ops(10) == 5
        assert sw._max_concurrent_ops(20) == 15
        assert sw._max_concurrent_ops(65) == 60

    def test_always_stays_strictly_below_pool_size(self) -> None:
        for pool in range(1, 65):
            assert 1 <= sw._max_concurrent_ops(pool) <= pool


class TestEffectiveRedisPoolSize:
    def test_enforces_concurrency_plus_reserve_floor(self) -> None:
        assert sw._effective_redis_pool_size(1, 1) == 6
        assert sw._effective_redis_pool_size(5, 20) == 25
        assert sw._effective_redis_pool_size(20, 20) == 25

    def test_keeps_larger_configured_pool(self) -> None:
        assert sw._effective_redis_pool_size(10, 5) == 10
        assert sw._effective_redis_pool_size(30, 20) == 30

    def test_never_below_concurrency(self) -> None:
        for pool in range(1, 10):
            for conc in range(1, 10):
                assert sw._effective_redis_pool_size(pool, conc) >= conc


class TestEffectiveDbPoolSize:
    def test_enforces_per_run_fanout_plus_reserve_floor(self) -> None:
        assert sw._effective_db_pool_size(1, 1) == 8
        assert sw._effective_db_pool_size(1, 10) == 35
        assert sw._effective_db_pool_size(30, 20) == 65
        assert sw._effective_db_pool_size(5, 20) == 65

    def test_keeps_larger_configured_pool(self) -> None:
        assert sw._effective_db_pool_size(10, 5) == 20
        assert sw._effective_db_pool_size(65, 20) == 65
        assert sw._effective_db_pool_size(80, 20) == 80

    def test_floor_uses_three_connections_per_run(self) -> None:
        for conc in range(1, 20):
            assert sw._effective_db_pool_size(1, conc) >= conc * 3 + 5


# ---------------------------------------------------------------------------
# Queue-name derivation — the worker MUST listen on the same queues the health
# gate and dispatch path check, or jobs are enqueued but never dequeued.
# ---------------------------------------------------------------------------


class TestQueueNameDerivation:
    def test_runs_queue_derives_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sw, "get_settings", lambda: SimpleNamespace(saq_runs_queue="staging-runs"))
        assert sw._runs_queue_name() == "staging-runs"

    @pytest.mark.parametrize(
        ("runs_queue", "expected"),
        [
            ("runs", "system"),
            ("staging-runs", "staging-system"),
            ("prod-runs", "prod-system"),
            ("custom", "system"),
        ],
    )
    def test_system_queue_derives_from_runs_queue(
        self, monkeypatch: pytest.MonkeyPatch, runs_queue: str, expected: str
    ) -> None:
        monkeypatch.setattr(sw, "get_settings", lambda: SimpleNamespace(saq_runs_queue=runs_queue))
        assert sw._system_queue_name() == expected


# ---------------------------------------------------------------------------
# Fail-closed system-web auth — refusing to boot is cheaper than exposing the
# worker web UI unauthenticated.
# ---------------------------------------------------------------------------


class TestAssertSystemAuthConfigured:
    def test_missing_password_refuses_to_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sw, "get_settings", lambda: SimpleNamespace(saq_auth_password="", saq_auth_username="admin")
        )
        with pytest.raises(RuntimeError, match="SAQ_AUTH_PASSWORD"):
            sw._assert_system_auth_configured()

    def test_missing_username_refuses_to_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sw, "get_settings", lambda: SimpleNamespace(saq_auth_password="s3cret", saq_auth_username=None)
        )
        with pytest.raises(RuntimeError, match="SAQ_AUTH_USERNAME"):
            sw._assert_system_auth_configured()

    def test_configured_auth_allows_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sw, "get_settings", lambda: SimpleNamespace(saq_auth_password="s3cret", saq_auth_username="admin")
        )
        assert sw._assert_system_auth_configured() is None


# ---------------------------------------------------------------------------
# Startup cron registration reconciliation (unit-level, fail-open posture).
# The integration twin (tests/integration/saq/test_cron_reconcile.py) proves
# the real Redis round-trip; these unit tests lock the skip/fail-open branches
# without needing a container.
# ---------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.zremmed: list[tuple[str, str]] = []
        self.lremmed: list[tuple[str, int, str]] = []
        self.executed = False
        self._raise_on_execute: Exception | None = None

    def fail_on_execute(self, exc: Exception) -> None:
        self._raise_on_execute = exc

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def delete(self, key: str) -> _FakePipeline:
        self.deleted.append(key)
        return self

    def zrem(self, name: str, key: str) -> _FakePipeline:
        self.zremmed.append((name, key))
        return self

    def lrem(self, name: str, count: int, key: str) -> _FakePipeline:
        self.lremmed.append((name, count, key))
        return self

    async def execute(self) -> list[int]:
        self.executed = True
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        return [1, 1, 1]


class _FakeRedis:
    def __init__(self, pipeline: _FakePipeline) -> None:
        self._pipeline = pipeline

    def pipeline(self) -> _FakePipeline:
        return self._pipeline


class _FakeQueue:
    def __init__(self, pipeline: _FakePipeline, job_id_prefix: str = "jid:") -> None:
        self.redis = _FakeRedis(pipeline)
        self._prefix = job_id_prefix

    def job_id(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def namespace(self, name: str) -> str:
        return f"ns:{name}"


def _fake_cron(*, unique: bool = True, function: Any = None) -> SimpleNamespace:
    return SimpleNamespace(unique=unique, function=function)


def _dummy_cron(*_args: object, **_kwargs: object) -> None:
    return None


class TestReconcileCronRegistrationsUnit:
    @pytest.mark.asyncio
    async def test_unique_cron_registration_cleared(self) -> None:
        pipeline = _FakePipeline()
        queue = _FakeQueue(pipeline)
        cron = _fake_cron(function=_dummy_cron, unique=True)

        await sw.reconcile_cron_registrations(queue, [cron])

        assert pipeline.deleted == ["jid:cron:_dummy_cron"]
        assert pipeline.zremmed == [("ns:incomplete", "jid:cron:_dummy_cron")]
        assert pipeline.lremmed == [("ns:queued", 0, "jid:cron:_dummy_cron")]
        assert pipeline.executed is True

    @pytest.mark.asyncio
    async def test_non_unique_cron_is_skipped(self) -> None:
        pipeline = _FakePipeline()
        queue = _FakeQueue(pipeline)
        cron = _fake_cron(function=_dummy_cron, unique=False)

        await sw.reconcile_cron_registrations(queue, [cron])

        assert not pipeline.deleted
        assert not pipeline.zremmed
        assert not pipeline.lremmed
        assert pipeline.executed is False

    @pytest.mark.asyncio
    async def test_cron_without_function_is_skipped(self) -> None:
        pipeline = _FakePipeline()
        queue = _FakeQueue(pipeline)
        cron = _fake_cron(function=None, unique=True)

        await sw.reconcile_cron_registrations(queue, [cron])

        assert not pipeline.deleted
        assert pipeline.executed is False

    @pytest.mark.asyncio
    async def test_redis_error_is_fail_open_and_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipeline = _FakePipeline()
        pipeline.fail_on_execute(RuntimeError("redis down"))
        queue = _FakeQueue(pipeline)
        cron = _fake_cron(function=_dummy_cron, unique=True)
        errors: list[tuple[str, dict[str, str]]] = []
        monkeypatch.setattr(sw._log, "exception", lambda msg, extra=None: errors.append((msg, extra or {})))

        await sw.reconcile_cron_registrations(queue, [cron])

        assert pipeline.executed is True
        assert len(errors) == 1
        assert errors[0][0] == "saq.reconcile_cron_registrations.failed"
        assert errors[0][1] == {"key": "cron:_dummy_cron"}

    @pytest.mark.asyncio
    async def test_mixed_cron_list_clears_only_unique(self) -> None:
        pipeline = _FakePipeline()
        queue = _FakeQueue(pipeline)

        await sw.reconcile_cron_registrations(
            queue,
            [
                _fake_cron(function=_dummy_cron, unique=True),
                _fake_cron(function=_dummy_cron, unique=False),
                _fake_cron(function=None, unique=True),
            ],
        )

        assert pipeline.deleted == ["jid:cron:_dummy_cron"]
        assert len(pipeline.zremmed) == 1
        assert len(pipeline.lremmed) == 1


@pytest.mark.asyncio
async def test_resume_run_accepts_claim_token_from_saq_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SAQ re-invokes resume_run with the ``claim_token`` stamped into job.kwargs
    by a prior attempt. The kwarg must be accepted (not raise TypeError) and
    ignored — the core claim regenerates its own token.
    """
    core_result = {"status": "resumed"}

    async def _fake_core(**_kwargs: Any) -> dict[str, Any]:
        return core_result

    monkeypatch.setattr("modulo.core.pipeline_execution.resume_run", _fake_core)
    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())

    # Mimics SAQ retry re-invocation: claim_token arrives via **job.kwargs.
    result = await sw.resume_run(
        ctx={},
        run_id="run-1",
        org_id="org-1",
        resume_data={"foo": "bar"},
        claim_token="stale-token",
    )
    assert result == core_result

    # And it must still work without the kwarg present.
    result2 = await sw.resume_run(ctx={}, run_id="run-2", org_id="org-2")
    assert result2 == core_result
