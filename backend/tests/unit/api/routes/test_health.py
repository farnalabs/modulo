"""Unit tests for the dispatcher_reconcile readiness check (cross-process stats).

The dispatcher_reconcile system cron runs in the SYSTEM WORKER process; the
/healthz/ready check runs in the WEB process. The check must read the shared
Redis key the cron persists every tick (the cron_helpers in-process dict is
worker-local and invisible to the health check) â€” these tests lock that in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from modulo.api.routes.health import (
    _check_dispatcher_reconcile,
    _check_migrations,
    _check_runner_health_probe,
    _check_runner_workspace_reconcile,
    _check_slot_reconciliation,
    _check_stale_run_recovery,
)
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
        missing) is unavailable â€” the system-worker cron is dead or its stats
        persistence failed, so readiness must gate rather than cut over."""
        fake = _FakeStatsRedis(blob=None)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "unavailable"
        assert result.detail is not None
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
        assert result.detail is not None
        assert "scanned=3" in result.detail

    @pytest.mark.asyncio
    async def test_stale_run_degraded(self) -> None:
        """One-missed-tick staleness (120s, below the 300s unavailable tier) is
        degraded, not unavailable â€” short staleness stays advisory (FAR-199)."""
        stale = _fresh_payload(last_run_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat())
        fake = _FakeStatsRedis(blob=stale.encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "stale" in result.detail
        assert result.detail is not None
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
        assert result.detail is not None
        assert "stale" in result.detail
        assert result.detail is not None
        assert "last_run_at=" in result.detail
        assert result.detail is not None
        assert "scanned=7" in result.detail
        assert result.detail is not None
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
        assert result.detail is not None
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
        assert result.detail is not None
        assert "unavailable" in result.detail

    @pytest.mark.asyncio
    async def test_cron_written_stats_reported_ok(self) -> None:
        """End-to-end fix exercise: the system worker persists its outcome via
        write_dispatcher_reconcile_stats, then the health check reads the SAME
        key and reports ok â€” not 'has never run'."""
        fake = _FakeStatsRedis(blob=None)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            await ch.write_dispatcher_reconcile_stats(fake, {"scanned": 2, "repaired": 0, "skipped": 2})  # type: ignore[arg-type]
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"
        assert result.detail is not None
        assert "scanned=2" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_run_detail_surfaces_new_counters(self) -> None:
        """The readiness detail surfaces the D1 counters (terminalizers,
        enqueue-failed recovery) and the FAR-714 claimed-but-never-dispatched
        counter even when zero."""
        fake = _FakeStatsRedis(
            blob=_fresh_payload(
                claim_cap_terminalized=1,
                nodeless_failed=2,
                enqueue_failed_redispatched=3,
                age_terminalized=4,
                claimed_but_never_dispatched=5,
            ).encode()
        )
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"
        assert result.detail is not None
        assert "claim_cap_terminalized=1" in result.detail
        assert result.detail is not None
        assert "nodeless_failed=2" in result.detail
        assert result.detail is not None
        assert "enqueue_failed_redispatched=3" in result.detail
        assert result.detail is not None
        assert "age_terminalized=4" in result.detail
        assert result.detail is not None
        assert "claimed_but_never_dispatched=5" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_timeout_status_degraded(self) -> None:
        """FAR-746: a fresh stats blob with status='timeout' (inner deadline
        fired) must return 'degraded' (non-gating) â€” a partially-working
        background sweep degrades the health report, not prod routing."""
        fake = _FakeStatsRedis(blob=_fresh_payload(status="timeout", last_error="TimeoutError: ...").encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "status=timeout" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_failed_status_degraded(self) -> None:
        """FAR-746: a fresh stats blob with status='failed' (unexpected
        exception) must return 'degraded' (non-gating)."""
        fake = _FakeStatsRedis(blob=_fresh_payload(status="failed", last_error="RuntimeError: boom").encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "status=failed" in result.detail

    @pytest.mark.asyncio
    async def test_stale_timeout_still_unavailable(self) -> None:
        """FAR-746: staleness >300s wins even when status='timeout' â€” the
        system worker's cron is dead, not just partially failing."""
        stale = _fresh_payload(
            last_run_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
            status="timeout",
            last_error="TimeoutError: ...",
        )
        fake = _FakeStatsRedis(blob=stale.encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "unavailable"
        assert result.detail is not None
        assert "stale" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_ok_status_ok(self) -> None:
        """FAR-746: a fresh stats blob with status='ok' (or absent, default)
        returns 'ok' â€” the normal path."""
        fake = _FakeStatsRedis(blob=_fresh_payload().encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"

    @pytest.mark.asyncio
    async def test_fresh_legacy_blob_without_status_ok(self) -> None:
        """FAR-746 backward compat: a pre-FAR-746 stats blob without the
        'status' key defaults to 'ok' (not degraded)."""
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
        fake = _FakeStatsRedis(blob=json.dumps(payload).encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_dispatcher_reconcile()
        assert result.status == "ok"


def _srr_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "recovered": 2,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _rhp_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "orgs_probed": 1,
        "orgs_failed": 0,
        "transitions": 0,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestSweepStatsAdvisoryError:
    """FAR-824: a stats payload that carries a non-null ``error`` must degrade
    the advisory â€” a cron that runs but FAILS must not read like a healthy
    one (FAR-808: runner_health_probe reported ok for hours while its stats
    carried ``error: probe_failed, orgs_probed: 0``)."""

    @pytest.mark.asyncio
    async def test_fresh_payload_with_error_reported_ok_before_fix(self) -> None:
        """The bug: a FRESH last_run_at masks the payload error â€” this is the
        exact regime the FAR-808 incident hit (fresh last_run_at, error set)."""
        fake = _FakeStatsRedis(blob=_rhp_payload(error="probe_failed", orgs_probed=0).encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_runner_health_probe()
        assert result.status != "ok"
        assert result.status == "degraded"
        assert result.detail is not None
        assert "probe_failed" in result.detail

    @pytest.mark.asyncio
    async def test_error_payload_degrades_via_sweep_wrapper(self) -> None:
        """The general fix applies through the shared reader to every system
        cron reported on this path (here via the stale_run_recovery wrapper)."""
        fake = _FakeStatsRedis(blob=_srr_payload(error="sweep_exploded").encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_stale_run_recovery()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "sweep_exploded" in result.detail

    @pytest.mark.asyncio
    async def test_clean_fresh_payload_still_ok(self) -> None:
        fake = _FakeStatsRedis(blob=_rhp_payload().encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_runner_health_probe()
        assert result.status == "ok"
        assert result.detail is not None
        assert "orgs_probed=1" in result.detail

    @pytest.mark.asyncio
    async def test_stale_payload_still_non_ok(self) -> None:
        stale_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        fake = _FakeStatsRedis(blob=_rhp_payload(last_run_at=stale_at).encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_runner_health_probe()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "stale" in result.detail

    @pytest.mark.asyncio
    async def test_error_wins_even_when_stale(self) -> None:
        """A payload with an error AND a stale last_run_at still degrades and
        surfaces the error text, not just the staleness."""
        stale_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        fake = _FakeStatsRedis(blob=_rhp_payload(last_run_at=stale_at, error="probe_failed", orgs_probed=0).encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_runner_health_probe()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "probe_failed" in result.detail


class TestCheckStaleRunRecovery:
    """D1 advisory check â€” the stale-run sweep (every 5 min) persists its
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
        assert result.detail is not None
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
        assert result.detail is not None
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
        assert result.detail is not None
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
        assert result.detail is not None
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
        assert result.detail is not None
        assert "unavailable" in result.detail


def _sr_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "released": 2,
        "per_pipeline": {"p1": 2},
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestCheckSlotReconciliation:
    """FAR-604 F6 advisory check â€” the slot-reconciliation sweep (every 5
    min) persists its outcome to a shared Redis key; a missing or
    >15min-stale key means a silently dead sweep that would re-open the
    FAR-604 admission wedge invisibly, so it warns without gating readiness."""

    @pytest.mark.asyncio
    async def test_never_run_degraded(self) -> None:
        fake = _FakeStatsRedis(blob=None)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_slot_reconciliation()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "has never run" in result.detail

    @pytest.mark.asyncio
    async def test_fresh_run_ok(self) -> None:
        fake = _FakeStatsRedis(blob=_sr_payload().encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_slot_reconciliation()
        assert result.status == "ok"
        assert result.detail is not None
        assert "released=2" in result.detail

    @pytest.mark.asyncio
    async def test_stale_run_degraded(self) -> None:
        stale_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        fake = _FakeStatsRedis(blob=_sr_payload(last_run_at=stale_at).encode())
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_slot_reconciliation()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "stale" in result.detail

    @pytest.mark.asyncio
    async def test_unparsable_degraded(self) -> None:
        fake = _FakeStatsRedis(blob=b"not-json")
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_slot_reconciliation()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "unparsable" in result.detail

    @pytest.mark.asyncio
    async def test_redis_read_error_fails_open(self) -> None:
        fake = _FakeStatsRedis(fail_get=True)
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            result = await _check_slot_reconciliation()
        assert result.status == "ok"
        assert result.detail is not None
        assert "unavailable" in result.detail


class TestCheckMigrationsDivergence:
    """FAR-925: the migration divergence check must produce distinguishable
    output for three states: checked-and-clean, divergence-found, and
    check-could-not-run.  A crashed guard must not read identically to a
    clean one."""

    @pytest.mark.asyncio
    async def test_checked_and_clean_ok(self) -> None:
        """Divergence check ran and found nothing — detail says 'up to date'."""
        from contextlib import asynccontextmanager

        from modulo.db.migration_guard import DivergenceCheckResult

        fake_divergence = DivergenceCheckResult(
            diverged=False,
            db_revisions={"001"},
            repo_revisions={"001"},
            orphaned_revisions=set(),
            detail="All applied revisions are present in the repo migration tree",
        )

        @asynccontextmanager
        async def fake_connect():
            class FakeConn:
                async def execute(self, stmt):
                    class FakeResult:
                        def fetchall(self):
                            return [("001",)]

                    return FakeResult()

            yield FakeConn()

        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health._resolve_alembic_ini", return_value=Path("/fake/alembic.ini")),
            patch("modulo.api.routes.health.ScriptDirectory") as mock_sd,
            patch("modulo.api.routes.health.get_or_create_engine") as mock_eng,
            patch("modulo.api.routes.health.check_migration_divergence", return_value=fake_divergence),
            patch.object(Path, "exists", return_value=True),
        ):
            mock_cfg = mock_sd.from_config.return_value
            mock_cfg.get_heads.return_value = {"001"}
            mock_eng.return_value.connect = fake_connect

            result = await _check_migrations()
        assert result.status == "ok"
        assert result.detail is not None
        assert "migrations up to date" in result.detail

    @pytest.mark.asyncio
    async def test_divergence_found_degraded(self) -> None:
        """Divergence check found orphaned revisions — degraded with detail."""
        from contextlib import asynccontextmanager

        from modulo.db.migration_guard import DivergenceCheckResult

        fake_divergence = DivergenceCheckResult(
            diverged=True,
            db_revisions={"001", "orphan"},
            repo_revisions={"001"},
            orphaned_revisions={"orphan"},
            detail="Migration divergence detected",
        )

        @asynccontextmanager
        async def fake_connect():
            class FakeConn:
                async def execute(self, stmt):
                    class FakeResult:
                        def fetchall(self):
                            return [("001",)]

                    return FakeResult()

            yield FakeConn()

        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health._resolve_alembic_ini", return_value=Path("/fake/alembic.ini")),
            patch("modulo.api.routes.health.ScriptDirectory") as mock_sd,
            patch("modulo.api.routes.health.get_or_create_engine") as mock_eng,
            patch("modulo.api.routes.health.check_migration_divergence", return_value=fake_divergence),
            patch.object(Path, "exists", return_value=True),
        ):
            mock_cfg = mock_sd.from_config.return_value
            mock_cfg.get_heads.return_value = {"001"}
            mock_eng.return_value.connect = fake_connect

            result = await _check_migrations()
        assert result.status == "degraded"
        assert result.detail is not None
        assert "DIVERGENCE" in result.detail
        assert "orphan" in result.detail

    @pytest.mark.asyncio
    async def test_divergence_check_failed_degraded(self) -> None:
        """FAR-925: when the divergence check raises, the result must be
        degraded (not ok) with a detail that makes clear the check did NOT
        run.  This MUST fail against the old behaviour (which returned ok /
        'migrations up to date')."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_connect():
            class FakeConn:
                async def execute(self, stmt):
                    class FakeResult:
                        def fetchall(self):
                            return [("001",)]

                    return FakeResult()

            yield FakeConn()

        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health._resolve_alembic_ini", return_value=Path("/fake/alembic.ini")),
            patch("modulo.api.routes.health.ScriptDirectory") as mock_sd,
            patch("modulo.api.routes.health.get_or_create_engine") as mock_eng,
            patch(
                "modulo.api.routes.health.check_migration_divergence",
                side_effect=RuntimeError("guard crashed"),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            mock_cfg = mock_sd.from_config.return_value
            mock_cfg.get_heads.return_value = {"001"}
            mock_eng.return_value.connect = fake_connect

            result = await _check_migrations()
        # Must NOT be "ok" / "migrations up to date" — that was the old bug.
        assert result.status == "degraded"
        assert result.detail is not None
        assert "could not run" in result.detail.lower()


def _rwr_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "orphans_destroyed": 0,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestRunnerWorkspaceReconcileNotApplicable:
    """FAR-1201: engine-less deployments read "not applicable" — distinct
    from a healthy sweep's ``last_run_at=...`` reading and from a failed
    sweep's error reading — while CONFIGURED deployments keep the exact
    stats path (engine-unreachable still surfaces as degraded)."""

    _REASON = "no Docker endpoint configured (MODULO_DOCKER_HOST unset, no socket)"

    async def _read(self, reason: str | None, fake: _FakeStatsRedis) -> Any:
        with (
            patch("modulo.api.routes.health.docker_endpoint_skip_reason", return_value=reason),
            patch("modulo.api.routes.health.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake),
        ):
            return await _check_runner_workspace_reconcile()

    @pytest.mark.asyncio
    async def test_not_applicable_when_endpoint_missing_and_stats_fresh(self) -> None:
        """EKS steady state: the SAQ wrapper persists clean zeros every tick
        after the sweep skips — the reading must be "not applicable", NOT the
        healthy "last_run_at=..." reading a bare-stats reader would produce."""
        result = await self._read(self._REASON, _FakeStatsRedis(blob=_rwr_payload().encode()))
        assert result.status == "ok"
        assert result.detail is not None
        assert result.detail.startswith("not applicable on this deployment")
        assert "last_run_at=" not in result.detail

    @pytest.mark.asyncio
    async def test_not_applicable_when_stats_never_persisted(self) -> None:
        """No stats blob + no endpoint -> "not applicable", never
        "sweep has never run" (liveness is meaningless when the sweep skips)."""
        result = await self._read(self._REASON, _FakeStatsRedis(blob=None))
        assert result.status == "ok"
        assert result.detail is not None
        assert "not applicable on this deployment" in result.detail
        assert "has never run" not in result.detail

    @pytest.mark.asyncio
    async def test_stale_stats_do_not_degrade_when_not_applicable(self) -> None:
        """Stale last_run_at is a LIVENESS signal — suppressed when the sweep
        is not applicable (worker death is caught by system_crons/saq_workers)."""
        stale = _rwr_payload(last_run_at=(datetime.now(UTC) - timedelta(minutes=30)).isoformat())
        result = await self._read(self._REASON, _FakeStatsRedis(blob=stale.encode()))
        assert result.status == "ok"
        assert result.detail is not None
        assert "not applicable on this deployment" in result.detail
        assert "stale" not in result.detail

    @pytest.mark.asyncio
    async def test_recorded_error_surfaces_when_endpoint_missing(self) -> None:
        """A persisted sweep error is a CONTENT signal and must surface even
        when the local process sees no endpoint — the web and worker endpoint
        views can diverge (raw-socket override mounted into the worker only),
        and a real recorded failure must never be hidden by the skip."""
        fake = _FakeStatsRedis(blob=_rwr_payload(error="sweep_failed (ReconcilerSweepError: engine down)").encode())
        result = await self._read(self._REASON, fake)
        assert result.status == "degraded"
        assert result.detail is not None
        assert "sweep reported error" in result.detail

    @pytest.mark.asyncio
    async def test_unparsable_stats_surface_when_endpoint_missing(self) -> None:
        """A corrupt stats blob is also a content signal — surfaces, never
        skipped away."""
        result = await self._read(self._REASON, _FakeStatsRedis(blob=b"not-json"))
        assert result.status == "degraded"
        assert result.detail is not None
        assert "unparsable" in result.detail

    @pytest.mark.asyncio
    async def test_configured_endpoint_uses_unchanged_stats_path(self) -> None:
        """Endpoint configured: the reader behaves exactly as before the skip
        existed — healthy stats read as "ok" with the count detail."""
        fake = _FakeStatsRedis(blob=_rwr_payload(orphans_destroyed=3).encode())
        result = await self._read(None, fake)
        assert result.status == "ok"
        assert result.detail is not None
        assert "orphans_destroyed=3" in result.detail
        assert "not applicable" not in result.detail

    @pytest.mark.asyncio
    async def test_configured_endpoint_missing_stats_still_degraded(self) -> None:
        """Endpoint configured + never-run stats -> degraded, exactly as
        before (a dead sweep on an applicable deployment must still alert)."""
        result = await self._read(None, _FakeStatsRedis(blob=None))
        assert result.status == "degraded"
        assert result.detail is not None
        assert "has never run" in result.detail

    @pytest.mark.asyncio
    async def test_configured_endpoint_error_still_degrades(self) -> None:
        """Endpoint configured + recorded error (engine-unreachable) ->
        degraded — the FAR-1201 signal is preserved for configured engines."""
        fake = _FakeStatsRedis(
            blob=_rwr_payload(error="sweep_failed (ReconcilerSweepError: engine unreachable)").encode()
        )
        result = await self._read(None, fake)
        assert result.status == "degraded"
        assert result.detail is not None
        assert "engine unreachable" in result.detail
