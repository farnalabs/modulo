"""Tests for readiness endpoint — aggregation logic, degraded/unavailable status, and check structure."""

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock, patch

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


@pytest.fixture
def reset_stale_probes() -> Generator[None, None, None]:
    import modulo.api.routes.health as health_mod

    health_mod._consecutive_stale_probes = 0
    yield
    health_mod._consecutive_stale_probes = 0


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
        this_host: str = "machine-a",
    ) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": saq_hard_gate})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])) as queues,
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
            patch.dict("os.environ", {"FLY_MACHINE_ID": this_host}, clear=False),
        ):
            queues.return_value = ["runs", "system"]

            async def _live_side_effect(qname: str) -> set[str]:
                return live_by_queue.get(qname, set())

            live.side_effect = _live_side_effect
            return await _check_saq_workers()

    async def test_live_on_both_queues_ok(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": {"machine-a"}, "system": {"machine-a"}})
        assert result.status == "ok"

    async def test_dead_runs_worker_not_masked_by_live_system(self, reset_stale_probes: None) -> None:
        # runs worker dead, system worker live — the machine-scoped gate MUST
        # fail because THIS machine is stale on the runs queue.
        result = await self._run({"runs": set(), "system": {"machine-a"}})
        assert result.status == "degraded"
        assert "runs" in result.detail

    async def test_dead_system_worker_not_masked_by_live_runs(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": {"machine-a"}, "system": set()})
        assert result.status == "degraded"
        assert "system" in result.detail

    async def test_other_machine_live_does_not_cover_this_machine(self, reset_stale_probes: None) -> None:
        # machine-b is live on both queues; THIS machine (machine-a) is stale.
        result = await self._run({"runs": {"machine-b"}, "system": {"machine-b"}})
        assert result.status == "degraded"
        assert "machine-a" in result.detail

    async def test_stale_four_probes_unavailable_when_gated(self, reset_stale_probes: None) -> None:
        result: CheckResult | None = None
        for _ in range(4):
            result = await self._run({"runs": set(), "system": set()})
        assert result is not None
        assert result.status == "unavailable"

    async def test_hard_gate_false_staleness_alert_only(self, reset_stale_probes: None) -> None:
        result = await self._run({"runs": set(), "system": set()}, saq_hard_gate=False)
        assert result.status == "ok"


class TestCheckSaqWorkersEndToEnd:
    """_check_saq_workers through the REAL _live_worker_hostnames (fake Redis)
    — the ok → degraded → unavailable transition is covered without patching
    the mechanism. Uses fake worker_info hashes with millisecond scores."""

    NOW_MS = 1_700_000_000_000

    async def _call(
        self,
        stats: dict[str, int],
        blobs: dict[str, str],
        *,
        this_host: str = "machine-a",
    ) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": True})
        fake = _PerQueueFakeStatsRedis(stats, blobs)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW_MS / 1000),
            patch.dict("os.environ", {"FLY_MACHINE_ID": this_host}, clear=False),
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
        assert "machine-a" in result.detail

    async def test_stale_on_one_queue_degraded_not_unavailable_yet(self, reset_stale_probes: None) -> None:
        # machine-a live on runs only; system worker dead -> degraded, never ok.
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
    """Fleet-wide SAQ worker gate — used by ``app`` machines (which run no workers)."""

    async def _run(self, live_by_queue: dict[str, set[str]], *, saq_hard_gate: bool = True) -> CheckResult:
        settings = _make_settings().model_copy(update={"saq_hard_gate": saq_hard_gate})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])) as queues,
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
        ):
            queues.return_value = ["runs", "system"]

            async def _live_side_effect(qname: str) -> set[str]:
                return live_by_queue.get(qname, set())

            live.side_effect = _live_side_effect
            return await _check_fleet_saq_workers()

    async def test_any_live_worker_on_each_queue_ok(self) -> None:
        # A worker machine elsewhere in the fleet covers app readiness.
        result = await self._run({"runs": {"machine-b"}, "system": {"machine-b"}})
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_no_worker_on_one_queue_unavailable(self) -> None:
        result = await self._run({"runs": set(), "system": {"machine-b"}})
        assert result.status == "unavailable"
        assert "runs" in result.detail

    async def test_no_worker_anywhere_unavailable(self) -> None:
        result = await self._run({"runs": set(), "system": set()})
        assert result.status == "unavailable"

    async def test_hard_gate_false_alert_only(self) -> None:
        result = await self._run({"runs": set(), "system": set()}, saq_hard_gate=False)
        assert result.status == "ok"

    async def test_redis_read_error_fails_open(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])),
            patch(
                "modulo.api.routes.health._live_worker_hostnames",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            result = await _check_fleet_saq_workers()
        assert result.status == "ok"


class TestCheckSaqWorkersProcessGroup:
    """Process-group routing: ``app`` -> fleet gate, ``worker``/unset -> machine-scoped gate."""

    async def test_app_machine_uses_fleet_gate(self) -> None:
        # FLY_PROCESS_GROUP=app + a worker live on ANOTHER machine -> ok, even
        # though THIS app machine is not live on any queue.
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])) as queues,
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
            patch.dict("os.environ", {"FLY_MACHINE_ID": "app-1", "FLY_PROCESS_GROUP": "app"}, clear=False),
        ):
            queues.return_value = ["runs", "system"]

            async def _live_side_effect(qname: str) -> set[str]:
                return {"machine-b"} if qname in ("runs", "system") else set()

            live.side_effect = _live_side_effect
            result = await _check_saq_workers()
        assert result.status == "ok"
        assert "fleet" in result.detail

    async def test_app_machine_fleet_outage_unavailable(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])) as queues,
            patch("modulo.api.routes.health._live_worker_hostnames", return_value=set()),
            patch.dict("os.environ", {"FLY_MACHINE_ID": "app-1", "FLY_PROCESS_GROUP": "app"}, clear=False),
        ):
            queues.return_value = ["runs", "system"]
            result = await _check_saq_workers()
        assert result.status == "unavailable"

    async def test_worker_machine_keeps_machine_scoped_gate(self, reset_stale_probes: None) -> None:
        # FLY_PROCESS_GROUP=worker + THIS machine stale on runs -> degraded,
        # regardless of a live worker elsewhere on the runs queue.
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health._configured_queues", AsyncMock(return_value=["runs", "system"])) as queues,
            patch("modulo.api.routes.health._live_worker_hostnames") as live,
            patch.dict("os.environ", {"FLY_MACHINE_ID": "machine-a", "FLY_PROCESS_GROUP": "worker"}, clear=False),
        ):
            queues.return_value = ["runs", "system"]

            async def _live_side_effect(qname: str) -> set[str]:
                # machine-b is live on runs but machine-a is not.
                if qname == "runs":
                    return {"machine-b"}
                return {"machine-a"}

            live.side_effect = _live_side_effect
            result = await _check_saq_workers()
        assert result.status == "degraded"
        assert "machine-a" in result.detail


class _FakeHeartbeatRedis:
    """Fake redis client for ``saq:cron:heartbeat:fire_due_triggers:*`` reads."""

    def __init__(self, heartbeats: dict[str, str]) -> None:
        self._heartbeats = heartbeats

    async def keys(self, pattern: str) -> list[bytes]:
        prefix = pattern.rstrip("*")
        return [k.encode() for k in self._heartbeats if k.startswith(prefix)]

    async def get(self, key: bytes | str) -> bytes | None:
        decoded = key.decode() if isinstance(key, bytes) else key
        value = self._heartbeats.get(decoded)
        return value.encode() if value is not None else None

    async def aclose(self) -> None:
        return None


class TestCheckFleetSystemCrons:
    """Fleet-wide fire_due_triggers cron liveness — used by ``app`` machines."""

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

    async def test_fresh_heartbeat_on_any_machine_ok(self) -> None:
        result = await self._run({"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 30)})
        assert result.status == "ok"

    async def test_only_stale_heartbeat_unavailable(self) -> None:
        result = await self._run({"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 121)})
        assert result.status == "unavailable"

    async def test_no_heartbeat_anywhere_unavailable(self) -> None:
        result = await self._run({})
        assert result.status == "unavailable"

    async def test_hard_gate_false_alert_only(self) -> None:
        result = await self._run(
            {"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 121)},
            saq_hard_gate=False,
        )
        assert result.status == "ok"

    async def test_redis_read_error_fails_open(self) -> None:
        settings = _make_settings()

        class _BrokenRedis:
            async def keys(self, _pattern: str) -> list[bytes]:
                raise RuntimeError("redis down")

            async def aclose(self) -> None:
                return None

        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=_BrokenRedis()),
        ):
            result = await _check_fleet_system_crons()
        assert result.status == "ok"


class TestCheckSystemCronsProcessGroup:
    """Process-group routing for the cron watchdog: ``app`` -> fleet, ``worker`` -> machine-scoped."""

    NOW = 1_700_000_000.0

    async def test_app_machine_uses_fleet_gate(self) -> None:
        # App machine with no local heartbeat is ok as long as another machine's
        # scheduler is fresh.
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

    async def test_worker_machine_keeps_machine_scoped_gate(self) -> None:
        # Worker machine with NO local heartbeat is unavailable even though
        # another machine's scheduler is alive.
        heartbeats = {"saq:cron:heartbeat:fire_due_triggers:machine-b": str(self.NOW - 30)}
        fake = _FakeHeartbeatRedis(heartbeats)
        settings = _make_settings()
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
            patch("modulo.api.routes.health.time.time", return_value=self.NOW),
            patch(
                "modulo.api.routes.health._START_TIME",
                datetime.now(UTC) - timedelta(hours=1),
            ),
            patch.dict("os.environ", {"FLY_MACHINE_ID": "machine-a", "FLY_PROCESS_GROUP": "worker"}, clear=False),
        ):
            result = await _check_system_crons()
        assert result.status == "unavailable"
        assert "machine-a" in result.detail
