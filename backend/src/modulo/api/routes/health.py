"""Health check endpoints — liveness, readiness, and dependency health.

PR B-2 (plan F7): ``/healthz/ready`` gains a machine-scoped SAQ worker check.
Each worker writes its metadata (``{"hostname": FLY_MACHINE_ID}``) to
``saq:{queue}:worker_info:{worker_id}``; this machine's readiness verifies that
a live worker for THIS hostname exists on EACH configured queue independently
(runs AND system — one live queue does not mask a dead sibling). Stale workers
for 4 consecutive probes => 503. Post-cutover (PR C) the gate is ALWAYS active
— there is no Celery path to fall back on — but can be relaxed to degraded
(alert-only) via ``SAQ_HARD_GATE=false`` after the hold (plan F7).

Plan F8 (restart-policy watchdog): ``/healthz/ready`` ALSO 503s when THIS
machine's ``fire_due_triggers`` system cron has not fired within 2x its 60s
cadence (machine-scoped Redis heartbeat), so Fly's health check removes a
machine whose system-worker cron scheduler is silently dead — the recovery the
``policy = "never"`` restart policy relies on (see fly.toml).

Process groups (PR dist/separate-workers): SAQ workers run ONLY on ``worker``
machines; ``app`` machines run nginx + uvicorn and no workers. On ``app``
machines the two machine-scoped worker gates are meaningless (this host is
never live on a queue / never writes a cron heartbeat), so they switch to a
FLEET-wide gate: any live worker on each queue (``_check_saq_workers``) and any
fresh ``fire_due_triggers`` heartbeat (``_check_system_crons``). Worker machines
and local dev (``FLY_PROCESS_GROUP`` unset) keep the machine-scoped semantics.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import asyncpg  # type: ignore[import-untyped]
import redis.asyncio as aioredis
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_or_create_engine, pg_connection_string
from modulo.core.cron_helpers import read_dispatcher_reconcile_stats
from modulo.settings import Settings, break_glass_boot_findings, get_settings

_CODE_HEALTH_CHECK_CHECKPOINTER = "health._check_checkpointer"


_log = logging.getLogger(__name__)


router = APIRouter(tags=["health"])

VERSION = "0.1.0"
_START_TIME: datetime = datetime.now(UTC)

# 4 consecutive stale probes before 503 (plan F7): 4 x ~15-30s probe interval
# leaves margin over the 90s worker_info TTL (3 strikes = exactly the TTL was
# fragile). Counter is per-process (each web machine tracks its own).
_STALE_PROBE_LIMIT = 4
_consecutive_stale_probes: int = 0

# dispatcher_reconcile runs on a 60s system-cron tick; a last_run_at older
# than 60s means at least one tick was missed -> report "stale" (degraded).
# FAR-199 two-tier gate: staleness past _RECONCILE_UNAVAILABLE_SECONDS (5 min
# — 5x the cadence) flips the check to "unavailable" so readiness 503s. A
# single missed tick must never block bluegreen, so the degraded tier stays
# advisory; a reconcile stale 5+ minutes means the system worker's cron is
# silently dead (a wedged worker fleet that can no longer terminalize
# stalled/never-dispatched runs), which MUST block readiness.
_RECONCILE_STALE_SECONDS = 60
_RECONCILE_UNAVAILABLE_SECONDS = 300

# stale_run_recovery (D1): the legacy sweep runs every 5 min on the system
# worker and persists its outcome to this Redis key (saq_worker wraps the sweep
# call to write it). A last_run_at older than 15 min — or no key at all — means
# the sweep is stale or never ran; the readiness check reports it ADVISORY
# (never gates) so a dead sweep alerts without blocking bluegreen.
_STALE_RUN_RECOVERY_STATS_KEY = "saq:cron:stats:stale_run_recovery"
_STALE_RUN_RECOVERY_STALE_SECONDS = 15 * 60

# System-cron liveness watchdog (plan F8): fire_due_triggers runs every 60s
# (SAQ system cron, cron="* * * * *"); a machine whose heartbeat is older than
# 2x the cadence has a silently dead cron scheduler and fails readiness so Fly
# removes the machine.
_FIRE_DUE_CRON_CADENCE_SECONDS = 60
_CRON_STALE_SECONDS = 2 * _FIRE_DUE_CRON_CADENCE_SECONDS

# Break-glass watchdog state, published at boot by the lifespan and exposed on
# /healthz as ADVISORY only — it never flips readiness.
_break_glass_watchdog: dict[str, str] = {"status": "ok", "detail": "break-glass watchdog not run at boot"}


def set_break_glass_watchdog(status: str, detail: str) -> None:
    """Record the boot-time break-glass watchdog outcome (called by the lifespan)."""
    _break_glass_watchdog["status"] = status
    _break_glass_watchdog["detail"] = detail


class CheckResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str
    uptime_seconds: float
    checks: dict[str, CheckResult]


def _check_break_glass() -> CheckResult:
    """ADVISORY break-glass watchdog exposure — never contributes to readiness.

    Re-evaluates the URL/secret-presence boot findings against the current
    settings; the allow-list/role-posture assertions are fatal at boot and do
    not recur here.
    """
    settings = get_settings()
    findings = break_glass_boot_findings(settings)
    if findings:
        return CheckResult(status="degraded", detail="; ".join(message for _blocking, message in findings))
    return CheckResult(status="ok", detail=_break_glass_watchdog.get("detail") or "break-glass boot config clean")


def _per_check_timeout(settings: Settings, override_field: str) -> float:
    """Resolve the timeout for one dependency check.

    Per-check overrides default to 0 (fall back to the global
    ``modulo_health_timeout_seconds`` value). This gives operators a single
    knob for the common case and a per-check knob for slow dependencies.
    """
    override: float = getattr(settings, override_field)
    if override and override > 0:
        return override
    return settings.modulo_health_timeout_seconds


def _timeout_result(
    status: Literal["unavailable", "degraded"],
    name: str,
    timeout: float,
    start: float,
) -> CheckResult:
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    return CheckResult(
        status=status,
        latency_ms=latency_ms,
        detail=f"{name} check timed out after {timeout:g}s",
    )


async def _check_database() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_db_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> None:
        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="database reachable",
        )
    except TimeoutError:
        _log.warning("health._check_database", exc_info=True)
        return _timeout_result("unavailable", "database", timeout, start)
    except Exception as exc:
        _log.warning("health._check_database", exc_info=True)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="unavailable",
            latency_ms=round(latency_ms, 1),
            detail=str(exc),
        )


async def _check_redis() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_redis_timeout_seconds")
    start = time.monotonic()
    r = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=timeout)
        await asyncio.wait_for(r.ping(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="redis reachable",
        )
    except TimeoutError:
        _log.warning("health._check_redis", exc_info=True)
        return _timeout_result("degraded", "redis", timeout, start)
    except Exception as exc:
        _log.warning("health._check_redis", exc_info=True)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="degraded",
            latency_ms=round(latency_ms, 1),
            detail=str(exc),
        )
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()


async def _check_checkpointer() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_checkpointer_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> tuple[Literal["ok", "degraded"], str]:
        conn_string = pg_connection_string(settings.database_url)
        conn = await asyncpg.connect(conn_string, timeout=timeout)
        try:
            await conn.fetchrow("SELECT 1 FROM checkpoint_migrations LIMIT 1")
        except Exception as exc:
            _log.warning(_CODE_HEALTH_CHECK_CHECKPOINTER, exc_info=True)
            return "degraded", f"checkpoint_migrations table not accessible: {exc}"
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
        return "ok", "checkpointer schema accessible"

    try:
        status, detail = await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(status=status, latency_ms=round(latency_ms, 1), detail=detail)
    except TimeoutError:
        _log.warning(_CODE_HEALTH_CHECK_CHECKPOINTER, exc_info=True)
        return _timeout_result("degraded", "checkpointer", timeout, start)
    except Exception as exc:
        _log.warning(_CODE_HEALTH_CHECK_CHECKPOINTER, exc_info=True)
        return CheckResult(
            status="degraded",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail=str(exc) or "checkpointer check failed",
        )


def _resolve_alembic_ini() -> Path:
    """Locate backend/alembic.ini robustly regardless of the process cwd.

    Same pattern as ``modulo.api.main._resolve_alembic_ini`` — the readiness
    migration check must not depend on the cwd (the pre-commit test harness
    runs pytest from the repo root while CI and the container run from
    ``backend/``).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return candidate
    return Path("alembic.ini")


async def _check_migrations() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_migrations_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> tuple[Literal["ok", "degraded"], str]:
        alembic_ini = _resolve_alembic_ini()
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
        alembic_cfg.set_main_option(
            "script_location",
            str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
        )

        script = ScriptDirectory.from_config(alembic_cfg)
        heads = set(script.get_heads())

        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            applied = {row[0] for row in result.fetchall()}

        if heads.issubset(applied):
            return "ok", "migrations up to date"
        missing = heads - applied
        return "degraded", f"pending migrations: {', '.join(sorted(missing))}"

    try:
        status, detail = await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(status=status, latency_ms=round(latency_ms, 1), detail=detail)
    except TimeoutError:
        _log.warning("health._check_migrations", exc_info=True)
        return _timeout_result("degraded", "migrations", timeout, start)
    except Exception as exc:
        _log.warning("health._check_migrations", exc_info=True)
        return CheckResult(
            status="degraded",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail=f"migration check failed: {exc}",
        )


async def _configured_queues() -> list[str]:
    """PREFIX-AWARE queue names for this environment (runs + system)."""
    settings = get_settings()
    runs_queue = settings.saq_runs_queue
    system_queue = runs_queue.replace("runs", "system") if "runs" in runs_queue else "system"
    return [runs_queue, system_queue]


async def _live_worker_hostnames(queue_name: str) -> set[str]:
    """Read live worker hostnames for *queue_name* from SAQ worker metadata.

    Live = a ``saq:{queue}:stats`` zset entry whose expiry score is in the
    future (worker_info timer 89s / TTL 90s). The metadata hash holds
    ``{"hostname": FLY_MACHINE_ID}`` written by the worker at startup.

    SAQ stores zset scores in MILLISECONDS (``saq.utils.now()`` is
    ``int(time.time() * 1000)``) — the comparison lower bound must be
    milliseconds too, or ``zrangebyscore(key, now_seconds, "+inf")`` matches
    every entry and stale workers are never filtered.
    """
    settings = get_settings()
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        stats_key = f"saq:{queue_name}:stats"
        now_ms = int(time.time() * 1000)
        member_keys = await r.zrangebyscore(stats_key, now_ms, "+inf")
        if not member_keys:
            return set()
        raw = await r.mget(cast("list[bytes | str]", member_keys))
        hostnames: set[str] = set()
        for blob in raw:
            if not blob:
                continue
            try:
                info = json.loads(blob)
            except (ValueError, TypeError):
                continue
            metadata = info.get("metadata") if isinstance(info, dict) else None
            hostname = (metadata or {}).get("hostname") if isinstance(metadata, dict) else None
            if hostname:
                hostnames.add(str(hostname))
        return hostnames
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._live_worker_hostnames queue=%s: %s", queue_name, exc)
        return set()
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()


async def _check_fleet_saq_workers() -> CheckResult:
    """Fleet-wide SAQ worker liveness for ``app`` machines (plan F7, PR dist/separate-workers).

    ``app`` machines run no SAQ workers, so the machine-scoped gate cannot
    apply. Instead readiness gates on ANY live worker being present on EACH
    configured queue — a dead worker machine does not fail an app machine, but
    a fleet-wide worker outage (no live worker on a queue) does. Fail-open on
    Redis read errors. ``SAQ_HARD_GATE=false`` relaxes to alert-only, matching
    the machine-scoped gate.
    """
    settings = get_settings()
    try:
        queues = await _configured_queues()
        live_by_queue: dict[str, set[str]] = {qname: await _live_worker_hostnames(qname) for qname in queues}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_fleet_saq_workers failed: %s", exc)
        return CheckResult(status="ok", detail="saq worker check unavailable (redis read failed)")

    empty_queues = [qname for qname, live in live_by_queue.items() if not live]
    if not empty_queues:
        return CheckResult(status="ok", detail=f"saq workers live on all queues (fleet: {live_by_queue})")
    if settings.saq_hard_gate:
        return CheckResult(
            status="unavailable",
            detail=f"no live saq workers on queue(s): {sorted(empty_queues)} (live_by_queue={live_by_queue})",
        )
    _log.warning("health.saq_workers_fleet_stale_relaxed empty_queues=%s", sorted(empty_queues))
    return CheckResult(
        status="ok",
        detail=f"no live saq workers on queue(s) {sorted(empty_queues)} (SAQ_HARD_GATE=false, alert-only)",
    )


async def _check_saq_workers() -> CheckResult:
    """SAQ worker liveness gate (plan F7).

    Process-group aware (PR dist/separate-workers): on ``app`` machines (which
    run no workers) this delegates to ``_check_fleet_saq_workers`` — a global
    "any live worker on each queue" gate. On ``worker`` machines and local dev
    (``FLY_PROCESS_GROUP`` unset) it is machine-scoped: it verifies THIS
    machine's workers (by FLY_MACHINE_ID hostname) are live on EACH configured
    queue independently (runs AND system) — a live system worker does not mask
    a dead runs worker on the same machine. After 4 consecutive stale probes
    the check reports ``unavailable`` (503). The 503 gate is ALWAYS active
    post-cutover (PR C — there is no Celery path), but ``SAQ_HARD_GATE=false``
    relaxes it to degraded (alert-only) after the hold (plan F7).
    """
    global _consecutive_stale_probes

    if os.environ.get("FLY_PROCESS_GROUP") == "app":
        return await _check_fleet_saq_workers()

    settings = get_settings()
    this_host = os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"
    try:
        queues = await _configured_queues()
        live_by_queue: dict[str, set[str]] = {qname: await _live_worker_hostnames(qname) for qname in queues}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_saq_workers failed: %s", exc)
        return CheckResult(status="ok", detail="saq worker check unavailable (redis read failed)")

    # THIS machine must be live on EVERY configured queue. A host that is live
    # on only one queue is a partially-dead worker and must fail the gate.
    missing_queues = [qname for qname, live in live_by_queue.items() if this_host not in live]
    this_machine_live = not missing_queues

    if this_machine_live:
        _consecutive_stale_probes = 0
        return CheckResult(
            status="ok",
            detail=f"saq workers live on this machine for all queues ({this_host})",
        )

    _consecutive_stale_probes += 1
    if settings.saq_hard_gate and _consecutive_stale_probes >= _STALE_PROBE_LIMIT:
        return CheckResult(
            status="unavailable",
            detail=(
                f"this machine's saq workers stale for {_consecutive_stale_probes} "
                f"consecutive probes (hostname={this_host}, stale_queues={sorted(missing_queues)}, "
                f"live_by_queue={live_by_queue})"
            ),
        )
    if settings.saq_hard_gate:
        return CheckResult(
            status="degraded",
            detail=(
                f"this machine's saq workers stale ({_consecutive_stale_probes}/"
                f"{_STALE_PROBE_LIMIT} probes; hostname={this_host}, stale_queues={sorted(missing_queues)})"
            ),
        )
    # SAQ_HARD_GATE=false (post-hold): alert-only — report ok so the check
    # never 503s a machine; alerting continues permanently (plan F7).
    _log.warning(
        "health.saq_workers_stale_relaxed hostname=%s stale_queues=%s probes=%d",
        this_host,
        sorted(missing_queues),
        _consecutive_stale_probes,
    )
    return CheckResult(
        status="ok",
        detail=f"saq workers stale (SAQ_HARD_GATE=false, alert-only) on this machine ({this_host})",
    )


async def _check_dispatcher_reconcile() -> CheckResult:
    """dispatcher_reconcile liveness — two-tier gate (FAR-199).

    The dispatcher_reconcile system cron runs in the SYSTEM WORKER process
    (PR dist/separate-workers: workers on ``worker`` machines, uvicorn on
    ``app`` machines), so the cron_helpers in-process stats dict is invisible
    here. This check reads the shared Redis key the cron persists every tick —
    "never run" now means the cron genuinely has not run (or its persistence
    failed, or the key has expired: the write carries a self-expiring TTL —
    cron_helpers.DISPATCHER_RECONCILE_STATS_TTL_SECONDS — so a dead worker's
    key self-expires; a missing key returns the SAME "unavailable" tier as a
    stale key, so expiry is signal-equivalent). Fail-open on Redis read errors
    (never degrade a healthy machine on a transient read).

    Tiering (FAR-199): the dispatcher gates readiness ONLY at its unavailable
    tier. A last_run_at older than the 60s cadence reports "stale" (degraded)
    to alert operators while the app remains healthy — a single missed tick
    must not block bluegreen. A last_run_at older than
    ``_RECONCILE_UNAVAILABLE_SECONDS`` (5 min — 5x the cadence, far beyond a
    transient tick gap) means the system worker's reconcile is silently dead:
    a wedged worker fleet can no longer terminalize stalled / never-dispatched
    runs, so the machine must NOT pass readiness and bluegreen must not cut
    over. The readiness aggregation 503s on this check's "unavailable" status.
    """
    settings = get_settings()
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        stats = await read_dispatcher_reconcile_stats(r)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_dispatcher_reconcile redis read failed: %s", exc)
        return CheckResult(status="ok", detail="dispatcher_reconcile check unavailable (redis read failed)")
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()

    if not stats or stats.get("last_run_at") is None:
        return CheckResult(
            status="unavailable",
            detail="dispatcher_reconcile has never run (system worker cron dead or stats persistence failing)",
        )
    try:
        last_run = datetime.fromisoformat(stats["last_run_at"])
    except (ValueError, TypeError):
        return CheckResult(status="degraded", detail="dispatcher_reconcile last_run_at unparsable")
    stale_seconds = (datetime.now(UTC) - last_run).total_seconds()
    if stale_seconds > _RECONCILE_UNAVAILABLE_SECONDS:
        return CheckResult(
            status="unavailable",
            detail=(
                f"dispatcher_reconcile stale ({stale_seconds:.0f}s since last run, "
                f"last_run_at={stats['last_run_at']}); {_format_reconcile_detail(stats)}"
            ),
        )
    if stale_seconds > _RECONCILE_STALE_SECONDS:
        return CheckResult(
            status="degraded",
            detail=(
                f"dispatcher_reconcile stale ({stale_seconds:.0f}s since last run, "
                f"last_run_at={stats['last_run_at']}); {_format_reconcile_detail(stats)}"
            ),
        )
    return CheckResult(
        status="ok",
        detail=f"last_run_at={stats['last_run_at']}, {_format_reconcile_detail(stats)}",
    )


def _format_reconcile_detail(stats: dict[str, Any]) -> str:
    """Human-readable reconciliation counters for the readiness check detail.

    Surfaces the reconcile outcome counters (D1): scanned/repaired/skipped/
    redis_errors/deduped plus the terminalizer and enqueue-failed recovery
    counters. Every counter defaults to 0 so a pre-D worker's payload renders
    without error.
    """
    return (
        f"scanned={stats.get('scanned', 0)}, repaired={stats.get('repaired', 0)}, "
        f"skipped={stats.get('skipped', 0)}, redis_errors={stats.get('redis_errors', 0)}, "
        f"deduped={stats.get('deduped', 0)}, nodeless_failed={stats.get('nodeless_failed', 0)}, "
        f"claim_cap_terminalized={stats.get('claim_cap_terminalized', 0)}, "
        f"age_terminalized={stats.get('age_terminalized', 0)}, "
        f"dispatch_failed_terminalized={stats.get('dispatch_failed_terminalized', 0)}, "
        f"enqueue_failed_ttl_terminalized={stats.get('enqueue_failed_ttl_terminalized', 0)}, "
        f"enqueue_failed_redispatched={stats.get('enqueue_failed_redispatched', 0)}, "
        f"enqueue_failed_capped={stats.get('enqueue_failed_capped', 0)}, "
        f"capacity_deferred={stats.get('capacity_deferred', 0)}"
    )


async def _check_stale_run_recovery() -> CheckResult:
    """ADVISORY — last stale_run_recovery sweep outcome (never gates readiness).

    The legacy stale-run sweep (``saq_worker.stale_run_recovery``) runs in the
    SYSTEM WORKER process every 5 min and persists its outcome (``recovered`` +
    ``last_run_at``) to ``saq:cron:stats:stale_run_recovery`` (D1). This check
    reads that key — "never run" now means the sweep genuinely has not run (or
    its persistence failed). A last_run_at older than 15 min reports
    "degraded" to alert operators while the app remains healthy. Fail-open on
    Redis read errors.
    """
    settings = get_settings()
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        raw = await r.get(_STALE_RUN_RECOVERY_STATS_KEY)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_stale_run_recovery redis read failed: %s", exc)
        return CheckResult(status="ok", detail="stale_run_recovery check unavailable (redis read failed)")
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()

    if raw is None:
        return CheckResult(status="degraded", detail="stale_run_recovery has never run")
    try:
        data = json.loads(raw)
        last_run_at = data.get("last_run_at")
        recovered = data.get("recovered", 0)
    except (ValueError, TypeError):
        return CheckResult(status="degraded", detail="stale_run_recovery stats unparsable")
    if not last_run_at:
        return CheckResult(status="degraded", detail="stale_run_recovery last_run_at missing")
    try:
        last_run = datetime.fromisoformat(last_run_at)
    except (ValueError, TypeError):
        return CheckResult(status="degraded", detail="stale_run_recovery last_run_at unparsable")
    stale_seconds = (datetime.now(UTC) - last_run).total_seconds()
    if stale_seconds > _STALE_RUN_RECOVERY_STALE_SECONDS:
        return CheckResult(
            status="degraded",
            detail=(f"stale_run_recovery stale ({stale_seconds:.0f}s since last run, last recovered={recovered})"),
        )
    return CheckResult(
        status="ok",
        detail=f"last_run_at={last_run_at}, recovered={recovered}",
    )


async def _check_fleet_system_crons() -> CheckResult:
    """Fleet-wide system-cron liveness for ``app`` machines (plan F8, PR dist/separate-workers).

    Only ``worker`` machines run the ``fire_due_triggers`` system-cron
    scheduler, so on an ``app`` machine the per-machine heartbeat is never
    written. Instead readiness gates on ANY machine having a fresh heartbeat —
    a fleet-wide scheduler death fails readiness, a single dead worker machine
    does not. Fail-open on Redis read errors. ``SAQ_HARD_GATE=false`` relaxes
    to alert-only, matching the machine-scoped gate.
    """
    settings = get_settings()
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        heartbeat_keys = await r.keys("saq:cron:heartbeat:fire_due_triggers:*")
        now = time.time()
        for key in heartbeat_keys:
            raw = await r.get(key)
            if raw is None:
                continue
            try:
                last_ts = float(raw)
            except (TypeError, ValueError):
                continue
            if now - last_ts <= _CRON_STALE_SECONDS:
                return CheckResult(status="ok", detail="system-cron heartbeat fresh on at least one machine")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_fleet_system_crons redis read failed: %s", exc)
        return CheckResult(status="ok", detail="system-cron liveness check unavailable (redis read failed)")
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()

    if settings.saq_hard_gate:
        return CheckResult(
            status="unavailable",
            detail="no fresh fire_due_triggers cron heartbeat on any machine",
        )
    _log.warning("health.system_cron_fleet_stale_relaxed")
    return CheckResult(status="ok", detail="system-cron heartbeat stale fleet-wide (SAQ_HARD_GATE=false, alert-only)")


async def _check_system_crons() -> CheckResult:
    """System-cron liveness watchdog (plan F8 cron watchdog).

    Process-group aware (PR dist/separate-workers): on ``app`` machines (which
    run no system worker) this delegates to ``_check_fleet_system_crons`` — any
    machine with a fresh heartbeat. On ``worker`` machines and local dev
    (``FLY_PROCESS_GROUP`` unset) it is machine-scoped: the SAQ system worker
    runs ``fire_due_triggers`` every 60s and writes a per-machine Redis
    heartbeat (``saq:cron:heartbeat:fire_due_triggers:{host}``). If THIS
    machine's heartbeat is stale by more than 2x the cadence (or was never
    written once the process has been up that long), the machine's cron
    scheduler is silently dead — a worker loop can stay alive while its cron
    scheduler is stuck — so return ``unavailable`` (503) to let Fly's health
    check remove the machine. Fail-open on Redis read errors (never 503 a
    healthy machine on a transient read). ``SAQ_HARD_GATE=false`` relaxes to
    alert-only, matching the SAQ worker gate.
    """
    if os.environ.get("FLY_PROCESS_GROUP") == "app":
        return await _check_fleet_system_crons()

    settings = get_settings()
    this_host = os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        last = await r.get(f"saq:cron:heartbeat:fire_due_triggers:{this_host}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_system_crons redis read failed: %s", exc)
        return CheckResult(status="ok", detail="system-cron liveness check unavailable (redis read failed)")
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()

    uptime_seconds = (datetime.now(UTC) - _START_TIME).total_seconds()
    if last is None:
        if uptime_seconds < _CRON_STALE_SECONDS:
            return CheckResult(status="ok", detail="no system-cron heartbeat yet (boot grace)")
        if settings.saq_hard_gate:
            return CheckResult(
                status="unavailable",
                detail=f"system-cron scheduler never fired on this machine ({this_host})",
            )
        _log.warning("health.system_cron_never_fired_relaxed hostname=%s", this_host)
        return CheckResult(status="ok", detail="system-cron never fired (SAQ_HARD_GATE=false, alert-only)")

    try:
        last_ts = float(last)
    except (TypeError, ValueError):
        return CheckResult(status="degraded", detail="system-cron heartbeat key unparseable")

    age = time.time() - last_ts
    if age <= _CRON_STALE_SECONDS:
        return CheckResult(status="ok", detail=f"system-cron heartbeat fresh ({age:.0f}s ago)")

    if settings.saq_hard_gate:
        return CheckResult(
            status="unavailable",
            detail=(
                f"this machine's fire_due_triggers cron heartbeat stale "
                f"({age:.0f}s > {_CRON_STALE_SECONDS}s; hostname={this_host})"
            ),
        )
    _log.warning("health.system_cron_stale_relaxed age=%.0fs hostname=%s", age, this_host)
    return CheckResult(status="ok", detail="system-cron heartbeat stale (SAQ_HARD_GATE=false, alert-only)")


@router.get("/healthz")
@handle_db_errors("health.liveness")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz/ready")
@handle_db_errors("health.readiness")
async def readiness(response: Response) -> ReadinessResponse:
    db_check, redis_check, cp_check, mig_check, saq_check, cron_check, dr_check, srr_check = await asyncio.gather(
        _check_database(),
        _check_redis(),
        _check_checkpointer(),
        _check_migrations(),
        _check_saq_workers(),
        _check_system_crons(),
        _check_dispatcher_reconcile(),
        _check_stale_run_recovery(),
    )
    bg_check = _check_break_glass()

    checks: dict[str, CheckResult] = {
        "database": db_check,
        "redis": redis_check,
        "checkpointer": cp_check,
        "migrations": mig_check,
        "saq_workers": saq_check,
        "system_crons": cron_check,
        # ADVISORY only — excluded from the aggregate so a break-glass config
        # warning never degrades readiness (plan §3 watchdog reduction).
        "break_glass": bg_check,
        # FAR-199: dispatcher_reconcile gates readiness at its "unavailable"
        # tier only (see the aggregation below); its "degraded" tier stays
        # advisory so a single missed reconcile tick never blocks bluegreen.
        "dispatcher_reconcile": dr_check,
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        "stale_run_recovery": srr_check,
    }

    # Aggregate over the NON-advisory checks only.
    statuses = [
        db_check.status,
        redis_check.status,
        cp_check.status,
        mig_check.status,
        saq_check.status,
        cron_check.status,
    ]
    # FAR-199: dispatcher_reconcile gates readiness ONLY at its "unavailable"
    # tier — reconcile stale past _RECONCILE_UNAVAILABLE_SECONDS means the
    # system worker's cron is silently dead (a wedged worker fleet that would
    # silently accumulate executor_stalled / never_dispatched runs), so
    # bluegreen must not cut over. Its "degraded" tier (a single missed 60s
    # tick) stays advisory and is deliberately excluded from the degraded
    # aggregation — short staleness must never flip readiness.
    if "unavailable" in statuses or dr_check.status == "unavailable":
        overall: Literal["ok", "degraded", "unavailable"] = "unavailable"
        response.status_code = 503
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"

    uptime_seconds = (datetime.now(UTC) - _START_TIME).total_seconds()

    return ReadinessResponse(
        status=overall,
        version=VERSION,
        uptime_seconds=uptime_seconds,
        checks=checks,
    )
