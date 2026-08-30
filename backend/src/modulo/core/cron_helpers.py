"""SAQ scheduler helpers — per-item fire jobs, fire_due_triggers, dispatcher_reconcile.

Plan F1 / F3c / F3d. This module is the SAQ fire scheduler (replaced Celery beat)
tasks (``CronFireTask`` / ``PollingFireTask`` / ``ReportFireTask``, all removed
in PR C). All fire logic is reimplemented async against the shared DB session
pattern.

Multi-machine safety (F1, the single most important invariant):
``fire_due_triggers`` (a system cron on EVERY machine) advances ``next_fire_at``
ATOMICALLY at enqueue time — a conditional ``UPDATE ... WHERE next_fire_at <= now()
RETURNING id`` — and enqueues a per-item fire job ONLY for returned rows. A second
machine's tick sees ``next_fire_at`` already advanced and skips. Per-item fire
jobs get unique dedupe keys (``fire:{trigger_id}:{fire_epoch}``) so SAQ dedupe
never suppresses a distinct fire. ``next_fire_at`` is NEVER advanced at per-item
job execution.

Lost-epoch on crash (documented accepted): if the process dies after the atomic
UPDATE commits but before enqueue, one epoch is missed and the trigger self-heals
on the next tick. Enqueue failures after the UPDATE ingest an ``error_event``
(source='saq') and rely on next-tick re-fire; the >=1h missed-fire alert is a
follow-up owned by the hold monitor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from redis.asyncio import Redis as AsyncRedis
from saq.queue.redis import RedisQueue
from sqlalchemy import or_, select, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.connectors._rate_bucket import SharedBudgetUnavailableError
from modulo.core.dispatch import SAQ_RUN_TIMEOUT
from modulo.core.exceptions import TriggersPausedError
from modulo.core.pipeline_engine.error_codes import sanitize_error_text

# FAR-190 streak engine lives in its own module (extracted so cron_helpers can
# stay focused on scheduling). Re-exported here for the dispatcher_reconcile
# wiring and for callers that historically imported them from cron_helpers.
# trigger_streak imports cron_helpers helpers lazily (never at module import
# time), so this module-level import cannot form a circular import.
from modulo.core.trigger_streak import (
    enforce_no_delivery_streaks,
)
from modulo.db.models.run import ACTIVE_RUN_STATUSES, ONGOING_ACTIVE_STATUSES, Run
from modulo.db.settings_resolver import PAUSE_SKIP_REASON, org_is_paused, org_row_is_paused
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEM_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# Per-item fire job knobs (plan F5): timeout=300, retries=2 (ONE retry),
# heartbeat=30, ttl=300. Reports share the runs queue as bounded jobs.
FIRE_JOB_TIMEOUT = 300
FIRE_JOB_HEARTBEAT = 30
FIRE_JOB_RETRIES = 2
FIRE_JOB_TTL = 300

# Advisory-lock SQL and paused-trigger skip log message (S1192). Pure aliases.
_SQL_TRY_ADVISORY_LOCK = "SELECT pg_try_advisory_xact_lock(:key1, :key2)"
_LOG_TRIGGERS_PAUSED_SKIP = "triggers.paused.skip trigger=%s org=%s"

# Missed-fire catch-up (2026-08-10 incident). The fire_due_triggers tick
# advances next_fire_at ATOMICALLY (claiming the epoch) and THEN enqueues the
# per-item fire job. If the worker machine dies between the two — or the
# enqueue fails — the epoch is consumed but never fired: a full missed cadence
# for a daily cron, with nothing ever re-firing it. Two guards close the gap:
#   1. The advance is rolled back when the enqueue fails (guarded, see
#      ``_rollback_cron_advance``) so the next tick re-selects the epoch.
#   2. A bounded catch-up scan re-fires a consumed epoch whose ``last_fired_at``
#      is genuinely behind (see ``_fire_missed_cron_epochs``).
# The grace/bound semantics mirror the hourly missed-fire alert
# (``SAQ_MISSED_FIRE_GRACE_SECONDS = 300`` in modulo.core.error_tracking).
CATCHUP_GRACE_SECONDS = 300  # a fire more than cadence + grace behind is "missed"
CATCHUP_BOUND_SECONDS = 48 * 3600  # only re-fire misses within the last 48h
_CATCHUP_MARKER_PREFIX = "saq:cron:catchup"
_CATCHUP_MARKER_TTL = FIRE_JOB_TIMEOUT * (FIRE_JOB_RETRIES + 1) + 60  # 960s >= worst-case in-flight (300*3)

# Report delivery (plan F1): failure backs off next_send_at +5min; deactivate
# after 5 consecutive failures. NEVER re-enqueue every 30s.
REPORT_BACKOFF_SECONDS = 300
REPORT_MAX_CONSECUTIVE_FAILURES = 5
_REPORT_FAILURE_COUNTER_TTL = 6 * 3600  # 6h — long enough to count 5 x 5min

# Ongoing trigger (FAR-158) — worker-pool top-up semantics. The scheduler tick
# is 60s, so a scan interval below that is meaningless (also validated >= 60 at
# create/update). ``next_fire_at`` is advanced by at least this far on every
# enqueue so a topped-up trigger is not re-scanned every tick.
ONGOING_MIN_INTERVAL_SECONDS = 60

# Per-tick enqueue cap for the ongoing scan — a burst of newly-created ongoing
# triggers must not flood the runs queue in one tick (mirrors
# ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK). Past the cap a row STILL advances
# next_fire_at (skip-not-defer — no double-fire on the next tick).
ONGOING_MAX_ENQUEUE_PER_TICK = 50

# Persistent-failure deactivation (report pattern): after this many consecutive
# no_pipeline / invalid-snapshot failures the ongoing trigger is deactivated to
# stop the ~1440 events/day flood on a deleted pipeline (the next_fire_at
# advance stops once active=False).
ONGOING_MAX_CONSECUTIVE_FAILURES = 5
_ONGOING_FAILURE_COUNTER_TTL = 6 * 3600  # 6h


# dispatcher_reconcile (system cron) — every 60s.
RECONCILE_STALE_HEARTBEAT_FACTOR = 2  # 2 * SAQ_JOB_HEARTBEAT = 600s

# Bounded re-dispatch window for stranded capacity-marked pending runs
# (FAR-108). A run demoted to ``pending`` with ``error_code`` in
# (``org_capacity_limited``, ``pipeline_capacity``) used to wait for the
# stale-run sweep's multi-minute stranded window (~12-min TTL + up-to-5-min
# sweep lag) — the observed ~18-minute pending gap on a busy deployment.
# dispatcher_reconcile (every 60s) now re-dispatches such a run once its
# heartbeat is older than this window (or NULL — a never-claimed org-capacity-
# deferred run). The heartbeat gate throttles the executor sandbox-cap
# claim/demote churn loop to one attempt per window; ``dispatch_run`` re-checks
# pipeline + org run concurrency atomically so a still capacity-blocked run is
# re-deferred without churn. The stale-run sweep's stranded branch remains the
# durable backstop.
CAPACITY_REDISPATCH_SECONDS = 120

# Claimed-but-nodeless zombie repair (2026-08-05). A SAQ run that has been
# 'running' with a FRESH heartbeat but has NEVER dispatched a node (zero
# LangGraph checkpoints for its thread) after SAQ_CLAIMED_NODELESS_MINUTES is a
# zombie: the execute_run watchdog (pipeline_execution.zombie_watchdog) normally
# fails these at SAQ_SETUP_GRACE_SECONDS, but a wedged worker process that can
# still refresh the DB heartbeat would otherwise slip through. This branch
# terminal-fails the run (never re-dispatches — a re-dispatch could
# double-execute a live-but-stuck execute_run).
_NODELESS_ZOMBIE_ERROR_CODE = "executor_stalled"

# ---------------------------------------------------------------------------
# Durable dispatch recovery (PR dist/runtime-reconcile, B2/B3).
# dispatch_run now leaves a failed-enqueue run ``pending`` with
# ``enqueue_failed_at`` stamped (never terminal-fails). dispatcher_reconcile
# re-dispatches it on a bounded interval with a per-tick cap, and terminal-fails
# it (``dispatch_failed``) only when Redis is verifiably reachable AND the
# marker is older than the TTL backstop.
# ---------------------------------------------------------------------------

# Min-redispatch heartbeat gate for the enqueue-failed branch: a run is
# re-dispatched only when its heartbeat is NULL or older than this window
# (~120s), matching the capacity redispatch gate's cadence so a fresh run is not
# hot-loop re-dispatched on every 60s tick.
ENQUEUE_FAILED_REDISPATCH_SECONDS = 120

# Per-tick re-dispatch cap for the enqueue-failed branch — a Redis outage
# window that hit hundreds of webhooks must not flood the queue the moment it
# recovers. Beyond the cap the remaining rows are deferred to later ticks.
ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK = 50

# TTL backstop: an enqueue-failed run whose marker is older than this is
# terminal-failed with ``dispatch_failed`` — but ONLY when Redis is verifiably
# reachable (lightweight ping). Redis down -> keep pending.
ENQUEUE_FAILED_TTL_BACKSTOP_MINUTES = 60

_DISPATCH_FAILED_ERROR_CODE = "dispatch_failed"

# Synthetic error_detail for the genuinely detail-less failure writers (P7'):
# applied ONLY where detail is currently NULL — never overwrites real detail.
# Derived from the ERROR_CODE_REGISTRY guidance in
# ``modulo.core.pipeline_engine.error_codes``.
_EXECUTOR_SUPERSEDED_ERROR_DETAIL = "Superseded by a newer run."
_CLAIM_CAP_EXHAUSTED_ERROR_DETAIL = "Claim capacity exhausted."
_DISPATCH_FAILED_ERROR_DETAIL = "Run was never dispatched (enqueue/Redis failure)."

# ---------------------------------------------------------------------------
# Age-bound mid-graph wedge terminalizer (B4). A run stuck mid-graph for longer
# than the max plausible run duration is wedged (heartbeat may still be fresh —
# the mid-graph stall can outlive the SAQ timeout window). Default is the max
# SAQ run timeout in minutes, floored at 2h, plus a 15-minute skew (~135 min).
# ---------------------------------------------------------------------------
_MID_GRAPH_WEDGE_MAX_AGE_MINUTES = max(SAQ_RUN_TIMEOUT // 60, 120) + 15
_EXECUTOR_SUPERSEDED_ERROR_CODE = "executor_superseded"

# Exported reconciliation stats for /healthz/ready (PR D — hitl-health-obs).
# The ``age_terminalized`` / ``enqueue_failed_ttl_terminalized`` keys are
# semantic aliases of the executor-superseded (age-bound wedge) and
# enqueue-failed-TTL terminalizers respectively, exposed alongside the
# implementation-named counters so ops has one vocabulary.
_dispatcher_reconcile_stats: dict[str, Any] = {
    "last_run_at": None,
    "scanned": 0,
    "repaired": 0,
    "skipped": 0,
    "redis_errors": 0,
    "deduped": 0,
    "nodeless_failed": 0,
    "nodeless_redispatched": 0,
    "claim_cap_terminalized": 0,
    "mid_graph_wedge_terminalized": 0,
    "age_terminalized": 0,
    "dispatch_failed_terminalized": 0,
    "enqueue_failed_ttl_terminalized": 0,
    "enqueue_failed_redispatched": 0,
    "enqueue_failed_capped": 0,
    "streak_scanned": 0,
    "streak_deactivated": 0,
    "streak_capped": 0,
    "streak_alerts": 0,
    "streak_notify_failed": 0,
    "run_api_key_scanned": 0,
    "run_api_key_revoked": 0,
    "run_api_key_errors": 0,
    "rollback_thresholds_checked": 0,
    "rollback_thresholds_flagged": 0,
}


def set_dispatcher_reconcile_stats(stats: dict[str, Any]) -> None:
    """Store the last dispatcher_reconcile outcome for /healthz/ready."""
    _dispatcher_reconcile_stats["last_run_at"] = datetime.now(UTC).isoformat()
    _dispatcher_reconcile_stats["scanned"] = stats.get("scanned", 0)
    _dispatcher_reconcile_stats["repaired"] = stats.get("repaired", 0)
    _dispatcher_reconcile_stats["skipped"] = stats.get("skipped", 0)
    _dispatcher_reconcile_stats["redis_errors"] = stats.get("redis_errors", 0)
    _dispatcher_reconcile_stats["deduped"] = stats.get("deduped", 0)
    _dispatcher_reconcile_stats["nodeless_failed"] = stats.get("nodeless_failed", 0)
    _dispatcher_reconcile_stats["nodeless_redispatched"] = stats.get("nodeless_redispatched", 0)
    _dispatcher_reconcile_stats["capacity_deferred"] = stats.get("capacity_deferred", 0)
    _dispatcher_reconcile_stats["claim_cap_terminalized"] = stats.get("claim_cap_terminalized", 0)
    _dispatcher_reconcile_stats["mid_graph_wedge_terminalized"] = stats.get("mid_graph_wedge_terminalized", 0)
    _dispatcher_reconcile_stats["age_terminalized"] = stats.get("age_terminalized", 0)
    _dispatcher_reconcile_stats["dispatch_failed_terminalized"] = stats.get("dispatch_failed_terminalized", 0)
    _dispatcher_reconcile_stats["enqueue_failed_ttl_terminalized"] = stats.get("enqueue_failed_ttl_terminalized", 0)
    _dispatcher_reconcile_stats["enqueue_failed_redispatched"] = stats.get("enqueue_failed_redispatched", 0)
    _dispatcher_reconcile_stats["enqueue_failed_capped"] = stats.get("enqueue_failed_capped", 0)
    _dispatcher_reconcile_stats["streak_scanned"] = stats.get("streak_scanned", 0)
    _dispatcher_reconcile_stats["streak_deactivated"] = stats.get("streak_deactivated", 0)
    _dispatcher_reconcile_stats["streak_capped"] = stats.get("streak_capped", 0)
    _dispatcher_reconcile_stats["streak_alerts"] = stats.get("streak_alerts", 0)
    _dispatcher_reconcile_stats["streak_notify_failed"] = stats.get("streak_notify_failed", 0)
    _dispatcher_reconcile_stats["run_api_key_scanned"] = stats.get("run_api_key_scanned", 0)
    _dispatcher_reconcile_stats["run_api_key_revoked"] = stats.get("run_api_key_revoked", 0)
    _dispatcher_reconcile_stats["run_api_key_errors"] = stats.get("run_api_key_errors", 0)
    _dispatcher_reconcile_stats["rollback_thresholds_checked"] = stats.get("rollback_thresholds_checked", 0)
    _dispatcher_reconcile_stats["rollback_thresholds_flagged"] = stats.get("rollback_thresholds_flagged", 0)


# Shared Redis key for dispatcher_reconcile outcome stats (cross-process).
# The dispatcher_reconcile cron runs in the SYSTEM WORKER process; /healthz/ready
# runs in the WEB process (PR dist/separate-workers: workers on ``worker``
# machines, uvicorn on ``app`` machines). The in-process dict above is invisible
# to the health check, so the cron persists its outcome here every tick and the
# health check reads this key.
DISPATCHER_RECONCILE_STATS_KEY = "saq:cron:stats:dispatcher_reconcile"


async def write_dispatcher_reconcile_stats(redis_client: AsyncRedis, stats: dict[str, Any]) -> None:
    """Persist the last dispatcher_reconcile outcome to the shared Redis store.

    The system worker (which runs the cron) and the web process (which serves
    /healthz/ready) are SEPARATE processes, so a process-local dict can never be
    observed by the health check. Writing the outcome — including a fresh
    ``last_run_at`` — to Redis lets any process see whether the cron actually
    ran. Best-effort: a persistence failure must never crash the reconcile tick.
    """
    payload: dict[str, Any] = dict(stats)
    payload["last_run_at"] = datetime.now(UTC).isoformat()
    try:
        await redis_client.set(DISPATCHER_RECONCILE_STATS_KEY, json.dumps(payload))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.dispatcher_reconcile stats persist failed")


async def read_dispatcher_reconcile_stats(redis_client: AsyncRedis) -> dict[str, Any] | None:
    """Read the persisted reconcile outcome; ``None`` when the cron has never
    run (or its persistence failed / the payload is unparsable).

    Raises on Redis errors — the health check decides fail-open behaviour.
    """
    raw = await redis_client.get(DISPATCHER_RECONCILE_STATS_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


_ACTIVE_STATUSES = ACTIVE_RUN_STATUSES


def _machine_hostname() -> str:
    """Machine identity shared with the health gate (FLY_MACHINE_ID or hostname)."""
    return os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"


def _cron_liveness_key(function: str) -> str:
    """Per-machine system-cron liveness key watched by /healthz/ready (plan F8).

    ``fire_due_triggers`` writes this on every tick; the health check 503s a
    machine whose key is stale by more than 2x the cron cadence, so Fly removes
    a machine whose system-worker cron scheduler is silently dead (a worker
    loop can stay alive while its cron scheduler is stuck).
    """
    return f"saq:cron:heartbeat:{function}:{_machine_hostname()}"


_ENGINE: AsyncEngine | None = None
_SYSTEM_ENGINE: AsyncEngine | None = None
_ENGINE_LOCK = threading.Lock()


def _get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                # D4: one engine per process — the shared factory (Fly/HAProxy
                # knobs) instead of a second singleton. Lazy import keeps
                # db.session's module-level `engine` out of the worker graph.
                from modulo.db.session import get_shared_engine

                _ENGINE = get_shared_engine()
    return _ENGINE


def _get_system_engine() -> AsyncEngine:
    """Engine for cross-org system crons using the modulo_system role.

    FAIL CLOSED when MODULO_SYSTEM_DATABASE_URL is not set: cross-org system
    crons require the modulo_system role (LOGIN, BYPASSRLS). Falling back to the
    regular engine would run them as modulo_app, which is NOBYPASSRLS (see
    bootstrap_role.py: the app role asserts ``rolbypassrls = false``), so any
    RLS-scoped read silently returns zero rows — a silent no-op that masks a
    misconfigured deployment. Instead, a clear ``RuntimeError`` is raised. The
    engine is created LAZILY on first use inside a cron invocation (never at
    worker startup), so this raise fails only the specific system cron — which
    SAQ logs — while the worker stays up to serve every other job. Mirrors
    ``saq_worker._get_system_async_engine``.
    """
    global _SYSTEM_ENGINE
    if _SYSTEM_ENGINE is None:
        settings = get_settings()
        if settings.modulo_system_database_url:
            from sqlalchemy.ext.asyncio import create_async_engine

            _SYSTEM_ENGINE = create_async_engine(
                settings.modulo_system_database_url,
                pool_pre_ping=True,
                connect_args={"ssl": False, "statement_cache_size": 0},
            )
        else:
            _log.error(
                "cron_helpers.system_engine_misconfigured",
                extra={
                    "reason": (
                        "MODULO_SYSTEM_DATABASE_URL not set — refusing to run cross-org "
                        "system crons as modulo_app (NOBYPASSRLS); RLS-scoped reads would "
                        "silently return zero rows. Set the modulo_system role URL."
                    )
                },
            )
            raise RuntimeError(
                "MODULO_SYSTEM_DATABASE_URL is not set: cross-org system crons require "
                "the modulo_system role (LOGIN, BYPASSRLS). Refusing to fail open to "
                "modulo_app (NOBYPASSRLS), which would silently return zero rows."
            )
    return _SYSTEM_ENGINE


def _open_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_get_engine(), expire_on_commit=False, autobegin=False)


def _open_system_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory for cross-org system crons (modulo_system role)."""
    return async_sessionmaker(_get_system_engine(), expire_on_commit=False, autobegin=False)


# ---------------------------------------------------------------------------
# Validation + next-fire computation (relocated from cron_scheduler.py)
# ---------------------------------------------------------------------------


def validate_cron_expression(expression: str, timezone: str = "UTC") -> str | None:
    """Validate a cron expression.

    Returns ``None`` if valid, or an error message string if invalid.
    """
    try:
        croniter(expression)
    except (ValueError, KeyError) as exc:
        return str(exc)
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(timezone)
    except (ValueError, KeyError, TypeError) as exc:
        return f"Invalid timezone: {exc}"
    return None


def compute_next_fire(
    cron_expression: str,
    after: datetime | None = None,
    *,
    timezone: str = "UTC",
) -> datetime:
    """Compute the next fire time in *timezone* and return canonical UTC.

    If *after* is ``None``, the current UTC time is used. Naive values are
    interpreted as UTC for backwards compatibility. ``croniter`` defines DST
    handling: nonexistent local times advance to the first valid instant and
    ambiguous local times use the first occurrence.
    """
    base = after or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    local_base = base.astimezone(ZoneInfo(timezone))
    cron = croniter(cron_expression, local_base)
    next_dt = cron.get_next(datetime)
    if not isinstance(next_dt, datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=local_base.tzinfo)
    return next_dt.astimezone(UTC)


def compute_next_send(cron_expression: str, after: datetime | None = None) -> datetime:
    """Compute the next send time for a report cron expression."""
    base = after or datetime.now(UTC)
    cron = croniter(cron_expression, base)
    next_dt = cron.get_next(datetime)
    if not isinstance(next_dt, datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    return next_dt


# ---------------------------------------------------------------------------
# Shared helpers (relocated from cron_scheduler.py)
# ---------------------------------------------------------------------------


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Set org-scoped RLS context for a cron/background transaction."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.organisation_id', :val, true)"),
            {"val": str(org_id)},
        )
        await session.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
    else:
        session.info["org_id"] = org_id


async def _count_active_runs(session: AsyncSession, trigger_id: uuid.UUID) -> int:
    from sqlalchemy import func, or_

    from modulo.db.models.run import Run

    result = await session.execute(
        select(func.count()).where(
            Run.trigger_id == trigger_id,
            Run.status.in_(_ACTIVE_STATUSES),
            or_(Run.cancellation_requested.is_(False), Run.cancellation_requested.is_(None)),
        )
    )
    return int(result.scalar_one() or 0)


async def _count_ongoing_runs(session: AsyncSession, trigger_id: uuid.UUID) -> int:
    """Count a trigger's in-flight runs for the ``ongoing`` top-up (FAR-158).

    Status-only count over ``ONGOING_ACTIVE_STATUSES`` (pending/running/claimed
    — see the set's comment in ``modulo.db.models.run`` for why awaiting_human
    is excluded: a never-answered HITL gate must not permanently starve the
    pool). DELIBERATELY NO ``cancellation_requested`` filter — unlike
    ``_count_active_runs``, a run whose cancellation was requested has already
    transitioned to the terminal ``cancelled`` status, so it falls out of the
    status set naturally and needs no extra predicate. This keeps the count a
    simple index-backed ``(trigger_id, status)`` scan (migration 0086).
    """
    from sqlalchemy import func

    from modulo.db.models.run import Run

    result = await session.execute(
        select(func.count()).where(
            Run.trigger_id == trigger_id,
            Run.status.in_(ONGOING_ACTIVE_STATUSES),
        )
    )
    return int(result.scalar_one() or 0)


def _build_trigger_event(
    *,
    org_id: uuid.UUID,
    trigger: Any,
    trigger_type: str,
    payload_salt: str,
    result: str,
    run_id: uuid.UUID | None,
    error_detail: str | None,
    sanitize: bool,
) -> Any:
    """Construct a TriggerEvent row (shared by the three fire-job loggers).

    ``payload_salt`` varies per trigger type so each type's ``raw_payload_hash``
    is distinct (cron uses a static salt; polling/ongoing mix in the trigger id
    and result). ``sanitize`` mirrors the per-type ``error_detail`` policy —
    cron and polling sanitise secrets, ongoing deliberately does not (it
    preserves the raw failure detail for the ongoing failure counter path).
    """
    from modulo.db.models.trigger_event import TriggerEvent

    payload_hash = hashlib.sha256(payload_salt.encode()).hexdigest()
    if error_detail is None:
        detail: str | None = None
    elif sanitize:
        detail = sanitize_error_text(error_detail)
    else:
        detail = error_detail
    return TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type=trigger_type,
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=detail,
    )


async def _add_trigger_event(
    session: AsyncSession,
    *,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_type: str,
    payload_salt: str,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
    sanitize: bool,
) -> Any:
    """Build, add and flush one TriggerEvent row."""
    event = _build_trigger_event(
        org_id=org_id,
        trigger=trigger,
        trigger_type=trigger_type,
        payload_salt=payload_salt,
        result=result,
        run_id=run_id,
        error_detail=error_detail,
        sanitize=sanitize,
    )
    session.add(event)
    await session.flush()
    return event


async def _log_event(
    session: AsyncSession,
    *,
    trigger: Any,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> Any:
    """Log one ``cron`` TriggerEvent."""
    return await _add_trigger_event(
        session,
        trigger=trigger,
        org_id=org_id,
        trigger_type="cron",
        payload_salt="cron",
        result=result,
        run_id=run_id,
        error_detail=error_detail,
        sanitize=True,
    )


async def _log_poll_event(
    session: AsyncSession,
    *,
    trigger: Any,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> Any:
    """Log one ``polling`` TriggerEvent."""
    return await _add_trigger_event(
        session,
        trigger=trigger,
        org_id=org_id,
        trigger_type="polling",
        payload_salt=f"polling:{trigger.id}:{result}",
        result=result,
        run_id=run_id,
        error_detail=error_detail,
        sanitize=True,
    )


async def _log_ongoing_event(
    session: AsyncSession,
    *,
    trigger: Any,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> Any:
    """Log one ``ongoing`` TriggerEvent (mirrors ``_log_poll_event``, FAR-158)."""
    return await _add_trigger_event(
        session,
        trigger=trigger,
        org_id=org_id,
        trigger_type="ongoing",
        payload_salt=f"ongoing:{trigger.id}:{result}",
        result=result,
        run_id=run_id,
        error_detail=error_detail,
        sanitize=False,
    )


async def _ingest_saq_error(
    _session: AsyncSession,
    org_id: uuid.UUID,
    *,
    function: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Ingest an error event with source='saq' (plan F3d/F1 enqueue-failure alert).

    Runs in its own session/transaction (the caller's transaction stays intact
    — ``_session`` is vestigial and kept only for caller symmetry) and never
    raises — error ingestion must not crash the scheduler tick.

    If ``org_id`` is the nil UUID (system error without tenant context), the
    error is logged and skipped — the ``error_events`` FK constraint requires a
    real organisation row.
    """
    if org_id == SYSTEM_ORG_ID:
        _log.error(
            "SAQ system error (no tenant context) — skipping DB ingest: function=%s message=%s",
            function,
            message,
        )
        return

    import os

    try:
        from modulo.core.error_tracking import ErrorIngestionService
        from modulo.db.rls import set_rls_org
        from modulo.version import get_version

        async with _open_factory()() as ingest_session, ingest_session.begin():
            await set_rls_org(ingest_session, org_id)
            await ErrorIngestionService().ingest(
                ingest_session,
                org_id,
                {
                    "level": "error",
                    "message": message,
                    "source": "saq",
                    "context_json": {"function": function, **(context or {})},
                    "environment": os.environ.get("MODULO_ENV", "development"),
                    "version": get_version(),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cron_helpers.ingest_saq_error_failed function=%s", function)
        # Never let error ingestion crash the scheduler tick.


# ---------------------------------------------------------------------------
# Per-item fire jobs (runs worker)
# ---------------------------------------------------------------------------


async def _org_is_paused_degraded(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Org-wide pause check for the per-item fire jobs, degraded on pre-migration.

    A ``ProgrammingError`` (missing ``triggers_paused`` column on a pre-migration
    DB) returns ``False`` (not paused) — mirroring the scheduler's not-paused
    choice so a legacy schema does NOT turn every cron/polling job into a
    dead-lettered failure. The read runs inside a SAVEPOINT so the failed
    statement never poisons the per-item transaction (Postgres aborts a
    transaction on any statement error; without the savepoint the next query
    in the same transaction raises PendingRollbackError).

    Any OTHER ``SQLAlchemyError`` propagates so the job fails and SAQ retries —
    never fabricate "paused" on a DB error.
    """
    try:
        async with session.begin_nested():
            return await org_is_paused(session, org_id)
    except ProgrammingError:
        _log.info("org_pause_check.degraded org=%s (pre-migration schema)", org_id)
        return False


async def _concurrency_limit_skip(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    active_count: int,
) -> dict[str, Any]:
    """Log a concurrency-limited fire and record the attempt (skip-not-defer)."""
    from sqlalchemy import update

    from modulo.db.models.trigger import Trigger

    await _log_event(
        session,
        trigger=trigger,
        org_id=org_id,
        result="concurrency_limit_reached",
        error_detail=(f"Active runs: {active_count}, limit: {trigger.max_concurrent_runs}"),
    )
    # Finding 2 (review PR #982): record the attempt (skip-not-defer).
    # A concurrency-limited fire is CONSUMED, not missed — without this
    # last_fired_at stays stale and the catch-up scan re-fires the epoch
    # every marker TTL forever. The next NORMAL scheduled fire handles
    # the future.
    await session.execute(update(Trigger).where(Trigger.id == trigger.id).values(last_fired_at=datetime.now(UTC)))
    return {
        "status": "skipped",
        "reason": "concurrency_limit",
        "active_runs": active_count,
    }


async def _spend_limit_skip(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    spend_limit: Any,
) -> dict[str, Any] | None:
    """Return a spend-limited skip result when today's cost already meets the limit."""
    from sqlalchemy import func, update

    from modulo.core.cost_controller import created_at_day_start
    from modulo.db.models.run import Run
    from modulo.db.models.trigger import Trigger

    today_start = created_at_day_start()
    cost_result = await session.execute(
        select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
            Run.trigger_id == trigger.id,
            Run.organisation_id == org_id,
            Run.created_at >= today_start,
        )
    )
    today_cost = cost_result.scalar_one()
    if today_cost is None or today_cost < spend_limit:
        return None
    await _log_event(
        session,
        trigger=trigger,
        org_id=org_id,
        result="spend_limit_reached",
        error_detail=(f"Daily spend limit {spend_limit} reached (today: {today_cost})"),
    )
    # Finding 2 (review PR #982): record the attempt (skip-not-defer)
    # so the catch-up scan does not re-fire a spend-limited epoch
    # forever. The next NORMAL scheduled fire handles the future.
    await session.execute(update(Trigger).where(Trigger.id == trigger.id).values(last_fired_at=datetime.now(UTC)))
    return {
        "status": "skipped",
        "reason": "spend_limit",
        "daily_spend_limit": str(spend_limit),
        "today_cost": str(today_cost),
    }


async def _auto_create_snapshot(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> uuid.UUID | None:
    """Auto-create a snapshot from the live graph; None means the pipeline is missing."""
    from sqlalchemy import update

    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
    from modulo.db.models.trigger import Trigger

    new_snapshot = await create_snapshot_from_live_graph(
        session,
        pipeline_id=pipeline_id,
        account_id=None,
    )
    if new_snapshot is None:
        await _log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="no_pipeline",
            error_detail="Pipeline not found when trying to auto-create snapshot",
        )
        # Finding 2 (review PR #982): record the attempt (skip-not-defer)
        # so the catch-up scan does not re-fire a pipeline-missing epoch
        # forever. The next NORMAL scheduled fire handles the future.
        await session.execute(update(Trigger).where(Trigger.id == trigger.id).values(last_fired_at=datetime.now(UTC)))
        return None
    snapshot_id = new_snapshot.id
    _log.info("Auto-created snapshot %s for cron trigger %s", snapshot_id, trigger.id)
    return snapshot_id


async def _build_polling_connector(
    session: AsyncSession,
    connector_instance: Any,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> tuple[Any, Any]:
    """Build the polling connector from stored creds; ``(None, None)`` on init failure.

    FAR-442: REST connectors invoked via a polling trigger are wired to the SHARED
    fleet-wide per-destination rate budget (same as run-executor connectors) by
    resolving a shared Redis client keyed to ``tenant_id=str(org_id)``. Each org
    gets its own budget (no cross-tenant ``"default"`` key). Delegates the wiring
    to the single shared helper (:func:`_build_polling_connector_from_instance`)
    so the trigger path can never fork from the executor path again.

    Returns ``(connector, redis_client)`` where ``redis_client`` may be ``None``
    (Redis not configured). The caller OWNS both — it MUST release them via
    :func:`_close_polling_resources` in a ``finally`` (the connector is a fresh
    per-fire build and the Redis client is a fresh ``Redis.from_url``).

    When Redis is configured but the shared budget cannot be resolved
    (settings-read failure or client-construction failure) the resolver RAISES
    SharedBudgetUnavailableError, which is deliberately NOT swallowed here — a
    trigger must fail closed rather than silently fall back to the local bucket
    (which would reconstruct the fleet-wide ``N x burst`` fail-open FAR-439
    removed).
    """
    from modulo.core.trigger_engine.polling import (
        _build_polling_connector_from_instance,
    )

    try:
        connector, redis_client = await _build_polling_connector_from_instance(session, connector_instance, org_id)
        return connector, redis_client
    except asyncio.CancelledError:
        raise
    except SharedBudgetUnavailableError:
        # Fail-closed: a configured-but-unresolvable shared budget must NOT be
        # downgraded to the per-process local bucket. Propagate so the fire job
        # surfaces the outage rather than silently enforcing a per-worker cap.
        raise
    except Exception as exc:
        _log.warning("Failed to initialise connector for polling trigger %s: %s", trigger_id, str(exc)[:200])
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="poll_error",
            error_detail=f"Failed to initialise connector: {str(exc)[:200]}",
        )
        return None, None


async def _run_poll_query(
    session: AsyncSession,
    connector: Any,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    poll_query: str,
) -> tuple[Any, dict[str, Any] | None]:
    """Run the poll query with a 60s bound.

    Returns ``(query_result, None)`` on success or ``(None, skip_result)`` with
    the specific error result to return on timeout/failure.
    """
    from modulo.connectors.base import ConnectorQuery

    try:
        query = ConnectorQuery(resource=poll_query)
        return await asyncio.wait_for(connector.query(query), timeout=60), None
    except TimeoutError:
        _log.warning("Poll query timed out for trigger %s", trigger_id)
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="poll_error",
            error_detail="Poll query timed out after 60s",
        )
        return None, {"status": "error", "reason": "query_timeout"}
    except asyncio.CancelledError:
        raise
    except SharedBudgetUnavailableError:
        # Fail-closed (FAR-442): the shared Redis rate budget is configured but
        # could not be charged during the poll query. Re-raise so the fire job
        # surfaces the outage instead of degrading to a "query_failed" skip — the
        # connector must never fall back to a per-process bucket under a shared
        # budget. The request was never sent on an unaccountable budget.
        raise
    except Exception as exc:
        _log.warning("Poll query failed for trigger %s: %s", trigger_id, str(exc)[:200])
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="poll_error",
            error_detail=f"Poll query failed: {str(exc)[:200]}",
        )
        return None, {"status": "error", "reason": "query_failed", "error": str(exc)[:200]}


async def _evaluate_poll_condition(
    session: AsyncSession,
    query_result: Any,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    condition_expression: str | None,
) -> tuple[bool | None, str | None]:
    """Evaluate the poll condition.

    Returns ``(condition_met, None)`` on success or ``(None, error_str)`` when
    evaluation raised.
    """
    from modulo.core.trigger_engine.polling import evaluate_condition

    try:
        return evaluate_condition(query_result, condition_expression), None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("Condition evaluation failed for trigger %s: %s", trigger_id, exc)
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="poll_error",
            error_detail=f"Condition evaluation failed: {str(exc)[:200]}",
        )
        return None, str(exc)


async def _cron_spend_gate_skip(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return a spend-limited skip result when today's cost already meets the limit.

    Extracted from ``fire_cron_trigger`` (no behaviour change) so the fire job
    body stays within the cognitive-complexity bound; delegates to the shared
    ``_spend_limit_skip`` helper.
    """
    spend_limit = trigger.daily_spend_limit
    if spend_limit is None:
        return None
    return await _spend_limit_skip(session, trigger, org_id, spend_limit)


async def _polling_spend_gate_skip(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return a polling spend-limit skip outcome when today's cost already meets the limit.

    Mirrors ``_cron_spend_gate_skip`` (the polling event logger + inline cost
    query, behaviour unchanged). Extracted from ``fire_polling_trigger`` so the
    fire job body stays within the cognitive-complexity bound.
    """
    spend_limit = trigger.daily_spend_limit
    if spend_limit is None:
        return None
    from sqlalchemy import func

    from modulo.core.cost_controller import created_at_day_start
    from modulo.db.models.run import Run

    today_start = created_at_day_start()
    cost_result = await session.execute(
        select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
            Run.trigger_id == trigger_id,
            Run.organisation_id == org_id,
            Run.created_at >= today_start,
        )
    )
    today_cost = cost_result.scalar_one()
    if today_cost is not None and today_cost >= spend_limit:
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="spend_limit_reached",
            error_detail=(f"Daily spend limit {spend_limit} reached (today: {today_cost})"),
        )
        return {
            "status": "skipped",
            "reason": "spend_limit",
            "daily_spend_limit": str(spend_limit),
            "today_cost": str(today_cost),
        }
    return None


async def fire_cron_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    cron_expression: str,
    snapshot_id: uuid.UUID | None = None,
    factory: async_sessionmaker[AsyncSession] | None = None,
    advance_next_fire_at: bool = False,
) -> dict[str, Any]:
    """Fire one cron trigger — create a run, log the TriggerEvent, dispatch it.

    SAQ per-item fire job (``advance_next_fire_at=False``, the default): the
    atomic next_fire_at advance already happened in ``fire_due_triggers`` at
    enqueue time; this job sets ``last_fired_at`` only.

    ``advance_next_fire_at=True`` preserves the legacy behaviour (CronFireTask,
    removed in PR C).
    """
    from sqlalchemy import update

    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.db.crud.run import create_run
    from modulo.db.models.trigger import Trigger

    if factory is None:
        factory = _open_factory()

    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)

        key1, key2 = _uuid_to_lock_keys(trigger_id)
        lock_result = await session.execute(
            text(_SQL_TRY_ADVISORY_LOCK),
            {"key1": key1, "key2": key2},
        )
        if not lock_result.scalar_one():
            return {"status": "skipped", "reason": "trigger_busy"}
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == org_id,
                Trigger.deleted_at.is_(None),
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None or not trigger.active:
            return {"status": "skipped", "reason": "trigger_inactive_or_missing"}

        # Org-wide pause (kill-switch) — checked BEFORE the snapshot auto-create
        # so a paused org never produces a snapshot. No paused TriggerEvent here
        # (this is the race backstop only; the create_run gate is the authority).
        # Degraded on a pre-migration ProgrammingError (not-paused) inside a
        # savepoint so the per-item transaction is never poisoned.
        if await _org_is_paused_degraded(session, org_id):
            return {"status": "skipped", "reason": PAUSE_SKIP_REASON}

        active_count = await _count_active_runs(session, trigger_id)
        if active_count >= trigger.max_concurrent_runs:
            return await _concurrency_limit_skip(session, trigger, org_id, active_count)

        skip = await _cron_spend_gate_skip(session, trigger, org_id)
        if skip is not None:
            return skip

        if snapshot_id is None:
            snapshot_id = await _auto_create_snapshot(session, trigger, org_id, pipeline_id)
            if snapshot_id is None:
                return {"status": "skipped", "reason": "pipeline_not_found"}

        config = trigger.config_json or {}
        input_payload = config.get("input_template", {})

        try:
            run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                trigger_type="cron",
                trigger_id=trigger_id,
                input_payload=input_payload,
            )
        except TriggersPausedError:
            # TOCTOU race backstop: the org was paused between the early check
            # and create_run. Skip, no paused TriggerEvent (race backstop only).
            _log.info(_LOG_TRIGGERS_PAUSED_SKIP, trigger_id, org_id)
            return {"status": "skipped", "reason": PAUSE_SKIP_REASON}

        event = await _log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="accepted",
            run_id=run.id,
        )

        # last_fired_at reflects an actual fire (run created). next_fire_at is
        # advanced ONLY at enqueue time (fire_due_triggers) — or here for the
        # legacy path (pre-PR C).
        values: dict[str, Any] = {"last_fired_at": datetime.now(UTC)}
        if advance_next_fire_at:
            values["next_fire_at"] = compute_next_fire(
                cron_expression,
                after=datetime.now(UTC),
                timezone=trigger.cron_timezone or "UTC",
            )
        await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(**values))

        _log.info("Cron trigger %s fired -> run %s", trigger_id, run.id)
        return {
            "status": "fired",
            "run_id": str(run.id),
            "event_id": str(event.id),
            "input_payload": input_payload,
        }

    return {"status": "error", "reason": "unexpected"}


async def _polling_pre_fire_gate(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[Any, dict[str, Any] | None]:
    """Advisory-lock + fetch the polling trigger; ``(trigger, None)`` when due to fire.

    Also enforces the kill-switch pause, the concurrency gate and the daily
    spend gate so the fire-job body stays within the cognitive-complexity
    bound. Returns ``(None, skip)`` with the outcome dict when the fire must be
    skipped (``trigger_busy`` first, mirroring ``fire_cron_trigger``).
    """
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.db.models.trigger import Trigger

    key1, key2 = _uuid_to_lock_keys(trigger_id)
    lock_result = await session.execute(
        text(_SQL_TRY_ADVISORY_LOCK),
        {"key1": key1, "key2": key2},
    )
    if not lock_result.scalar_one():
        return None, {"status": "skipped", "reason": "trigger_busy"}
    trigger = (
        await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == org_id,
                Trigger.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if trigger is None or not trigger.active:
        return None, {"status": "skipped", "reason": "trigger_inactive_or_missing"}

    # Org-wide pause (kill-switch). No paused TriggerEvent here (race backstop
    # only; the create_run gate is the authority). Degraded on a pre-migration
    # ProgrammingError (not-paused) inside a savepoint.
    if await _org_is_paused_degraded(session, org_id):
        return None, {"status": "skipped", "reason": PAUSE_SKIP_REASON}

    active_count = await _count_active_runs(session, trigger_id)
    if active_count >= trigger.max_concurrent_runs:
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="concurrency_limit_reached",
            error_detail=(f"Active runs: {active_count}, limit: {trigger.max_concurrent_runs}"),
        )
        return None, {"status": "skipped", "reason": "concurrency_limit", "active_runs": active_count}

    # Daily spend limit check (mirrors fire_cron_trigger) — run BEFORE the
    # connector query so an over-budget trigger stops polling the external
    # service instead of running the query every cycle.
    skip = await _polling_spend_gate_skip(session, trigger, org_id, trigger_id)
    if skip is not None:
        return None, skip
    return trigger, None


async def _run_poll_fire(
    session: AsyncSession,
    *,
    trigger: Any,
    connector: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    poll_query: str,
    condition_expression: str | None,
) -> dict[str, Any]:
    """Run the poll query, evaluate the condition and create the run when met.

    Encapsulates the whole poll-fire decision (query -> condition -> create) so
    the fire-job body stays within the cognitive-complexity bound. The caller
    OWNS the connector/Redis lifecycle (released in a ``finally``) and the
    ``SharedBudgetUnavailableError`` / ``CancelledError`` propagation.
    """
    from sqlalchemy import update

    from modulo.db.crud.run import create_run
    from modulo.db.models.trigger import Trigger

    query_result, query_skip = await _run_poll_query(session, connector, trigger, org_id, trigger_id, poll_query)
    if query_skip is not None:
        return query_skip

    condition_met, condition_error = await _evaluate_poll_condition(
        session, query_result, trigger, org_id, trigger_id, condition_expression
    )
    if condition_error is not None:
        return {"status": "error", "reason": "condition_eval_failed", "error": condition_error}

    if not condition_met:
        await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="no_match",
        )
        return {"status": "no_match"}

    config = trigger.config_json or {}
    snapshot_id_str = config.get("snapshot_id")
    try:
        snapshot_id = uuid.UUID(str(snapshot_id_str)) if snapshot_id_str else uuid.UUID(int=0)
    except (ValueError, TypeError):
        snapshot_id = uuid.UUID(int=0)

    input_payload: dict[str, Any] = {
        "records": query_result.records,
        "total": query_result.total,
        "poll_query": poll_query,
    }

    try:
        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="polling",
            trigger_id=trigger_id,
            input_payload=input_payload,
        )
    except TriggersPausedError:
        _log.info(_LOG_TRIGGERS_PAUSED_SKIP, trigger_id, org_id)
        return {"status": "skipped", "reason": PAUSE_SKIP_REASON}

    event = await _log_poll_event(
        session,
        trigger=trigger,
        org_id=org_id,
        result="condition_met",
        run_id=run.id,
    )

    # last_fired_at only — next_fire_at was advanced at enqueue time.
    await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=datetime.now(UTC)))

    _log.info("Polling trigger %s fired -> run %s (condition met)", trigger_id, run.id)
    return {"status": "fired", "run_id": str(run.id), "event_id": str(event.id)}


async def fire_polling_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    connector_instance_id: uuid.UUID,
    poll_query: str,
    condition_expression: str | None,
) -> dict[str, Any]:
    """Fire one polling trigger — run the poll query, evaluate, create run.

    SAQ per-item fire job. ``next_fire_at`` was already advanced at enqueue time
    by ``fire_due_triggers``; this job sets ``last_fired_at`` only when a run is
    created (condition met). It does NOT re-check ``next_fire_at`` (the advance
    is enqueue-time by design).
    """
    from sqlalchemy import update

    from modulo.db.models.connector_instance import ConnectorInstance
    from modulo.db.models.trigger import Trigger

    factory = _open_factory()

    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)

        trigger, skip = await _polling_pre_fire_gate(session, trigger_id=trigger_id, org_id=org_id)
        if skip is not None:
            return skip

        conn_result = await session.execute(
            select(ConnectorInstance).where(
                ConnectorInstance.id == connector_instance_id,
                ConnectorInstance.organisation_id == org_id,
            )
        )
        connector_instance = conn_result.scalar_one_or_none()
        if connector_instance is None:
            _log.warning("Connector instance %s not found for polling trigger %s", connector_instance_id, trigger_id)
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail=f"Connector instance {connector_instance_id} not found",
            )
            return {"status": "error", "reason": "connector_not_found"}

        # The connector + its shared Redis client are fresh per-fire builds
        # (FAR-442) — the caller owns both and must release them regardless of
        # outcome (a poll hit, a fail-closed budget outage, or an exception).
        from modulo.core.trigger_engine.polling import _close_polling_resources

        connector = None
        redis_client = None
        try:
            connector, redis_client = await _build_polling_connector(
                session,
                connector_instance,
                trigger,
                org_id,
                trigger_id,
            )
            if connector is None:
                return {"status": "error", "reason": "connector_init_failed"}
            return await _run_poll_fire(
                session,
                trigger=trigger,
                connector=connector,
                org_id=org_id,
                trigger_id=trigger_id,
                pipeline_id=pipeline_id,
                poll_query=poll_query,
                condition_expression=condition_expression,
            )
        except asyncio.CancelledError:
            raise
        except SharedBudgetUnavailableError as exc:
            # Fail-closed (FAR-442): the shared Redis rate budget is configured but
            # could not be charged during the poll query. The connector must never
            # fall back to a per-process bucket under a shared budget; the epoch was
            # ALREADY claimed at enqueue time, so treat this as "epoch NOT consumed"
            # — reset next_fire_at to due-now so the next fire_due_triggers tick
            # re-selects and re-enqueues it instead of silently dropping the cadence
            # when the outage outlasts FIRE_JOB_RETRIES.
            await session.execute(
                update(Trigger).where(Trigger.id == trigger_id).values(next_fire_at=datetime.now(UTC))
            )
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail=f"shared rate-limit budget unavailable: {exc}",
            )
            return {
                "status": "error",
                "reason": "shared_budget_unavailable",
                "error": f"shared rate-limit budget unavailable: {exc}",
            }
        finally:
            await _close_polling_resources(connector, redis_client)

    return {"status": "error", "reason": "unexpected"}


async def fire_report_trigger(*, report_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, Any]:
    """Fire one scheduled report — generate, format, deliver (SAQ bounded job).

    Plan F1 report delivery: timeout=300 / retries=2 SAQ knobs at enqueue; on
    failure the job backs off ``next_send_at`` by +5min (or deactivates after 5
    consecutive failures) so ``fire_due_triggers`` NEVER re-enqueues every 30s.
    ``next_send_at`` was already advanced at enqueue time; success sets
    ``last_sent_at`` only.
    """
    from sqlalchemy import update

    from modulo.core.reports.scheduler import (
        _deliver_via_config,
        get_deliverer,
        get_formatter,
        get_generator,
    )
    from modulo.db.models.scheduled_report import ScheduledReport

    factory = _open_factory()
    settings = get_settings()
    redis_client = AsyncRedis.from_url(settings.redis_url, socket_connect_timeout=5)
    try:
        async with factory() as session, session.begin():
            await _set_rls_org(session, org_id)
            now = datetime.now(UTC)
            result = await session.execute(
                select(ScheduledReport)
                .where(
                    ScheduledReport.id == report_id,
                    ScheduledReport.organisation_id == org_id,
                    ScheduledReport.active.is_(True),
                )
                .with_for_update()
            )
            report = result.scalar_one_or_none()
            if report is None:
                return {"status": "skipped", "reason": "report_inactive_or_missing"}

            generator = get_generator(report.report_type)
            if generator is None:
                _log.warning("No generator registered for report type %s", report.report_type)
                await _handle_report_failure(session, redis_client, report_id, now)
                return {"status": "failed", "reason": f"no_generator_for_{report.report_type}"}

            try:
                config = report.config_json or {}
                report_data = await generator(session, org_id, config)
                formatter = get_formatter(report.report_type)
                payload: Any = report_data
                if formatter is not None:
                    payload = formatter(report_data)
                deliverer = get_deliverer(report.report_type)
                recipient_config = report.recipient_config or {}
                if deliverer is not None:
                    delivery_results = await deliverer(payload, recipient_config)
                else:
                    delivery_results = await _deliver_via_config(payload, recipient_config)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Report %s (%s) generation or delivery failed", report_id, report.report_type)
                await _handle_report_failure(session, redis_client, report_id, now)
                return {"status": "failed", "reason": "generation_or_delivery_failed"}

            schedule_type = (report.config_json or {}).get("schedule_type")
            values: dict[str, Any] = {"last_sent_at": now}
            if schedule_type == "one_time":
                values["active"] = False
                values["next_send_at"] = None
            # next_send_at was already advanced to the next cron match at enqueue time.

            await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(**values))
            await _clear_report_failure_counter(redis_client, report_id)
            _log.info("Report %s (%s) sent", report_id, report.report_type)
            return {
                "status": "sent",
                "report_id": str(report_id),
                "report_type": report.report_type,
                "delivery_results": delivery_results,
            }
    finally:
        with _suppress_aclose():
            await redis_client.aclose()

    return {"status": "error", "reason": "unexpected"}


async def _handle_report_failure(
    session: AsyncSession,
    redis_client: AsyncRedis,
    report_id: uuid.UUID,
    now: datetime,
) -> None:
    """Back off next_send_at +5min; deactivate after 5 consecutive failures."""
    from sqlalchemy import update

    from modulo.db.models.scheduled_report import ScheduledReport

    backoff = now + timedelta(seconds=REPORT_BACKOFF_SECONDS)
    await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(next_send_at=backoff))
    try:
        key = _report_failure_counter_key(report_id)
        count = await redis_client.incr(key)
        await redis_client.expire(key, _REPORT_FAILURE_COUNTER_TTL)
        if count >= REPORT_MAX_CONSECUTIVE_FAILURES:
            await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(active=False))
            _log.warning(
                "Report %s deactivated after %d consecutive failures",
                report_id,
                REPORT_MAX_CONSECUTIVE_FAILURES,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.report_failure_counter_unavailable report=%s", report_id)
        # Best-effort counter — the next_send_at backoff alone already stops the
        # every-30s re-enqueue loop.


def _report_failure_counter_key(report_id: uuid.UUID) -> str:
    return f"saq:report:consecutive_failures:{report_id}"


async def _clear_report_failure_counter(redis_client: AsyncRedis, report_id: uuid.UUID) -> None:
    try:
        await redis_client.delete(_report_failure_counter_key(report_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.clear_report_failure_counter failed for %s", report_id)


# ---------------------------------------------------------------------------
# Ongoing triggers (FAR-158) — worker-pool top-up semantics.
#
# The ``ongoing`` trigger keeps its pipeline topped up to ``max_concurrent_runs``
# in-flight (pending/running/claimed) runs. The per-item fire job splits into
# two phases at a hard commit boundary:
#
#   * ``_ongoing_topup``        — DB phase, inside the caller's transaction.
#                                 Creates the missing runs (effective_target -
#                                 in_flight), logs one 'accepted' event per run,
#                                 updates last_fired_at. Returns the runs UNCOMMITTED.
#   * ``_dispatch_ongoing_runs`` — queue phase, called ONLY after that
#                                 transaction committed. Dispatches each created
#                                 run to SAQ; NEVER raises post-commit (a SAQ
#                                 retry re-counts committed pendings — idempotent),
#                                 and committed-but-never-dispatched pendings are
#                                 recovered by dispatcher_reconcile's existing
#                                 ``pending + dispatched_at IS NULL`` branch.
# ---------------------------------------------------------------------------


def _ongoing_failure_key(trigger_id: uuid.UUID) -> str:
    return f"saq:trigger:consecutive_failures:{trigger_id}"


async def _bump_ongoing_failure(
    session: AsyncSession,
    redis_client: AsyncRedis | None,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID | None = None,
) -> None:
    """Increment the consecutive-failure counter; deactivate after the cap.

    Report-pattern persistent-failure guard: a deleted pipeline (auto-create
    failure) or a broken pinned snapshot would otherwise log ~1440 no_pipeline
    events/day forever. After ``ONGOING_MAX_CONSECUTIVE_FAILURES`` consecutive
    failures the trigger is set ``active=False`` (the scan's ``active IS TRUE``
    filter then stops advancing next_fire_at). When ``org_id`` is provided the
    deactivation branch also emits the shared auto-deactivation lifecycle
    ceremony (AuditEvent + TriggerEvent, ``deactivated_by='config_failure'``) so
    the config-failure path leaves the same searchable audit records as the
    FAR-190 no-delivery-streak path. Best-effort — a Redis failure must never
    crash the top-up job.
    """
    if redis_client is None:
        return
    try:
        key = _ongoing_failure_key(trigger_id)
        count = await redis_client.incr(key)
        await redis_client.expire(key, _ONGOING_FAILURE_COUNTER_TTL)
        if count >= ONGOING_MAX_CONSECUTIVE_FAILURES:
            from sqlalchemy import update

            from modulo.db.models.trigger import Trigger

            stmt = update(Trigger).where(Trigger.id == trigger_id)
            if org_id is not None:
                stmt = stmt.where(Trigger.organisation_id == org_id)
            await session.execute(stmt.values(active=False))
            if org_id is not None:
                from modulo.core.trigger_streak import record_ongoing_deactivation_lifecycle

                await record_ongoing_deactivation_lifecycle(
                    session,
                    org_id=org_id,
                    trigger_id=trigger_id,
                    streak=int(count),
                    threshold=ONGOING_MAX_CONSECUTIVE_FAILURES,
                    reason="persistent_failure",
                    deactivated_by="config_failure",
                )
            _log.warning(
                "ongoing trigger %s deactivated after %d consecutive failures",
                trigger_id,
                ONGOING_MAX_CONSECUTIVE_FAILURES,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.ongoing_failure_counter_unavailable trigger=%s", trigger_id)


async def _clear_ongoing_failure(redis_client: AsyncRedis | None, trigger_id: uuid.UUID) -> None:
    """Clear the consecutive-failure counter after a successful top-up."""
    if redis_client is None:
        return
    try:
        await redis_client.delete(_ongoing_failure_key(trigger_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.clear_ongoing_failure failed for %s", trigger_id)


async def _ongoing_topup(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    now: datetime,
    latest_snapshot_id: uuid.UUID | None = None,
    redis_client: AsyncRedis | None = None,
    outcome: dict[str, Any] | None = None,
) -> list[Run]:
    """DB phase of the ongoing top-up — create the missing runs in the caller's tx.

    Must be called inside an ``async with session.begin()`` with the org RLS
    context set. Takes a NON-blocking advisory xact lock on the trigger; returns
    ``[]`` (never raises) when another worker holds the lock or the trigger is
    missing/inactive/soft-deleted. The returned runs are NOT committed — the
    caller commits when the ``async with`` exits, then dispatches.

    ``outcome`` is an optional mutable dict the caller may pass to receive the
    reason for an empty return (``{'status': 'skipped', 'reason': ...}``); when
    runs are created the caller infers ``status='fired'`` from the non-empty list.
    """
    if outcome is not None:
        outcome.clear()

    trigger = await _acquire_ongoing_trigger(session, trigger_id=trigger_id, org_id=org_id, now=now, outcome=outcome)
    if trigger is None:
        return []

    to_create = await _ongoing_shortfall(
        session, trigger, trigger_id=trigger_id, pipeline_id=pipeline_id, outcome=outcome
    )
    if to_create <= 0:
        return []

    # Snapshot resolution: pinned config snapshot_id (invalid/missing -> explicit
    # no_pipeline event + skip, NEVER silent auto-create) > latest_snapshot_id
    # (pre-resolved by the scan) > auto-create once.
    config = trigger.config_json or {}
    snapshot_id, snapshot_skip = await _resolve_ongoing_snapshot(
        session, trigger, org_id, pipeline_id, config, latest_snapshot_id, redis_client
    )
    if snapshot_skip is not None:
        if outcome is not None:
            outcome.update(snapshot_skip)
        return []
    if snapshot_id is None:
        raise RuntimeError("ongoing topup: snapshot_id unresolved after skip check")

    return await _create_ongoing_runs(
        session,
        trigger,
        trigger_id=trigger_id,
        org_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        config=config,
        now=now,
        to_create=to_create,
        redis_client=redis_client,
        outcome=outcome,
    )


def _ongoing_outcome_update(outcome: dict[str, Any] | None, updates: dict[str, Any]) -> None:
    """Merge skip/status updates into the caller's mutable outcome dict (if any).

    Shared by the ongoing helpers so the consuming functions stay within the
    cognitive-complexity bound instead of nesting a ``if outcome is not None``
    guard at every skip site.
    """
    if outcome is not None:
        outcome.update(updates)


async def _acquire_ongoing_trigger(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    now: datetime,
    outcome: dict[str, Any] | None,
) -> Any | None:
    """Advisory-lock + fetch the ongoing trigger; ``None`` with ``outcome`` set on any skip."""
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.db.models.trigger import Trigger

    key1, key2 = _uuid_to_lock_keys(trigger_id)
    lock_result = await session.execute(
        text(_SQL_TRY_ADVISORY_LOCK),
        {"key1": key1, "key2": key2},
    )
    if not lock_result.scalar_one():
        _ongoing_outcome_update(outcome, {"status": "skipped", "reason": "trigger_busy"})
        return None

    trigger_result = await session.execute(
        select(Trigger).where(
            Trigger.id == trigger_id,
            Trigger.organisation_id == org_id,
            Trigger.deleted_at.is_(None),
        )
    )
    trigger = trigger_result.scalar_one_or_none()
    if trigger is None or not trigger.active:
        _ongoing_outcome_update(outcome, {"status": "skipped", "reason": "trigger_inactive_or_missing"})
        return None

    # Org-wide pause (kill-switch) — race backstop before the spend gate / count
    # (the create_run gate is the authority). Degraded on a pre-migration
    # ProgrammingError (not-paused) inside a savepoint, matching cron/polling.
    if await _org_is_paused_degraded(session, org_id):
        _ongoing_outcome_update(outcome, {"status": "skipped", "reason": PAUSE_SKIP_REASON})
        return None

    # Daily spend gate (mirrors fire_cron_trigger) — run BEFORE the count so an
    # over-budget daemon stops creating runs. Skip-not-defer: last_fired_at is
    # still stamped so the trigger's cadence is not misread as stalled.
    spend_limit = trigger.daily_spend_limit
    if spend_limit is not None:
        skip = await _ongoing_spend_gate(session, trigger, org_id, trigger_id, now, spend_limit)
        if skip is not None:
            _ongoing_outcome_update(outcome, skip)
            return None
    return trigger


async def _ongoing_shortfall(
    session: AsyncSession,
    trigger: Any,
    *,
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    outcome: dict[str, Any] | None,
) -> int:
    """Return how many runs to create (0 = at/above target or pipeline missing)."""
    from modulo.db.models.pipeline import Pipeline

    in_flight = await _count_ongoing_runs(session, trigger_id)

    pipeline_result = await session.execute(select(Pipeline.max_concurrent_runs).where(Pipeline.id == pipeline_id))
    pipeline_max = pipeline_result.scalar_one_or_none()
    if pipeline_max is None:
        if outcome is not None:
            outcome.update({"status": "skipped", "reason": "pipeline_not_found"})
        return 0
    # Effective target = min(trigger target, pipeline cap) — handles a pipeline
    # cap lowered after create. Multiple ongoing triggers on one pipeline each
    # top up to their own target, so combined in-flight may exceed the cap; that
    # is bounded downstream by capacity/claim demotion, not by this min().
    effective_target = min(int(trigger.max_concurrent_runs), int(pipeline_max))
    if in_flight >= effective_target:
        # At/above target — genuine no-op. NO event, NO last_fired_at write.
        if outcome is not None:
            outcome.update({"status": "noop", "in_flight": in_flight, "target": effective_target})
        return 0
    return effective_target - in_flight


async def _create_ongoing_runs(
    session: AsyncSession,
    trigger: Any,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    config: dict[str, Any],
    now: datetime,
    to_create: int,
    redis_client: AsyncRedis | None,
    outcome: dict[str, Any] | None,
) -> list[Run]:
    """Create ``to_create`` runs for the ongoing trigger, stamping ``last_fired_at``."""
    from sqlalchemy import update

    from modulo.db.crud.run import create_run
    from modulo.db.models.trigger import Trigger

    created: list[Run] = []
    try:
        for _ in range(to_create):
            run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                trigger_type="ongoing",
                trigger_id=trigger_id,
                input_payload=config.get("input_template", {}),
            )
            created.append(run)
            await _log_ongoing_event(session, trigger=trigger, org_id=org_id, result="accepted", run_id=run.id)
    except TriggersPausedError:
        # TOCTOU race backstop: the org was paused mid-loop. Stop creating —
        # the runs already created stay (they are dispatched below).
        _log.info(_LOG_TRIGGERS_PAUSED_SKIP, trigger_id, org_id)
        if outcome is not None:
            outcome.update({"status": "skipped", "reason": PAUSE_SKIP_REASON})

    if created:
        await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=now))
        await _clear_ongoing_failure(redis_client, trigger_id)
        _log.info("Ongoing trigger %s topped up -> %d run(s)", trigger_id, len(created))

    return created


async def _ongoing_spend_gate(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    now: datetime,
    spend_limit: Any,
) -> dict[str, Any] | None:
    """Return the spend-limit skip outcome when today's cost already meets the limit."""
    from sqlalchemy import func, update

    from modulo.core.cost_controller import created_at_day_start
    from modulo.db.models.run import Run
    from modulo.db.models.trigger import Trigger

    today_start = created_at_day_start()
    cost_result = await session.execute(
        select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
            Run.trigger_id == trigger_id,
            Run.organisation_id == org_id,
            Run.created_at >= today_start,
        )
    )
    today_cost = cost_result.scalar_one()
    if today_cost is None or today_cost < spend_limit:
        return None
    await _log_ongoing_event(
        session,
        trigger=trigger,
        org_id=org_id,
        result="spend_limit_reached",
        error_detail=(f"Daily spend limit {spend_limit} reached (today: {today_cost})"),
    )
    await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=now))
    return {
        "status": "skipped",
        "reason": "spend_limit",
        "daily_spend_limit": str(spend_limit),
        "today_cost": str(today_cost),
    }


async def _resolve_ongoing_snapshot(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    config: dict[str, Any],
    latest_snapshot_id: uuid.UUID | None,
    redis_client: AsyncRedis | None,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Resolve the ongoing trigger's snapshot; ``(snapshot_id, None)`` on success.

    Pinned config ``snapshot_id`` (invalid/missing -> explicit ``no_pipeline``
    event + skip, NEVER silent auto-create) > ``latest_snapshot_id`` (pre-resolved
    by the scan) > auto-create once. The second element is the skip outcome when
    resolution failed; on the auto-create failure it carries the
    ``pipeline_not_found`` reason.
    """

    pinned = config.get("snapshot_id")
    if pinned:
        try:
            candidate = uuid.UUID(str(pinned))
        except (ValueError, TypeError):
            candidate = None
        if candidate is None:
            await _log_ongoing_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="no_pipeline",
                error_detail=f"Invalid snapshot_id pinned in ongoing trigger config: {pinned!r}",
            )
            await _bump_ongoing_failure(session, redis_client, trigger.id, org_id=org_id)
            return None, {"status": "skipped", "reason": "invalid_pinned_snapshot"}
        from modulo.db.models.pipeline_snapshot import PipelineSnapshot

        snap_result = await session.execute(select(PipelineSnapshot.id).where(PipelineSnapshot.id == candidate))
        if snap_result.scalar_one_or_none() is None:
            await _log_ongoing_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="no_pipeline",
                error_detail=f"Pinned snapshot_id not found: {candidate}",
            )
            await _bump_ongoing_failure(session, redis_client, trigger.id, org_id=org_id)
            return None, {"status": "skipped", "reason": "pinned_snapshot_missing"}
        return candidate, None

    if latest_snapshot_id is not None:
        return latest_snapshot_id, None

    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

    new_snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id, account_id=None)
    if new_snapshot is None:
        await _log_ongoing_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="no_pipeline",
            error_detail="Pipeline not found when trying to auto-create snapshot",
        )
        await _bump_ongoing_failure(session, redis_client, trigger.id, org_id=org_id)
        return None, {"status": "skipped", "reason": "pipeline_not_found"}
    _log.info("Auto-created snapshot %s for ongoing trigger %s", new_snapshot.id, trigger.id)
    return new_snapshot.id, None


async def _dispatch_ongoing_runs(
    _q_or_none: Any,
    org_id: uuid.UUID,
    run_ids: list[uuid.UUID],
    _redis_client: AsyncRedis | None = None,
) -> list[dict[str, Any]]:
    """Queue phase of the ongoing top-up — dispatch each created run to SAQ.

    Called ONLY after the top-up transaction committed (``_q_or_none`` is a
    placeholder kept for seam parity; the queue name resolves from settings).
    ``_redis_client`` is retained for caller symmetry but unused — per-run
    try/except collects ``{'run_id', 'outcome', 'job_id'?}`` and NEVER raises
    after commit: a SAQ retry would re-count the committed pendings
    (idempotent), and committed-but-never-dispatched pendings are recovered by
    ``dispatcher_reconcile``'s existing ``pending + dispatched_at IS NULL``
    branch.
    """
    from modulo.core.dispatch import dispatch_run

    settings = get_settings()
    outcomes: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            outcome, job_id = await dispatch_run(str(run_id), str(org_id), queue=settings.saq_runs_queue)
            outcomes.append({"run_id": str(run_id), "outcome": outcome, "job_id": job_id})
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("fire_ongoing_trigger: dispatch failed for run %s", run_id)
            outcomes.append({"run_id": str(run_id), "outcome": "dispatch_error", "job_id": None})
    return outcomes


async def fire_ongoing_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    latest_snapshot_id: str = "",
) -> dict[str, Any]:
    """Per-item ongoing fire job — top up + dispatch the created runs (SAQ).

    The DB phase (``_ongoing_topup``) runs inside ONE transaction; once the
    ``async with`` exits (commit) the created runs are dispatched in the queue
    phase (``_dispatch_ongoing_runs``), which never raises post-commit.
    """
    settings = get_settings()
    factory = _open_factory()
    parsed_snapshot: uuid.UUID | None = None
    if latest_snapshot_id:
        try:
            parsed_snapshot = uuid.UUID(str(latest_snapshot_id))
        except (ValueError, TypeError):
            parsed_snapshot = None
    redis_client = AsyncRedis.from_url(settings.redis_url, socket_connect_timeout=5)
    try:
        outcome: dict[str, Any] = {}
        created: list[Run] = []
        async with factory() as session, session.begin():
            await _set_rls_org(session, org_id)
            created = await _ongoing_topup(
                session,
                trigger_id=trigger_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                now=datetime.now(UTC),
                latest_snapshot_id=parsed_snapshot,
                redis_client=redis_client,
                outcome=outcome,
            )
        # Committed here — safe to dispatch.
        dispatched = await _dispatch_ongoing_runs(None, org_id, [r.id for r in created])
        if created:
            summary: dict[str, Any] = {
                "status": "fired",
                "created": len(created),
                "dispatched": dispatched,
                "run_ids": [str(r.id) for r in created],
            }
        else:
            summary = {
                "status": outcome.get("status", "noop"),
                "reason": outcome.get("reason"),
                "created": 0,
                "dispatched": [],
                "in_flight": outcome.get("in_flight"),
                "target": outcome.get("target"),
            }
        # Per-item outcome persistence (debug-only — NOT wired to /healthz/ready).
        # Best-effort: a stats write failure must never fail the fire job.
        try:
            await redis_client.set(f"saq:cron:stats:ongoing:{trigger_id}", json.dumps(summary))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("cron_helpers.ongoing_stats_persist_failed trigger=%s", trigger_id)
        _log.info("fire_ongoing_trigger summary: %s", summary)
        return summary
    finally:
        with _suppress_aclose():
            await redis_client.aclose()


async def _acquire_suite_run_trigger(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[Any, dict[str, Any] | None]:
    """Advisory-lock + fetch the suite_run trigger; ``(trigger, None)`` when due to fire.

    Guarded by the same lock-then-fetch order as ``fire_cron_trigger``; also
    enforces the ``run_kind == 'suite_run'`` contract and the org-wide pause
    (kill-switch). Returns ``(None, skip)`` with the outcome dict on any skip so
    the fire-job body stays within the cognitive-complexity bound.
    """
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.db.models.trigger import Trigger

    key1, key2 = _uuid_to_lock_keys(trigger_id)
    lock_result = await session.execute(text(_SQL_TRY_ADVISORY_LOCK), {"key1": key1, "key2": key2})
    if not lock_result.scalar_one():
        return None, {"status": "skipped", "reason": "trigger_busy"}

    trigger = (
        await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == org_id,
                Trigger.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if trigger is None or not trigger.active:
        return None, {"status": "skipped", "reason": "trigger_inactive_or_missing"}
    if trigger.run_kind != "suite_run" or trigger.eval_suite_id is None:
        return None, {"status": "skipped", "reason": "not_suite_run_trigger"}

    # Org-wide pause (kill-switch) — race backstop before building the run.
    if await _org_is_paused_degraded(session, org_id):
        return None, {"status": "skipped", "reason": PAUSE_SKIP_REASON}
    return trigger, None


def _resolve_suite_run_config(
    config: dict[str, Any],
) -> tuple[uuid.UUID | None, uuid.UUID | None, dict[str, Any] | None]:
    """Resolve dataset/model-backend ids from the trigger config.

    Returns ``(dataset_id, model_backend_id, None)`` on success or
    ``(None, None, skip)`` when the config is missing/invalid.
    """
    dataset_id_raw = config.get("dataset_id")
    model_backend_id_raw = config.get("model_backend_id")
    if not dataset_id_raw or not model_backend_id_raw:
        return None, None, {"status": "skipped", "reason": "missing_suite_run_config"}
    try:
        return uuid.UUID(str(dataset_id_raw)), uuid.UUID(str(model_backend_id_raw)), None
    except (ValueError, TypeError):
        return None, None, {"status": "skipped", "reason": "invalid_suite_run_config"}


async def _suite_run_fire_gates(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    dataset_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Enforce the suite-run concurrency + daily-spend pools; ``skip`` when gated.

    Uses the SUITE-RUN pools (never ``runs``): non-terminal ``suite_runs`` for
    the trigger's suite + dataset, and the per-trigger daily spend over
    ``suite_runs.extra``. Skip-not-defer (PR #982): a gated fire stamps
    ``last_fired_at`` so a serviced low-cadence trigger does not trip the
    hourly missed-fire alert on a stale ``last_fired_at``.
    """
    from sqlalchemy import func, update

    from modulo.core.eval_engine.execute_suite_run import (
        suite_run_daily_spend_exceeded,
        suite_run_daily_spend_used_for_trigger,
    )
    from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
    from modulo.db.models.trigger import Trigger

    # Separate concurrency pool: non-terminal SuiteRuns for this suite+dataset.
    active_count = (
        await session.execute(
            select(func.count()).where(
                SuiteRun.organisation_id == org_id,
                SuiteRun.suite_id == trigger.eval_suite_id,
                SuiteRun.dataset_id == dataset_id,
                SuiteRun.state.in_([SuiteRunState.PENDING.value, SuiteRunState.RUNNING.value]),
            )
        )
    ).scalar_one() or 0
    if int(active_count) >= int(trigger.max_concurrent_runs):
        await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=datetime.now(UTC)))
        return {
            "status": "skipped",
            "reason": "concurrency_limit",
            "active_suite_runs": int(active_count),
        }

    # Separate daily spend pool: sum TODAY's suite_runs cost for THIS trigger
    # only (never the org, never runs). ``daily_spend_limit`` is a per-trigger
    # knob, so the pool must be scoped to the trigger that owns the run (id
    # stamped onto ``suite_runs.extra`` at fire time).
    if trigger.daily_spend_limit is not None:
        used = await suite_run_daily_spend_used_for_trigger(session, org_id, trigger_id)
        if suite_run_daily_spend_exceeded(used, trigger.daily_spend_limit):
            await session.execute(
                update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=datetime.now(UTC))
            )
            return {
                "status": "skipped",
                "reason": "spend_limit",
                "daily_spend_limit": str(trigger.daily_spend_limit),
                "today_cost": str(used),
            }
    return None


async def _suite_run_llm_judge_guard(
    session: AsyncSession,
    org_id: uuid.UUID,
    eval_suite_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Reject an ``llm_judge`` suite_run fire up front; ``skip`` when unsupported.

    ``llm_judge`` suites are not yet supported on the scheduled path: the SAQ
    ``execute_suite_run`` job does not wire a judge callable, so any
    ``llm_judge`` definition would hard-fail the run at execution time
    (``suite contains llm_judge definitions but no judge callable was
    provided``). Reject the fire up front (a clear, observable skip) rather
    than letting the run build and then fail — config must not promise a run it
    cannot deliver. This is the fire-boundary guard that mirrors "reject
    llm_judge suite_run triggers" (FAR-377 reviewer finding).
    """
    from modulo.core.eval_engine.execute_suite_run import load_suite_definitions

    suite_defs = await load_suite_definitions(session, org_id, eval_suite_id)
    if any(str(getattr(d, "eval_type", "")) == "llm_judge" for d in suite_defs):
        _log.warning(
            "fire_suite_run_trigger: suite %s contains llm_judge definitions which are not "
            "supported on the scheduled path; skipping fire (run would hard-fail at execution)",
            eval_suite_id,
        )
        return {
            "status": "skipped",
            "reason": "llm_judge_unsupported",
            "suite_id": str(eval_suite_id),
        }
    return None


async def _build_suite_run_or_skip(
    session: AsyncSession,
    trigger: Any,
    org_id: uuid.UUID,
    dataset_id: uuid.UUID,
    model_backend_id: uuid.UUID,
    config: dict[str, Any],
    pipeline_id: uuid.UUID,
) -> tuple[Any, dict[str, Any] | None]:
    """Build the SuiteRun; ``(run, None)`` on success, ``(None, skip)`` otherwise.

    A non-skip failure class is deliberately NOT swallowed: ``build_suite_run``
    is expected to raise only the two typed errors, which map to explicit skips
    (``empty_dataset`` / ``suite_run_config_error``) — a genuinely unexpected
    error still propagates so the SAQ job fails and retries.
    """
    from modulo.core.eval_engine.execute_suite_run import (
        SuiteRunEmptyDatasetError,
        SuiteRunExecutionError,
        build_suite_run,
    )

    scenario_inputs = config.get("scenario_inputs") or {}
    try:
        run = await build_suite_run(
            session,
            org_id=org_id,
            suite_id=trigger.eval_suite_id,
            dataset_id=dataset_id,
            model_backend_id=model_backend_id,
            scenario_inputs=scenario_inputs,
            pipeline_id=pipeline_id,
        )
    except SuiteRunEmptyDatasetError as exc:
        # Never a silent pass — surface the empty dataset as a missed run.
        return None, {"status": "skipped", "reason": "empty_dataset", "detail": str(exc)}
    except SuiteRunExecutionError as exc:
        return None, {"status": "skipped", "reason": "suite_run_config_error", "detail": str(exc)}
    return run, None


def _stamp_suite_run_extra(
    run: Any,
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    dataset_id: uuid.UUID,
    model_backend_id: uuid.UUID,
    config: dict[str, Any],
) -> None:
    """Stamp the config-derived execution context + the owning trigger id onto the run.

    The id stamps let the SAQ job run the suite and ensure a finished eval never
    touches the production pool. ``str(None)`` would render as the literal
    ``'None'`` and crash ``Decimal``, so a config key explicitly set to
    ``null`` falls back to the default.
    """
    from decimal import Decimal

    from modulo.core.eval_engine.execute_suite_run import _DEFAULT_COST_PER_LLM_CASE

    scenario_inputs = config.get("scenario_inputs") or {}
    cost_raw = config.get("cost_per_llm_case")
    cost_per_case = _DEFAULT_COST_PER_LLM_CASE if cost_raw is None else Decimal(str(cost_raw))
    suite_ceiling_raw = config.get("suite_ceiling")
    run.extra = {
        "trigger_id": str(trigger_id),
        "pipeline_id": str(pipeline_id),
        "dataset_id": str(dataset_id),
        "model_backend_id": str(model_backend_id),
        "scenario_inputs": scenario_inputs,
        "entity_thresholds": config.get("entity_thresholds") or {},
        # ``extra`` is a plain ``JSON`` column with no ``default=str``
        # serializer, so every value written here MUST be JSON-native.
        # Store ``cost_per_llm_case`` and ``suite_ceiling`` as ``str`` — the
        # SAQ job coerces them back to ``Decimal`` on read. Writing a raw
        # ``Decimal`` here raises ``TypeError`` at flush (json.dumps), which
        # kills every fire inside ``session.begin()``.
        "suite_ceiling": str(suite_ceiling_raw) if suite_ceiling_raw is not None else None,
        "eval_definition_version": int(config.get("eval_definition_version", 1)),
        "cost_per_llm_case": str(cost_per_case),
    }


async def fire_suite_run_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> dict[str, Any]:
    """Per-item fire job for a ``run_kind='suite_run'`` cron/event trigger (FAR-377).

    Builds a ``pending`` SuiteRun instead of a pipeline ``Run``. Uses the
    SUITE-RUN spend pool (over ``suite_runs``, never ``runs``) and its own
    concurrency pool (non-terminal ``suite_runs`` for the trigger's suite +
    dataset). It writes NO ``TriggerEvent`` and NO ``Run`` — a SuiteRun is the
    audit record, and writing into the trigger-watch/dedup event set would
    violate the loop guard (a finished eval must never re-fire an eval).

    Returns ``{'status': 'fired', 'suite_run_id': <id>}`` on success, or a skip
    dict. The caller (SAQ wrapper) enqueues the ``execute_suite_run`` job after
    commit.
    """
    from sqlalchemy import update

    from modulo.db.models.trigger import Trigger

    factory = _open_factory()
    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)

        trigger, skip = await _acquire_suite_run_trigger(session, trigger_id=trigger_id, org_id=org_id)
        if skip is not None:
            return skip

        config = trigger.config_json or {}
        dataset_id, model_backend_id, skip = _resolve_suite_run_config(config)
        if skip is not None:
            return skip
        assert dataset_id is not None and model_backend_id is not None  # nosec B101 - non-None whenever skip is None (see _resolve_suite_run_config)

        skip = await _suite_run_fire_gates(session, trigger, org_id, trigger_id, dataset_id)
        if skip is not None:
            return skip

        skip = await _suite_run_llm_judge_guard(session, org_id, trigger.eval_suite_id)
        if skip is not None:
            return skip

        run, skip = await _build_suite_run_or_skip(
            session,
            trigger,
            org_id,
            dataset_id,
            model_backend_id,
            config,
            pipeline_id,
        )
        if skip is not None:
            return skip

        _stamp_suite_run_extra(run, trigger_id, pipeline_id, dataset_id, model_backend_id, config)
        await session.flush()
        await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=datetime.now(UTC)))
        _log.info("suite_run trigger %s fired -> suite_run %s", trigger_id, run.id)
        return {"status": "fired", "suite_run_id": str(run.id), "trigger_id": str(trigger_id)}


def _suppress_aclose() -> Any:
    from contextlib import suppress

    return suppress(Exception)


# ---------------------------------------------------------------------------
# fire_due_triggers (system cron) — multi-machine-safe enqueue
# ---------------------------------------------------------------------------


async def _enqueue_fire_job_async(
    q: RedisQueue,
    function: str,
    key: str,
    **kwargs: Any,
) -> str | None:
    """Enqueue a per-item fire job with bounded knobs and a per-epoch dedupe key.

    Returns the job id, or ``None`` when SAQ deduped it (a concurrent machine
    already enqueued the same epoch — the atomic next_fire_at advance makes this
    the exceptional path).
    """
    job = await q.enqueue(
        function,
        key=key,
        timeout=FIRE_JOB_TIMEOUT,
        heartbeat=FIRE_JOB_HEARTBEAT,
        retries=FIRE_JOB_RETRIES,
        ttl=FIRE_JOB_TTL,
        **kwargs,
    )
    return job.id if job is not None else None


def _atomic_advance_stmt() -> Any:
    """Conditional next_fire_at advance — the multi-machine safety primitive.

    Only rows whose ``next_fire_at`` is still due (<= now, or never set) are
    advanced and RETURNED. A second machine's concurrent tick blocks on the row
    lock, re-evaluates the WHERE after the first commits, and returns nothing.
    """
    return text(
        "UPDATE triggers SET next_fire_at = :nf "
        "WHERE id = :tid "
        "  AND trigger_type = :ttype "
        "  AND active "
        "  AND (next_fire_at IS NULL OR next_fire_at <= now()) "
        "RETURNING id"
    )


async def _advance_cron_next_fire(
    session: AsyncSession,
    trigger_id: uuid.UUID,
    cron_expression: str,
    cron_timezone: str | None = None,
) -> bool:
    """Atomically advance a cron trigger's ``next_fire_at`` (multi-machine).

    The next fire is computed in the trigger's configured timezone
    (``cron_timezone``), matching the legacy ``CronFireTask`` behaviour; a
    non-UTC trigger must not fire on UTC schedules.
    """
    nf = compute_next_fire(cron_expression, after=datetime.now(UTC), timezone=cron_timezone or "UTC")
    r = await session.execute(
        _atomic_advance_stmt(),
        {"nf": nf, "tid": str(trigger_id), "ttype": "cron"},
    )
    return r.fetchone() is not None


async def _advance_polling_next_fire(session: AsyncSession, trigger_id: uuid.UUID, poll_interval: int) -> bool:
    nf = datetime.now(UTC) + timedelta(seconds=max(int(poll_interval), 1))
    r = await session.execute(
        _atomic_advance_stmt(),
        {"nf": nf, "tid": str(trigger_id), "ttype": "polling"},
    )
    return r.fetchone() is not None


async def _advance_ongoing_next_fire(session: AsyncSession, trigger_id: uuid.UUID, scan_interval: int) -> bool:
    """Atomically advance an ongoing trigger's ``next_fire_at`` (multi-machine).

    Mirrors ``_advance_polling_next_fire`` with ``trigger_type='ongoing'``. The
    advance is floored at ``ONGOING_MIN_INTERVAL_SECONDS`` (the scheduler tick)
    so a topped-up trigger is not re-scanned every tick; the trigger's own
    ``scan_interval_seconds`` config may be larger.
    """
    nf = datetime.now(UTC) + timedelta(seconds=max(int(scan_interval), ONGOING_MIN_INTERVAL_SECONDS))
    r = await session.execute(
        _atomic_advance_stmt(),
        {"nf": nf, "tid": str(trigger_id), "ttype": "ongoing"},
    )
    return r.fetchone() is not None


async def _advance_report_next_send(session: AsyncSession, report_id: uuid.UUID, cron_expression: str) -> bool:
    ns = compute_next_send(cron_expression, after=datetime.now(UTC))
    r = await session.execute(
        text(
            "UPDATE scheduled_reports SET next_send_at = :ns "
            "WHERE id = :rid AND active "
            "AND (next_send_at IS NULL OR next_send_at <= now()) "
            "RETURNING id"
        ),
        {"ns": ns, "rid": str(report_id)},
    )
    return r.fetchone() is not None


async def _rollback_cron_advance(
    session: AsyncSession,
    trigger_id: uuid.UUID,
    cron_expression: str,
    cron_timezone: str | None,
    *,
    previous_next_fire: datetime | None,
    now: datetime,
) -> None:
    """Compensating rollback after a cron enqueue failure (2026-08-10 incident).

    ``fire_due_triggers`` advances ``next_fire_at`` ATOMICALLY (claiming the
    epoch) and then enqueues the per-item fire job. If the enqueue fails (a
    Redis transient), the epoch is consumed but no job exists — a full missed
    cadence for a daily cron. This restores ``next_fire_at`` to the value the
    row held BEFORE the advance so the next tick re-selects the epoch and
    retries the fire.

    GUARDED: the restore fires only when the row STILL holds the value THIS
    machine advanced it to (``next_fire_at = :advanced``). A concurrent
    machine that already moved the row again (or a manual edit) is never
    clobbered — the rollback becomes a no-op and the catch-up scan
    (``_fire_missed_cron_epochs``) is the backstop. ``:advanced`` is
    recomputed from the same ``now`` the loop captured; the advance's internal
    ``now`` is at most sub-seconds later, so the recomputation matches for
    every cron granularity >= 1s except a sub-second boundary race — where the
    guard simply no-ops (the epoch stays consumed) and the catch-up re-fires.
    """
    if previous_next_fire is None:
        return
    advanced = compute_next_fire(cron_expression, after=now, timezone=cron_timezone or "UTC")
    await session.execute(
        text(
            "UPDATE triggers SET next_fire_at = :old "
            "WHERE id = :tid AND trigger_type = 'cron' AND next_fire_at = :advanced"
        ),
        {"old": previous_next_fire, "tid": str(trigger_id), "advanced": advanced},
    )


def _catchup_advance_stmt() -> Any:
    """Atomic ``next_fire_at`` advance for the missed-fire catch-up scan.

    Same single-winner primitive shape as ``_atomic_advance_stmt``, but for a
    row whose ``next_fire_at`` is already in the FUTURE (a consumed-but-unfired
    epoch — the normal ``next_fire_at <= now()`` guard would never match). The
    guard is ``next_fire_at = :expected`` — the exact value the scan observed —
    so only ONE machine (the one that selected the row in that state) wins; a
    concurrent catch-up or a manual edit blocks the advance and the winner
    fires the epoch.
    """
    return text(
        "UPDATE triggers SET next_fire_at = :nf "
        "WHERE id = :tid "
        "  AND trigger_type = 'cron' "
        "  AND active "
        "  AND next_fire_at = :expected "
        "RETURNING id"
    )


def _cron_cadence_seconds(cron_expression: str, cron_timezone: str | None, now: datetime) -> int | None:
    """Cadence (seconds) between two consecutive fires of a cron expression.

    Reuses the cadence helper from ``modulo.core.error_tracking`` — the same
    computation the hourly missed-fire alert probe uses — so the catch-up scan
    and the alert agree on what "behind schedule" means. Imported lazily (the
    module chain is heavy and only needed on the catch-up path); it does not
    import ``cron_helpers``, so there is no circular dependency (the existing
    ``_ingest_saq_error`` already imports from it lazily).
    """
    from modulo.core.error_tracking import _trigger_period_seconds

    return _trigger_period_seconds("cron", cron_expression, cron_timezone, None, now)


async def _claim_catchup_marker(redis_client: AsyncRedis, trigger_id: uuid.UUID, missed_epoch: int) -> bool:
    """Atomically claim the catch-up for *missed_epoch* (SET NX EX).

    The claim is a single atomic ``SET key 1 NX EX`` so concurrent catch-up
    scans across worker machines can never both fire the same missed epoch
    (TOCTOU-free; review PR #982 finding 3). Returns True only when THIS call
    won the claim. The marker lives for the worst-case fire-job in-flight
    window (``_CATCHUP_MARKER_TTL`` >= FIRE_JOB_TIMEOUT * (RETRIES+1)); once the
    job runs, ``last_fired_at`` is fresh and the trigger is no longer eligible
    anyway. A Redis failure fails OPEN (the fire is allowed) — if Redis is down
    the enqueue itself fails and the tick logs the failure.
    """
    key = f"{_CATCHUP_MARKER_PREFIX}:{trigger_id}:{missed_epoch}"
    try:
        return bool(await redis_client.set(key, "1", nx=True, ex=_CATCHUP_MARKER_TTL))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.catchup_marker_claim_failed trigger=%s", trigger_id)
        return True


async def _mark_catchup_fired(redis_client: AsyncRedis, trigger_id: uuid.UUID, missed_epoch: int) -> None:
    """Mark a catch-up fire so the same missed epoch is not re-fired while its
    fire job is still pending (see :func:`_catchup_marker_ok`). Best-effort: a
    marker write failure only re-opens the (small, grace-bounded) re-fire
    window, never a lost fire.
    """
    key = f"{_CATCHUP_MARKER_PREFIX}:{trigger_id}:{missed_epoch}"
    try:
        await redis_client.set(key, "1", ex=_CATCHUP_MARKER_TTL)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.catchup_marker_write_failed trigger=%s", trigger_id)


async def _advance_catchup_epoch(
    session: AsyncSession,
    row: Any,
    now: datetime,
) -> tuple[datetime, bool]:
    """Atomically advance the observed epoch; ``(next_nf, True)`` when won.

    The CAS-style guard (``next_fire_at = :expected``) means only ONE machine
    wins the epoch; a concurrent catch-up or manual edit returns ``False`` and
    the row is left for the winner. Extracted unchanged from
    ``_fire_missed_cron_epochs`` (complexity bound).
    """
    next_nf = compute_next_fire(row.cron_expression, after=now, timezone=row.cron_timezone or "UTC")
    try:
        r = await session.execute(
            _catchup_advance_stmt(),
            {"nf": next_nf, "tid": str(row.id), "expected": row.next_fire_at},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: catch-up advance failed %s", row.id)
        return next_nf, False
    return next_nf, r.fetchone() is not None


async def _enqueue_catchup_fire(
    session: AsyncSession,
    redis_client: AsyncRedis,
    q: RedisQueue,
    org_id: uuid.UUID,
    row: Any,
    now: datetime,
    snapshot_id: uuid.UUID | None,
    next_nf: datetime,
    missed_epoch: int,
    summary: dict[str, Any],
) -> None:
    """Enqueue the catch-up fire job and mark the epoch fired (best-effort).

    On enqueue failure the consumed advance is rolled back (guarded) and an
    error event is ingested — the next tick re-selects the epoch. Extracted
    unchanged from ``_fire_missed_cron_epochs`` (complexity bound).
    """
    try:
        if getattr(row, "run_kind", "run") == "suite_run":
            job_id = await _enqueue_fire_job_async(
                q,
                "modulo.core.saq_worker.fire_suite_run_trigger",
                f"suite_catchup:{row.id}:{int(now.timestamp())}",
                trigger_id=str(row.id),
                org_id=str(org_id),
                pipeline_id=str(row.pipeline_id),
            )
        else:
            job_id = await _enqueue_fire_job_async(
                q,
                "modulo.core.saq_worker.fire_cron_trigger",
                f"fire:{row.id}:{int(now.timestamp())}",
                trigger_id=str(row.id),
                org_id=str(org_id),
                pipeline_id=str(row.pipeline_id),
                cron_expression=row.cron_expression,
                snapshot_id=str(snapshot_id) if snapshot_id else "",
            )
        if job_id is not None:
            summary["cron_catchup_enqueued"] += 1
        await _mark_catchup_fired(redis_client, row.id, missed_epoch)
        _log.info(
            "fire_due_triggers.catchup_refire trigger=%s last_fired=%s cadence=%s missed_epoch=%s",
            row.id,
            row.last_fired_at.isoformat(),
            _cron_cadence_seconds(row.cron_expression, row.cron_timezone, now),
            missed_epoch,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["enqueue_failures"] += 1
        _log.exception("fire_due_triggers: catch-up enqueue failed %s", row.id)
        await _rollback_catchup_advance(session, row.id, row.next_fire_at, next_nf)
        await _ingest_saq_error(
            session,
            org_id,
            function="fire_due_triggers",
            message=f"fire_due_triggers: catch-up enqueue failed for cron trigger {row.id}",
            context={"trigger_id": str(row.id), "trigger_type": "cron", "catchup": True},
        )


async def _fire_missed_cron_epochs(
    session: AsyncSession,
    redis_client: AsyncRedis,
    q: RedisQueue,
    org_id: uuid.UUID,
    now: datetime,
    *,
    summary: dict[str, Any],
    advanced_this_tick: set[uuid.UUID],
) -> None:
    """Re-fire cron epochs consumed-but-never-fired (2026-08-10 incident).

    A cron epoch is "consumed" when ``fire_due_triggers`` atomically advanced
    ``next_fire_at`` past it but no per-item fire job was ever enqueued — the
    worker machine was killed between the advance and the enqueue (the Daily
    Watcher miss), or the enqueue failed and the compensating rollback could
    not apply. The epoch then sits with ``next_fire_at`` in the FUTURE (the
    normal due-loop never sees it) while ``last_fired_at`` is stale — nothing
    would ever re-fire it.

    This scan finds exactly those rows and re-fires each ONCE:
      - Selection: active cron with ``next_fire_at`` in the future AND
        ``last_fired_at`` NOT NULL and genuinely behind — older than
        ``cadence + CATCHUP_GRACE_SECONDS`` (the same "missed" notion as the
        hourly missed-fire alert) AND older than ``next_fire_at - cadence``
        (the epoch ``next_fire_at`` points past was never fired).
      - Bounded: only misses within ``min(CATCHUP_BOUND_SECONDS, cadence * 3)``
        are caught up — an ancient stale trigger (disabled for months, then
        re-enabled) is never fired.
      - Single-winner: the advance guard is ``next_fire_at = :expected`` (the
        exact value this scan observed), so a concurrent catch-up or a manual
        edit blocks the advance and only ONE machine fires the epoch.
      - No double-fire: rows this tick already advanced AND enqueued are
        excluded (``advanced_this_tick``), and a short-lived Redis marker
        (keyed by the stable missed epoch) prevents re-firing the same missed
        epoch while its fire job is still pending.
    """
    from modulo.db.models.trigger import Trigger

    try:
        candidates = (
            await session.execute(
                select(
                    Trigger.id,
                    Trigger.pipeline_id,
                    Trigger.config_json,
                    Trigger.cron_expression,
                    Trigger.cron_timezone,
                    Trigger.next_fire_at,
                    Trigger.last_fired_at,
                    Trigger.run_kind,
                ).where(
                    Trigger.trigger_type == "cron",
                    Trigger.active.is_(True),
                    Trigger.deleted_at.is_(None),
                    Trigger.next_fire_at.isnot(None),
                    Trigger.next_fire_at > now,
                    Trigger.last_fired_at.isnot(None),
                    Trigger.cron_expression.isnot(None),
                )
            )
        ).all()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: catch-up read failed (org %s)", org_id)
        return

    for row in candidates:
        if not _is_catchup_eligible(row, advanced_this_tick, now):
            continue
        missed_epoch = _missed_epoch_of(row, now)
        if missed_epoch is None:
            continue
        if not await _claim_catchup_marker(redis_client, row.id, missed_epoch):
            continue  # another machine already claimed this epoch — single winner
        next_nf, advanced = await _advance_catchup_epoch(session, row, now)
        if not advanced:
            continue  # another machine won the epoch (or the row changed)
        snapshot_id = _resolve_snapshot_id(row, {})
        await _enqueue_catchup_fire(
            session, redis_client, q, org_id, row, now, snapshot_id, next_nf, missed_epoch, summary
        )


def _is_catchup_eligible(row: Any, advanced_this_tick: set[uuid.UUID], now: datetime) -> bool:
    """True when the row is a genuinely missed epoch within the bounded window."""
    if row.id in advanced_this_tick:
        return False  # this tick already advanced + enqueued the epoch
    if row.next_fire_at is None or row.last_fired_at is None:
        return False
    cadence = _cron_cadence_seconds(row.cron_expression, row.cron_timezone, now)
    if cadence is None:
        return False  # uncomputable cadence — leave it to the missed-fire alert
    age_seconds = (now - row.last_fired_at).total_seconds()
    if age_seconds <= cadence + CATCHUP_GRACE_SECONDS:
        return False  # last fire is fresh (normal state — never catch up)
    if age_seconds > min(CATCHUP_BOUND_SECONDS, cadence * 3):
        return False  # ancient stale — beyond the bounded catch-up window
    return bool(row.last_fired_at < row.next_fire_at - timedelta(seconds=cadence))


def _missed_epoch_of(row: Any, now: datetime) -> int | None:
    """The stable missed-epoch marker for a row, or None when not catchable."""
    cadence = _cron_cadence_seconds(row.cron_expression, row.cron_timezone, now)
    if cadence is None or row.next_fire_at is None:
        return None
    return int((row.next_fire_at - timedelta(seconds=cadence)).timestamp())


async def _rollback_catchup_advance(
    session: AsyncSession,
    trigger_id: uuid.UUID,
    original_next_fire_at: datetime,
    advanced_next_fire_at: datetime,
) -> None:
    """Restore the catch-up advanced next_fire_at (guarded, never raises)."""
    try:
        await session.execute(
            text(
                "UPDATE triggers SET next_fire_at = :nf "
                "WHERE id = :tid AND trigger_type = 'cron' AND next_fire_at = :set"
            ),
            {"nf": original_next_fire_at, "tid": str(trigger_id), "set": advanced_next_fire_at},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: catch-up enqueue rollback failed %s", trigger_id)


async def fire_due_triggers() -> dict[str, Any]:
    """System cron — read due cron/polling/report/ongoing rows and enqueue fire jobs.

    Multi-machine safety (plan F1): each due row's ``next_fire_at`` is advanced
    ATOMICALLY (conditional ``UPDATE ... RETURNING id``) and a per-item fire job
    is enqueued ONLY for returned rows. Per-type isolation: an exception in one
    trigger type does not stop the others. Enqueue failures ingest an
    ``error_event`` (source='saq') AND roll the advance back (guarded) so the
    next tick re-selects the epoch; a bounded missed-fire catch-up scan re-fires
    an epoch that was consumed-but-never-fired (worker killed between advance
    and enqueue) — see ``_rollback_cron_advance`` / ``_fire_missed_cron_epochs``.
    The ``ongoing`` scan (FAR-158) is LAST, before the catch-up, and is
    intentionally excluded from catch-up (it self-heals from current state).

    Runs per-org (RLS-safe): the org context is set per transaction so the
    scheduler sees all orgs under FORCE RLS (integration) and behaves
    identically in production.

    Soft-deleted triggers (``deleted_at`` set — ``soft_delete_trigger`` does NOT
    flip ``active``) are excluded from every scan (cron, polling, missed-fire
    catch-up) AND skipped by the per-item fire jobs (``fire_cron_trigger`` /
    ``fire_polling_trigger`` treat them as ``trigger_inactive_or_missing``), so
    a soft-deleted cron/polling trigger can never keep firing (FAR-166).
    """
    settings = get_settings()
    queue_name = settings.saq_runs_queue
    summary = _new_fire_due_summary()

    factory = _open_factory()

    # Collect all org ids first (organisations is the root table — no RLS).
    org_ids = await _collect_org_ids(factory)

    if not org_ids:
        return summary

    # Org-wide trigger pause map (kill-switch). A SEPARATE batched read (its own
    # session + transaction) so a pre-migration DB (no triggers_paused column
    # yet) degrades gracefully AND a read failure never poisons the per-org
    # tick transactions below:
    #   - ProgrammingError  -> not-paused for every org + ``pause_read=degraded``
    #                          (transient pre-migration schema; matches the
    #                          migration-before-deploy contract). The deploy
    #                          pipeline migrates before cutover, so treating
    #                          every org as not-paused during the window is the
    #                          documented, benign choice.
    #   - other SQLAlchemyError (DB down / connection error) -> RE-RAISE so the
    #                          tick FAILS and the SAQ system cron retries. NEVER
    #                          fabricate "paused" for every org on a DB blip —
    #                          a pause read failure is not evidence of a pause.
    pause_by_org = await _read_pause_by_org(factory, summary)

    redis_client = AsyncRedis.from_url(
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    try:
        q = RedisQueue(redis_client, name=queue_name)
        # Machine-scoped cron liveness heartbeat (plan F8): /healthz/ready
        # watches this key so Fly removes a machine whose system-worker cron
        # scheduler is silently dead (worker loop alive, cron stuck).
        await _write_cron_liveness(redis_client)
        for org_id in org_ids:
            summary["orgs_scanned"] += 1
            # Org-wide pause (kill-switch): fire jobs are SKIP-not-defer — the
            # per-row atomic advance below still moves next_fire_at forward so
            # unpausing never causes a catch-up storm.
            org_paused = pause_by_org.get(org_id, False)
            async with factory() as session, session.begin():
                await _set_rls_org(session, org_id)
                now = datetime.now(UTC)
                advanced_this_tick = await _process_due_cron_scan(
                    session, q, redis_client, now, org_id, org_paused, summary
                )

                await _process_due_polling_scan(session, q, now, org_id, org_paused, summary)

                await _process_due_report_scan(session, q, now, org_id, summary)

                # ---- missed-fire catch-up (2026-08-10 incident) ----
                # Re-fire cron epochs consumed-but-never-fired (worker killed
                # between the atomic advance and the enqueue, or an enqueue
                # failure whose rollback could not apply). Gated on the org not
                # being paused: paused fires are SKIP-not-defer, so catch-up
                # never fires a trigger while its org is paused.
                #
                # ``ongoing`` is INTENTIONALLY excluded from catch-up — it
                # self-heals: the top-up recomputes from current state every
                # scan, so a missed tick needs no re-fire, and an at-target
                # no-op is NOT a missed fire.
                if not org_paused:
                    await _fire_missed_cron_epochs(
                        session,
                        redis_client,
                        q,
                        org_id,
                        now,
                        summary=summary,
                        advanced_this_tick=advanced_this_tick,
                    )

                # ---- ongoing triggers (FAR-158) ----
                # Worker-pool top-up scan. Runs in the tick's transaction ONLY
                # for the atomic next_fire_at advance + enqueue (like cron/
                # polling); the top-up itself happens in the per-item job.
                # Selection: active, not soft-deleted, next_fire_at due OR never
                # set (a fresh trigger with NULL next_fire_at must fire on the
                # first tick). Placed LAST in the per-org tick so the existing
                # fixed-order _MockSession unit tests (whose sequences end at
                # the report/catch-up reads) hit the exhausted MagicMock and
                # iterate empty.
                await _process_due_ongoing_scan(session, q, now, org_id, org_paused, summary)
    finally:
        with _suppress_aclose():
            await redis_client.aclose()

    _log.info("fire_due_triggers summary: %s", summary)
    return summary


def _new_fire_due_summary() -> dict[str, Any]:
    return {
        "orgs_scanned": 0,
        "cron_due": 0,
        "cron_enqueued": 0,
        "cron_catchup_enqueued": 0,
        "cron_skipped_paused": 0,
        "polling_due": 0,
        "polling_enqueued": 0,
        "polling_skipped_paused": 0,
        "report_due": 0,
        "report_enqueued": 0,
        "report_skipped_suite_run": 0,
        "ongoing_due": 0,
        "ongoing_enqueued": 0,
        "ongoing_skipped_paused": 0,
        "ongoing_enqueue_failures": 0,
        "enqueue_failures": 0,
    }


async def _collect_org_ids(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    from modulo.db.models.organisation import Organisation

    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        return list(result.scalars())


async def _read_pause_by_org(
    factory: async_sessionmaker[AsyncSession], summary: dict[str, Any]
) -> dict[uuid.UUID, bool]:
    from modulo.db.models.organisation import Organisation

    pause_by_org: dict[uuid.UUID, bool] = {}
    try:
        async with factory() as session, session.begin():
            pause_rows = (
                await session.execute(select(Organisation.id, Organisation.triggers_paused, Organisation.status))
            ).all()
        for oid, triggers_paused, org_status in pause_rows:
            pause_by_org[oid] = org_row_is_paused(org_status, triggers_paused)
    except ProgrammingError:
        _log.exception("fire_due_triggers: pause-column read failed — treating all orgs as not-paused (legacy schema)")
        summary["pause_read"] = "degraded"
        pause_by_org = {}
    except SQLAlchemyError:
        _log.exception(
            "fire_due_triggers: pause read failed — re-raising so the tick fails and the SAQ system cron retries"
        )
        raise
    return pause_by_org


async def _write_cron_liveness(redis_client: AsyncRedis) -> None:
    try:
        await redis_client.set(_cron_liveness_key("fire_due_triggers"), int(time.time()))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.fire_due_triggers liveness heartbeat write failed")


async def _process_due_cron_scan(
    session: AsyncSession,
    q: RedisQueue,
    redis_client: AsyncRedis,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    summary: dict[str, Any],
) -> set[uuid.UUID]:
    """Read + enqueue one org's due cron rows; returns ``advanced_this_tick``."""
    from modulo.db.models.trigger import Trigger

    try:
        cron_rows = (
            await session.execute(
                select(
                    Trigger.id,
                    Trigger.pipeline_id,
                    Trigger.config_json,
                    Trigger.cron_expression,
                    Trigger.cron_timezone,
                    Trigger.next_fire_at,
                    Trigger.run_kind,
                    Trigger.eval_suite_id,
                ).where(
                    Trigger.trigger_type == "cron",
                    Trigger.active.is_(True),
                    Trigger.deleted_at.is_(None),
                    Trigger.next_fire_at.isnot(None),
                    Trigger.next_fire_at <= now,
                    Trigger.cron_expression.isnot(None),
                )
            )
        ).all()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: cron read failed (org %s)", org_id)
        cron_rows = []

    pipelines_needing_snapshots = {
        row.pipeline_id for row in cron_rows if not (row.config_json or {}).get("snapshot_id")
    }
    latest_snapshots = await _resolve_latest_snapshots(session, pipelines_needing_snapshots)

    # ``advanced_this_tick`` tracks epochs THIS tick advanced AND enqueued (or
    # SAQ-deduped as already handled). The missed-fire catch-up scan excludes
    # them so it can never double-fire a trigger the normal loop already fired
    # this tick.
    advanced_this_tick: set[uuid.UUID] = set()
    await _process_due_cron_rows(
        session,
        q,
        redis_client,
        now,
        org_id,
        org_paused,
        cron_rows,
        latest_snapshots,
        advanced_this_tick,
        summary,
    )
    return advanced_this_tick


async def _process_due_polling_scan(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    summary: dict[str, Any],
) -> None:
    from modulo.db.models.trigger import Trigger

    try:
        polling_rows = (
            await session.execute(
                select(
                    Trigger.id,
                    Trigger.pipeline_id,
                    Trigger.config_json,
                    Trigger.next_fire_at,
                    Trigger.run_kind,
                ).where(
                    Trigger.trigger_type == "polling",
                    Trigger.active.is_(True),
                    Trigger.deleted_at.is_(None),
                    Trigger.next_fire_at.isnot(None),
                    Trigger.next_fire_at <= now,
                )
            )
        ).all()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: polling read failed (org %s)", org_id)
        polling_rows = []

    await _process_due_polling_rows(session, q, now, org_id, org_paused, polling_rows, summary)


async def _process_due_report_scan(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    summary: dict[str, Any],
) -> None:
    from modulo.db.models.scheduled_report import ScheduledReport

    try:
        report_rows = (
            await session.execute(
                select(ScheduledReport.id, ScheduledReport.cron_expression).where(
                    ScheduledReport.active.is_(True),
                    ScheduledReport.next_send_at.isnot(None),
                    ScheduledReport.next_send_at <= now,
                )
            )
        ).all()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: report read failed (org %s)", org_id)
        report_rows = []

    await _process_due_report_rows(session, q, now, org_id, report_rows, summary)


async def _process_due_ongoing_scan(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    summary: dict[str, Any],
) -> None:
    from modulo.db.models.trigger import Trigger

    try:
        ongoing_rows = (
            await session.execute(
                select(
                    Trigger.id,
                    Trigger.pipeline_id,
                    Trigger.config_json,
                    Trigger.next_fire_at,
                    Trigger.run_kind,
                ).where(
                    Trigger.trigger_type == "ongoing",
                    Trigger.active.is_(True),
                    Trigger.deleted_at.is_(None),
                    or_(
                        Trigger.next_fire_at.is_(None),
                        Trigger.next_fire_at <= now,
                    ),
                )
            )
        ).all()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: ongoing read failed (org %s)", org_id)
        ongoing_rows = []

    # Pre-resolve latest snapshots per pipeline for ongoing rows WITHOUT a
    # pinned snapshot_id (DISTINCT ON, mirroring cron).
    ongoing_needing_snapshots = {
        row.pipeline_id for row in ongoing_rows if not (row.config_json or {}).get("snapshot_id")
    }
    ongoing_latest_snapshots = await _resolve_latest_snapshots(session, ongoing_needing_snapshots)

    ongoing_enqueued = 0
    for row in ongoing_rows:
        ongoing_enqueued += await _process_one_ongoing_row(
            session,
            q,
            now,
            org_id,
            org_paused,
            row,
            ongoing_latest_snapshots,
            summary,
            ongoing_enqueued,
        )


async def _resolve_latest_snapshots(
    session: AsyncSession,
    pipeline_ids: set[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Resolve the latest snapshot id per pipeline (DISTINCT ON, by created_at).

    Shared by the cron and ongoing scans in ``fire_due_triggers`` for the rows
    that do NOT carry a pinned ``snapshot_id``. Returns a map of
    ``{pipeline_id: latest_snapshot_id}`` (empty when no pipeline needs one).
    """
    if not pipeline_ids:
        return {}
    pids = list(pipeline_ids)
    snap_result = await session.execute(
        text(
            "SELECT DISTINCT ON (pipeline_id) pipeline_id, id "
            "FROM pipeline_snapshots "
            "WHERE pipeline_id = ANY(:pids) "
            "ORDER BY pipeline_id, created_at DESC"
        ),
        {"pids": [str(p) for p in pids]},
    )
    return {row[0]: row[1] for row in snap_result}


def _resolve_snapshot_id(row: Any, latest_snapshots: dict[uuid.UUID, uuid.UUID]) -> uuid.UUID | None:
    config = row.config_json or {}
    snapshot_id_str = config.get("snapshot_id")
    if snapshot_id_str:
        try:
            return uuid.UUID(str(snapshot_id_str))
        except (ValueError, TypeError):
            return None
    return latest_snapshots.get(row.pipeline_id)


async def _enqueue_cron_fire(
    q: RedisQueue,
    redis_client: AsyncRedis,
    now: datetime,
    org_id: uuid.UUID,
    row: Any,
    snapshot_id: uuid.UUID | None,
    advanced_this_tick: set[uuid.UUID],
    summary: dict[str, Any],
) -> bool:
    """Enqueue one cron fire job + mark the consumed epoch (best-effort).

    Returns ``False`` only when the enqueue raised (the caller then rolls the
    atomic advance back). The epoch is marked consumed even when SAQ deduped
    the job (a concurrent machine already enqueued the same epoch) so the
    catch-up scan never re-fires it. Extracted unchanged from
    ``_process_due_cron_rows`` (complexity bound).

    FAR-377: a ``run_kind == 'suite_run'`` cron row enqueues the
    ``fire_suite_run_trigger`` per-item job (with NO snapshot — the suite run
    resolves its own dataset/backend from the trigger config) instead of
    ``fire_cron_trigger``.
    """
    try:
        if getattr(row, "run_kind", "run") == "suite_run":
            job_id = await _enqueue_fire_job_async(
                q,
                "modulo.core.saq_worker.fire_suite_run_trigger",
                f"suite_fire:{row.id}:{int(now.timestamp())}",
                trigger_id=str(row.id),
                org_id=str(org_id),
                pipeline_id=str(row.pipeline_id),
            )
        else:
            job_id = await _enqueue_fire_job_async(
                q,
                "modulo.core.saq_worker.fire_cron_trigger",
                f"fire:{row.id}:{int(now.timestamp())}",
                trigger_id=str(row.id),
                org_id=str(org_id),
                pipeline_id=str(row.pipeline_id),
                cron_expression=row.cron_expression,
                snapshot_id=str(snapshot_id) if snapshot_id else "",
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["enqueue_failures"] += 1
        _log.exception("fire_due_triggers: cron enqueue failed %s", row.id)
        return False
    if job_id is not None:
        summary["cron_enqueued"] += 1
    # Enqueue succeeded or SAQ-deduped (a concurrent machine
    # already enqueued the same epoch) — handled, so the
    # catch-up scan must not re-fire it this tick.
    advanced_this_tick.add(row.id)
    # Finding 1 (review PR #982): ALSO mark the consumed epoch
    # so the catch-up scan does not re-fire it on the NEXT
    # tick while the fire job is still pending. row.next_fire_at
    # still holds the pre-advance value = the epoch consumed.
    try:
        await _mark_catchup_fired(redis_client, row.id, int(row.next_fire_at.timestamp()))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: catchup_marker_write_failed %s", row.id)
    return True


async def _process_one_due_cron_row(
    session: AsyncSession,
    q: RedisQueue,
    redis_client: AsyncRedis,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    row: Any,
    latest_snapshots: dict[uuid.UUID, uuid.UUID],
    advanced_this_tick: set[uuid.UUID],
    summary: dict[str, Any],
) -> None:
    """Advance + enqueue ONE due cron row; roll the advance back on enqueue failure."""
    summary["cron_due"] += 1
    try:
        advanced = await _advance_cron_next_fire(session, row.id, row.cron_expression, row.cron_timezone)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: cron advance failed %s", row.id)
        return
    if not advanced:
        return  # another machine advanced this epoch
    if org_paused:
        # SKIP-not-defer: the epoch is consumed (advance above)
        # but no fire job is enqueued. Counters + summary are the
        # scheduled-path audit — no per-trigger TriggerEvent.
        summary["cron_skipped_paused"] += 1
        return
    snapshot_id = _resolve_snapshot_id(row, latest_snapshots)
    if not await _enqueue_cron_fire(
        q,
        redis_client,
        now,
        org_id,
        row,
        snapshot_id,
        advanced_this_tick,
        summary,
    ):
        # Roll back the atomic advance so the next tick
        # re-selects the epoch and retries — never leave an
        # epoch consumed-but-unfired (2026-08-10 incident).
        try:
            await _rollback_cron_advance(
                session,
                row.id,
                row.cron_expression,
                row.cron_timezone,
                previous_next_fire=row.next_fire_at,
                now=now,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("fire_due_triggers: cron enqueue rollback failed %s", row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="fire_due_triggers",
            message=f"fire_due_triggers: enqueue failed for cron trigger {row.id}",
            context={"trigger_id": str(row.id), "trigger_type": "cron"},
        )


async def _process_due_cron_rows(
    session: AsyncSession,
    q: RedisQueue,
    redis_client: AsyncRedis,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    cron_rows: Sequence[Any],
    latest_snapshots: dict[uuid.UUID, uuid.UUID],
    advanced_this_tick: set[uuid.UUID],
    summary: dict[str, Any],
) -> None:
    """Advance + enqueue the due cron rows (one epoch each, atomic)."""
    for row in cron_rows:
        await _process_one_due_cron_row(
            session,
            q,
            redis_client,
            now,
            org_id,
            org_paused,
            row,
            latest_snapshots,
            advanced_this_tick,
            summary,
        )


async def _enqueue_polling_fire(
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    row: Any,
    config: dict[str, Any],
    connector_instance_id: uuid.UUID | None,
    summary: dict[str, Any],
) -> bool:
    """Enqueue ONE polling fire job; ``False`` when the enqueue raised.

    No advance rollback for polling — a consumed epoch self-heals on the next
    tick — the caller ingests the error event. Extracted unchanged from
    ``_process_due_polling_rows`` (complexity bound).

    FAR-377: a ``run_kind == 'suite_run'`` polling row routes to the SuiteRun
    fire job (mirroring the cron scan) — it builds a SuiteRun, never a pipeline
    ``Run``. The write-surface loop guard depends on this discriminator.
    """
    if getattr(row, "run_kind", "run") == "suite_run":
        # The suite run resolves its own dataset/backend from the trigger config;
        # no connector instance, poll query or condition is needed.
        return await _enqueue_suite_run_fire(q, now, org_id, row, summary, "polling_enqueued")
    try:
        job_id = await _enqueue_fire_job_async(
            q,
            "modulo.core.saq_worker.fire_polling_trigger",
            f"fire:{row.id}:{int(now.timestamp())}",
            trigger_id=str(row.id),
            org_id=str(org_id),
            pipeline_id=str(row.pipeline_id),
            connector_instance_id=str(connector_instance_id),
            poll_query=config.get("poll_query", ""),
            condition_expression=config.get("condition_expression"),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["enqueue_failures"] += 1
        _log.exception("fire_due_triggers: polling enqueue failed %s", row.id)
        return False
    if job_id is not None:
        summary["polling_enqueued"] += 1
    return True


async def _enqueue_suite_run_fire(
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    row: Any,
    summary: dict[str, Any],
    counter_key: str,
) -> bool:
    """Enqueue the ``fire_suite_run_trigger`` job for a ``run_kind='suite_run'`` row.

    Shared by the coupling that routes a suite_run row surfaced by the ongoing or
    polling dispatch scans (FAR-377). Mirrors the cron scan: builds a SuiteRun,
    never a pipeline ``Run``, and passes NO snapshot/connector args (the suite run
    resolves its own dataset/backend from the trigger config). Returns ``False``
    only when the enqueue raised (the caller then ingests the error event).
    """
    try:
        job_id = await _enqueue_fire_job_async(
            q,
            "modulo.core.saq_worker.fire_suite_run_trigger",
            f"suite_fire:{row.id}:{int(now.timestamp())}",
            trigger_id=str(row.id),
            org_id=str(org_id),
            pipeline_id=str(row.pipeline_id),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["enqueue_failures"] += 1
        _log.exception("fire_due_triggers: suite_run fire enqueue failed %s", row.id)
        return False
    if job_id is not None:
        summary[counter_key] += 1
    return True


async def _process_one_due_polling_row(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    row: Any,
    summary: dict[str, Any],
) -> None:
    """Advance + enqueue ONE due polling row (one epoch each, atomic)."""
    config = row.config_json or {}
    ci_id_str = config.get("connector_instance_id")
    try:
        connector_instance_id = uuid.UUID(str(ci_id_str)) if ci_id_str else None
    except (ValueError, TypeError):
        connector_instance_id = None
    try:
        interval = max(int(config.get("poll_interval_seconds") or 60), 1)
    except (ValueError, TypeError):
        _log.warning(
            "fire_due_triggers: invalid poll_interval_seconds for trigger %s, using default",
            row.id,
        )
        interval = 60
    is_suite_run = getattr(row, "run_kind", "run") == "suite_run"
    if connector_instance_id is None and not is_suite_run:
        # Missing connector instance — log poll_error and advance
        # (mirrors the legacy beat _fetch_due_triggers behaviour). A suite_run
        # polling row is exempt: it resolves its own dataset/backend from the
        # trigger config and needs no connector instance.
        await _polling_missing_connector(session, org_id, row, interval, summary)
        return

    summary["polling_due"] += 1
    try:
        advanced = await _advance_polling_next_fire(session, row.id, interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: polling advance failed %s", row.id)
        return
    if not advanced:
        return
    if org_paused:
        summary["polling_skipped_paused"] += 1
        return
    if not await _enqueue_polling_fire(q, now, org_id, row, config, connector_instance_id, summary):
        await _ingest_saq_error(
            session,
            org_id,
            function="fire_due_triggers",
            message=f"fire_due_triggers: enqueue failed for polling trigger {row.id}",
            context={"trigger_id": str(row.id), "trigger_type": "polling"},
        )


async def _process_due_polling_rows(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    polling_rows: Sequence[Any],
    summary: dict[str, Any],
) -> None:
    """Advance + enqueue the due polling rows (one epoch each, atomic)."""
    for row in polling_rows:
        await _process_one_due_polling_row(session, q, now, org_id, org_paused, row, summary)


async def _polling_missing_connector(
    session: AsyncSession,
    org_id: uuid.UUID,
    row: Any,
    interval: int,
    summary: dict[str, Any],
) -> None:
    """Log a poll_error + advance for a polling trigger missing its connector id."""
    from modulo.db.models.trigger_event import TriggerEvent

    summary["polling_due"] += 1
    try:
        await session.execute(
            text(
                "UPDATE triggers SET next_fire_at = :nf "
                "WHERE id = :tid AND trigger_type = 'polling' AND active "
                "AND (next_fire_at IS NULL OR next_fire_at <= now())"
            ),
            {"nf": datetime.now(UTC) + timedelta(seconds=interval), "tid": str(row.id)},
        )
        session.add(
            TriggerEvent(
                organisation_id=org_id,
                trigger_id=row.id,
                trigger_type="polling",
                raw_payload_hash=hashlib.sha256(f"polling:{row.id}:poll_error".encode()).hexdigest(),
                validation_result="poll_error",
                error_detail="Polling trigger missing connector_instance_id in config_json",
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: polling missing-connector handling failed %s", row.id)


async def _process_due_report_rows(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    report_rows: Sequence[Any],
    summary: dict[str, Any],
) -> None:
    """Advance + enqueue the due scheduled-report rows (one epoch each, atomic)."""
    for row in report_rows:
        await _process_one_due_report_row(session, q, now, org_id, row, summary)


async def _process_one_due_report_row(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    row: Any,
    summary: dict[str, Any],
) -> None:
    """Advance + enqueue ONE due scheduled-report row (one epoch each, atomic).

    Extracted from ``_process_due_report_rows`` (complexity bound).
    """
    # FAR-377: never mis-dispatch a ``run_kind == 'suite_run'`` row through the
    # report path (which drives ``fire_report_trigger``, a scheduled report
    # delivery — not a pipeline Run). A suite_run scheduled report should not
    # exist, but the guard keeps a mis-typed row from firing a report.
    if getattr(row, "run_kind", "run") == "suite_run":
        summary["report_skipped_suite_run"] += 1
        return
    summary["report_due"] += 1
    try:
        if not await _advance_report_next_send(session, row.id, row.cron_expression):
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: report advance failed %s", row.id)
        return
    try:
        job_id = await _enqueue_fire_job_async(
            q,
            "modulo.core.saq_worker.fire_report_trigger",
            f"fire:report:{row.id}:{int(now.timestamp())}",
            report_id=str(row.id),
            org_id=str(org_id),
        )
        if job_id is not None:
            summary["report_enqueued"] += 1
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["enqueue_failures"] += 1
        _log.exception("fire_due_triggers: report enqueue failed %s", row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="fire_due_triggers",
            message=f"fire_due_triggers: enqueue failed for report {row.id}",
            context={"report_id": str(row.id), "trigger_type": "report"},
        )


async def _process_one_ongoing_row(
    session: AsyncSession,
    q: RedisQueue,
    now: datetime,
    org_id: uuid.UUID,
    org_paused: bool,
    row: Any,
    ongoing_latest_snapshots: dict[uuid.UUID, uuid.UUID],
    summary: dict[str, Any],
    ongoing_enqueued: int,
) -> int:
    """Advance + enqueue ONE due ongoing row; returns 1 when a job was enqueued."""
    config = row.config_json or {}
    interval = max(int(config.get("scan_interval_seconds") or 60), 60)
    summary["ongoing_due"] += 1
    try:
        if not await _advance_ongoing_next_fire(session, row.id, interval):
            return 0  # another machine advanced this epoch
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_due_triggers: ongoing advance failed %s", row.id)
        return 0
    if org_paused:
        # SKIP-not-defer: the epoch is consumed (advance above)
        # but no fire job is enqueued.
        summary["ongoing_skipped_paused"] += 1
        return 0
    if ongoing_enqueued >= ONGOING_MAX_ENQUEUE_PER_TICK:
        # Per-tick enqueue cap — defer to the next tick. The
        # advance already happened (no double-fire).
        return 0
    if getattr(row, "run_kind", "run") == "suite_run":
        # FAR-377: a suite_run ongoing row routes to the SuiteRun fire job
        # (mirroring the cron scan) — it builds a SuiteRun, never a pipeline
        # ``Run``. The suite run resolves its own dataset/backend; no snapshot id.
        return 1 if await _enqueue_suite_run_fire(q, now, org_id, row, summary, "ongoing_enqueued") else 0
    snapshot_id = _resolve_snapshot_id(row, ongoing_latest_snapshots)
    try:
        job_id = await _enqueue_fire_job_async(
            q,
            "modulo.core.saq_worker.fire_ongoing_trigger",
            f"fire:{row.id}:{int(now.timestamp())}",
            trigger_id=str(row.id),
            org_id=str(org_id),
            pipeline_id=str(row.pipeline_id),
            latest_snapshot_id=str(snapshot_id) if snapshot_id else "",
        )
        if job_id is not None:
            summary["ongoing_enqueued"] += 1
            return 1
        return 0
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["ongoing_enqueue_failures"] += 1
        _log.exception("fire_due_triggers: ongoing enqueue failed %s", row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="fire_due_triggers",
            message=f"fire_due_triggers: enqueue failed for ongoing trigger {row.id}",
            context={"trigger_id": str(row.id), "trigger_type": "ongoing"},
        )
        return 0


# ---------------------------------------------------------------------------
# dispatcher_reconcile (system cron) — DB/queue reconciliation (plan F3c)
# ---------------------------------------------------------------------------


def _reconcile_capacity_marker_exclusion(capacity_redispatch_seconds: int) -> Any:
    """Exclude capacity-block reason markers from re-dispatch — EXCEPT for a
    pending capacity-marked run whose heartbeat is stale or NULL.

    A capacity-blocked run (demoted to ``pending`` with ``error_code`` in
    (``org_capacity_limited``, ``pipeline_capacity``)) is recovered by the
    stranded re-dispatch branch of ``stale_run_recovery_sweep``
    (``pipeline_execution.py``) — the durable liveness owner that refreshes
    ``heartbeat_at`` before re-dispatching. The executor's in-process
    ``_retry_pending`` loop was REMOVED (plan F3b), but the exclusion must
    stay for FRESH-heartbeat rows: it keeps ``dispatcher_reconcile`` from
    hot-looping a run whose heartbeat was just refreshed by the executor's
    claim→demote cycle (the org sandbox-cap churn loop), preserving exactly
    ONE re-dispatch owner and preventing double-recovery churn.

    The STALE-heartbeat carve-out (FAR-108) admits a pending capacity-marked
    run that has sat unexecuted for ``capacity_redispatch_seconds`` (or whose
    heartbeat is NULL — a never-claimed org-capacity-deferred run). Those rows
    fall to ``dispatcher_reconcile``'s 60s cadence instead of waiting for the
    sweep's multi-minute window: the re-dispatch is gated atomically by
    ``dispatch_run`` (pipeline + org run concurrency re-checked in one
    transaction) and the heartbeat gate throttles the sandbox-cap churn loop
    to one claim→demote attempt per window. Literal markers, matching the
    stale-run sweep.
    """
    from sqlalchemy import and_, or_

    from modulo.db.models.run import Run

    return or_(
        Run.error_code.is_(None),
        Run.error_code.not_in(("org_capacity_limited", "pipeline_capacity")),
        and_(
            Run.status == "pending",
            Run.error_code.in_(("org_capacity_limited", "pipeline_capacity")),
            or_(
                Run.heartbeat_at.is_(None),
                Run.heartbeat_at < func_now_minus(capacity_redispatch_seconds),
            ),
        ),
    )


def _nodeless_zombie_predicate(age_minutes: int) -> Any:
    """Match a claimed-but-never-executed SAQ zombie.

    Predicate: ``running`` + ``dispatcher='saq'`` + NO finalised node output
    (``node_token_usage``/``outputs_json`` both NULL — these are only written at
    run finalisation) + started more than *age_minutes* ago + ZERO LangGraph
    checkpoints for the run's thread (checkpoints are written when a node
    COMPLETES a super-step).

    The age gate MUST exceed the pipeline's max node timeout: a legitimate
    long-running first node writes its first checkpoint only after it finishes,
    so a genuinely-executing run is never matched. Only a run stuck in the
    pre-node setup window (or a wedged worker with a live heartbeat) stays
    eligible this long.

    Caveat: the age gate counts from ``started_at`` only and does NOT account
    for graph-compile time, so a legitimately slow run (slow graph compile +
    max-length first node, zero checkpoints in between) could be false-failed.
    ``SAQ_CLAIMED_NODELESS_MINUTES`` must be tuned to exceed worst-case
    compile + first-node duration.
    """
    from sqlalchemy import and_
    from sqlalchemy import exists as sa_exists
    from sqlalchemy import select as sa_select

    from modulo.db.models.run import Run

    checkpoint_subquery = (
        sa_select(1)
        .select_from(text("checkpoints c"))
        .where(
            text("c.organisation_id = runs.organisation_id"),
            text("c.thread_id = runs.langgraph_thread_id"),
        )
    )
    return and_(
        Run.status == "running",
        Run.dispatcher == "saq",
        Run.node_token_usage.is_(None),
        Run.outputs_json.is_(None),
        Run.started_at < func_now_minus(age_minutes * 60),
        ~sa_exists(checkpoint_subquery),
    )


def _is_nodeless_zombie_row(row: Any, age_minutes: int) -> bool:
    """Row-level re-check that a selected row matched the nodeless branch.

    The combined reconcile predicate can match a row via MULTIPLE branches
    (e.g. stale heartbeat AND nodeless); this discriminates the nodeless repair
    (terminal-fail) from the stale repair (re-dispatch).
    """
    if row.status != "running":
        return False
    if row.node_token_usage is not None or row.outputs_json is not None:
        return False
    if row.started_at is None:
        return False
    return bool((datetime.now(UTC) - row.started_at).total_seconds() > age_minutes * 60)


def _should_redispatch_nodeless(row: Any) -> bool:
    """Decide whether a nodeless zombie should be RE-DISPATCHED (not terminal-failed).

    A nodeless zombie executed ZERO nodes (no checkpoint, no node_token_usage,
    no outputs_json), so re-dispatch is SAFE — there is nothing to
    double-execute, and these pipelines only create PRs after a node runs.

    Retry budgeting (FAR — nodeless safe re-dispatch):
      * ``retry_policy`` present (non-empty) with ``"stall"`` in ``on``: honor the
        ``max_retries`` budget. ``claim_count`` is 1 for the initial claim, so a
        re-dispatch is allowed while ``claim_count <= max_retries`` (initial
        attempt + up to ``max_retries`` retries).
      * ``retry_policy`` absent/None OR an empty policy (the column defaults to
        ``{}``): re-dispatch ONCE only, bounded by ``claim_count <= 1`` (the
        original claim has not yet been re-dispatched).
      * ``retry_policy`` present (non-empty) but WITHOUT ``"stall"`` in ``on``:
        terminal-fail — never re-dispatch a nodeless zombie for a trigger it does
        not cover.
    """
    retry_policy = getattr(row, "retry_policy", None)
    if isinstance(retry_policy, dict) and retry_policy:
        on = retry_policy.get("on") or []
        if "stall" in on:
            max_retries = int(retry_policy.get("max_retries", 0) or 0)
            return bool(row.claim_count <= max_retries)
        # A non-empty policy that does not cover "stall" must NOT re-dispatch a
        # nodeless zombie — terminal-fail it.
        return False
    # No stall retry policy (or no/empty policy): re-dispatch exactly once
    # (only the un-re-redispatched claim).
    return bool(row.claim_count <= 1)


async def _fail_nodeless_run(session: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Terminal-fail a claimed-but-nodeless zombie in the reconcile transaction.

    Only transitions a run still ``running`` (a run already terminal, or
    capacity-deferred to ``pending``, is left untouched). Runs inside the
    per-org RLS context of the caller.
    """
    from modulo.db.models.run import Run

    run = await session.get(Run, run_id)
    if run is None or run.status != "running":
        return
    run.status = "failed"
    run.error_code = _NODELESS_ZOMBIE_ERROR_CODE
    run.error_detail = (
        "Claimed by SAQ but dispatched no node within the nodeless window (dispatcher_reconcile zombie repair)"
    )
    run.completed_at = datetime.now(UTC)
    _log.warning(
        "dispatcher_reconcile.nodeless_zombie_failed run=%s org=%s",
        run_id,
        org_id,
    )


def _build_re_dispatch_predicate(
    *,
    reenqueue_window: int,
    stale_window: int,
    capacity_redispatch_seconds: int,
    enqueue_failed_redispatch_seconds: int = ENQUEUE_FAILED_REDISPATCH_SECONDS,
) -> Any:
    """Build the dispatcher_reconcile re-dispatch predicate (F3c + F6a).

    The predicate is a SQL OR of the recovery branches. ``awaiting_human``/
    ``claimed`` runs are matched ONLY under the F6a gated recovery (stale
    heartbeat by 2*SAQ_JOB_HEARTBEAT; the no-SAQ-job gate is applied per-row
    in the loop) so a half-resumed run whose ``resume_run`` job was lost is
    recovered. ``awaiting_human`` rows additionally require a committed HITL
    gate decision (guard applied per-row in the loop) so a genuinely-waiting
    run is never auto-resumed with an empty decision. Exposed as a module
    function so the reconcile tests can exercise it directly with mocked rows.

    FAR-108: a ``capacity_marked_stale`` branch admits a pending run carrying
    ``org_capacity_limited``/``pipeline_capacity`` whose heartbeat is stale or
    NULL — the 60s reconcile (not the multi-minute stale-run sweep) becomes
    the fast re-dispatch path for stranded capacity-blocked runs. The heartbeat
    gate throttles the sandbox-cap claim/demote churn loop (one attempt per
    ``capacity_redispatch_seconds``); ``dispatch_run`` re-checks capacity
    atomically so a still-blocked run is re-deferred without churn.

    B3 (durable dispatch): the zombie branch now REQUIRES ``enqueue_failed_at
    IS NULL``, and a dedicated ``enqueue_failed_stale`` branch re-dispatches a
    pending run whose enqueue-failure marker is set once its heartbeat is stale
    past ``enqueue_failed_redispatch_seconds``. ``enqueue_failed_at`` is read via
    raw SQL (the column ships in a parallel migration; the ORM model on this
    branch does not yet map it).
    """
    from sqlalchemy import and_, or_

    from modulo.db.models.run import Run

    capacity_deferred = and_(
        Run.status == "pending",
        Run.dispatched_at.is_(None),
    )
    capacity_marked_stale = and_(
        Run.status == "pending",
        Run.error_code.in_(("org_capacity_limited", "pipeline_capacity")),
        or_(
            Run.heartbeat_at.is_(None),
            Run.heartbeat_at < func_now_minus(capacity_redispatch_seconds),
        ),
    )
    return or_(
        capacity_deferred,
        capacity_marked_stale,
        # Zombie branch: pending + dispatched_at set + dispatcher NULL AND no
        # enqueue-failure marker (the marker-holding rows are recovered by the
        # gated enqueue_failed_stale branch below). A bare zombie — the rare
        # fail-fast failure before the marker migration — is re-dispatched
        # immediately.
        and_(
            Run.status == "pending",
            Run.dispatched_at.is_not(None),
            Run.dispatcher.is_(None),
            text("runs.enqueue_failed_at IS NULL"),
        ),
        # B3 enqueue-failed branch: pending + dispatched_at set + dispatcher
        # NULL + enqueue_failed_at set + heartbeat stale past the bounded
        # redispatch interval. The heartbeat gate throttles recovery to one
        # attempt per window (no hot-loop on the 60s tick).
        and_(
            Run.status == "pending",
            Run.dispatched_at.is_not(None),
            Run.dispatcher.is_(None),
            text("runs.enqueue_failed_at IS NOT NULL"),
            or_(
                Run.heartbeat_at.is_(None),
                Run.heartbeat_at < func_now_minus(enqueue_failed_redispatch_seconds),
            ),
        ),
        and_(
            Run.status == "pending",
            Run.dispatcher == "saq",
            Run.dispatched_at < func_now_minus(reenqueue_window),
        ),
        and_(
            Run.status == "running",
            Run.dispatcher == "saq",
            Run.heartbeat_at < func_now_minus(stale_window),
        ),
        # F6a gated recovery: awaiting_human/claimed + dispatcher='saq' + stale
        # heartbeat. The no-job gate is applied per-row (q.job() is None).
        and_(
            Run.status.in_(("awaiting_human", "claimed")),
            Run.dispatcher == "saq",
            Run.heartbeat_at < func_now_minus(stale_window),
        ),
    )


def _reconcile_job_type(status: str) -> str:
    """Re-dispatch job-type discriminator (F6a).

    awaiting_human/claimed -> ``resume_run`` (the gate decision is committed
    on the checkpoint); pending/running -> ``execute_run``. The awaiting_human
    case is guarded per-row: the run is re-dispatched as ``resume_run`` ONLY
    when a gate decision is actually committed (see
    :func:`_awaiting_human_has_committed_decision`) — never with an empty
    decision.
    """
    return "resume_run" if status in ("awaiting_human", "claimed") else "execute_run"


async def _awaiting_human_has_committed_decision(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> bool:
    """True when the run has a committed HITL gate decision.

    F6a auto-approve guard: an ``awaiting_human`` run may only be re-dispatched
    as ``resume_run`` when a human actually committed a gate decision
    (``hitl_claims.decision IS NOT NULL``). ``executor.resume`` injects the
    resume payload as ``_hitl_decision``; the HITL gate node treats any
    non-None decision as a human verdict (an empty ``{}`` resumes as
    ``approved``). Re-dispatching a genuinely-waiting run — no human action, no
    committed decision, whose completed job hash expired and heartbeat froze —
    with EMPTY resume_data would therefore auto-approve its gates. ``claimed``
    rows are exempt from the guard (a claim was already made, so the resume is
    safe mid-crash recovery).

    Stricter than the old ``decision IS NOT NULL`` guard (B1-reconcile): a
    decision is only ``committed`` when the decision is present AND — for
    payload-carrying actions (``approved_with_modification``/``manual_output``)
    — the persisted ``decision_payload`` is present and actually carries the
    required data. A payload-carrying decision without its payload cannot be
    faithfully resumed. ``decision_payload`` is read via raw SQL (the jsonb
    column ships in a parallel migration).

    The payload-requirement is keyed off the persisted ``decision_payload``'s
    ``action`` member, NOT the ``decision`` column — the column only ever holds
    ``approved``/``rejected``/``deliver_manual``, so a column-keyed check would
    be dead code and could never protect a manual-output decision whose payload
    was lost: a payload-less recovery degrades to ``{"action": "approved"}``,
    auto-approving the gate (a manual-output decision would pass
    ``{"action": "approved"}`` to the manual node as its output). A payload-less
    row (legacy/pre-migration) is treated as a plain approval/rejection that
    needs no payload to resume faithfully.
    """
    result = await session.execute(
        text(
            "SELECT decision, decision_payload FROM hitl_claims "
            "WHERE organisation_id=:oid AND run_id=:rid AND decision IS NOT NULL "
            "ORDER BY decision_at DESC NULLS LAST LIMIT 1"
        ),
        {"oid": str(org_id), "rid": str(run_id)},
    )
    row = result.first()
    if row is None or row[0] is None:
        return False
    payload = row[1]
    if isinstance(payload, dict):
        action = payload.get("action")
        if action == "manual_output":
            # A manual-output decision MUST carry its output — otherwise the
            # manual node resumes with ``{"action": "approved"}`` as its output.
            return "output" in payload
        if action == "approved_with_modification":
            # An approve-with-modification MUST carry the modified output —
            # otherwise the gate resumes as a plain approval, dropping the
            # human's modification.
            return "modified_output" in payload
    # A payload-less row (legacy/pre-migration) degrades to
    # ``{"action": <decision>}`` — a plain approval/rejection/deliver_manual
    # needs no payload to be faithfully resumed.
    return True


async def _committed_decision_resume_data(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Reconstruct ``resume_data`` from the run's latest committed HITL decision.

    Returns the persisted ``hitl_claims.decision_payload`` (jsonb) verbatim when
    present, else ``{"action": <decision>}`` for a payload-less committed
    decision (legacy rows / pre-migration DBs). ``None`` when no decision is
    committed. NEVER returns ``{}`` for a committed decision — a recovered
    rejection must resume as rejected, and an approve-with-modification must
    carry its modification.
    """
    result = await session.execute(
        text(
            "SELECT decision, decision_payload FROM hitl_claims "
            "WHERE organisation_id=:oid AND run_id=:rid AND decision IS NOT NULL "
            "ORDER BY decision_at DESC NULLS LAST LIMIT 1"
        ),
        {"oid": str(org_id), "rid": str(run_id)},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    decision, payload = row[0], row[1]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = None
    if isinstance(payload, dict):
        return dict(payload)
    if payload is None and decision:
        return {"action": decision}
    return None


def _saq_run_claim_cap() -> int:
    """SAQ run claim cap for the claim-cap terminalizer (plan F8).

    Reads the settings field ``SAQ_RUN_CLAIM_CAP`` (``get_settings().saq_run_claim_cap``,
    default 20) — the single source of truth for the claim cap. The former
    ``pipeline_execution.SAQ_RUN_CLAIM_CAP`` module constant was removed; claim
    caps now resolve from settings everywhere. cron_helpers never imports
    pipeline_execution (import-linter: api must not reach langgraph transitively
    through pipeline_execution -> executor).
    """
    return int(get_settings().saq_run_claim_cap)


async def _terminalize_mid_graph_wedges(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    max_age_minutes: int,
) -> list[uuid.UUID]:
    """Terminal-fail SAQ runs wedged mid-graph for longer than *max_age_minutes*.

    DB-only, org-scoped (B4): a run stuck ``running`` with ``dispatcher='saq'``
    whose ``started_at`` is older than the max plausible run duration is wedged
    — a mid-graph stall can keep a fresh heartbeat alive (the in-process
    executor may be gone while the job hash lingers), so the stale-heartbeat
    branch never matches it. The age gate bounds the damage: ``executor_superseded``.
    Runs ``UPDATE ... RETURNING id`` so each failure is logged and the returned
    run ids drive the post-commit compensating analytics fact (P6', FAR-162).
    """
    result = await session.execute(
        text(
            "UPDATE runs SET status='failed', error_code=:code, "
            "error_detail=:detail, completed_at=now() "
            "WHERE organisation_id=:oid AND status='running' AND dispatcher='saq' "
            "AND started_at < now() - (:max_age_minutes * interval '1 minute') "
            "RETURNING id"
        ),
        {
            "oid": str(org_id),
            "code": _EXECUTOR_SUPERSEDED_ERROR_CODE,
            "detail": _EXECUTOR_SUPERSEDED_ERROR_DETAIL,
            "max_age_minutes": max_age_minutes,
        },
    )
    rows = result.all()
    for (run_id,) in rows:
        _log.warning(
            "dispatcher_reconcile: mid-graph wedge terminalized %s (started > %d min ago)",
            run_id,
            max_age_minutes,
        )
    return [run_id for (run_id,) in rows]


async def _terminalize_claim_cap_exhausted(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    claim_cap: int,
    stale_seconds: int,
) -> list[uuid.UUID]:
    """Terminal-fail SAQ runs at the claim cap whose heartbeat is STALE.

    DB-only, org-scoped, selected INDEPENDENTLY of the other reconcile
    predicates (B5). The old per-row terminalizer fired on ANY running row at
    ``claim_count >= cap`` regardless of heartbeat freshness — killing a LIVE
    run on its final claim (claim_run_async increments claim_count on EVERY
    claim, so a legitimately-running final claim could trip the cap while the
    executor is mid-node). Gating on a stale heartbeat means a capped run with
    checkpoints is still caught once nothing claims it for *stale_seconds*; a
    fresh-heartbeat capped run is left alone. Returns the terminalized run ids
    (``UPDATE ... RETURNING id``) to drive the post-commit compensating
    analytics fact (P6', FAR-162).
    """
    result = await session.execute(
        text(
            "UPDATE runs SET status='failed', error_code='claim_cap_exhausted', "
            "error_detail=:detail, completed_at=now() "
            "WHERE organisation_id=:oid AND status='running' AND claim_count >= :cap "
            "AND (heartbeat_at IS NULL OR heartbeat_at < now() - (:stale * interval '1 second')) "
            "RETURNING id"
        ),
        {"oid": str(org_id), "cap": claim_cap, "stale": stale_seconds, "detail": _CLAIM_CAP_EXHAUSTED_ERROR_DETAIL},
    )
    rows = result.all()
    for (run_id,) in rows:
        _log.warning(
            "dispatcher_reconcile: claim-cap-exhausted SAQ run terminalized %s (claim_count >= %d, stale heartbeat)",
            run_id,
            claim_cap,
        )
    return [run_id for (run_id,) in rows]


async def _fail_run_dispatch_failed(session: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Terminal-fail an enqueue-failed run past the TTL backstop.

    Org-scoped (C2-cron). Only transitions a run still ``pending`` with the
    ``enqueue_failed_at`` marker set (a run already dispatched/claimed/terminal
    is left untouched). The caller has already verified Redis is reachable.
    """
    await session.execute(
        text(
            "UPDATE runs SET status='failed', error_code=:code, error_detail=:detail, completed_at=now() "
            "WHERE id=:rid AND organisation_id=:oid AND status='pending' AND dispatcher IS NULL "
            "AND enqueue_failed_at IS NOT NULL"
        ),
        {
            "rid": str(run_id),
            "oid": str(org_id),
            "code": _DISPATCH_FAILED_ERROR_CODE,
            "detail": _DISPATCH_FAILED_ERROR_DETAIL,
        },
    )


async def _record_fact_for_terminalized_run(run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Best-effort daily-fact write for a run terminalised by dispatcher_reconcile (P6').

    The terminalizer UPDATEs commit inside the per-org session transaction;
    this helper opens its OWN session AFTER that commit, sets the RLS org
    context, re-selects the Run ORM (a pre-write entity would record
    ``status='running'`` with a NULL ``completed_at``), and records the daily
    fact via the shared ``record_fact_for_terminal_failed_run`` wrapper.
    None-guarded and fail-open: a facts-write failure is logged and swallowed —
    it must never fail the reconcile tick or roll back the already-committed
    terminal write.
    """
    try:
        from modulo.core.analytics import record_fact_for_terminal_failed_run
        from modulo.db.crud.run import get_run

        async with _open_factory()() as session, session.begin():
            await _set_rls_org(session, org_id)
            run = await get_run(session, run_id)
            if run is None:
                _log.warning("cron_helpers.terminalized_facts_run_missing run=%s", run_id)
                return
            await record_fact_for_terminal_failed_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.terminalized_facts_failed run=%s", run_id, exc_info=True)


async def run_classification_reconcile() -> dict[str, int]:
    """FAR-189 backfill: classify terminal runs missed by the inline hook.

    Invoked from :func:`dispatcher_reconcile` (every 60s) — the periodic
    production path for the FAR-189 reconciliation sweep. The raw-SQL
    terminalizers in this module (nodeless zombie, mid-graph wedge,
    claim-cap-exhausted, enqueue-failed) never run the classification hook, so
    without this their ``run_classification`` stays NULL forever and the
    FAR-190 streak walk loses the infra failures this feature exists to count.

    Runs system-scoped (modulo_system role, LOGIN, BYPASSRLS): the sweep itself
    processes each org under its own RLS context. Bounded + idempotent, so an
    every-60s tick simply drains whatever backlog remains. Best-effort and never
    raises: a sweep failure is logged by the caller and must never fail the
    reconcile tick.
    """
    from modulo.core.pipeline_engine.classify import reconcile_missing_classifications

    return await reconcile_missing_classifications(_open_system_factory())


async def dispatcher_reconcile() -> dict[str, Any]:
    """System cron — re-dispatch runs whose SAQ job is missing (every 60s).

    Drain note (FAR-402 P5 / design §10 O7): a run in ``unknown`` (adopted from
    FAR-410, a NON-TERMINAL recovery status) holds a concurrency slot while its
    outcome is indeterminate. It must be DETECTIBLE here — reconciled to a
    terminal outcome via an operator re-run that reuses the persisted run-level
    ``idempotency_key`` (never silently dropped, and never re-dispatched as a
    fresh unit of work). ``unknown`` is intentionally absent from
    ``ONGOING_ACTIVE_STATUSES`` so a stuck UNKNOWN does not trigger an ongoing
    top-up. Retry/compensation runs compose with it by re-executing against the
    same pinned snapshot and reusing the run-level idempotency key.

    Predicate (plan F3c + F6a): ``queue.job(run:{id})`` IS None AND staleness:

      * pending + dispatched_at IS NULL: capacity-deferred — matched on the
        run's CREATION path (SAQ mode only), NOT ``dispatcher='saq'``, because
        ``dispatch_run`` returns deferred BEFORE recording dispatched_at/
        dispatcher. NO staleness gate (re-dispatch when capacity frees).
      * pending + capacity marker (``org_capacity_limited``/
        ``pipeline_capacity``) + stale or NULL heartbeat (FAR-108): a
        stranded capacity-blocked run is re-dispatched on the 60s cadence once
        its heartbeat ages past ``CAPACITY_REDISPATCH_SECONDS`` (default
        120s) — the fast path that used to wait for the multi-minute stale-run
        sweep. The heartbeat gate throttles the sandbox-cap claim/demote churn
        loop; ``dispatch_run`` re-checks capacity atomically so a still-blocked
        run is re-deferred (counted ``capacity_deferred``, never alerted).
      * pending + dispatched_at set + ``dispatcher='saq'``: stale by the
        re-enqueue window.
      * pending + dispatched_at set + dispatcher IS NULL + NO
        ``enqueue_failed_at``: zombie from a fail-fast SAQ enqueue failure
        BEFORE the durable-dispatch marker existed. NO staleness gate
        (re-dispatch immediately).
      * pending + dispatched_at set + dispatcher IS NULL +
        ``enqueue_failed_at`` SET (B3 durable dispatch): the run's enqueue
        failed non-terminally. Re-dispatched once its heartbeat is stale past
        ``ENQUEUE_FAILED_REDISPATCH_SECONDS`` (~120s), capped at
        ``ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK`` (50) per tick. A marker
        older than ``ENQUEUE_FAILED_TTL_BACKSTOP_MINUTES`` (60m) is
        terminal-failed ``dispatch_failed`` — ONLY when Redis is verifiably
        reachable (lightweight ping); Redis down -> keep pending.
      * running: ``dispatcher='saq'``, heartbeat stale by 2*SAQ_JOB_HEARTBEAT.
      * running + ``dispatcher='saq'`` + FRESH heartbeat but zero node
        progress after SAQ_CLAIMED_NODELESS_MINUTES (node_token_usage/out-
        puts_json both NULL + no LangGraph checkpoint for the thread):
        nodeless zombie - terminal-failed with ``executor_stalled``, NEVER
        re-dispatched (a re-dispatch could double-execute a live-but-stuck
        execute_run).
      * awaiting_human/claimed: ``dispatcher='saq'``, heartbeat stale by
        2*SAQ_JOB_HEARTBEAT, AND no SAQ job in Redis (F6a gated recovery — the
        no-job gate is applied per-row). A half-resumed run whose ``resume_run``
        job was lost (crash between the HITL decision commit and enqueue) would
        otherwise sit awaiting_human/claimed forever with no job; this recovers
        it as ``resume_run``. The gate is narrow: a waiting run's completed
        ``execute_run`` job hash is stored for its finish-origin ttl (300s)
        then expires, and its frozen heartbeat only crosses the stale line once
        nothing has claimed it for 2x the SAQ heartbeat window.

        F6a auto-approve guard: an ``awaiting_human`` row is re-dispatched ONLY
        when a gate decision is actually committed AND (for payload-carrying
        actions) its ``decision_payload`` is present — checked per-row via
        :func:`_awaiting_human_has_committed_decision`. A genuinely-waiting
        run (no decision committed) whose job hash expired + heartbeat froze
        must NOT be resumed: ``executor.resume`` injects the (empty) payload as
        ``_hitl_decision``, which the HITL gate node treats as an approval —
        auto-approving the gate. ``claimed`` rows are exempt (a claim was
        already made — mid-resume crash recovery). Resume ``resume_data`` is
        reconstructed from the persisted ``hitl_claims.decision_payload``
        (B1-reconcile) — a recovered rejection resumes as rejected, never as an
        empty ``{}``.

    Per-org DB-only terminalizers (before the row select):
      * B4 age-bound: any ``running`` + ``dispatcher='saq'`` row whose
        ``started_at`` is older than ``_MID_GRAPH_WEDGE_MAX_AGE_MINUTES``
        (~135m) is terminal-failed ``executor_superseded``.
      * B5 claim-cap: any ``running`` row at ``claim_count >= cap`` whose
        heartbeat is STALE is terminal-failed ``claim_cap_exhausted`` —
        selected INDEPENDENTLY of the reconcile predicates so a capped
        fresh-heartbeat run with checkpoints is still caught once its heartbeat
        goes stale, while a LIVE run on its final claim is never killed.

    On match: verify the Redis read, RE-CHECK ``q.job()`` AFTER the decision
    and immediately before enqueue (skip if a job now exists — a concurrent
    worker may have re-enqueued), then a normal ``queue.enqueue()`` with a
    FRESH ``key_suffix`` so SAQ's key-based dedupe never suppresses the
    recovery enqueue. NO SAQ-internal structures (``saq:abort:*``, incomplete
    zset, queued/active lists) are read or written — the atomic claim UPDATE
    (``claim_run_async``) is the real at-most-once dedupe.

    Re-dispatch type (discriminator): awaiting_human/claimed -> ``resume_run``;
    pending/running -> ``execute_run``. Capacity-deferred runs are re-dispatched
    only when their pipeline has free capacity.

    Every run terminalised this tick (``executor_superseded`` /
    ``claim_cap_exhausted`` / ``dispatch_failed``) gets a compensating
    ``run_daily_facts`` row (FAR-162, P6') written after the per-org
    transactions commit — the terminalizers never run ``finalize_cost``, so
    without this the failed runs would be invisible to analytics.
    """
    from sqlalchemy import or_

    settings = get_settings()
    queue_name = settings.saq_runs_queue
    reenqueue_window = int(settings.saq_reenqueue_window)
    stale_window = RECONCILE_STALE_HEARTBEAT_FACTOR * int(settings.saq_job_heartbeat)
    nodeless_window = int(settings.saq_claimed_nodeless_minutes)
    capacity_redispatch_seconds = CAPACITY_REDISPATCH_SECONDS
    max_age_minutes = _MID_GRAPH_WEDGE_MAX_AGE_MINUTES
    claim_cap = _saq_run_claim_cap()
    factory = _open_system_factory()
    summary = _dispatcher_summary()
    # Runs terminalised by this tick's terminalizers — (run_id, org_id) — whose
    # compensating daily fact must be recorded once the per-org transactions
    # commit (FAR-162, P6'): the terminalizers write raw UPDATEs and never run
    # finalize_cost.
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]] = []

    redis_client = AsyncRedis.from_url(
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    try:
        org_ids = await _collect_org_ids(factory)
        if not org_ids:
            # Still record the run so /healthz/ready sees a fresh last_run_at
            # even in an empty-org environment (the cron keeps ticking every 60s).
            set_dispatcher_reconcile_stats(summary)
            await write_dispatcher_reconcile_stats(redis_client, summary)
            return summary
        q = RedisQueue(redis_client, name=queue_name)
        # Per-tick re-dispatch cap counter for the B3 enqueue-failed branch.
        enqueue_failed_redispatched = 0
        re_dispatch_predicate = or_(
            _build_re_dispatch_predicate(
                reenqueue_window=reenqueue_window,
                stale_window=stale_window,
                capacity_redispatch_seconds=capacity_redispatch_seconds,
                enqueue_failed_redispatch_seconds=ENQUEUE_FAILED_REDISPATCH_SECONDS,
            ),
            # Claimed-but-nodeless zombie branch: running + saq + FRESH
            # heartbeat but ZERO node progress after the nodeless window.
            # The fresh heartbeat excludes it from the stale branch above
            # (that is the primary hang mechanism - a live heartbeat keeps
            # the run 'running' forever), so it gets its own predicate.
            # Repaired by terminal-fail, NOT re-dispatch (see _fail_nodeless_run).
            _nodeless_zombie_predicate(nodeless_window),
        )
        for org_id in org_ids:
            enqueue_failed_redispatched = await _reconcile_org(
                factory,
                q,
                redis_client,
                org_id,
                re_dispatch_predicate,
                nodeless_window,
                max_age_minutes,
                claim_cap,
                stale_window,
                capacity_redispatch_seconds,
                enqueue_failed_redispatched,
                summary,
                terminalized_run_ids,
            )
        # FAR-162 (P6') — record a daily fact for every run terminalised this
        # tick (executor_superseded / claim_cap_exhausted / dispatch_failed):
        # the terminalizers write raw UPDATEs and never run finalize_cost, so
        # without this the failed runs would be invisible to the analytics
        # failure/stall dimensions. All per-org terminalizer transactions have
        # committed by now; each facts write opens its own RLS-scoped session.
        for run_id, run_org_id in terminalized_run_ids:
            await _record_fact_for_terminalized_run(run_id, run_org_id)
        await _run_reconcile_sweeps(redis_client, summary)
        # Record the outcome for /healthz/ready BEFORE the client is closed:
        # the shared Redis key is what the WEB process reads (the in-process
        # dict lives only in this worker process).
        await _update_reconcile_telemetry(summary)
        set_dispatcher_reconcile_stats(summary)
        await write_dispatcher_reconcile_stats(redis_client, summary)
        return summary
    finally:
        with _suppress_aclose():
            await redis_client.aclose()


def _dispatcher_summary() -> dict[str, Any]:
    return {
        "scanned": 0,
        "repaired": 0,
        "skipped": 0,
        "redis_errors": 0,
        "deduped": 0,
        "nodeless_failed": 0,
        "nodeless_redispatched": 0,
        "claim_cap_terminalized": 0,
        "mid_graph_wedge_terminalized": 0,
        "age_terminalized": 0,
        "dispatch_failed_terminalized": 0,
        "enqueue_failed_ttl_terminalized": 0,
        "enqueue_failed_redispatched": 0,
        "enqueue_failed_capped": 0,
        "capacity_deferred": 0,
        "streak_scanned": 0,
        "streak_deactivated": 0,
        "streak_capped": 0,
        "streak_alerts": 0,
        "streak_notify_failed": 0,
        "run_api_key_scanned": 0,
        "run_api_key_revoked": 0,
        "run_api_key_errors": 0,
    }


async def _reconcile_org(
    factory: async_sessionmaker[AsyncSession],
    q: RedisQueue,
    redis_client: AsyncRedis,
    org_id: uuid.UUID,
    re_dispatch_predicate: Any,
    nodeless_window: int,
    max_age_minutes: int,
    claim_cap: int,
    stale_window: int,
    capacity_redispatch_seconds: int,
    enqueue_failed_redispatched: int,
    summary: dict[str, Any],
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
) -> int:
    """Run one org's reconcile pass (terminalizers + row select + per-row loop)."""
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.run import Run

    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)
        try:
            # B4: age-bound mid-graph wedge terminalizer (DB-only, org-scoped).
            # Runs stuck 'running' past the max plausible duration are wedged —
            # fail them BEFORE the row select so they are excluded from
            # re-dispatch.
            wedged = await _terminalize_mid_graph_wedges(session, org_id, max_age_minutes=max_age_minutes)
            summary["mid_graph_wedge_terminalized"] += len(wedged)
            summary["age_terminalized"] = summary["mid_graph_wedge_terminalized"]
            terminalized_run_ids.extend((run_id, org_id) for run_id in wedged)
            # B5: claim-cap terminalizer — INDEPENDENT of the reconcile
            # predicates, stale-heartbeat gated (a LIVE run on its final claim
            # is never killed; a capped run whose heartbeat froze is still
            # caught).
            capped = await _terminalize_claim_cap_exhausted(
                session, org_id, claim_cap=claim_cap, stale_seconds=stale_window
            )
            summary["claim_cap_terminalized"] += len(capped)
            terminalized_run_ids.extend((run_id, org_id) for run_id in capped)
            rows = (
                await session.execute(
                    select(
                        Run.id,
                        Run.pipeline_id,
                        Run.status,
                        Run.dispatched_at,
                        Run.heartbeat_at,
                        Run.node_token_usage,
                        Run.outputs_json,
                        Run.started_at,
                        Run.claim_count,
                        Run.dispatcher,
                        text("runs.enqueue_failed_at AS enqueue_failed_at"),
                        Pipeline.retry_policy,
                    )
                    .join(Pipeline, Pipeline.id == Run.pipeline_id, isouter=True)
                    .where(
                        Run.organisation_id == org_id,
                        Run.status.in_(("pending", "running", "awaiting_human", "claimed")),
                        re_dispatch_predicate,
                        _reconcile_capacity_marker_exclusion(capacity_redispatch_seconds),
                    )
                )
            ).all()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("dispatcher_reconcile: read failed (org %s)", org_id)
            return enqueue_failed_redispatched

        for row in rows:
            summary["scanned"] += 1
            enqueue_failed_redispatched = await _reconcile_one_row(
                session,
                q,
                redis_client,
                org_id,
                row,
                nodeless_window,
                enqueue_failed_redispatched,
                summary,
                terminalized_run_ids,
            )
    return enqueue_failed_redispatched


async def _run_reconcile_sweeps(redis_client: AsyncRedis, summary: dict[str, Any]) -> None:
    """FAR-189/FAR-190 compensating sweeps + the healthz stats write (best-effort)."""
    try:
        classification = await run_classification_reconcile()
        summary["classification_classified"] = classification.get("classified", 0)
        summary["classification_unclassified"] = classification.get("unclassified", 0)
        summary["classification_errors"] = classification.get("errors", 0)
        if classification.get("classified") or classification.get("unclassified"):
            _log.info(
                "dispatcher_reconcile.classification_sweep",
                extra={
                    "classified": classification.get("classified", 0),
                    "unclassified": classification.get("unclassified", 0),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("dispatcher_reconcile.classification_sweep_failed", exc_info=True)
    try:
        streak = await enforce_no_delivery_streaks(redis_client=redis_client)
        summary["streak_scanned"] = streak.get("scanned", 0)
        summary["streak_deactivated"] = streak.get("deactivated", 0)
        summary["streak_capped"] = streak.get("capped", 0)
        summary["streak_alerts"] = streak.get("alerts", 0)
        summary["streak_notify_failed"] = streak.get("notify_failed", 0)
        if streak.get("deactivated") or streak.get("alerts"):
            _log.info(
                "dispatcher_reconcile.streak_sweep",
                extra={
                    "deactivated": streak.get("deactivated", 0),
                    "alerts": streak.get("alerts", 0),
                    "capped": streak.get("capped", 0),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("dispatcher_reconcile.streak_sweep_failed", exc_info=True)
    try:
        from modulo.auth.api_key import revoke_run_api_key_sweep

        revoked_keys = await revoke_run_api_key_sweep(_open_system_factory())
        summary["run_api_key_scanned"] = revoked_keys.get("scanned", 0)
        summary["run_api_key_revoked"] = revoked_keys.get("revoked", 0)
        summary["run_api_key_errors"] = revoked_keys.get("errors", 0)
        if revoked_keys.get("revoked") or revoked_keys.get("errors"):
            _log.info(
                "dispatcher_reconcile.run_api_key_sweep",
                extra={
                    "scanned": revoked_keys.get("scanned", 0),
                    "revoked": revoked_keys.get("revoked", 0),
                    "errors": revoked_keys.get("errors", 0),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("dispatcher_reconcile.run_api_key_sweep_failed", exc_info=True)
    try:
        from modulo.core.rollback_thresholds import evaluate_rollback_thresholds

        threshold_result = await evaluate_rollback_thresholds(_open_factory())
        summary["rollback_thresholds_checked"] = threshold_result.get("orgs_checked", 0)
        summary["rollback_thresholds_flagged"] = len(threshold_result.get("flagged_orgs", []))
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["rollback_thresholds_checked"] = 0
        summary["rollback_thresholds_flagged"] = 0
        _log.warning("dispatcher_reconcile.rollback_thresholds_failed", exc_info=True)


async def _update_reconcile_telemetry(summary: dict[str, Any]) -> None:
    """D1: update the OTel runtime gauges/counters from this tick (best-effort)."""
    if not get_settings().modulo_telemetry_enabled:
        return
    try:
        from modulo.core.error_tracking.metrics import (
            record_stall_reason,
            sample_error_group_metrics,
            sample_run_runtime_metrics,
        )

        await sample_run_runtime_metrics(_open_system_factory())
        await sample_error_group_metrics(_open_system_factory())
        if summary["nodeless_failed"]:
            record_stall_reason("executor_stalled", summary["nodeless_failed"])
        if summary["claim_cap_terminalized"]:
            record_stall_reason("claim_cap_exhausted", summary["claim_cap_terminalized"])
        if summary["mid_graph_wedge_terminalized"]:
            record_stall_reason("executor_superseded", summary["mid_graph_wedge_terminalized"])
        if summary["dispatch_failed_terminalized"]:
            record_stall_reason("dispatch_failed", summary["dispatch_failed_terminalized"])
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("dispatcher_reconcile.metrics_update_failed", exc_info=True)


async def _reconcile_enqueue_failed(
    redis_client: AsyncRedis,
    session: AsyncSession,
    org_id: uuid.UUID,
    row: Any,
    is_enqueue_failed: bool,
    enqueue_failed_redispatched: int,
    summary: dict[str, Any],
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
) -> int | None:
    """Handle the B3 enqueue-failed branch; ``None`` when the row continues.

    Terminal-fails past the TTL backstop (only when Redis is verifiably
    reachable — Redis down keeps the row pending), else re-dispatches under the
    bounded interval + per-tick cap. Returns the (possibly updated)
    enqueue-failed counter when the branch fully handled the row, ``None`` when
    the caller must proceed with the normal repairs. Extracted unchanged from
    ``_reconcile_one_row`` (complexity bound).
    """
    if not is_enqueue_failed:
        return None
    marker_age = (datetime.now(UTC) - row.enqueue_failed_at).total_seconds()
    if marker_age > ENQUEUE_FAILED_TTL_BACKSTOP_MINUTES * 60:
        try:
            await redis_client.ping()
        except asyncio.CancelledError:
            raise
        except Exception:
            summary["skipped"] += 1
            # Redis down — do NOT terminal-fail; keep pending for a later tick.
            return enqueue_failed_redispatched
        await _fail_run_dispatch_failed(session, row.id, org_id)
        terminalized_run_ids.append((row.id, org_id))
        summary["dispatch_failed_terminalized"] += 1
        summary["enqueue_failed_ttl_terminalized"] += 1
        return enqueue_failed_redispatched
    if enqueue_failed_redispatched >= ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK:
        summary["enqueue_failed_capped"] += 1
        _log.warning(
            "dispatcher_reconcile: enqueue-failed re-dispatch cap hit (%d/tick); deferring run %s to a later tick",
            ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK,
            row.id,
        )
        return enqueue_failed_redispatched
    return None


async def _read_reconcile_job(
    session: AsyncSession,
    q: RedisQueue,
    org_id: uuid.UUID,
    row: Any,
    job_key: str,
    description: str,
    summary: dict[str, Any],
) -> tuple[Any, bool]:
    """Read the run's SAQ job; ``(job, True)`` when the read succeeded.

    ``description`` is "read"/"re-check" and only tags the log/error text. On a
    Redis failure the error is ingested and ``(None, False)`` returned —
    fail-safe: NEVER act on an unreadable Redis. Extracted unchanged from
    ``_reconcile_one_row`` (complexity bound).
    """
    try:
        return await q.job(job_key), True
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["redis_errors"] += 1
        _log.exception("dispatcher_reconcile: Redis %s failed for run %s", description, row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="dispatcher_reconcile",
            message=f"dispatcher_reconcile: Redis {description} failed for run {row.id}",
            context={"run_id": str(row.id)},
        )
        return None, False


async def _resolve_hitl_resume_or_skip(
    session: AsyncSession,
    org_id: uuid.UUID,
    row: Any,
    summary: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """F6a auto-approve guard + durable resume payload for one row.

    Returns ``(skip, resume_data)``: ``skip`` is True when a genuinely-waiting
    awaiting_human run must NOT be resumed (no committed gate decision — a
    resumed empty decision would auto-approve the gate); otherwise ``resume_data``
    is reconstructed from the committed HITL decision (never ``{}``). ``claimed``
    rows are exempt from the guard (a claim was already made — mid-resume crash
    recovery). Extracted unchanged from ``_reconcile_one_row``.
    """
    if row.status not in ("awaiting_human", "claimed"):
        return False, None
    if row.status == "awaiting_human" and not await _awaiting_human_has_committed_decision(session, org_id, row.id):
        summary["skipped"] += 1
        _log.info(
            "dispatcher_reconcile: awaiting_human run %s has no committed HITL decision — not re-dispatched",
            row.id,
        )
        return True, None
    return False, await _committed_decision_resume_data(session, org_id, row.id)


async def _re_dispatch_reconciled_run(
    session: AsyncSession,
    q: RedisQueue,
    org_id: uuid.UUID,
    row: Any,
    job_type: str,
    key_suffix: str,
    resume_data: dict[str, Any] | None,
    is_enqueue_failed: bool,
    enqueue_failed_redispatched: int,
    summary: dict[str, Any],
) -> int:
    """Re-dispatch the repaired run through ``dispatch_run``; gate on the outcome.

    Returns the updated enqueue-failed counter. Every exception path ingests an
    error event and only bumps ``redis_errors`` — a re-dispatch never raises.
    Extracted unchanged from ``_reconcile_one_row`` (complexity bound).
    """
    try:
        outcome, new_job_id = await _re_enqueue_run(
            q.name,
            str(row.id),
            str(org_id),
            job_type,
            resume_data=resume_data,
            key_suffix=key_suffix,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["redis_errors"] += 1
        _log.exception("dispatcher_reconcile: re-enqueue failed for run %s", row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="dispatcher_reconcile",
            message=f"dispatcher_reconcile: re-enqueue failed for run {row.id}",
            context={"run_id": str(row.id), "job_type": job_type},
        )
        return enqueue_failed_redispatched
    if outcome == "enqueued":
        summary["repaired"] += 1
        if is_enqueue_failed:
            enqueue_failed_redispatched += 1
            summary["enqueue_failed_redispatched"] += 1
        _log.info(
            "dispatcher_reconcile: re-dispatched run %s as %s (%s)",
            row.id,
            job_type,
            new_job_id,
        )
    elif outcome == "deferred":
        # Still capacity-blocked: dispatch_run re-checked the pipeline + org
        # run concurrency limits in one transaction and deferred without
        # enqueueing. Not an error and NOT a dedup — counted separately so ops
        # can see the stranded-pending cohort, and no false error_event is
        # ingested.
        summary["capacity_deferred"] += 1
        _log.warning(
            "dispatcher_reconcile: run %s still capacity-deferred — pending undispatched",
            row.id,
        )
    elif outcome == "enqueue_failed":
        # The re-dispatch itself failed to enqueue: the run is left pending
        # with enqueue_failed_at refreshed by dispatch_run — a later tick
        # retries. Not a dedup, not a terminal failure.
        summary["skipped"] += 1
        _log.warning(
            "dispatcher_reconcile: re-enqueue failed for run %s (left pending for retry)",
            row.id,
        )
    else:
        summary["deduped"] += 1
        _log.warning("dispatcher_reconcile: re-enqueue still deduped for run %s", row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="dispatcher_reconcile",
            message=f"dispatcher_reconcile: re-enqueue still deduped for run {row.id}",
            context={"run_id": str(row.id), "job_type": job_type},
        )
    return enqueue_failed_redispatched


async def _reconcile_nodeless_repair(
    session: AsyncSession,
    q: RedisQueue,
    org_id: uuid.UUID,
    row: Any,
    nodeless_window: int,
    enqueue_failed_redispatched: int,
    summary: dict[str, Any],
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
) -> int | None:
    """Repair a claimed-but-nodeless zombie; ``None`` when the row continues.

    A nodeless zombie executed ZERO nodes (no checkpoint, no
    ``node_token_usage``, no ``outputs_json``), so re-dispatch is SAFE (no
    double-execution; these pipelines only create PRs after a node runs).
    Re-dispatch it back to the queue (a fresh worker picks it up) instead of
    terminal-failing, bounded by ``retry_policy`` / ``claim_count``. Only
    terminal-fail when re-dispatch is NOT warranted (retry budget exhausted)
    or the re-dispatch itself fails (fall back so the run is never left
    dangling). Returns the (possibly updated) enqueue-failed counter when the
    branch fully handled the row, ``None`` when the caller must proceed with
    the normal repairs. Extracted from ``_reconcile_one_row`` (complexity
    bound).
    """
    if not _is_nodeless_zombie_row(row, nodeless_window):
        return None
    if _should_redispatch_nodeless(row):
        job_type = _reconcile_job_type(row.status)
        key_suffix = uuid.uuid4().hex
        await _redispatch_nodeless(
            session,
            q,
            org_id,
            row,
            job_type,
            key_suffix,
            summary,
            terminalized_run_ids,
        )
        return enqueue_failed_redispatched
    # Re-dispatch not warranted (retry budget exhausted / policy excludes
    # 'stall'): terminal-fail exactly as before.
    summary["nodeless_failed"] += 1
    await _fail_nodeless_run(session, row.id, org_id)
    # FAR-162 (P6'): the nodeless terminalizer writes a raw
    # ORM UPDATE (never finalize_cost) — add the run so its
    # compensating daily fact is recorded once the per-org
    # transaction commits, like the other terminalizers.
    terminalized_run_ids.append((row.id, org_id))
    return enqueue_failed_redispatched


async def _redispatch_nodeless(
    session: AsyncSession,
    q: RedisQueue,
    org_id: uuid.UUID,
    row: Any,
    job_type: str,
    key_suffix: str,
    summary: dict[str, Any],
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """Re-dispatch ONE nodeless zombie; terminal-fail when the enqueue fails.

    A failed re-enqueue terminal-fails the run so it is never left dangling
    (the fallback). A deferred/deduped/enqueue_failed outcome is counted as
    skipped — not an error, not a terminal failure — so a later tick retries.
    Returns ``None`` — the branch always fully handles the row, so the caller
    passes its enqueue-failed counter through unchanged. Extracted from
    ``_reconcile_one_row`` (complexity bound).
    """
    try:
        outcome, _ = await _re_enqueue_run(
            q.name,
            str(row.id),
            str(org_id),
            job_type,
            key_suffix=key_suffix,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["redis_errors"] += 1
        _log.exception("dispatcher_reconcile: nodeless re-enqueue failed for run %s", row.id)
        await _ingest_saq_error(
            session,
            org_id,
            function="dispatcher_reconcile",
            message=f"dispatcher_reconcile: nodeless re-enqueue failed for run {row.id}",
            context={"run_id": str(row.id)},
        )
        # Fallback: terminal-fail so the run is never left dangling
        # when re-dispatch is impossible.
        await _fail_nodeless_run(session, row.id, org_id)
        summary["nodeless_failed"] += 1
        terminalized_run_ids.append((row.id, org_id))
        return
    if outcome == "enqueued":
        summary["nodeless_redispatched"] += 1
        _log.info(
            "dispatcher_reconcile.nodeless_redispatched run=%s org=%s",
            row.id,
            org_id,
        )
    else:
        # deferred / deduped / enqueue_failed -> not an error, not a
        # terminal failure: let a later tick retry. Count as skipped
        # (the run stays 'running' / pending undispatched).
        summary["skipped"] += 1
        _log.warning(
            "dispatcher_reconcile: nodeless re-enqueue outcome=%s for run %s (deferring to later tick)",
            outcome,
            row.id,
        )


async def _reconcile_one_row(
    session: AsyncSession,
    q: RedisQueue,
    redis_client: AsyncRedis,
    org_id: uuid.UUID,
    row: Any,
    nodeless_window: int,
    enqueue_failed_redispatched: int,
    summary: dict[str, Any],
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
) -> int:
    """Reconcile ONE matched run row; returns the updated enqueue-failed counter.

    Handles the nodeless zombie (via ``_reconcile_nodeless_repair``), B3
    enqueue-failed branch, F6a resume guard, capacity check, the job
    re-check, and the re-dispatch decision for a single row. ``summary`` and
    ``terminalized_run_ids`` are mutated in place.
    """
    # Claimed-but-never-executed zombie repair — see
    # ``_reconcile_nodeless_repair`` / ``_redispatch_nodeless``.
    handled = await _reconcile_nodeless_repair(
        session,
        q,
        org_id,
        row,
        nodeless_window,
        enqueue_failed_redispatched,
        summary,
        terminalized_run_ids,
    )
    if handled is not None:
        return handled

    # B3 enqueue-failed branch: pending + dispatched + dispatcher
    # NULL + enqueue_failed_at set.
    is_enqueue_failed = (
        row.status == "pending"
        and row.dispatched_at is not None
        and getattr(row, "dispatcher", None) is None
        and getattr(row, "enqueue_failed_at", None) is not None
    )
    early = await _reconcile_enqueue_failed(
        redis_client,
        session,
        org_id,
        row,
        is_enqueue_failed,
        enqueue_failed_redispatched,
        summary,
        terminalized_run_ids,
    )
    if early is not None:
        return early

    job_key = f"run:{row.id}"
    job, ok = await _read_reconcile_job(session, q, org_id, row, job_key, "read", summary)
    if not ok:
        return enqueue_failed_redispatched
    if job is not None:
        summary["skipped"] += 1  # job still exists — nothing to repair
        return enqueue_failed_redispatched

    # F6a auto-approve guard + durable resume payload (B1-reconcile) — see
    # ``_resolve_hitl_resume_or_skip``.
    skip, resume_data = await _resolve_hitl_resume_or_skip(session, org_id, row, summary)
    if skip:
        return enqueue_failed_redispatched

    # Capacity check for capacity-deferred runs (pending + no
    # dispatched_at). Re-dispatch only when the pipeline has free
    # capacity (plan F3b/F3c).
    if row.status == "pending" and row.dispatched_at is None:
        deferred = await _capacity_defer_pending_run(session, row, summary)
        if deferred:
            return enqueue_failed_redispatched

    # NO SAQ-internal eviction (B2): a version-pinned DEL/ZREM/LREM of
    # saq:abort:*/incomplete/queued/active is TOCTOU-unsafe across machines
    # and can permanently strand a job. The atomic claim UPDATE is the real
    # dedupe — a second worker claiming the same run loses. Re-check
    # q.job() AFTER the decision and immediately before enqueue: if a job now
    # exists under the original key, a concurrent worker already re-enqueued
    # it — skip.
    job, ok = await _read_reconcile_job(session, q, org_id, row, job_key, "re-check", summary)
    if not ok:
        return enqueue_failed_redispatched
    if job is not None:
        summary["skipped"] += 1
        return enqueue_failed_redispatched

    # Discriminator (F6a): awaiting_human/claimed -> resume_run;
    # pending/running -> execute_run. Re-dispatch with a FRESH
    # key_suffix so SAQ key dedupe never suppresses the recovery
    # enqueue.
    job_type = _reconcile_job_type(row.status)
    key_suffix = uuid.uuid4().hex
    return await _re_dispatch_reconciled_run(
        session,
        q,
        org_id,
        row,
        job_type,
        key_suffix,
        resume_data,
        is_enqueue_failed,
        enqueue_failed_redispatched,
        summary,
    )


async def _capacity_defer_pending_run(
    session: AsyncSession,
    row: Any,
    summary: dict[str, Any],
) -> bool:
    """Mark a capacity-blocked pending run (FAR-225); True when deferred."""
    from modulo.db.crud.run import ERROR_CODE_PIPELINE_CAPACITY, count_active_runs_for_pipeline
    from modulo.db.models.pipeline import Pipeline

    pipeline = await session.get(Pipeline, row.pipeline_id)
    max_concurrent = pipeline.max_concurrent_runs if pipeline is not None else 0
    if max_concurrent <= 0:
        return False
    active = await count_active_runs_for_pipeline(
        session, row.pipeline_id, include_pending=False, exclude_run_id=row.id
    )
    if active < max_concurrent:
        return False
    # FAR-225: mark the skipped run so it is rescued,
    # not killed. Stamping error_code='pipeline_capacity'
    # (a) excludes the run from the never_dispatched
    # kill sweep and (b) admits it to the
    # capacity_marked_stale re-dispatch branch once a
    # pipeline slot frees (heartbeat-stale gated), so
    # a webhook-burst orphan is recovered instead of
    # terminal-failed at the 300s window. Idempotent —
    # an already-marked run is not re-marked each tick.
    await session.execute(
        text("UPDATE runs SET error_code = :code WHERE id = :rid AND error_code IS DISTINCT FROM :code").bindparams(
            code=ERROR_CODE_PIPELINE_CAPACITY, rid=row.id
        )
    )
    summary["capacity_deferred"] += 1
    return True


async def _re_enqueue_run(
    queue_name: str,
    run_id_str: str,
    org_id_str: str,
    job_type: str,
    *,
    resume_data: dict[str, Any] | None = None,
    key_suffix: str | None = None,
) -> tuple[str, str | None]:
    """Normal re-dispatch through ``dispatch_run``; gate on the return value.

    ``dispatch_run`` is the single gating point (F3e): it capacity-checks,
    writes ``dispatched_at``, enqueues (with a FRESH ``key_suffix`` so SAQ key
    dedupe never suppresses a recovery enqueue), and records ``dispatcher='saq'``
    + fresh claim token, clearing ``enqueue_failed_at`` on success. For a
    ``resume_run`` the caller passes the durable ``resume_data`` reconstructed
    from the committed HITL decision payload (B1-reconcile) — never ``{}`` for a
    committed decision. A still-deduped result logs + alerts — it does NOT loop.
    """
    from modulo.core.dispatch import dispatch_run

    return await dispatch_run(
        run_id_str,
        org_id_str,
        queue=queue_name,
        job_type=job_type,
        resume_data=resume_data,
        key_suffix=key_suffix,
    )


def func_now_minus(seconds: int) -> Any:
    """SQLAlchemy expression ``now() - interval`` for staleness predicates."""
    from sqlalchemy import text as _text

    return _text("now() - :seconds * interval '1 second'").bindparams(seconds=seconds)
