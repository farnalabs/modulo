"""Tests for readiness endpoint — aggregation logic, degraded/unavailable status, and check structure."""

import asyncio
import json
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.api.routes.health import (
    _LOOP_LAG_DEGRADED_MS,
    CheckResult,
    DbHygieneReading,
    _check_checkpointer,
    _check_database,
    _check_db_hygiene,
    _check_fleet_saq_workers,
    _check_fleet_system_crons,
    _check_migrations,
    _check_redis,
    _check_saq_workers,
    _check_system_crons,
    _grade_db_hygiene,
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


@pytest.fixture(autouse=True)
def _reset_repo_heads_cache() -> Generator[None, None, None]:
    """FAR-1439: ``_check_migrations`` memoizes the parsed alembic heads in a
    process-wide cache; reset it around every test so one test's canned (or
    real) heads can never leak into another test's assertions."""
    from modulo.api.routes import health as health_mod

    health_mod._REPO_HEADS_CACHE = None
    yield
    health_mod._REPO_HEADS_CACHE = None


def _ok_check(name: str = "ok") -> CheckResult:
    return CheckResult(status="ok", latency_ms=1.0, detail=f"{name} reachable")


def _degraded_check(detail: str = "degraded") -> CheckResult:
    return CheckResult(status="degraded", detail=detail)


def _unavailable_check(detail: str = "unavailable") -> CheckResult:
    return CheckResult(status="unavailable", latency_ms=5000.0, detail=detail)


# --- FAR-1439 event-loop stall guard doubles -------------------------------
# Both doubles are deliberately PLAIN module-level functions: the test-suite
# scanner flags ``time.sleep`` anywhere inside an ``async def`` body, and the
# thread-identity probe must be callable from async tests without living in
# them.

#: Threads ``ScriptDirectory.get_heads`` was invoked from, per test.
_GET_HEADS_THREAD_IDS: list[int] = []


def _recorded_get_heads() -> set[str]:
    """``get_heads`` double that records which thread it ran on."""
    _GET_HEADS_THREAD_IDS.append(threading.get_ident())
    return {"0001"}


#: Duration of the simulated sync block in ``_stall_event_loop``.
_STALL_SECONDS = 0.4


def _stall_event_loop() -> None:
    """Simulate the FAR-1439 bug class: a synchronous call that blocks the
    event loop for a measured duration (literal sleep, hang-simulation)."""
    time.sleep(_STALL_SECONDS)


async def _stalling_redis_check() -> CheckResult:
    """Redis-check double that blocks the loop before answering."""
    _stall_event_loop()
    return _ok_check("redis")


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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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

        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            result = await _check_checkpointer()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_checkpointer_check_uses_engine_pool_not_fresh_connection(self) -> None:
        """FAR-1426 regression: the probe must go through the pooled engine.

        The old probe opened a brand-new raw ``asyncpg`` connection from a
        helper-built DSN on every readiness request. On prod, whenever the
        event loop stalled between TCP connect and the StartupPacket, the
        peer closed the fresh connection (~1s startup-idle close) and the
        check reported ``ConnectionDoesNotExistError`` as a checkpointer
        failure — while the pooled ``database`` check (same DB, same
        moment) stayed OK. The probe must therefore depend on the engine
        pool exactly like ``_check_database`` does, and must succeed
        whenever the pool can run the schema query.
        """
        settings = _make_settings()
        with patch("modulo.api.routes.health.get_settings", return_value=settings):
            engine = AsyncMock()
            conn = AsyncMock()
            conn.__aenter__ = AsyncMock(return_value=conn)
            conn.__aexit__ = AsyncMock(return_value=None)
            engine.connect = lambda: conn
            with patch("modulo.api.routes.health.get_or_create_engine", return_value=engine) as engine_factory:
                result = await _check_checkpointer()

        engine_factory.assert_called_once_with(settings)
        assert result.status == "ok"
        assert result.detail == "checkpointer schema accessible"

    async def test_migrations_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_migrations_timeout_seconds": 0.2})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            # ScriptDirectory patched so the probe does not spawn a real
            # (lingering) migration-tree parse in a worker thread — this
            # test is about the DB-hang timeout, not the parse (FAR-1439).
            patch("modulo.api.routes.health.ScriptDirectory") as script_cls,
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            script_cls.from_config.return_value.get_heads.return_value = {"0001"}
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


class TestEventLoopStallGuard:
    """FAR-1439: the ~2.4s event-loop stall on ``/healthz/ready``.

    Root cause: ``_check_migrations`` ran alembic's
    ``ScriptDirectory.from_config()`` + ``get_heads()`` — a synchronous
    parse/execute of every migration module — inline in the async probe on
    every readiness request, freezing the whole event loop for the parse's
    duration (measured: loop heartbeat lag == parse duration).  These tests
    pin both halves of the fix: the parse runs in a worker thread and is
    served from a process-wide cache afterwards, and a sync block on the
    loop surfaces on the advisory ``event_loop_lag`` check.
    """

    async def test_heads_parse_runs_in_worker_thread_not_on_event_loop(self) -> None:
        """``get_heads()`` must not execute on the event-loop thread.

        A regression to the inline synchronous parse runs it on exactly the
        thread the loop runs on, freezing every other readiness sub-check
        for the parse's duration (~2.4s in production).
        """
        _GET_HEADS_THREAD_IDS.clear()
        loop_thread = threading.get_ident()
        script = MagicMock()
        script.get_heads.side_effect = _recorded_get_heads
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.ScriptDirectory.from_config", return_value=script),
            patch(
                "modulo.api.routes.health.get_or_create_engine",
                return_value=_fake_migrations_engine(["0001"]),
            ),
            patch(
                "modulo.api.routes.health.check_migration_divergence",
                return_value=DivergenceCheckResult(
                    diverged=False,
                    db_revisions={"0001"},
                    repo_revisions={"0001"},
                    orphaned_revisions=set(),
                    detail="clean",
                ),
            ),
        ):
            result = await _check_migrations()
        assert result.status == "ok"
        assert _GET_HEADS_THREAD_IDS, "get_heads never ran — the probe did not parse the migration tree"
        assert _GET_HEADS_THREAD_IDS[0] != loop_thread, (
            "get_heads() ran on the event-loop thread; the alembic parse must go through "
            "asyncio.to_thread so it can never block /healthz/ready (FAR-1439)"
        )

    async def test_heads_parsed_once_then_served_from_process_cache(self) -> None:
        """Second and later probes must reuse the process-wide heads cache."""
        script = MagicMock()
        script.get_heads.return_value = {"0001"}
        clean = DivergenceCheckResult(
            diverged=False,
            db_revisions={"0001"},
            repo_revisions={"0001"},
            orphaned_revisions=set(),
            detail="clean",
        )
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.ScriptDirectory.from_config", return_value=script),
            patch(
                "modulo.api.routes.health.get_or_create_engine",
                return_value=_fake_migrations_engine(["0001"]),
            ),
            patch("modulo.api.routes.health.check_migration_divergence", return_value=clean),
        ):
            first = await _check_migrations()
            second = await _check_migrations()
        assert first.status == "ok"
        assert second.status == "ok"
        assert script.get_heads.call_count == 1, (
            "second probe re-parsed the migration tree instead of using the process cache"
        )

    def test_readiness_reports_advisory_event_loop_lag_check_when_healthy(self, client: TestClient) -> None:
        """A stall-free probe surfaces an ok ``event_loop_lag`` check."""
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        lag = resp.json()["checks"]["event_loop_lag"]
        assert lag["status"] == "ok"
        assert lag["latency_ms"] is not None
        assert lag["latency_ms"] < _LOOP_LAG_DEGRADED_MS
        assert "responsive" in lag["detail"]

    def test_readiness_surfaces_sync_loop_stall_as_degraded_event_loop_lag(self, client: TestClient) -> None:
        """A synchronous call blocking the loop mid-probe — the FAR-1439
        failure mode — must surface on the advisory ``event_loop_lag`` check
        and must NOT gate readiness."""
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", new=_stalling_redis_check),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        body = resp.json()
        lag = body["checks"]["event_loop_lag"]
        assert lag["status"] == "degraded"
        assert lag["latency_ms"] is not None
        assert lag["latency_ms"] >= _LOOP_LAG_DEGRADED_MS
        assert "STALL" in lag["detail"]
        # Advisory only: the stall must not flip overall readiness or gate.
        assert resp.status_code == 200
        assert body["status"] == "ok"

    def test_empty_heads_are_not_cached_so_the_parse_is_retried(self) -> None:
        """A parse yielding no heads must return empty and stay UNcached.

        ``_load_repo_heads`` caches only successful NON-EMPTY loads (mirroring
        ``migration_guard._load_repo_revisions``), so a transient parse that
        yields an empty head set is retried on the next probe rather than
        pinned in the process-wide cache for the process's lifetime.
        """
        from modulo.api.routes import health as health_mod

        script = MagicMock()
        script.get_heads.return_value = set()
        with patch("modulo.api.routes.health.ScriptDirectory.from_config", return_value=script):
            first = health_mod._load_repo_heads(_make_settings())
            second = health_mod._load_repo_heads(_make_settings())
        assert first == set()
        assert second == set()
        assert health_mod._REPO_HEADS_CACHE is None, (
            "an empty head set must not populate the process-wide cache — a transient parse "
            "failure has to be retried on the next probe (FAR-1439)"
        )
        assert script.get_heads.call_count == 2, (
            "an empty (uncached) load must re-parse on the next probe, not be served from cache"
        )


# --- FAR-1445: database-hygiene sub-check --------------------------------
#
# Two thresholds (both settings) and one routing guarantee: hygiene can move
# the overall readiness status to "degraded" but can NEVER produce
# "unavailable", so it can never take the endpoint to 503.

#: Realistic healthy reading — nothing over the dead-tuple floor, freeze age
#: four orders of magnitude under the ceiling.
_HYGIENE_HEALTHY_ROW: dict[str, object] = {
    "freeze_max_age": 200_000_000,
    "frozen_age": 4_821,
    "relname": None,
    "n_live_tup": None,
    "n_dead_tup": None,
    "dead_ratio": None,
}

#: The FAR-1445 incident's own numbers: the runs table at 361,302 dead
#: tuples against 10,411 live (97% bloat, last_autovacuum = never), which
#: went unnoticed for months and surfaced only as 504-ing readiness.
_HYGIENE_INCIDENT_ROW: dict[str, object] = {
    "freeze_max_age": 200_000_000,
    "frozen_age": 4_821,
    "relname": "runs",
    "n_live_tup": 10_411,
    "n_dead_tup": 361_302,
    "dead_ratio": 0.972,
}


def _reading(
    *,
    frozen_age: int = 4_821,
    freeze_max_age: int = 200_000_000,
    worst_table: str | None = None,
    n_live_tup: int | None = None,
    n_dead_tup: int | None = None,
    dead_ratio: float | None = None,
) -> DbHygieneReading:
    """Build a reading from EXPLICIT literals — never derived from the guards."""
    return DbHygieneReading(
        frozen_age=frozen_age,
        freeze_max_age=freeze_max_age,
        worst_table=worst_table,
        n_live_tup=n_live_tup,
        n_dead_tup=n_dead_tup,
        dead_ratio=dead_ratio,
    )


def _grade(
    reading: DbHygieneReading,
    *,
    min_dead_tuples: int = 10_000,
    dead_ratio_threshold: float = 0.60,
) -> tuple[str, str]:
    status, detail = _grade_db_hygiene(
        reading,
        min_dead_tuples=min_dead_tuples,
        dead_ratio_threshold=dead_ratio_threshold,
    )
    return status, detail


class _FakeHygieneResult:
    """Result double exposing just the ``mappings().first()`` read the probe does."""

    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> "_FakeHygieneResult":
        return self

    def first(self) -> dict[str, object] | None:
        return self._row


class _FakeHygieneEngine:
    """Engine double returning a canned hygiene row (mirrors _FakeMigrationEngine)."""

    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row
        self.seen_params: dict[str, object] | None = None

    def connect(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _stmt: object, params: dict[str, object] | None = None) -> _FakeHygieneResult:
        self.seen_params = params
        return _FakeHygieneResult(self._row)


class _ExplodingHygieneEngine:
    """Engine double whose probe raises — the check-could-not-run path."""

    def connect(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _stmt: object, params: dict[str, object] | None = None) -> _FakeHygieneResult:
        raise RuntimeError("statistics views unreadable")


class TestDbHygieneGradingBoundaries:
    """Threshold boundaries — every boundary pinned as an explicit literal.

    The numbers are written out, never recomputed from the expression the
    production code uses: a test that mirrors the guard only proves the guard
    agrees with itself.
    """

    # --- absolute dead-tuple floor (default 10,000) ---------------------

    def test_ratio_only_dead_rows_below_floor_are_ok(self) -> None:
        """99.99% dead but only 9,999 dead rows — churn, not bloat → ok."""
        status, detail = _grade(
            _reading(worst_table="small_events", n_live_tup=1, n_dead_tup=9_999, dead_ratio=0.9999),
        )
        assert status == "ok"
        assert "9,999" in detail

    def test_dead_rows_exactly_at_floor_are_degraded(self) -> None:
        status, detail = _grade(
            _reading(worst_table="small_events", n_live_tup=1, n_dead_tup=10_000, dead_ratio=0.9999),
        )
        assert status == "degraded"
        assert "10,000" in detail

    def test_dead_rows_above_floor_are_degraded(self) -> None:
        status, _detail = _grade(
            _reading(worst_table="small_events", n_live_tup=1, n_dead_tup=10_001, dead_ratio=0.9999),
        )
        assert status == "degraded"

    # --- dead-tuple ratio (default 0.60) --------------------------------

    def test_ratio_just_below_threshold_is_ok(self) -> None:
        """59.99% dead on a table well over the floor → within thresholds."""
        status, detail = _grade(
            _reading(worst_table="runs", n_live_tup=40_001, n_dead_tup=60_000, dead_ratio=0.5999),
        )
        assert status == "ok"
        assert "within thresholds" in detail

    def test_ratio_exactly_at_threshold_is_degraded(self) -> None:
        status, detail = _grade(
            _reading(worst_table="runs", n_live_tup=40_000, n_dead_tup=60_000, dead_ratio=0.60),
        )
        assert status == "degraded"
        assert "60% threshold" in detail
        assert "runs" in detail

    def test_ratio_just_above_threshold_is_degraded(self) -> None:
        status, _detail = _grade(
            _reading(worst_table="runs", n_live_tup=40_000, n_dead_tup=60_001, dead_ratio=0.6001),
        )
        assert status == "degraded"

    def test_dead_rows_over_floor_but_ratio_under_threshold_is_ok(self) -> None:
        """400k dead rows on a 1M-row table is under the 60% threshold — the
        floor alone must not flag a table autovacuum services normally."""
        status, _detail = _grade(
            _reading(worst_table="huge", n_live_tup=600_000, n_dead_tup=400_000, dead_ratio=0.40),
        )
        assert status == "ok"

    # --- freeze (wraparound) age against the server's own ceiling -------

    def test_freeze_age_just_below_warning_tier_is_ok(self) -> None:
        status, detail = _grade(_reading(frozen_age=99_999_999, freeze_max_age=200_000_000))
        assert status == "ok"
        assert "freeze age 99,999,999/200,000,000" in detail

    def test_freeze_age_exactly_at_warning_tier_is_degraded(self) -> None:
        status, detail = _grade(_reading(frozen_age=100_000_000, freeze_max_age=200_000_000))
        assert status == "degraded"
        assert "100,000,000" in detail
        assert "autovacuum_freeze_max_age" in detail

    def test_freeze_age_just_above_warning_tier_is_degraded(self) -> None:
        status, _detail = _grade(_reading(frozen_age=100_000_001, freeze_max_age=200_000_000))
        assert status == "degraded"

    def test_freeze_age_at_the_ceiling_is_degraded(self) -> None:
        status, detail = _grade(_reading(frozen_age=200_000_000, freeze_max_age=200_000_000))
        assert status == "degraded"
        assert "AT/ABOVE autovacuum_freeze_max_age" in detail

    def test_minimum_legal_freeze_ceiling_still_grades(self) -> None:
        """There is no "operator-disabled ceiling" state to special-case:
        autovacuum_freeze_max_age is server-start-only and Postgres rejects 0
        ("FATAL: 0 is outside the valid range for parameter
        \"autovacuum_freeze_max_age\" (100000 .. 2000000000)"), so the smallest
        legal ceiling is 100,000 and it grades like any other."""
        ok_status, ok_detail = _grade(_reading(frozen_age=49_999, freeze_max_age=100_000))
        assert ok_status == "ok"
        assert "disabled" not in ok_detail
        bad_status, bad_detail = _grade(_reading(frozen_age=100_000, freeze_max_age=100_000))
        assert bad_status == "degraded"
        assert "AT/ABOVE autovacuum_freeze_max_age" in bad_detail
        assert "disabled" not in bad_detail

    # --- the regression this check exists for ---------------------------

    def test_incident_bloat_numbers_are_flagged(self) -> None:
        """Negative control: the FAR-1445 incident's own row must be caught.

        10,411 live / 361,302 dead (97.2% dead) on ``runs`` sat unnoticed
        for months and only surfaced as 504-ing readiness — this is the
        exact state the check exists to report.
        """
        status, detail = _grade(
            _reading(worst_table="runs", n_live_tup=10_411, n_dead_tup=361_302, dead_ratio=0.972),
        )
        assert status == "degraded"
        assert "runs" in detail
        assert "361,302" in detail
        assert "371,713" in detail
        assert "97.2% dead" in detail

    def test_healthy_database_grades_ok(self) -> None:
        status, detail = _grade(_reading())
        assert status == "ok"
        assert "no table over the 10,000-dead floor" in detail
        assert "freeze age 4,821/200,000,000" in detail

    def test_grading_can_never_produce_unavailable(self) -> None:
        """Structural guarantee: the return type excludes ``unavailable``, so
        a hygiene report can never 503 /healthz/ready on its own."""
        from typing import get_type_hints

        return_hint = get_type_hints(_grade_db_hygiene)["return"]
        allowed = set(return_hint.__args__[0].__args__)
        assert allowed == {"ok", "degraded"}


class TestDbHygieneCheck:
    """``_check_db_hygiene`` end-to-end over a canned query result."""

    async def _run(
        self, row: dict[str, object] | None, **settings_updates: object
    ) -> tuple[CheckResult, _FakeHygieneEngine]:
        settings = _make_settings().model_copy(update=settings_updates)
        engine = _FakeHygieneEngine(row)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=engine),
        ):
            return await _check_db_hygiene(), engine

    async def test_healthy_database_grades_ok(self) -> None:
        result, _engine = await self._run(_HYGIENE_HEALTHY_ROW)
        assert result.status == "ok"
        assert result.detail is not None
        assert "freeze age 4,821/200,000,000" in result.detail
        assert result.latency_ms is not None

    async def test_incident_bloat_grades_degraded_and_names_the_table(self) -> None:
        result, _engine = await self._run(_HYGIENE_INCIDENT_ROW)
        assert result.status == "degraded"
        assert result.detail is not None
        assert "runs" in result.detail
        assert "361,302" in result.detail

    async def test_probe_binds_the_configured_dead_tuple_floor(self) -> None:
        result, engine = await self._run(_HYGIENE_HEALTHY_ROW)
        assert result.status == "ok"
        assert engine.seen_params == {"min_dead": 10_000}

    async def test_probe_binds_an_operator_lowered_floor(self) -> None:
        settings_updates = {"modulo_health_db_hygiene_min_dead_tuples": 500}
        result, engine = await self._run(_HYGIENE_HEALTHY_ROW, **settings_updates)
        assert result.status == "ok"
        assert engine.seen_params == {"min_dead": 500}

    async def test_uses_shared_engine_pool_not_a_fresh_connection(self) -> None:
        """FAR-1445 cost contract: one pooled-engine read, never a per-probe
        connection (the FAR-1426 failure mode on the checkpointer probe)."""
        settings = _make_settings()
        engine = _FakeHygieneEngine(_HYGIENE_HEALTHY_ROW)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=engine) as engine_factory,
        ):
            result = await _check_db_hygiene()
        engine_factory.assert_called_once_with(settings)
        assert result.status == "ok"

    async def test_timeout_reports_degraded_never_unavailable(self) -> None:
        settings_updates = {"modulo_health_db_hygiene_timeout_seconds": 0.2}
        with (
            patch(
                "modulo.api.routes.health.get_settings",
                return_value=_make_settings().model_copy(update=settings_updates),
            ),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            result = await _check_db_hygiene()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in (result.detail or "").lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_probe_failure_reports_degraded_never_unavailable(self) -> None:
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_ExplodingHygieneEngine()),
        ):
            result = await _check_db_hygiene()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "could not run" in result.detail

    async def test_probe_returning_no_row_is_degraded_never_ok(self) -> None:
        """``row = None`` from ``mappings().first()`` (the query yielded
        nothing): the probe raises and the check reports degraded — a check
        that produced NO reading must never read as a clean one."""
        result, _engine = await self._run(None)
        assert result.status == "degraded"
        assert result.detail is not None
        assert "could not run" in result.detail

    async def test_dead_ratio_setting_changes_the_grade_end_to_end(self) -> None:
        """The configured ratio must flow settings → probe → grade.

        The SAME incident row grades degraded at the default 0.60 and ok when
        the operator raises the threshold above it — hardcoding 0.60 inside
        the check would still pass the rest of the suite but fails here.
        """
        default_result, _ = await self._run(_HYGIENE_INCIDENT_ROW)
        assert default_result.status == "degraded"

        raised, _ = await self._run(_HYGIENE_INCIDENT_ROW, modulo_health_db_hygiene_dead_ratio=0.99)
        assert raised.status == "ok"
        assert raised.detail is not None
        assert "within thresholds" in raised.detail

        # The other direction: a 40%-dead table over the floor is within
        # thresholds at the default 0.60, degraded when tightened to 0.30.
        mid_dead_row: dict[str, object] = {
            **_HYGIENE_INCIDENT_ROW,
            "relname": "half_dead",
            "n_live_tup": 600_000,
            "n_dead_tup": 400_000,
            "dead_ratio": 0.40,
        }
        at_default, _ = await self._run(mid_dead_row)
        assert at_default.status == "ok"

        tightened, _ = await self._run(mid_dead_row, modulo_health_db_hygiene_dead_ratio=0.30)
        assert tightened.status == "degraded"
        assert tightened.detail is not None
        assert "half_dead" in tightened.detail

    def test_settings_defaults(self) -> None:
        settings = _make_settings()
        assert settings.modulo_health_db_hygiene_timeout_seconds == 1.0
        assert settings.modulo_health_db_hygiene_min_dead_tuples == 10_000
        assert settings.modulo_health_db_hygiene_dead_ratio == pytest.approx(0.60)

    def test_db_hygiene_defaults_to_its_own_small_budget(self) -> None:
        """Unlike its neighbours this override defaults to 1s, not 0: the
        probe is one statistics-view read, so it must not inherit the 5s
        global budget that the 504-ing gateway endpoint already runs on."""
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 2.5})
        assert _per_check_timeout(settings, "modulo_health_db_hygiene_timeout_seconds") == 1.0

    def test_per_check_timeout_falls_back_to_global_when_cleared(self) -> None:
        """0 keeps the documented escape hatch every other override has."""
        settings = _make_settings().model_copy(
            update={
                "modulo_health_timeout_seconds": 2.5,
                "modulo_health_db_hygiene_timeout_seconds": 0.0,
            }
        )
        assert _per_check_timeout(settings, "modulo_health_db_hygiene_timeout_seconds") == 2.5

    def test_per_check_timeout_override_wins(self) -> None:
        settings = _make_settings().model_copy(
            update={
                "modulo_health_timeout_seconds": 2.5,
                "modulo_health_db_hygiene_timeout_seconds": 0.5,
            }
        )
        assert _per_check_timeout(settings, "modulo_health_db_hygiene_timeout_seconds") == 0.5


class TestDbHygieneAggregation:
    """FAR-1445: a hygiene finding degrades the report, never the status code."""

    def test_degraded_hygiene_keeps_http_200_and_overall_degraded(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch(
                "modulo.api.routes.health._check_db_hygiene",
                AsyncMock(return_value=_degraded_check('DEAD-TUPLE BLOAT: worst table "runs"')),
            ),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        # The Fly service check and every deploy gate key on the status CODE:
        # a hygiene report must never take the machine out of rotation.
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["db_hygiene"]["status"] == "degraded"
        assert body["checks"]["database"]["status"] == "ok"

    def test_healthy_hygiene_keeps_overall_ok(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch("modulo.api.routes.health._check_db_hygiene", AsyncMock(return_value=_ok_check("db_hygiene"))),
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
        assert body["checks"]["db_hygiene"]["status"] == "ok"

    def test_hygiene_cannot_supply_the_503(self, client: TestClient) -> None:
        """Even the worst hygiene outcome leaves the 503 to the real gates:
        with hygiene degraded AND every other gate ok, the endpoint is 200."""
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
            patch(
                "modulo.api.routes.health._check_db_hygiene",
                AsyncMock(return_value=CheckResult(status="degraded", detail="DEAD-TUPLE BLOAT: runs")),
            ),
            patch("modulo.api.routes.health._check_saq_workers", AsyncMock(return_value=_ok_check("saq_workers"))),
            patch("modulo.api.routes.health._check_system_crons", AsyncMock(return_value=_ok_check("system_crons"))),
            patch(
                "modulo.api.routes.health._check_dispatcher_reconcile",
                AsyncMock(return_value=_ok_check("dispatcher_reconcile")),
            ),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] != "unavailable"
