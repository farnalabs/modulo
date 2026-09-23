"""Tests for readiness endpoint — aggregation logic, degraded/unavailable status, and check structure."""

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.api.routes.health import (
    CheckResult,
    _check_checkpointer,
    _check_database,
    _check_fleet_saq_workers,
    _check_fleet_system_crons,
    _check_migrations,
    _check_redis,
    _check_saq_workers,
    _check_system_crons,
    _live_worker_hostnames,
    _per_check_timeout,
)
from modulo.db.migration_guard import DivergenceCheckResult
from modulo.settings import Settings, get_settings
from modulo.version import get_version


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="redis://localhost:6379/0",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ok_check(name: str = "ok") -> CheckResult:
    return CheckResult(status="ok", latency_ms=1.0, detail=f"{name} reachable")


def _degraded_check(detail: str = "degraded") -> CheckResult:
    return CheckResult(status="degraded", detail=detail)


def _unavailable_check(detail: str = "unavailable") -> CheckResult:
    return CheckResult(status="unavailable", latency_ms=5000.0, detail=detail)


class TestReadiness:
    def test_healthz_ready_mounted(self, client: TestClient) -> None:
        resp = client.get("/healthz/ready")
        assert resp.status_code in (200, 503, 504)

    def test_healthz_ready_structure_when_unavailable(self, client: TestClient) -> None:
        resp = client.get("/healthz/ready")
        assert resp.status_code in (200, 503, 504)
        body = resp.json()
        if resp.status_code == 504:
            return
        if resp.status_code == 503:
            assert body["status"] == "unavailable"
        assert body["version"] == get_version()
        assert isinstance(body["uptime_seconds"], float)
        assert isinstance(body["checks"], dict)
        for key in ("database", "redis", "checkpointer", "migrations"):
            assert key in body["checks"], f"missing check key: {key}"

    def test_healthz_ready_degraded_overall(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch(
                "modulo.api.routes.health._check_redis", AsyncMock(return_value=_degraded_check("redis not configured"))
            ),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["redis"]["status"] == "degraded"
        for key in ("database", "checkpointer", "migrations"):
            assert body["checks"][key]["status"] == "ok"

    def test_healthz_ready_unavailable_overall(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_unavailable_check("db down"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["database"]["status"] == "unavailable"

    def test_healthz_ready_all_ok(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        for key in ("database", "redis", "checkpointer", "migrations"):
            assert body["checks"][key]["status"] == "ok"

    def test_healthz_ready_check_keys_present(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        body = resp.json()
        for key in ("database", "redis", "checkpointer", "migrations"):
            c = body["checks"][key]
            assert "status" in c
            assert "latency_ms" in c
            assert "detail" in c

    def test_healthz_ready_dispatcher_unavailable_gates(self, client: TestClient) -> None:
        """FAR-199: a dispatcher_reconcile check that is unavailable (reconcile
        stale past the 300s tier — wedged system worker) 503s readiness even
        when every other check is ok."""
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_unavailable_check("dispatcher_reconcile stale 360s")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["dispatcher_reconcile"]["status"] == "unavailable"

    def test_healthz_ready_dispatcher_degraded_stays_advisory(self, client: TestClient) -> None:
        """FAR-199: a dispatcher_reconcile check that is degraded (a single
        missed 60s tick) must NOT flip overall readiness — it stays advisory."""
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_degraded_check("dispatcher_reconcile stale 120s")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["dispatcher_reconcile"]["status"] == "degraded"


class TestHttpTimeout:
    def test_healthz_ready_with_timeout_check(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.health._check_database",
                AsyncMock(return_value=_unavailable_check("timeout after 5s")),
            ),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "timeout" in body["checks"]["database"]["detail"].lower()


class _HangingEngine:
    """Fake SQLAlchemy engine whose connect() never returns."""

    def connect(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        await asyncio.sleep(60)
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class TestPerCheckTimeouts:
    """Configurable per-check timeout limits (feat-infra-health)."""

    def test_settings_defaults_apply(self) -> None:
        settings = _make_settings()
        assert settings.modulo_health_timeout_seconds == 5.0
        for field in (
            "modulo_health_db_timeout_seconds",
            "modulo_health_redis_timeout_seconds",
            "modulo_health_checkpointer_timeout_seconds",
            "modulo_health_migrations_timeout_seconds",
        ):
            assert getattr(settings, field) == 0.0

    def test_per_check_timeout_falls_back_to_global(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 2.5})
        assert _per_check_timeout(settings, "modulo_health_db_timeout_seconds") == 2.5
        assert _per_check_timeout(settings, "modulo_health_redis_timeout_seconds") == 2.5

    def test_per_check_timeout_override_wins(self) -> None:
        settings = _make_settings().model_copy(
            update={
                "modulo_health_timeout_seconds": 2.5,
                "modulo_health_redis_timeout_seconds": 0.5,
            }
        )
        assert _per_check_timeout(settings, "modulo_health_db_timeout_seconds") == 2.5
        assert _per_check_timeout(settings, "modulo_health_redis_timeout_seconds") == 0.5

    async def test_database_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 0.2})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            result = await _check_database()
        assert result.status == "unavailable"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_redis_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_redis_timeout_seconds": 0.2})

        async def _hang() -> None:
            await asyncio.sleep(60)

        redis_client = AsyncMock()
        redis_client.ping.side_effect = _hang
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=redis_client),
        ):
            result = await _check_redis()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_checkpointer_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_checkpointer_timeout_seconds": 0.2})

        async def _hang(*args: object, **kwargs: object) -> None:
            await asyncio.sleep(60)

        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.pg_connection_string", return_value="postgresql://test"),
            patch("modulo.api.routes.health.asyncpg.connect", side_effect=_hang),
        ):
            result = await _check_checkpointer()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_migrations_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_migrations_timeout_seconds": 0.2})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            result = await _check_migrations()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_database_check_ok_reports_latency(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 1.0})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine") as engine_factory,
        ):
            engine = AsyncMock()
            conn = AsyncMock()
            conn.__aenter__ = AsyncMock(return_value=conn)
            conn.__aexit__ = AsyncMock(return_value=None)
            engine.connect = lambda: conn
            engine_factory.return_value = engine
            result = await _check_database()
        assert result.status == "ok"
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000


class _FakeMigrationResult:
    """Canned result set for a fake ``alembic_version`` SELECT."""

    def __init__(self, applied: list[str]) -> None:
        self._applied = applied

    def fetchall(self) -> list[tuple[str]]:
        return [(rev,) for rev in self._applied]


class _FakeMigrationEngine:
    """Fake SQLAlchemy engine returning a canned ``alembic_version`` result.

    Serves as its own async context manager (mirroring ``_HangingEngine``) so
    the ``async with engine.connect()`` block in ``_check_migrations`` completes.
    """

    def __init__(self, applied: list[str]) -> None:
        self._applied = applied

    def connect(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _stmt: object) -> _FakeMigrationResult:
        return _FakeMigrationResult(self._applied)


class TestCheckMigrationsDivergence:
    """FAR-872: ``_check_migrations`` surfaces repo-vs-DB divergence.

    The new-code coverage gate needs the success and divergence branches of the
    FAR-872 migration-guard integration exercised, not just the timeout path.
    """

    async def test_migrations_up_to_date(self) -> None:
        settings = _make_settings()
        clean = DivergenceCheckResult(
            diverged=False,
            db_revisions={"0001"},
            repo_revisions={"0001"},
            orphaned_revisions=set(),
            detail="clean",
        )
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_FakeMigrationEngine(["0001"])),
            patch("modulo.api.routes.health.ScriptDirectory") as script_cls,
            patch("modulo.api.routes.health.check_migration_divergence", return_value=clean),
        ):
            script_cls.from_config.return_value.get_heads.return_value = {"0001"}
            result = await _check_migrations()
        assert result.status == "ok"
        assert result.detail == "migrations up to date"

    async def test_migrations_report_pending_and_divergence(self) -> None:
        settings = _make_settings()
        diverged = DivergenceCheckResult(
            diverged=True,
            db_revisions={"0001", "0099"},
            repo_revisions={"0001"},
            orphaned_revisions={"0099"},
            detail="diverged",
        )
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch(
                "modulo.api.routes.health.get_or_create_engine",
                return_value=_FakeMigrationEngine(["0001", "0099"]),
            ),
            patch("modulo.api.routes.health.ScriptDirectory") as script_cls,
            patch("modulo.api.routes.health.check_migration_divergence", return_value=diverged),
        ):
            script_cls.from_config.return_value.get_heads.return_value = {"0001", "0002"}
            result = await _check_migrations()
        detail = result.detail or ""
        assert result.status == "degraded"
        assert "pending migrations: 0002" in detail
        assert "DIVERGENCE" in detail
        assert "0099" in detail

    async def test_divergence_check_error_is_fail_open(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_FakeMigrationEngine(["0001"])),
            patch("modulo.api.routes.health.ScriptDirectory") as script_cls,
            patch(
                "modulo.api.routes.health.check_migration_divergence",
                side_effect=RuntimeError("divergence probe exploded"),
            ),
        ):
            script_cls.from_config.return_value.get_heads.return_value = {"0001"}
            result = await _check_migrations()
        # FAR-925: a crashed guard returns degraded, not ok — the check
        # could not run, so the result must not read as "clean".
        assert result.status == "degraded"
        assert result.detail is not None
        assert "could not run" in result.detail.lower()


class _FakeStatsRedis:
    """Fake redis client exposing zrangebyscore/mget over an in-memory zset.

    Scores are milliseconds, matching SAQ 0.26.4 (``saq.utils.now()``).
    """

    def __init__(self, stats: dict[str, int], blobs: dict[str, str]) -> None:
        self._stats = stats
        self._blobs = blobs

    async def zrangebyscore(self, _key: str, min_score: int, max_score: str) -> list[bytes]:
        out = []
        for member, score in self._stats.items():
            if score >= min_score and (max_score == "+inf" or score <= max_score):
                out.append(member.encode())
        return out

    async def mget(self, members: list[bytes]) -> list[bytes | None]:
        return [self._blobs.get(m.decode()).encode() if m.decode() in self._blobs else None for m in members]

    async def aclose(self) -> None:
        return None


class _PerQueueFakeStatsRedis(_FakeStatsRedis):
    """Fake redis that serves per-queue stats (worker_info hashes keyed by the
    ``saq:{queue}:stats`` zset), so ``_check_saq_workers`` can use the REAL
    ``_live_worker_hostnames`` against multiple queues."""

    async def zrangebyscore(self, key: str, min_score: int, max_score: str) -> list[bytes]:
        prefix = f"saq:{key.split(':')[1]}:stats:"
        out = []
        for member, score in self._stats.items():
            if member.startswith(prefix) and score >= min_score and (max_score == "+inf" or score <= max_score):
                out.append(member.encode())
        return out


def _worker_blob(hostname: str) -> str:
    return json.dumps({"metadata": {"hostname": hostname}})


@pytest.fixture(autouse=True)
def reset_stale_probes() -> Generator[None, None, None]:
    """Reset both fleet-gate probe counters AND skip the boot grace window.

    Autouse (ADR 043): every test in this module starts from a clean gate
    state, and ``_START_TIME`` is pushed an hour into the past so the 120s
    fleet boot grace never masks a failure a test intends to exercise. Tests
    that DO want the boot grace re-patch ``_START_TIME`` locally (inner patch
    wins while active).
    """
    import modulo.api.routes.health as health_mod

    health_mod._consecutive_stale_probes = 0
    health_mod._consecutive_cron_stale_probes = 0
    start_time_patch = patch.object(health_mod, "_START_TIME", datetime.now(UTC) - timedelta(hours=1))
    start_time_patch.start()
    yield
    start_time_patch.stop()
    health_mod._consecutive_stale_probes = 0
    health_mod._consecutive_cron_stale_probes = 0


class TestLiveWorkerHostnamesMsScores:
    """SAQ stats zset scores are milliseconds — the liveness filter must compare in ms."""

    async def _call(self, stats: dict[str, int], blobs: dict[str, str], now_ms: int) -> set[str]:
        settings = _make_settings()
        fake = _FakeStatsRedis(stats, blobs)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=now_ms / 1000),
        ):
            return await _live_worker_hostnames("runs")

    async def test_fresh_included_stale_excluded_same_ms(self) -> None:
        now_ms = 1_700_000_000_000
        stats = {
            "saq:runs:stats:fresh": now_ms + 90_000,
            "saq:runs:stats:stale": now_ms - 1_000,
            "saq:runs:stats:boundary": now_ms,
        }
        blobs = {
            "saq:runs:stats:fresh": _worker_blob("machine-a"),
            "saq:runs:stats:stale": _worker_blob("machine-b"),
            "saq:runs:stats:boundary": _worker_blob("machine-c"),
        }
        hosts = await self._call(stats, blobs, now_ms)
        # boundary (score == now_ms) is live; stale (now_ms - 1s) is excluded.
        assert hosts == {"machine-a", "machine-c"}

    async def test_crossing_second_boundary(self) -> None:
        # now_ms lands on a fractional second boundary: scores 1ms apart on
        # either side of the comparison point must be correctly split.
        now_ms = int(1_700_000_000.999 * 1000)
        stats = {
            "saq:runs:stats:just_before": now_ms - 1,
            "saq:runs:stats:just_after": now_ms + 1,
        }
        blobs = {
            "saq:runs:stats:just_before": _worker_blob("machine-stale"),
            "saq:runs:stats:just_after": _worker_blob("machine-fresh"),
        }
        hosts = await self._call(stats, blobs, now_ms)
        assert hosts == {"machine-fresh"}


class TestCheckSaqWorkersPerQueue:
    async def _run(
        self,
        live_by_queue: dict[str, set[str]],
        *,
        saq_hard_gate: bool = True,
    ) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": saq_hard_gate})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
        ):

            async def _live_side_effect(qname: str) -> set[str]:
                return live_by_queue.get(qname, set())

            live.side_effect = _live_side_effect
            return await _check_saq_workers()

    async def _run_read_error(self, *, saq_hard_gate: bool = True) -> CheckResult:
        """One probe where EVERY queue read fails (undeterminable, ADR 043)."""
        settings = _make_settings().model_copy(update={"saq_hard_gate": saq_hard_gate})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames", side_effect=RuntimeError("redis down")),
        ):
            return await _check_saq_workers()

    async def test_live_on_both_queues_ok(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": {"machine-a"}, "system": {"machine-a"}})
        assert result.status == "ok"

    async def test_dead_runs_worker_not_masked_by_live_system(self, reset_stale_probes: None) -> None:
        # runs confirmed-empty, system live — the gate must surface runs
        # (degraded during probe grace, ADR 043), never mask it behind the
        # live sibling.
        result = await self._run({"runs": set(), "system": {"machine-a"}})
        assert result.status == "degraded"
        assert "runs" in result.detail

    async def test_dead_system_worker_not_masked_by_live_runs(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": {"machine-a"}, "system": set()})
        assert result.status == "degraded"
        assert "system" in result.detail

    async def test_workers_on_other_node_yield_ready(self, reset_stale_probes: None) -> None:
        """FAR-1158 / ADR 043 (required test): workers on a DIFFERENT node
        than the backend still yield Ready — readiness is deployment-scoped,
        so ANY live worker on each queue covers it, wherever it runs."""
        result = await self._run({"runs": {"worker-node-2"}, "system": {"worker-node-3"}})
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_dead_queue_escalates_to_unavailable(self, reset_stale_probes: None) -> None:
        """FAR-1158 (required test): a genuinely dead queue fails CLOSED —
        degraded through the probe-grace tier, then unavailable at the limit
        (SAQ_HARD_GATE=true), while its live sibling never masks it."""
        statuses = [await self._run({"runs": set(), "system": {"machine-b"}}) for _ in range(4)]
        assert [r.status for r in statuses] == ["degraded", "degraded", "degraded", "unavailable"]
        assert "runs" in statuses[-1].detail

    async def test_undeterminable_degrades_then_escalates_after_grace(self, reset_stale_probes: None) -> None:
        """FAR-1158 (required test — the bounded fail-open / relaxation path):
        an unreadable store reports degraded (non-gating) but advances the
        SAME probe counter, escalating to unavailable after the grace tier —
        a transient blip never gates, a sustained outage does."""
        first = await self._run_read_error()
        assert first.status == "degraded"
        assert "undeterminable" in first.detail
        statuses = [(await self._run_read_error()).status for _ in range(3)]
        assert statuses == ["degraded", "degraded", "unavailable"]

    async def test_undeterminable_shares_counter_with_confirmed_empty(self, reset_stale_probes: None) -> None:
        """Confirmed-empty and undeterminable advance ONE counter (ADR 043):
        one read-error probe + three confirmed-empty probes reach the limit
        together — the read error must not reset or isolate the tier."""
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames", side_effect=RuntimeError("redis down")),
        ):
            mixed_error = await _check_saq_workers()
        assert mixed_error.status == "degraded"
        result = await self._run({"runs": set(), "system": set()})
        assert result.status == "degraded"
        result = await self._run({"runs": set(), "system": set()})
        assert result.status == "degraded"
        result = await self._run({"runs": set(), "system": set()})
        assert result.status == "unavailable"

    async def test_confirmed_empty_beats_sibling_read_failure(self, reset_stale_probes: None) -> None:
        """Aggregation precedence (ADR 043): a confirmed-empty queue forces
        the failed-closed path even when a sibling queue's read failed — the
        classification must stay confirmed-empty, not degrade into
        undeterminable-only."""

        async def _mixed(qname: str) -> set[str]:
            if qname == "runs":
                return set()
            raise RuntimeError("redis down for system")

        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames", side_effect=_mixed),
        ):
            result = await _check_saq_workers()
        assert result.status == "degraded"
        assert "no live saq workers" in result.detail
        assert "undeterminable" not in result.detail

    async def test_boot_grace_reports_ok_without_advancing_counter(self, reset_stale_probes: None) -> None:
        """Both fleet gates get boot grace (ADR 043): inside the 120s window a
        dead deployment reports ok AND the probe counter does not advance —
        the first post-grace probe is 1/4, not 5/4."""
        import modulo.api.routes.health as health_mod

        with patch.object(health_mod, "_START_TIME", datetime.now(UTC)):
            result = await self._run({"runs": set(), "system": set()})
        assert result.status == "ok"
        assert "boot grace" in result.detail
        assert health_mod._consecutive_stale_probes == 0

    async def test_stale_four_probes_unavailable_when_gated(self, reset_stale_probes: None) -> None:
        result: CheckResult | None = None
        for _ in range(4):
            result = await self._run({"runs": set(), "system": set()})
        assert result is not None
        assert result.status == "unavailable"

    async def test_hard_gate_false_staleness_alert_only(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": set(), "system": set()}, saq_hard_gate=False)
        assert result.status == "ok"

    async def test_hard_gate_false_undeterminable_alert_only(self, reset_stale_probes: None) -> None:
        """SAQ_HARD_GATE=false relaxes BOTH outcomes to alert-only (ADR 043):
        a sustained read failure never gates readiness, only logs."""
        result = await self._run_read_error(saq_hard_gate=False)
        assert result.status == "ok"
        assert "SAQ_HARD_GATE=false" in result.detail

    async def test_configured_queues_failure_is_undeterminable(self, reset_stale_probes: None) -> None:
        """Resolving the queue list can itself fail (bad config): readiness
        must not crash — the failed read is classified undeterminable
        (ADR 043 bounded fail-open), degrades on the first probe via the
        shared counter, and names the ``<queue-config>`` sentinel."""
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch(
                "modulo.api.routes.health._configured_queues",
                side_effect=RuntimeError("queue config exploded"),
            ),
        ):
            result = await _check_saq_workers()
        assert result.status == "degraded"
        assert "undeterminable" in result.detail
        assert "<queue-config>" in result.detail

    async def test_cancelled_queue_read_propagates(self, reset_stale_probes: None) -> None:
        """Cancellation is never swallowed (ADR 043): an in-flight queue read
        cancelled during shutdown must propagate, NOT be counted as a failed
        probe that would gate readiness."""
        import modulo.api.routes.health as health_mod

        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs"])),
            patch(
                "modulo.api.routes.health._live_worker_hostnames",
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _check_saq_workers()
        assert health_mod._consecutive_stale_probes == 0


class TestCheckSaqWorkersEndToEnd:
    """_check_saq_workers through the REAL _live_worker_hostnames (fake Redis)
    — the ok → degraded → unavailable transition is covered without patching
    the mechanism. Uses fake worker_info hashes with millisecond scores."""

    NOW_MS = 1_700_000_000_000

    async def _call(
        self,
        stats: dict[str, int],
        blobs: dict[str, str],
    ) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": True})
        fake = _PerQueueFakeStatsRedis(stats, blobs)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW_MS / 1000),
        ):
            return await _check_saq_workers()

    async def test_ok_then_degraded_then_unavailable(self, reset_stale_probes: None) -> None:
        # Worker live on both queues (score in the future = within the 90s TTL).
        live_stats = {
            "saq:runs:stats:w1": self.NOW_MS + 90_000,
            "saq:system:stats:w1": self.NOW_MS + 90_000,
        }
        live_blobs = {
            "saq:runs:stats:w1": _worker_blob("machine-a"),
            "saq:system:stats:w1": _worker_blob("machine-a"),
        }
        # Worker gone -> stale on BOTH queues.
        stale_stats: dict[str, int] = {}
        stale_blobs: dict[str, str] = {}

        ok = await self._call(live_stats, live_blobs)
        assert ok.status == "ok"

        statuses: list[str] = []
        for _ in range(4):
            result = await self._call(stale_stats, stale_blobs)
            statuses.append(result.status)
        # stale -> degraded (x3) -> unavailable after the 4th stale probe.
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]
        assert "no live saq workers" in result.detail

    async def test_workers_on_other_hostnames_end_to_end(self, reset_stale_probes: None) -> None:
        """FAR-1158 (required test, real decoder): the live workers carry
        hostnames the backend could never match machine-scoped (other nodes)
        — readiness is still ok, proving the co-location requirement is gone
        end-to-end through the real _live_worker_hostnames path."""
        stats = {
            "saq:runs:stats:w1": self.NOW_MS + 90_000,
            "saq:system:stats:w2": self.NOW_MS + 90_000,
        }
        blobs = {
            "saq:runs:stats:w1": _worker_blob("modulo-saq-runner-node-2-abc"),
            "saq:system:stats:w2": _worker_blob("modulo-saq-system-node-3-def"),
        }
        result = await self._call(stats, blobs)
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_stale_on_one_queue_degraded_not_unavailable_yet(self, reset_stale_probes: None) -> None:
        # runs live; system worker dead -> confirmed-empty on system -> degraded, never ok.
        stats = {
            "saq:runs:stats:w1": self.NOW_MS + 90_000,
            "saq:system:stats:w1": self.NOW_MS - 1_000,  # stale -> excluded
        }
        blobs = {
            "saq:runs:stats:w1": _worker_blob("machine-a"),
            "saq:system:stats:w1": _worker_blob("machine-a"),
        }
        result = await self._call(stats, blobs)
        assert result.status == "degraded"
        assert "system" in result.detail


class TestCheckFleetSaqWorkers:
    """Deployment-scoped fleet SAQ gate — the sole readiness semantics (ADR 043).

    ``_check_fleet_saq_workers`` and ``_check_saq_workers`` are ONE path now;
    these tests exercise the shared implementation directly.
    """

    async def _run(self, live_by_queue: dict[str, set[str]], *, saq_hard_gate: bool = True) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": saq_hard_gate})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
        ):

            async def _live_side_effect(qname: str) -> set[str]:
                return live_by_queue.get(qname, set())

            live.side_effect = _live_side_effect
            return await _check_fleet_saq_workers()

    async def test_any_live_worker_on_each_queue_ok(self, reset_stale_probes: None) -> None:
        # A worker anywhere in the deployment covers readiness — no
        # co-location with this backend required (FAR-1158).
        result = await self._run({"runs": {"machine-b"}, "system": {"machine-b"}})
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_no_worker_on_one_queue_escalates_to_unavailable(self, reset_stale_probes: None) -> None:
        # probe grace: degraded first, unavailable at the limit (ADR 043 —
        # the fleet gate now HAS a probe tier, unlike the old fail-closed
        # instant 503).
        first = await self._run({"runs": set(), "system": {"machine-b"}})
        assert first.status == "degraded"
        assert "runs" in first.detail
        statuses = [(await self._run({"runs": set(), "system": {"machine-b"}})).status for _ in range(3)]
        assert statuses == ["degraded", "degraded", "unavailable"]

    async def test_no_worker_anywhere_escalates_to_unavailable(self, reset_stale_probes: None) -> None:
        statuses = [(await self._run({"runs": set(), "system": set()})).status for _ in range(4)]
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]

    async def test_hard_gate_false_alert_only(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": set(), "system": set()}, saq_hard_gate=False)
        assert result.status == "ok"

    async def test_redis_read_error_fails_open_then_escalates(self, reset_stale_probes: None) -> None:
        """Bounded fail-open (ADR 043): a Redis read failure is NOT
        confirmed-empty — it reports degraded (non-gating) on the first probe
        (the old docstring's fail-open claim, now true), and a SUSTAINED
        failure escalates to unavailable after the grace tier instead of
        staying open forever."""
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch(
                "modulo.api.routes.health._live_worker_hostnames",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            first = await _check_fleet_saq_workers()
        assert first.status == "degraded"
        assert "undeterminable" in first.detail


class TestCheckSaqWorkersProcessGroup:
    """FLY_PROCESS_GROUP no longer routes anything (ADR 043 / FAR-1158).

    The machine-scoped branch is deleted: every process-group value — unset,
    ``app``, or ``worker`` — takes the SAME deployment-scoped fleet path.
    These tests pin that the env var cannot resurrect machine-scoping.
    """

    async def _run_with_env(
        self,
        env: dict[str, str],
        live_by_queue: dict[str, set[str]],
    ) -> CheckResult:
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
            patch.dict("os.environ", env, clear=False),
        ):

            async def _live_side_effect(qname: str) -> set[str]:
                return live_by_queue.get(qname, set())

            live.side_effect = _live_side_effect
            return await _check_saq_workers()

    async def test_app_machine_takes_fleet_path(self, reset_stale_probes: None) -> None:
        # FLY_PROCESS_GROUP=app + a worker live on ANOTHER host -> ok (fleet).
        result = await self._run_with_env(
            {"FLY_MACHINE_ID": "app-1", "FLY_PROCESS_GROUP": "app"},
            {"runs": {"machine-b"}, "system": {"machine-b"}},
        )
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_worker_machine_takes_fleet_path_too(self, reset_stale_probes: None) -> None:
        """FAR-1158 regression: FLY_PROCESS_GROUP=worker (the OLD trigger for
        the machine-scoped branch) + workers live only on ANOTHER host must
        still be ok — the machine-scoped path is deleted, not re-routed."""
        result = await self._run_with_env(
            {"FLY_MACHINE_ID": "machine-a", "FLY_PROCESS_GROUP": "worker"},
            {"runs": {"machine-b"}, "system": {"machine-b"}},
        )
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_fleet_outage_unavailable_on_any_process_group(self, reset_stale_probes: None) -> None:
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", MagicMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health._live_worker_hostnames", return_value=set()),
            patch.dict("os.environ", {"FLY_MACHINE_ID": "app-1", "FLY_PROCESS_GROUP": "app"}, clear=False),
        ):
            statuses = [(await _check_saq_workers()).status for _ in range(4)]
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]


class _FakeHeartbeatRedis:
    """Fake redis client for ``saq:cron:heartbeat:fire_due_triggers:*`` reads.

    Exposes ``scan_iter`` (ADR 043: the fleet gate SCANs, never KEYS — a fake
    with only ``keys()`` would let a KEYS regression pass unnoticed).
    """

    def __init__(self, heartbeats: dict[str, str]) -> None:
        self._heartbeats = heartbeats

    async def scan_iter(self, match: str = "*", **_kwargs: object):
        prefix = match.rstrip("*")
        for key in self._heartbeats:
            if key.startswith(prefix):
                yield key.encode()

    async def get(self, key: bytes | str) -> bytes | None:
        decoded = key.decode() if isinstance(key, bytes) else key
        value = self._heartbeats.get(decoded)
        return value.encode() if value is not None else None

    async def aclose(self) -> None:
        return None


class TestCheckFleetSystemCrons:
    """Deployment-scoped fire_due_triggers cron liveness — the sole path (ADR 043).

    Uses ``scan_iter`` (never KEYS) and applies boot grace + probe grace to
    BOTH the confirmed-stale and the undeterminable outcomes.
    """

    NOW = 1_700_000_000.0

    async def _run(
        self,
        heartbeats: dict[str, str],
        *,
        saq_hard_gate: bool = True,
        now: float | None = None,
    ) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": saq_hard_gate})
        fake = _FakeHeartbeatRedis(heartbeats)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW if now is None else now),
        ):
            return await _check_fleet_system_crons()

    async def test_fresh_heartbeat_on_any_machine_ok(self, reset_stale_probes: None) -> None:
        result = await self._run({"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 30)})
        assert result.status == "ok"

    async def test_stale_heartbeat_escalates_to_unavailable(self, reset_stale_probes: None) -> None:
        """Confirmed-stale goes through probe grace (degraded x3 -> unavailable)
        — the fleet crons gate now HAS a tier (ADR 043), unlike the old
        instant 503."""
        heartbeats = {"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 121)}
        statuses = [(await self._run(heartbeats)).status for _ in range(4)]
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]

    async def test_no_heartbeat_anywhere_escalates_to_unavailable(self, reset_stale_probes: None) -> None:
        statuses = [(await self._run({})).status for _ in range(4)]
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]

    async def test_boot_grace_reports_ok(self, reset_stale_probes: None) -> None:
        """Both fleet gates get boot grace (ADR 043): right after process
        start an absent fleet heartbeat reports ok, and the counter does not
        advance."""
        import modulo.api.routes.health as health_mod

        with patch.object(health_mod, "_START_TIME", datetime.now(UTC)):
            result = await self._run({})
        assert result.status == "ok"
        assert "boot grace" in result.detail
        assert health_mod._consecutive_cron_stale_probes == 0

    async def test_hard_gate_false_alert_only(self, reset_stale_probes: None) -> None:
        result = await self._run(
            {"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 121)},
            saq_hard_gate=False,
        )
        assert result.status == "ok"

    async def test_redis_read_error_fails_open_then_escalates(self, reset_stale_probes: None) -> None:
        """Bounded fail-open (ADR 043): an unreadable store is undeterminable
        — degraded (non-gating) on the first probe, escalating to unavailable
        after the grace tier, never a permanent fail-open."""
        settings = _make_settings()

        class _BrokenRedis:
            def scan_iter(self, _match: str = "*", **_kwargs: object):
                raise RuntimeError("redis down")

            async def aclose(self) -> None:
                return None

        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=_BrokenRedis()),
        ):
            first = await _check_fleet_system_crons()
            statuses = [first.status]
            for _ in range(3):
                statuses.append((await _check_fleet_system_crons()).status)
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]
        assert "undeterminable" in first.detail

    async def test_unparseable_heartbeat_counts_as_confirmed_stale(self, reset_stale_probes: None) -> None:
        """A corrupt heartbeat value is a READ SUCCESS with garbage — fail
        closed (confirmed-stale tier), never classify it as a read failure."""
        result = await self._run({"saq:cron:heartbeat:fire_due_triggers:machine-b": "not-a-float"})
        assert result.status == "degraded"
        assert "undeterminable" not in result.detail


class TestCheckSystemCronsProcessGroup:
    """FLY_PROCESS_GROUP no longer routes the cron watchdog (ADR 043 / FAR-1158).

    Every process-group value takes the SAME deployment-scoped fleet path.
    """

    NOW = 1_700_000_000.0

    async def test_app_machine_takes_fleet_path(self, reset_stale_probes: None) -> None:
        # Any machine's fresh scheduler covers readiness — no co-location.
        heartbeats = {"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 30)}
        fake = _FakeHeartbeatRedis(heartbeats)
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW),
            patch.dict("os.environ", {"FLY_MACHINE_ID": "app-1", "FLY_PROCESS_GROUP": "app"}, clear=False),
        ):
            result = await _check_system_crons()
        assert result.status == "ok"

    async def test_worker_machine_takes_fleet_path_too(self, reset_stale_probes: None) -> None:
        """FAR-1158 regression: FLY_PROCESS_GROUP=worker (the OLD trigger for
        the machine-scoped heartbeat lookup) with NO local heartbeat but a
        fresh one elsewhere must be ok — machine-scoping is deleted."""
        heartbeats = {"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 30)}
        fake = _FakeHeartbeatRedis(heartbeats)
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW),
            patch.dict("os.environ", {"FLY_MACHINE_ID": "machine-a", "FLY_PROCESS_GROUP": "worker"}, clear=False),
        ):
            result = await _check_system_crons()
        assert result.status == "ok"

    async def test_fleet_wide_cron_death_unavailable_on_any_process_group(self, reset_stale_probes: None) -> None:
        fake = _FakeHeartbeatRedis({})
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW),
            patch.dict("os.environ", {"FLY_MACHINE_ID": "machine-a", "FLY_PROCESS_GROUP": "worker"}, clear=False),
        ):
            statuses = [(await _check_system_crons()).status for _ in range(4)]
        assert statuses == ["degraded", "degraded", "degraded", "unavailable"]


def _fake_migrations_engine(applied: list[str]) -> AsyncMock:
    """Fake engine whose ``connect()`` yields rows for the alembic_version query."""
    engine = AsyncMock()
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    rows = MagicMock()
    rows.fetchall.return_value = [(rev,) for rev in applied]
    conn.execute = AsyncMock(return_value=rows)
    engine.connect = lambda: conn
    return engine


class TestMigrationsDivergence:
    """FAR-872: the /healthz/ready migration check surfaces repo-vs-DB divergence.

    The health.py integration is the path that loses status and becomes
    ``degraded``; these tests fail without the health.py change.
    """

    async def test_divergence_surfaces_degraded_detail(self) -> None:
        from modulo.db.migration_guard import DivergenceCheckResult

        settings = _make_settings()
        divergent = DivergenceCheckResult(
            diverged=True,
            db_revisions={"0001", "0099"},
            repo_revisions={"0001"},
            orphaned_revisions={"0099"},
            detail="Migration divergence detected: 0099",
        )
        script = MagicMock()
        script.get_heads.return_value = ["0001"]
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch(
                "modulo.api.routes.health.get_or_create_engine",
                return_value=_fake_migrations_engine(["0001", "0099"]),
            ),
            patch("modulo.api.routes.health.ScriptDirectory.from_config", return_value=script),
            patch("modulo.api.routes.health.check_migration_divergence", return_value=divergent),
        ):
            result = await _check_migrations()
        assert result.status == "degraded"
        assert "DIVERGENCE" in result.detail
        assert "0099" in result.detail

    async def test_divergence_check_failure_is_logged_and_fail_open(self) -> None:
        settings = _make_settings()
        script = MagicMock()
        script.get_heads.return_value = ["0001"]
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch(
                "modulo.api.routes.health.get_or_create_engine",
                return_value=_fake_migrations_engine(["0001"]),
            ),
            patch("modulo.api.routes.health.ScriptDirectory.from_config", return_value=script),
            patch("modulo.api.routes.health.check_migration_divergence", side_effect=RuntimeError("boom")),
            patch("modulo.api.routes.health._log") as log,
        ):
            result = await _check_migrations()
        # FAR-925: a crashed guard returns degraded, not ok — the check
        # could not run, so the result must not read as "clean".
        assert result.status == "degraded"
        assert result.detail is not None
        assert "could not run" in result.detail.lower()
        log.exception.assert_called_once()
