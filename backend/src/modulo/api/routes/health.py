"""Health check endpoints — liveness, readiness, and dependency health.

Readiness (``GET /healthz/ready``) is DEPLOYMENT-scoped (ADR 043 / FAR-1158):
it asks "will work actually get processed?" — are live workers present for
EACH configured queue anywhere in THIS deployment — never "on this host".
The former machine-scoped gates required a live worker for THIS hostname
(``FLY_MACHINE_ID``/``HOSTNAME``) and a per-host ``fire_due_triggers``
heartbeat, an assumption that holds only where backend and workers share a
host (docker-compose, local dev) and NEVER on multi-node Kubernetes, where a
correctly-serving backend could never become Ready and the kubelet killed it
on the liveness probe. There is exactly ONE path — no process-role
(``FLY_PROCESS_GROUP``) routing, no platform env var for identity.

Determination contract (bounded fail-open, ADR 043 Decision 3):

- confirmed non-empty (live workers / a fresh heartbeat read successfully)
  → ``ok``; the consecutive-probe counter resets.
- confirmed-empty (the store read succeeded, nothing live) → fails closed
  after grace: within boot grace → ``ok``; probes 1..limit-1 → ``degraded``;
  probe >= ``_STALE_PROBE_LIMIT`` → ``unavailable`` under the default
  ``SAQ_HARD_GATE=true``. A confirmed-empty queue forces this path
  regardless of sibling read failures — it is never masked into
  "undeterminable".
- undeterminable (a store read failed) → ``degraded`` (non-gating) but it
  advances the SAME consecutive-probe counter, escalating to ``unavailable``
  after the grace tier — a transient blip never gates, a sustained outage
  does. This makes the fail-open claim true and BOUNDED.
- ``SAQ_HARD_GATE=false`` keeps the operator's explicit alert-only
  relaxation (``ok`` + warning) for both outcomes.

Scope (ADR 043 Decision 4): these checks are READINESS-ONLY. Liveness
(``GET /healthz`` — process-local, always ``ok``) and any restart evaluator
must key on a process-local signal, never on a deployment-scoped check.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import asyncpg
import redis.asyncio as aioredis
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.db_error_reporting import log_service_unavailable
from modulo.api.dependencies import get_or_create_engine, pg_connection_string
from modulo.core.bundled_runner.runner_reconciler import docker_endpoint_skip_reason
from modulo.core.cron_helpers import read_dispatcher_reconcile_stats
from modulo.db.migration_guard import DivergenceCheckResult, check_migration_divergence
from modulo.settings import Settings, break_glass_boot_findings, get_settings, resolve_instance_identity
from modulo.version import get_version

_CODE_HEALTH_CHECK_CHECKPOINTER = "health._check_checkpointer"


_log = logging.getLogger(__name__)


router = APIRouter(tags=["health"])

VERSION = get_version()
_START_TIME: datetime = datetime.now(UTC)

# 4 consecutive failed probes before a fleet readiness gate reports 503
# (plan F7 grace tier, ADR 043): 4 x ~15-30s probe interval leaves margin
# over the 90s worker_info TTL (3 strikes = exactly the TTL was fragile).
# Counter is per-process (each web instance tracks its own probe streak) and
# is shared by BOTH outcomes of the SAQ fleet gate — confirmed-empty and
# undeterminable advance the same counter (ADR 043 Decision 3).
_STALE_PROBE_LIMIT = 4
_consecutive_stale_probes: int = 0
# System-cron fleet gate has its OWN probe-grace counter (the machine-scoped
# crons gate historically had boot grace but no probe tier — the fleet check
# gets both, ADR 043 Decision 3).
_consecutive_cron_stale_probes: int = 0
# Boot grace for BOTH fleet gates: right after process start the workers /
# heartbeats of the wider deployment may not have been observed yet, so a
# failure inside this window reports ok without advancing any counter.
_FLEET_BOOT_GRACE_SECONDS = 120

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

# slot_reconciliation (FAR-604 F6): the slot-reconciliation sweep runs every
# 5 min on the system worker and persists its outcome to this Redis key. Same
# advisory contract as stale_run_recovery: a missing or >15min-stale key means
# a silently dead sweep — which would re-open the FAR-604 admission wedge
# invisibly — so it warns without gating readiness.
_SLOT_RECONCILIATION_STATS_KEY = "saq:cron:stats:slot_reconciliation"
_SLOT_RECONCILIATION_STALE_SECONDS = 15 * 60

# hitl_park_sweep (FAR-604 D2, qa F5): the HITL park-on-expiry sweep runs
# every 5 min on the system worker and persists its outcome to this Redis
# key. Same advisory contract as slot_reconciliation: parking is post-D1
# hygiene (a parked run holds no pipeline slot), so a dead sweep does not
# wedge admission — but the visibility half of the design silently stops, so
# a missing or >15min-stale key warns without gating readiness.
_HITL_PARK_SWEEP_STATS_KEY = "saq:cron:stats:hitl_park_sweep"
_HITL_PARK_SWEEP_STALE_SECONDS = 15 * 60

# runner_workspace_reconcile (FAR-590 D4): the Bundled Runner orphan
# reconciler runs every 5 min on the system worker and persists its outcome
# to this Redis key. Same advisory contract as its siblings: the destroy
# path is soak-gated (RUNNER_RECONCILER_DESTROY_ENABLED defaults False), so
# a dead sweep cannot wedge anything — but labelled-container leaks would
# silently stop being even logged, so a missing or >15min-stale key warns
# without gating readiness.
_RUNNER_WORKSPACE_RECONCILE_STATS_KEY = "saq:cron:stats:runner_workspace_reconcile"
_RUNNER_WORKSPACE_RECONCILE_STALE_SECONDS = 15 * 60

# runner_marker_sweep (FAR-594 D8, qa F9): the runner dispatch-marker
# reconciliation sweep runs every 5 min on the system worker (plus the 60s
# dispatcher_reconcile path) and persists its outcome to this Redis key.
# Same advisory contract as its siblings: a dead sweep does not wedge
# dispatch (the gate fails open) — but stale markers would silently
# accumulate as phantom capacity and the D8 rollback signal
# (runner.capacity.violation) would go dark, so a missing or >15min-stale
# key warns without gating readiness.
_RUNNER_MARKER_SWEEP_STATS_KEY = "saq:cron:stats:runner_marker_sweep"
_RUNNER_MARKER_SWEEP_STALE_SECONDS = 15 * 60

# runner_health_probe (FAR-591 D5, qa F3): the per-machine runner health
# probe runs every 60s on the system worker and persists its outcome to
# this Redis key. Same advisory contract as its siblings, with the stale
# window tuned to the 60s cadence (3x = 180s — a single missed tick never
# alerts): a dead probe stops refreshing the Runners page's cache, so every
# org's strip silently ages to "status unknown" — a missing or stale key
# warns without gating readiness.
_RUNNER_HEALTH_PROBE_STATS_KEY = "saq:cron:stats:runner_health_probe"
_RUNNER_HEALTH_PROBE_STALE_SECONDS = 3 * 60

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
    except Exception:
        _log.warning("health._check_database", exc_info=True)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="unavailable",
            latency_ms=round(latency_ms, 1),
            detail="database unreachable",
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
    except Exception:
        _log.warning("health._check_redis", exc_info=True)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="degraded",
            latency_ms=round(latency_ms, 1),
            detail="redis unreachable",
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
        except Exception:
            _log.warning(_CODE_HEALTH_CHECK_CHECKPOINTER, exc_info=True)
            return "degraded", "checkpoint_migrations table not accessible"
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
    except Exception:
        _log.warning(_CODE_HEALTH_CHECK_CHECKPOINTER, exc_info=True)
        return CheckResult(
            status="degraded",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail="checkpointer check failed",
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

        # FAR-872: check for repo-vs-DB migration divergence (DB has applied
        # revisions the repo does not ship).  Logged at ERROR in
        # migration_guard; surfaced here so it is visible without grepping logs.
        divergence: DivergenceCheckResult | None = None
        divergence_check_failed = False
        try:
            divergence = check_migration_divergence(applied)
        except Exception:
            # Contractually fail-open, but never silent: a failure here would
            # otherwise hide a real bug behind the "migrations up to date" path.
            _log.exception("health._check_migrations divergence check failed")
            divergence_check_failed = True

        pending = heads - applied
        parts: list[str] = []
        if pending:
            parts.append(f"pending migrations: {', '.join(sorted(pending))}")
        if divergence_check_failed:
            # FAR-925: the divergence guard itself crashed — report degraded
            # (not ok) so this state cannot be mistaken for "checked and clean".
            # Fail-open: the check is advisory, so a crashed guard degrades
            # the report without blocking readiness.
            parts.append("divergence check could not run (exception raised, see logs)")
        elif divergence is not None and divergence.diverged:
            parts.append(
                f"DIVERGENCE: DB has revision(s) not in repo: {', '.join(sorted(divergence.orphaned_revisions))}"
            )
        if not parts:
            return "ok", "migrations up to date"
        return "degraded", "; ".join(parts)

    try:
        status, detail = await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(status=status, latency_ms=round(latency_ms, 1), detail=detail)
    except TimeoutError:
        _log.warning("health._check_migrations", exc_info=True)
        return _timeout_result("degraded", "migrations", timeout, start)
    except Exception:
        _log.warning("health._check_migrations", exc_info=True)
        return CheckResult(
            status="degraded",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail="migration check failed",
        )


def _configured_queues() -> list[str]:
    """PREFIX-AWARE queue names for this environment (runs + system)."""
    settings = get_settings()
    runs_queue = settings.saq_runs_queue
    system_queue = runs_queue.replace("runs", "system") if "runs" in runs_queue else "system"
    return [runs_queue, system_queue]


def _hostname_from_worker_blob(blob: bytes | str) -> str | None:
    """Extract the worker hostname from one SAQ worker metadata blob, or None."""
    try:
        info = json.loads(blob)
    except (ValueError, TypeError):
        return None
    metadata = info.get("metadata") if isinstance(info, dict) else None
    hostname = (metadata or {}).get("hostname") if isinstance(metadata, dict) else None
    return str(hostname) if hostname else None


def _hostnames_from_worker_blobs(raw: Iterable[bytes | str | None]) -> set[str]:
    """Decode SAQ worker metadata blobs into the set of live hostnames."""
    hostnames: set[str] = set()
    for blob in raw:
        if not blob:
            continue
        hostname = _hostname_from_worker_blob(blob)
        if hostname:
            hostnames.add(hostname)
    return hostnames


async def _live_worker_hostnames(queue_name: str) -> set[str]:
    """Read live worker hostnames for *queue_name* from SAQ worker metadata.

    Live = a ``saq:{queue}:stats`` zset entry whose expiry score is in the
    future (worker_info timer 89s / TTL 90s). The metadata hash holds
    ``{"hostname": <instance identity>}`` written by the worker at startup
    (platform-neutral — ADR 043 Decision 5).

    SAQ stores zset scores in MILLISECONDS (``saq.utils.now()`` is
    ``int(time.time() * 1000)``) — the comparison lower bound must be
    milliseconds too, or ``zrangebyscore(key, now_seconds, "+inf")`` matches
    every entry and stale workers are never filtered.

    Errors PROPAGATE (this helper deliberately does not swallow them):
    distinguishing "read successfully, no live workers" (confirmed-empty)
    from "could not read" (undeterminable) is the caller's job — the
    determination contract in ``_check_fleet_saq_workers`` classifies a
    raised error as undeterminable (bounded fail-open) instead of letting it
    masquerade as a dead queue (ADR 043 Decision 3).
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
        return _hostnames_from_worker_blobs(raw)
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()


def _in_fleet_boot_grace() -> bool:
    """True within the fleet gates' post-boot grace window (ADR 043 Decision 3)."""
    return (datetime.now(UTC) - _START_TIME).total_seconds() < _FLEET_BOOT_GRACE_SECONDS


async def _check_fleet_saq_workers() -> CheckResult:
    """Fleet-wide SAQ worker gate — the sole readiness path (ADR 043 / FAR-1158).

    Deployment-scoped: ANY live worker on EACH configured queue covers
    readiness, wherever in the deployment it runs — there is no co-location
    requirement and no process-role routing. Determination contract (module
    docstring): confirmed non-empty resets the probe counter; a confirmed-
    empty queue forces the failed-closed path even when a sibling queue's
    read failed; undeterminable shares the same counter (bounded fail-open);
    boot grace reports ok without advancing the counter; and
    ``SAQ_HARD_GATE=false`` is the operator's alert-only relaxation.
    """
    global _consecutive_stale_probes
    settings = get_settings()

    live_by_queue: dict[str, set[str]] = {}
    failed_queues: list[str] = []
    try:
        queues = _configured_queues()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_fleet_saq_workers queue config failed: %s", exc)
        queues = []
        failed_queues.append("<queue-config>")
    for qname in queues:
        try:
            live_by_queue[qname] = await _live_worker_hostnames(qname)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("health._check_fleet_saq_workers queue=%s read failed: %s", qname, exc)
            failed_queues.append(qname)

    empty_queues = sorted(qname for qname, live in live_by_queue.items() if not live)
    if not empty_queues and not failed_queues:
        _consecutive_stale_probes = 0
        return CheckResult(status="ok", detail=f"saq workers live on all queues (fleet: {live_by_queue})")

    # Aggregation precedence (ADR 043 Decision 3): a confirmed-empty queue
    # forces the failed-closed path regardless of sibling read failures —
    # it is never masked into the undeterminable classification.
    probe_host = resolve_instance_identity()
    if empty_queues:
        problem = f"no live saq workers on queue(s) {empty_queues}"
        if failed_queues:
            problem += f" (sibling read failed for {sorted(failed_queues)})"
    else:
        problem = f"saq worker check undeterminable: read failed for queue(s) {sorted(failed_queues)}"
    problem += f", live_by_queue={live_by_queue}, probe_host={probe_host}"

    if _in_fleet_boot_grace():
        return CheckResult(status="ok", detail=f"{problem} (within {_FLEET_BOOT_GRACE_SECONDS}s boot grace)")

    _consecutive_stale_probes += 1
    probes = _consecutive_stale_probes
    if settings.saq_hard_gate:
        if probes >= _STALE_PROBE_LIMIT:
            return CheckResult(status="unavailable", detail=f"{problem} ({probes} consecutive probes)")
        return CheckResult(status="degraded", detail=f"{problem} ({probes}/{_STALE_PROBE_LIMIT} probes)")
    # SAQ_HARD_GATE=false (post-hold): alert-only — never 503s; alerting
    # continues permanently (plan F7).
    _log.warning("health.saq_workers_fleet_relaxed probes=%d %s", probes, problem)
    return CheckResult(status="ok", detail=f"{problem} (SAQ_HARD_GATE=false, alert-only)")


async def _check_saq_workers() -> CheckResult:
    """SAQ worker liveness gate — deployment-scoped, ONE path (ADR 043 / FAR-1158).

    Delegates unconditionally to ``_check_fleet_saq_workers``. The former
    machine-scoped branch (``FLY_PROCESS_GROUP`` routing + THIS-host hostname
    match against ``FLY_MACHINE_ID``/``HOSTNAME``) is removed: readiness never
    assumes backend and workers share a host — that assumption never held on
    multi-node Kubernetes, where a correctly-serving backend could never
    become Ready (FAR-1158).
    """
    return await _check_fleet_saq_workers()


async def _check_dispatcher_reconcile() -> CheckResult:
    """dispatcher_reconcile liveness — two-tier gate (FAR-199 / FAR-746).

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

    Tiering (FAR-199, updated FAR-746): the dispatcher gates readiness ONLY
    at its unavailable tier. A last_run_at older than the 60s cadence reports
    "stale" (degraded) to alert operators while the app remains healthy — a
    single missed tick must not block bluegreen. A last_run_at older than
    ``_RECONCILE_UNAVAILABLE_SECONDS`` (5 min — 5x the cadence, far beyond a
    transient tick gap) means the system worker's reconcile is silently dead:
    a wedged worker fleet can no longer terminalize stalled / never-dispatched
    runs, so the machine must NOT pass readiness and bluegreen must not cut
    over. The readiness aggregation 503s on this check's "unavailable" status.

    FAR-746 failure heartbeat: when the stats blob is FRESH (within the
    staleness window) but ``status`` is ``"timeout"`` or ``"failed"``, the
    check returns "degraded" (non-gating) — a partially-working background
    sweep must degrade the health REPORT, not take down prod routing (that
    was the outage mechanism). Only "never ran" / truly stale (>300s) stays
    "unavailable" (gating).
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
    # FAR-746 failure heartbeat: when the stats are FRESH but the sweep
    # reported a failure (inner deadline or unexpected exception), return
    # "degraded" (non-gating) — a partially-working background sweep must
    # degrade the health report, not take down prod routing.  Only the
    # "never ran" / truly stale (>300s) tiers gate readiness.
    tick_status = stats.get("status", "ok")
    if tick_status in ("timeout", "failed"):
        return CheckResult(
            status="degraded",
            detail=(
                f"dispatcher_reconcile fresh but status={tick_status} "
                f"(last_run_at={stats['last_run_at']}, "
                f"last_error={stats.get('last_error', 'n/a')}); "
                f"{_format_reconcile_detail(stats)}"
            ),
        )
    return CheckResult(
        status="ok",
        detail=f"last_run_at={stats['last_run_at']}, {_format_reconcile_detail(stats)}",
    )


def _format_reconcile_detail(stats: dict[str, Any]) -> str:
    """Human-readable reconciliation counters for the readiness check detail.

    Surfaces the reconcile outcome counters (D1): scanned/repaired/skipped/
    redis_errors/deduped plus the claimed-but-never-dispatched recovery
    counter (FAR-714) and the terminalizer and enqueue-failed recovery
    counters. Every counter defaults to 0 so a pre-D worker's payload renders
    without error.
    """
    return (
        f"scanned={stats.get('scanned', 0)}, repaired={stats.get('repaired', 0)}, "
        f"skipped={stats.get('skipped', 0)}, redis_errors={stats.get('redis_errors', 0)}, "
        f"deduped={stats.get('deduped', 0)}, nodeless_failed={stats.get('nodeless_failed', 0)}, "
        f"claimed_but_never_dispatched={stats.get('claimed_but_never_dispatched', 0)}, "
        f"claim_cap_terminalized={stats.get('claim_cap_terminalized', 0)}, "
        f"age_terminalized={stats.get('age_terminalized', 0)}, "
        f"dispatch_failed_terminalized={stats.get('dispatch_failed_terminalized', 0)}, "
        f"enqueue_failed_ttl_terminalized={stats.get('enqueue_failed_ttl_terminalized', 0)}, "
        f"enqueue_failed_redispatched={stats.get('enqueue_failed_redispatched', 0)}, "
        f"enqueue_failed_capped={stats.get('enqueue_failed_capped', 0)}, "
        f"capacity_deferred={stats.get('capacity_deferred', 0)}, "
        f"terminalize_capped={stats.get('terminalize_capped', 0)}, "
        f"facts_deferred={stats.get('facts_deferred', 0)}"
    )


async def _check_sweep_stats_advisory(
    key: str, stale_seconds: int, count_key: str, *, not_applicable: str | None = None
) -> CheckResult:
    """Shared ADVISORY reader for a sweep's Redis liveness stats key.

    Sweep wrappers persist their outcome (``last_run_at`` + a count) to a
    shared Redis key every tick; this reader reports "degraded" when the key
    is missing (never run), its ``last_run_at`` is older than
    *stale_seconds* (stale), the payload is unparsable, or the payload
    carries a non-null ``error`` (FAR-824 — a cron that runs but FAILS must
    not read like a healthy one; see the FAR-808 incident where
    ``runner_health_probe`` reported ok for hours while its stats carried
    ``error: probe_failed, orgs_probed: 0``), and "ok" otherwise. Fail-open
    on Redis read errors (never gates readiness).

    FAR-1201: when *not_applicable* is provided (the deployment has no
    Docker endpoint, so the sweep skips every tick — see
    ``runner_reconciler.docker_endpoint_skip_reason``), the LIVENESS
    readings (missing key, stale ``last_run_at``) are replaced by an
    explicit "not applicable on this deployment" reading, because a skipped
    sweep's liveness is meaningless. Content signals still surface even
    then: an unparsable payload or a persisted ``error`` reports degraded —
    the reader's local endpoint view can differ from the worker's (the
    raw-socket operator override can mount the socket into the worker but
    not the web process), and a recorded worker-side failure must never be
    hidden by the skip.
    """
    settings = get_settings()
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        raw = await r.get(key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Fail-open contract unchanged even when not_applicable: we could not
        # inspect the blob, so we do not claim its reading either way. Redis
        # itself is reported by the non-advisory ``redis`` check.
        _log.warning("health.sweep_stats_read_failed key=%s: %s", key, exc)
        return CheckResult(status="ok", detail="sweep liveness check unavailable (redis read failed)")
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()

    if raw is None:
        if not_applicable is not None:
            return CheckResult(status="ok", detail=f"not applicable on this deployment: {not_applicable}")
        return CheckResult(status="degraded", detail="sweep has never run")
    try:
        data = json.loads(raw)
        last_run_at = data.get("last_run_at")
        count = data.get(count_key, 0)
    except (ValueError, TypeError):
        return CheckResult(status="degraded", detail="sweep stats unparsable")
    if error := data.get("error"):
        # FAR-905: surface the underlying exception detail when the sweep
        # persisted one — a bare error token is unactionable in a readout.
        error_detail = data.get("error_detail")
        msg = f"sweep reported error: {error}"
        if error_detail:
            msg += f" ({error_detail})"
        return CheckResult(status="degraded", detail=f"{msg}, last {count_key}={count}")
    if not_applicable is not None:
        return CheckResult(status="ok", detail=f"not applicable on this deployment: {not_applicable}")
    if not last_run_at:
        return CheckResult(status="degraded", detail="sweep last_run_at missing")
    try:
        last_run = datetime.fromisoformat(last_run_at)
    except (ValueError, TypeError):
        return CheckResult(status="degraded", detail="sweep last_run_at unparsable")
    stale_since = (datetime.now(UTC) - last_run).total_seconds()
    if stale_since > stale_seconds:
        return CheckResult(
            status="degraded",
            detail=(f"sweep stale ({stale_since:.0f}s since last run, last {count_key}={count})"),
        )
    return CheckResult(
        status="ok",
        detail=f"last_run_at={last_run_at}, {count_key}={count}",
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
    return await _check_sweep_stats_advisory(
        _STALE_RUN_RECOVERY_STATS_KEY, _STALE_RUN_RECOVERY_STALE_SECONDS, "recovered"
    )


async def _check_slot_reconciliation() -> CheckResult:
    """ADVISORY — last slot_reconciliation sweep outcome (never gates readiness).

    The FAR-604 slot-reconciliation sweep (``saq_worker.slot_reconciliation``)
    runs in the SYSTEM WORKER process every 5 min and persists its outcome
    (``released`` + ``last_run_at``) to ``saq:cron:stats:slot_reconciliation``
    (F6). A silently dead sweep re-opens the FAR-604 admission wedge
    invisibly, so a missing or >15min-stale key reports "degraded" to alert
    operators while the app remains healthy. Fail-open on Redis read errors.
    """
    return await _check_sweep_stats_advisory(
        _SLOT_RECONCILIATION_STATS_KEY, _SLOT_RECONCILIATION_STALE_SECONDS, "released"
    )


async def _check_hitl_park_sweep() -> CheckResult:
    """ADVISORY — last hitl_park_sweep outcome (never gates readiness).

    The FAR-604 D2 HITL park-on-expiry sweep (``saq_worker.hitl_park_sweep``)
    runs in the SYSTEM WORKER process every 5 min and persists its outcome
    (``parked`` + ``last_run_at``) to ``saq:cron:stats:hitl_park_sweep``
    (qa F5). Parking is post-D1 hygiene (a parked run holds no pipeline
    slot), so a dead sweep does not wedge admission — but the "expired —
    parked" visibility silently stops, so a missing or >15min-stale key
    reports "degraded" to alert operators while the app remains healthy.
    Fail-open on Redis read errors.
    """
    return await _check_sweep_stats_advisory(_HITL_PARK_SWEEP_STATS_KEY, _HITL_PARK_SWEEP_STALE_SECONDS, "parked")


async def _check_runner_workspace_reconcile() -> CheckResult:
    """ADVISORY — last runner_workspace_reconcile outcome (never gates readiness).

    The FAR-590 D4 Bundled Runner orphan reconciler
    (``saq_worker.runner_workspace_reconcile``) runs in the SYSTEM WORKER
    process every 5 min and persists its outcome (``orphans_destroyed`` +
    ``last_run_at``) to ``saq:cron:stats:runner_workspace_reconcile``. A
    silently dead sweep stops labelled-container leak repair (and even the
    log-only soak detection), so a missing or >15min-stale key reports
    "degraded" to alert operators while the app remains healthy. Fail-open
    on Redis read errors.

    FAR-1201 follow-up: when the deployment has NO Docker endpoint at all,
    the sweep skips every tick (``docker_endpoint_skip_reason``), so the
    liveness readings are meaningless — the check reads an explicit
    "not applicable on this deployment: <reason>" instead (see
    ``_check_sweep_stats_advisory``'s *not_applicable* contract: persisted
    sweep errors still surface, so an asymmetric raw-socket config cannot
    hide a real worker failure). Status stays ``ok`` because a fourth
    status value would require regenerating ``frontend/src/lib/api/schema.ts``
    (schema-freshness CI gate) and updating ``uptime-monitor.yml`` (which
    alerts on ANY non-ok sub-check) — both outside this change's footprint;
    a non-ok status here would keep engine-less deployments permanently
    alerting, the exact problem this skip fixes. Configured deployments
    take the unchanged stats path, so configured-but-unreachable engines
    keep reporting degraded.
    """
    skip_reason = docker_endpoint_skip_reason()
    return await _check_sweep_stats_advisory(
        _RUNNER_WORKSPACE_RECONCILE_STATS_KEY,
        _RUNNER_WORKSPACE_RECONCILE_STALE_SECONDS,
        "orphans_destroyed",
        not_applicable=skip_reason,
    )


async def _check_runner_marker_sweep() -> CheckResult:
    """ADVISORY — last runner_marker_sweep outcome (never gates readiness).

    The FAR-594 D8 runner dispatch-marker reconciliation sweep
    (``saq_worker.runner_marker_sweep``) runs in the SYSTEM WORKER process
    every 5 min (plus the 60s dispatcher_reconcile path) and persists its
    outcome (``cleared`` + ``last_run_at``) to
    ``saq:cron:stats:runner_marker_sweep`` (qa F9). A silently dead sweep
    lets stale markers accumulate as phantom capacity and the D8 rollback
    signal (``runner.capacity.violation``) goes dark — the gate itself fails
    open, so nothing wedges, but the capacity numbers rot. A missing or
    >15min-stale key reports "degraded" to alert operators while the app
    remains healthy. Fail-open on Redis read errors.
    """
    return await _check_sweep_stats_advisory(
        _RUNNER_MARKER_SWEEP_STATS_KEY,
        _RUNNER_MARKER_SWEEP_STALE_SECONDS,
        "cleared",
    )


async def _check_runner_health_probe() -> CheckResult:
    """ADVISORY — last runner_health_probe outcome (never gates readiness).

    The FAR-591 D5 per-machine runner health probe
    (``saq_worker.runner_health_probe``) runs in the SYSTEM WORKER process
    every 60s and persists its outcome (``orgs_probed`` + ``orgs_failed`` +
    ``transitions`` + ``last_run_at``) to
    ``saq:cron:stats:runner_health_probe`` (qa F3). A silently dead probe
    stops refreshing the probe cache, so every org's Runners strip ages to
    "status unknown" and transition alerts (``runner_unavailable``) stop
    firing — a missing or >180s-stale key reports "degraded" to alert
    operators while the app remains healthy. Fail-open on Redis read
    errors.
    """
    return await _check_sweep_stats_advisory(
        _RUNNER_HEALTH_PROBE_STATS_KEY,
        _RUNNER_HEALTH_PROBE_STALE_SECONDS,
        "orgs_probed",
    )


async def _check_fleet_system_crons() -> CheckResult:
    """Fleet-wide system-cron liveness — the sole readiness path (ADR 043 / FAR-1158).

    Deployment-scoped: readiness gates on ANY machine in the deployment
    having a fresh ``fire_due_triggers`` heartbeat — a fleet-wide scheduler
    death fails readiness, a single dead worker machine (or a backend pod on
    a different node) does not. Heartbeat keys are enumerated with SCAN, never
    KEYS (KEYS blocks Redis on large keyspaces).

    Determination contract (module docstring): a fresh heartbeat read resets
    the probe counter; a confirmed-stale store (read succeeded, no fresh
    heartbeat anywhere) and an unreadable store (undeterminable) both advance
    the SAME counter — degraded during probe grace, ``unavailable`` after the
    grace tier under the default ``SAQ_HARD_GATE=true``; boot grace reports
    ok; ``SAQ_HARD_GATE=false`` is alert-only.
    """
    global _consecutive_cron_stale_probes
    settings = get_settings()
    probe_host = resolve_instance_identity()

    r: aioredis.Redis | None = None
    read_failed = False
    fresh = False
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        now = time.time()
        async for key in r.scan_iter(match="saq:cron:heartbeat:fire_due_triggers:*"):
            raw = await r.get(key)
            if raw is None:
                continue
            try:
                last_ts = float(raw)
            except (TypeError, ValueError):
                # Unparseable heartbeat: the store WAS read (confirmed), the
                # value is just garbage — treat as not-fresh, never as a read
                # failure. Fail closed on corrupt state, not open.
                continue
            if now - last_ts <= _CRON_STALE_SECONDS:
                fresh = True
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_fleet_system_crons redis read failed: %s", exc)
        read_failed = True
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()

    if fresh:
        _consecutive_cron_stale_probes = 0
        return CheckResult(status="ok", detail="system-cron heartbeat fresh on at least one machine")

    if read_failed:
        problem = f"system-cron check undeterminable: redis read failed, probe_host={probe_host}"
    else:
        problem = f"no fresh fire_due_triggers cron heartbeat on any machine, probe_host={probe_host}"

    if _in_fleet_boot_grace():
        return CheckResult(status="ok", detail=f"{problem} (within {_FLEET_BOOT_GRACE_SECONDS}s boot grace)")

    _consecutive_cron_stale_probes += 1
    probes = _consecutive_cron_stale_probes
    if settings.saq_hard_gate:
        if probes >= _STALE_PROBE_LIMIT:
            return CheckResult(status="unavailable", detail=f"{problem} ({probes} consecutive probes)")
        return CheckResult(status="degraded", detail=f"{problem} ({probes}/{_STALE_PROBE_LIMIT} probes)")
    # SAQ_HARD_GATE=false: alert-only — never gates readiness (plan F7/F8).
    _log.warning("health.system_cron_fleet_relaxed probes=%d %s", probes, problem)
    return CheckResult(status="ok", detail=f"{problem} (SAQ_HARD_GATE=false, alert-only)")


async def _check_system_crons() -> CheckResult:
    """System-cron liveness watchdog — deployment-scoped, ONE path (ADR 043 / FAR-1158).

    Delegates unconditionally to ``_check_fleet_system_crons``. The former
    machine-scoped branch (``FLY_PROCESS_GROUP`` routing + THIS-host
    ``saq:cron:heartbeat:fire_due_triggers:{FLY_MACHINE_ID|HOSTNAME}`` lookup)
    is removed: whether the deployment's cron scheduler is alive is a
    deployment question, not a host question — a backend pod sharing no node
    with its workers can never write nor observe a co-located heartbeat
    (FAR-1158). Per-instance worker liveness remains ADR 021's port-8082
    check, unchanged.
    """
    return await _check_fleet_system_crons()


@router.get("/healthz")
@handle_db_errors("health.liveness")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz/ready")
@handle_db_errors("health.readiness")
async def readiness(response: Response) -> ReadinessResponse:
    (
        db_check,
        redis_check,
        cp_check,
        mig_check,
        saq_check,
        cron_check,
        dr_check,
        srr_check,
        sr_check,
        hps_check,
        rwr_check,
        rms_check,
        rhp_check,
    ) = await asyncio.gather(
        _check_database(),
        _check_redis(),
        _check_checkpointer(),
        _check_migrations(),
        _check_saq_workers(),
        _check_system_crons(),
        _check_dispatcher_reconcile(),
        _check_stale_run_recovery(),
        _check_slot_reconciliation(),
        _check_hitl_park_sweep(),
        _check_runner_workspace_reconcile(),
        _check_runner_marker_sweep(),
        _check_runner_health_probe(),
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
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        # FAR-604 F6: a silently dead slot-reconciliation sweep re-opens the
        # admission wedge invisibly, so it alerts here without gating.
        "slot_reconciliation": sr_check,
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        # FAR-604 D2 / qa F5: a dead park sweep only delays the "expired —
        # parked" transition (no capacity wedge), so it stays alert-only.
        "hitl_park_sweep": hps_check,
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        # FAR-590 D4: a dead orphan reconciler stops labelled-container leak
        # repair (destroy path soak-gated, so no capacity wedge), so it stays
        # alert-only.
        "runner_workspace_reconcile": rwr_check,
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        # FAR-594 D8 (qa F9): a dead marker sweep lets stale markers
        # accumulate as phantom capacity and the rollback signal goes dark;
        # the gate fails open, so it stays alert-only.
        "runner_marker_sweep": rms_check,
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        # FAR-591 D5 (qa F3): a dead health probe stops refreshing the
        # probe cache (strips age to "status unknown") and silences the
        # transition alerts, so it stays alert-only.
        "runner_health_probe": rhp_check,
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
        unavailable_checks = sorted(name for name, check in checks.items() if check.status == "unavailable")
        degraded_checks = sorted(name for name, check in checks.items() if check.status == "degraded")
        log_service_unavailable(
            "readiness_check_unavailable",
            route="health.readiness",
            detail=f"unavailable={unavailable_checks} degraded={degraded_checks}",
        )
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
