"""Unit tests for the dispatcher_reconcile readiness check (cross-process stats).

The dispatcher_reconcile system cron runs in the SYSTEM WORKER process; the
/healthz/ready check runs in the WEB process. The check must read the shared
Redis key the cron persists every tick (the cron_helpers in-process dict is
worker-local and invisible to the health check) — these tests lock that in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from modulo.api.routes.health import _check_dispatcher_reconcile, _check_stale_run_recovery
from modulo.core import cron_helpers as ch
from modulo.settings import Settings


def _make_settings(redis_url: str = "redis://localhost:6379/0") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url=redis_url,
    )


class _FakeStatsRedis:
    """In-memory redis double: get/set over the reconcile stats key."""

    def __init__(self, blob: bytes | None = None, fail_get: bool = False) -> None:
        self._blob = blob
        self._fail_get = fail_get

    async def get(self, _key: str) -> bytes | None:
        if self._fail_get:
            raise RuntimeError("redis down")
        return self._blob

    async def set(self, _key: str, value: str, ex: int | None = None) -> None:
        self._blob = value.encode()

    async def aclose(self) -> None:
        return None


def _fresh_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "scanned": 3,
        "repaired": 1,
        "skipped": 2,
        "redis_errors": 0,
        "deduped": 0,
        "nodeless_failed": 0,
        "capacity_deferred": 0,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestCheckDispatcherReconcile:
    @pytest.mark.asyncio
    async def test_never_run_unavailable(self) -> None:
        """FAR-199: a reconcile that has never run (Redis reachable, key
        missing) is unavailable — the system-worker cron is dead or its stats
        persistence failed, so readiness must gate rather than cut over."""
        fake = _FakeStatsRedis(blob=None)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "unavailable"
        assert "has never run" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_run_ok(self) -> None:
        fake = _FakeStatsRedis(blob=_fresh_payload().encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"
        assert "scanned=3" in result.detail

    @pytest.mark.asyncio
    async def test_stale_run_degraded(self) -> None:
        """One-missed-tick staleness (120s, below the 300s unavailable tier) is
        degraded, not unavailable — short staleness stays advisory (FAR-199)."""
        stale = _fresh_payload(last_run_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat())
        fake = _FakeStatsRedis(blob=stale.encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "degraded"
        assert "stale" in result.detail
        assert "last_run_at=" in result.detail

    @pytest.mark.asyncio
    async def test_long_stale_unavailable(self) -> None:
        """FAR-199: reconcile stale past the 300s unavailable tier (6 min) is
        unavailable and carries the reconcile detail (last_run_at, scanned) so
        the wedge symptom is visible in /healthz/ready output."""
        stale = _fresh_payload(
            last_run_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
            scanned=7,
            repaired=3,
        )
        fake = _FakeStatsRedis(blob=stale.encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "unavailable"
        assert "stale" in result.detail
        assert "last_run_at=" in result.detail
        assert "scanned=7" in result.detail
        assert "repaired=3" in result.detail

    @pytest.mark.asyncio
    async def test_unparsable_last_run_at_degraded(self) -> None:
        fake = _FakeStatsRedis(blob=_fresh_payload(last_run_at="not-a-date").encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "degraded"
        assert "unparsable" in result.detail

    @pytest.mark.asyncio
    async def test_redis_read_error_fails_open(self) -> None:
        fake = _FakeStatsRedis(fail_get=True)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"
        assert "unavailable" in result.detail

    @pytest.mark.asyncio
    async def test_cron_written_stats_reported_ok(self) -> None:
        """End-to-end fix exercise: the system worker persists its outcome via
        write_dispatcher_reconcile_stats, then the health check reads the SAME
        key and reports ok — not 'has never run'."""
        fake = _FakeStatsRedis(blob=None)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            await ch.write_dispatcher_reconcile_stats(fake, {"scanned": 2, "repaired": 0, "skipped": 2})
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"
        assert "scanned=2" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_run_detail_surfaces_new_counters(self) -> None:
        """The readiness detail surfaces the D1 counters (terminalizers,
        enqueue-failed recovery) even when zero."""
        fake = _FakeStatsRedis(
            blob=_fresh_payload(
                claim_cap_terminalized=1,
                nodeless_failed=2,
                enqueue_failed_redispatched=3,
                age_terminalized=4,
            ).encode()
        )
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"
        assert "claim_cap_terminalized=1" in result.detail
        assert "nodeless_failed=2" in result.detail
        assert "enqueue_failed_redispatched=3" in result.detail
        assert "age_terminalized=4" in result.detail


def _srr_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "recovered": 2,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestCheckStaleRunRecovery:
    """D1 advisory check — the stale-run sweep (every 5 min) persists its
    outcome to a shared Redis key; a missing or >15min-stale key warns without
    gating readiness."""

    @pytest.mark.asyncio
    async def test_never_run_degraded(self) -> None:
        fake = _FakeStatsRedis(blob=None)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_stale_run_recovery()
        assert result.status == "degraded"
        assert "has never run" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_run_ok(self) -> None:
        fake = _FakeStatsRedis(blob=_srr_payload().encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_stale_run_recovery()
        assert result.status == "ok"
        assert "recovered=2" in result.detail

    @pytest.mark.asyncio
    async def test_stale_run_degraded(self) -> None:
        stale_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        fake = _FakeStatsRedis(blob=_srr_payload(last_run_at=stale_at).encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_stale_run_recovery()
        assert result.status == "degraded"
        assert "stale" in result.detail

    @pytest.mark.asyncio
    async def test_unparsable_degraded(self) -> None:
        fake = _FakeStatsRedis(blob=b"not-json")
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_stale_run_recovery()
        assert result.status == "degraded"
        assert "unparsable" in result.detail

    @pytest.mark.asyncio
    async def test_redis_read_error_fails_open(self) -> None:
        fake = _FakeStatsRedis(fail_get=True)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_stale_run_recovery()
        assert result.status == "ok"
        assert "unavailable" in result.detail
