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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import redis.asyncio as aioredis
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import text

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.db_error_reporting import log_service_unavailable
from modulo.api.dependencies import get_or_create_engine
from modulo.core.bundled_runner.runner_reconciler import docker_endpoint_skip_reason
from modulo.core.cron_helpers import DISPATCHER_RECONCILE_TICK_SECONDS, read_dispatcher_reconcile_stats
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

# dispatcher_reconcile runs on a 60s system-cron tick. The degraded "stale"
# tier fires at 3x that cadence — DERIVED from the cron's own tick constant
# (cron_helpers.DISPATCHER_RECONCILE_TICK_SECONDS) rather than a bare 180
# literal, the SAME 3x-multiplier precedent as the sibling
# runner_health_probe below (also a 60s cadence, 3 * 60 = 180s "so a single
# missed tick never alerts"). At the old 60s threshold a single slow or
# missed tick read stale and paged the operator, contradicting FAR-199's own
# note that this tier "stays advisory … a single missed tick must never
# block bluegreen".
# The threshold must exceed the WORST healthy inter-stamp gap: a reconcile
# tick can spend its whole inner budget (settings
# `dispatcher_reconcile_budget_seconds`, default 95s) and that budget is
# stamped into last_run_at at tick END, plus up to one 60s cron wait before
# the next run starts — ≈155s, leaving ~25s of margin under 180s. Raising
# the budget toward its ceiling (pydantic-capped at 119s: 119 + 60 = 179s)
# would erode the margin to ~1s — re-raise THIS threshold in the same
# change if that ceiling ever moves.
# FAR-199 two-tier gate: staleness past _RECONCILE_UNAVAILABLE_SECONDS (5 min
# — 5x the cadence) flips the check to "unavailable" so readiness 503s. A
# single missed tick must never block bluegreen, so the degraded tier stays
# advisory; a reconcile stale 5+ minutes means the system worker's cron is
# silently dead (a wedged worker fleet that can no longer terminalize
# stalled/never-dispatched runs), which MUST block readiness.
_RECONCILE_STALE_SECONDS = 3 * DISPATCHER_RECONCILE_TICK_SECONDS  # 180s (3x cadence, same as runner_health_probe)
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

# decision_record_reconcile (FAR-1108 chunk 8b): the read-only
# decision-record corruption-detection sweep runs hourly on the system worker
# and persists its outcome (``scanned`` + anomaly counts) to this Redis key.
# Same advisory contract as its siblings, with the stale window tuned to the
# hourly cadence (3h — two missed ticks before it alerts): a dead sweep means
# the corruption-detection surface has silently stopped, so a missing or stale
# key warns without gating readiness.
_DECISION_RECORD_RECONCILE_STATS_KEY = "saq:cron:stats:decision_record_reconcile"
_DECISION_RECORD_RECONCILE_STALE_SECONDS = 3 * 60 * 60

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
    # FAR-1510: internal classification — "this result must not gate the
    # aggregate". Only the db_hygiene probe sets it, and only when the probe
    # did NOT complete (timeout / crash): a failure to INSPECT hygiene is not
    # a hygiene reading, and `database` already owns the reachability verdict.
    # Deliberately off the wire (``SkipJsonSchema`` keeps it out of the OpenAPI
    # document — so the generated frontend/src/lib/api/schema.ts is unchanged —
    # and ``exclude=True`` keeps it out of the response body): the readiness
    # payload contract predates this flag, advisory-ness is already conveyed by
    # the aggregate status staying `ok`, and the other advisory checks
    # (event_loop_lag, break_glass, ...) carry no such flag either.
    advisory: SkipJsonSchema[bool] = Field(default=False, exclude=True)


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


# ---------------------------------------------------------------------------
# FAR-1445 — database-hygiene sub-check (dead-tuple bloat + freeze age).
#
# Every other readiness sub-check asks "is the dependency REACHABLE / does it
# answer?"; none asked whether the database is still in a state where it can
# answer FAST. The runs table reached 361,302 dead tuples against 10,411 live
# (97% bloat, last_autovacuum = never) before anyone saw it, and it surfaced
# only as ~2.6s DB latency, 504-ing /healthz/ready (5s gateway budget) and a
# five-day deploy blockade. This check reports the two hygiene facts that are
# cheap to read and expensive to ignore.
#
# COST CONTRACT — /healthz/ready must not get slower (the endpoint 504'd on a
# 5s budget): ONE statement, ONE round trip through the ALREADY-POOLED engine
# (get_or_create_engine — no new connection per probe), reading only the
# in-memory statistics views (pg_stat_user_tables is one row per table, not a
# scan of table contents) plus the database's own freeze age. Budget: a 1s
# per-check timeout (modulo_health_db_hygiene_timeout_seconds), the smallest
# of any sub-check, and the check runs inside the same asyncio.gather as its
# siblings so it only costs the endpoint whatever is SLOWER than the current
# slowest check.
# ---------------------------------------------------------------------------

#: One round trip, no table-content reads: the worst dead-tuple ratio among
#: tables already over the absolute floor (the floor is applied in SQL so the
#: ORDER BY ranks the tables that can actually trip the check, then re-applied
#: in :func:`_grade_db_hygiene` so the grading rule stays a single, directly
#: testable function), plus ``age(datfrozenxid)`` against the server's own
#: ``autovacuum_freeze_max_age``. LEFT JOIN so a database with nothing over
#: the floor still returns its freeze age.
#:
#: Zero-size relations are excluded (``n_live_tup + n_dead_tup > 0``) in
#: addition to the ``NULLS LAST`` belt: a 0/0 table's ratio is NULL (0/0),
#: and Postgres sorts NULLs FIRST under ``DESC`` — with a legal
#: ``min_dead = 0`` floor (the field is ``ge=0``; the max-sensitivity setting
#: an operator would pick) an empty table would win the pick, the grader would
#: see ``dead_ratio is None`` and read ``ok`` while a 97%-dead table sat
#: right there: a silent fail-open through a legal config value. Both guards
#: state the intent; neither ordering clause alone is the contract.
_DB_HYGIENE_SQL = text(
    """
    WITH db_freeze AS (
        SELECT
            current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age,
            age(datfrozenxid)::bigint                            AS frozen_age
        FROM pg_database
        WHERE datname = current_database()
    ),
    worst AS (
        SELECT
            relname,
            n_live_tup,
            n_dead_tup,
            (n_dead_tup::float8 / NULLIF(n_live_tup + n_dead_tup, 0)) AS dead_ratio
        FROM pg_stat_user_tables
        WHERE n_dead_tup >= :min_dead
          AND (n_live_tup + n_dead_tup) > 0
        ORDER BY dead_ratio DESC NULLS LAST
        LIMIT 1
    )
    SELECT
        d.freeze_max_age,
        d.frozen_age,
        w.relname,
        w.n_live_tup,
        w.n_dead_tup,
        w.dead_ratio
    FROM db_freeze d
    LEFT JOIN worst w ON TRUE
    """,
)

#: Freeze-age warning tier: half of the server's ``autovacuum_freeze_max_age``
#: (200,000,000 by default → 100,000,000). Half the ceiling is reached only
#: after the freeze machinery has demonstrably stopped advancing
#: ``datfrozenxid`` for a long stretch, while still leaving the entire second
#: half as runway to act — and it is orders of magnitude above what a
#: vacuuming database shows, so it cannot fire on normal operation.
_DB_HYGIENE_FREEZE_WARN_FRACTION = 0.5

#: Wraparound-tier labels appended to the freeze-age detail.
_DB_HYGIENE_FREEZE_AT_CEILING = "AT/ABOVE autovacuum_freeze_max_age — wraparound protection must be forced now"
_DB_HYGIENE_FREEZE_WARN_TIER = "at or past the {warn_at:,} warning tier (50% of autovacuum_freeze_max_age)"


@dataclass(frozen=True)
class DbHygieneReading:
    """The two hygiene facts the database-hygiene probe read back.

    ``worst_table``/``n_live_tup``/``n_dead_tup``/``dead_ratio`` describe the
    worst table already over the dead-tuple floor (all ``None`` when no table
    is). Kept separate from the SQL row so the grading rule can be exercised
    directly at its threshold boundaries.
    """

    frozen_age: int
    freeze_max_age: int
    worst_table: str | None = None
    n_live_tup: int | None = None
    n_dead_tup: int | None = None
    dead_ratio: float | None = None


def _grade_db_hygiene(
    reading: DbHygieneReading,
    *,
    min_dead_tuples: int,
    dead_ratio_threshold: float,
) -> tuple[Literal["ok", "degraded"], str]:
    """Grade one hygiene reading against the two configured thresholds.

    Both thresholds are settings (``modulo_health_db_hygiene_min_dead_tuples``,
    ``modulo_health_db_hygiene_dead_ratio``) — see the reasoning on those
    fields. In short: a ratio is only acted on once the table also carries a
    material absolute number of dead rows (10,000 — a tiny table that is
    momentarily 90% dead is ordinary churn), and the ratio sits at 60%, 3x
    autovacuum's own 20% trigger (20-40% is normal operation, not bloat) but
    still 37 points below the 97% the incident reached.

    The function can ONLY return ``ok`` or ``degraded``: hygiene is a report
    about table maintenance, never about whether the deployment can serve
    work, so it must be structurally incapable of returning ``unavailable``
    and flipping /healthz/ready to 503.
    """
    over_floor = (
        reading.worst_table is not None
        and reading.n_dead_tup is not None
        and reading.dead_ratio is not None
        and reading.n_dead_tup >= min_dead_tuples
    )
    bloat = bool(over_floor and reading.dead_ratio is not None and reading.dead_ratio >= dead_ratio_threshold)

    if reading.worst_table is None:
        table_part = f"no table over the {min_dead_tuples:,}-dead floor"
    else:
        live = reading.n_live_tup or 0
        dead = reading.n_dead_tup or 0
        table_part = (
            f'worst table "{reading.worst_table}": {dead:,} dead of {live + dead:,} rows '
            f"({(reading.dead_ratio or 0.0) * 100:.1f}% dead)"
        )

    if bloat:
        parts = [
            (
                f"DEAD-TUPLE BLOAT: {table_part} at/above the "
                f"{dead_ratio_threshold * 100:.0f}% threshold (floor {min_dead_tuples:,} dead)"
            ),
        ]
    else:
        parts = [f"dead-tuples within thresholds ({table_part}; threshold {dead_ratio_threshold * 100:.0f}%)"]

    freeze_part = f"freeze age {reading.frozen_age:,}/{reading.freeze_max_age:,}"
    freeze_degraded = False
    # No "operator-disabled ceiling" branch: autovacuum_freeze_max_age is
    # server-start-only and Postgres rejects 0 outright ("FATAL: 0 is outside
    # the valid range for parameter \"autovacuum_freeze_max_age\" (100000 ..
    # 2000000000)"), so freeze_max_age <= 0 cannot reach here — and if a bad
    # reading somehow did, grading it at/above the ceiling is the fail-closed
    # answer (never a silent "clean").
    warn_at = int(reading.freeze_max_age * _DB_HYGIENE_FREEZE_WARN_FRACTION)
    if reading.frozen_age >= reading.freeze_max_age:
        freeze_degraded = True
        freeze_part += f" {_DB_HYGIENE_FREEZE_AT_CEILING}"
    elif reading.frozen_age >= warn_at:
        freeze_degraded = True
        freeze_part += " " + _DB_HYGIENE_FREEZE_WARN_TIER.format(warn_at=warn_at)
    parts.append(freeze_part)

    status: Literal["ok", "degraded"] = "ok"
    if bloat or freeze_degraded:
        status = "degraded"
    return status, "; ".join(parts)


async def _check_db_hygiene() -> CheckResult:
    """Database hygiene: worst-table dead-tuple ratio + freeze age (FAR-1445).

    Reports the two numbers the bloat incident had no surface for:

    1. the worst per-table dead-tuple ratio ``n_dead_tup / (n_live_tup +
       n_dead_tup)`` from ``pg_stat_user_tables``, naming the offending table,
       gated by BOTH configured thresholds (absolute dead-tuple floor, then
       ratio — see :func:`_grade_db_hygiene`);
    2. ``age(datfrozenxid)`` for this database against the server's own
       ``autovacuum_freeze_max_age``, degraded at half the ceiling.

    Placement: it rides /healthz/ready (a single pooled-engine read of
    in-memory statistics, 1s budget, concurrent with the sibling checks) so
    the health surface finally sees hygiene — but it reports ONLY ``ok`` or
    ``degraded``, never ``unavailable``. ``degraded`` leaves the endpoint at
    HTTP 200 (the aggregation 503s solely on ``unavailable``), which is what
    the Fly service check and every deploy gate key on; the finding reaches
    operators through the readiness body and the product's own in-app
    readiness alerting (``core/health_alerts.py``) instead of by taking the
    deployment out of rotation. The advisory/dead-sweep class the removed
    external uptime monitor used to scan is tracked by FAR-1571.

    FAR-1510 — an outcome that produced NO READING (the probe timed out, or
    raised) reports ``degraded`` together with ``advisory=True``. That result
    is a statement about the PROBE, not about hygiene: it stays visible in the
    readiness body, but ``evaluate_readiness`` excludes it from the aggregate
    gate, so it can no longer fire the ``health_readiness_alert`` email on its
    own. This is the exact misattribution seen on production — a 0.6-1.2s
    event-loop stall blew the 1s budget and the alert email claimed "database
    hygiene ... timed out" while live hygiene was clean (the stall itself is
    what the advisory ``event_loop_lag`` check reports). Only a COMPLETED
    reading graded over a threshold by :func:`_grade_db_hygiene` gates.
    """
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_db_hygiene_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> DbHygieneReading:
        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(
                _DB_HYGIENE_SQL,
                {"min_dead": settings.modulo_health_db_hygiene_min_dead_tuples},
            )
            row = result.mappings().first()
        if row is None:
            # Unreachable in practice (the db_freeze CTE always yields a row) —
            # fail loudly rather than grade an invented reading.
            raise RuntimeError("database-hygiene query returned no rows")
        return DbHygieneReading(
            frozen_age=int(row["frozen_age"]),
            freeze_max_age=int(row["freeze_max_age"]),
            worst_table=row["relname"],
            n_live_tup=int(row["n_live_tup"]) if row["n_live_tup"] is not None else None,
            n_dead_tup=int(row["n_dead_tup"]) if row["n_dead_tup"] is not None else None,
            dead_ratio=float(row["dead_ratio"]) if row["dead_ratio"] is not None else None,
        )

    try:
        reading = await asyncio.wait_for(_probe(), timeout=timeout)
        status, detail = _grade_db_hygiene(
            reading,
            min_dead_tuples=settings.modulo_health_db_hygiene_min_dead_tuples,
            dead_ratio_threshold=settings.modulo_health_db_hygiene_dead_ratio,
        )
        if status != "ok":
            _log.warning("health.db_hygiene %s", detail)
        return CheckResult(
            status=status,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail=detail,
        )
    except TimeoutError:
        # FAR-1510: a timeout is not a hygiene READING — it reports that the
        # probe did not finish inside its budget, which on production meant a
        # stalled event loop (see the advisory event_loop_lag check), not
        # bloat. Keep it visible as degraded (never "clean", never
        # unavailable) but advisory, so it cannot gate the aggregate or fire
        # the readiness email on its own.
        _log.warning("health._check_db_hygiene", exc_info=True)
        return CheckResult(
            status="degraded",
            advisory=True,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail=(
                f"database-hygiene probe did not complete within {timeout:g}s "
                "(likely an event-loop stall; see event_loop_lag) — hygiene not measured"
            ),
        )
    except Exception:
        # The check could not run — degraded, never "clean" (a crashed check
        # must not read as an ok one) and never unavailable: a failure to
        # INSPECT hygiene says nothing about the deployment's ability to
        # serve, and `database` already owns the reachability verdict.
        # Advisory for the same reason as the timeout above (FAR-1510): no
        # reading was taken, so it must not be presented or gated as one.
        _log.warning("health._check_db_hygiene", exc_info=True)
        return CheckResult(
            status="degraded",
            advisory=True,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail="database-hygiene check could not run (see logs)",
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
    """Probe the checkpointer schema through the shared engine pool.

    FAR-1426: probe via the pooled engine — the same connection path the
    ``database``/``migrations`` checks and every real query use. The
    previous implementation opened a brand-new raw ``asyncpg`` connection
    on every readiness request from a helper-built DSN; whenever the event
    loop stalled between TCP connect and the StartupPacket (the peer
    closes startup-idle connections after ~1s), that fresh connection was
    dropped and the check reported ``ConnectionDoesNotExistError`` as a
    checkpointer failure that no real checkpointer path experienced.
    """
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_checkpointer_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> tuple[Literal["ok", "degraded"], str]:
        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            try:
                await conn.execute(text("SELECT 1 FROM checkpoint_migrations LIMIT 1"))
            except Exception:
                _log.warning(_CODE_HEALTH_CHECK_CHECKPOINTER, exc_info=True)
                return "degraded", "checkpoint_migrations table not accessible"
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


#: Process-wide cache of the alembic head revisions.  The migration tree is
#: fixed for the lifetime of a process, so re-parsing it on every
#: ``/healthz/ready`` probe is pure overhead — and a synchronous re-parse is
#: exactly the event-loop stall FAR-1439 identified (see
#: ``_load_repo_heads``).  Only successful non-empty loads are cached so a
#: transient parse failure is retried on the next probe.
_REPO_HEADS_CACHE: set[str] | None = None


def _load_repo_heads(settings: Settings) -> set[str]:
    """Parse the migration tree for its head revisions (BLOCKING).

    Alembic's ``ScriptDirectory.get_heads()`` reads and executes every
    migration module to discover the heads — measured at ~0.8-1.4s for this
    repo's ~170 migrations, and longer on a shared-CPU container.  Running
    that inline inside the readiness probe froze the whole event loop for the
    parse's duration (FAR-1439: every sub-check reported the stall as its own
    latency, and probes timed out at the proxy).  Callers must therefore run
    this via ``asyncio.to_thread``; the process-wide cache makes every probe
    after the first a plain in-memory set read.

    The cache deliberately short-circuits BEFORE ``_resolve_alembic_ini``:
    a warm process must not touch the filesystem at all.

    Only successful non-empty loads are cached, mirroring
    ``migration_guard._load_repo_revisions`` — a transient failure returns
    uncached and is retried on the next probe.
    """
    global _REPO_HEADS_CACHE
    if _REPO_HEADS_CACHE is None:
        alembic_ini = _resolve_alembic_ini()
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
        alembic_cfg.set_main_option(
            "script_location",
            str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
        )
        script = ScriptDirectory.from_config(alembic_cfg)
        heads = set(script.get_heads())
        if heads:
            _REPO_HEADS_CACHE = heads
        return heads
    return set(_REPO_HEADS_CACHE)


async def _check_migrations() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_migrations_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> tuple[Literal["ok", "degraded"], str]:
        # FAR-1439: the alembic head parse is synchronous CPU + filesystem
        # work — run it in a worker thread so a cold (or cache-bypassed)
        # probe can never block the event loop.  Warm probes hit the
        # process-wide cache inside the thread for a few microseconds.
        heads = set(await asyncio.to_thread(_load_repo_heads, settings))

        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            applied = {row[0] for row in result.fetchall()}

        # FAR-872: check for repo-vs-DB migration divergence (DB has applied
        # revisions the repo does not ship).  Logged at ERROR in
        # migration_guard; surfaced here so it is visible without grepping
        # logs.  Also threaded (FAR-1439): the guard's first call parses the
        # migration tree a second time before its own process-wide cache
        # warms, and that parse must not run on the event loop either.
        divergence: DivergenceCheckResult | None = None
        divergence_check_failed = False
        try:
            divergence = await asyncio.to_thread(check_migration_divergence, applied)
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
    at its unavailable tier. A last_run_at older than
    ``_RECONCILE_STALE_SECONDS`` (180s — 3x the 60s cadence, matching the
    ``runner_health_probe`` precedent) reports "stale" (degraded) to alert
    operators while the app remains healthy — a single slow or missed tick
    must not read stale, and must not block bluegreen. A last_run_at older than
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

    FAR-1621: the per-org counters are included so an alert email can tell the
    cases apart at a glance — ``org_timeouts`` (the FAR-1525 30s per-org
    cut), ``orgs_deferred`` (orgs never started because only the reserved tick
    tail remained), ``org_lock_timeouts`` (FAR-1601 SQLSTATE 55P03 skip),
    ``org_pool_timeouts`` (system-engine pool checkout), ``org_connect_timeouts``
    (asyncpg's TCP connect bound during session acquisition) and
    ``org_statement_timeouts`` (FAR-1621 SQLSTATE 57014 statement bound) —
    five bounded-failure counters plus ``orgs_deferred``. Before them the
    detail ended at ``rows_deferred``, so the exact class of bounded failure
    was invisible to the alert.
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
        f"facts_deferred={stats.get('facts_deferred', 0)}, "
        f"rows_deferred={stats.get('rows_deferred', 0)}, "
        f"org_timeouts={stats.get('org_timeouts', 0)}, "
        f"orgs_deferred={stats.get('orgs_deferred', 0)}, "
        f"org_lock_timeouts={stats.get('org_lock_timeouts', 0)}, "
        f"org_pool_timeouts={stats.get('org_pool_timeouts', 0)}, "
        f"org_connect_timeouts={stats.get('org_connect_timeouts', 0)}, "
        f"org_statement_timeouts={stats.get('org_statement_timeouts', 0)}"
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
    (schema-freshness CI gate) and reworking every status consumer — the
    aggregate plus the in-app readiness alerting — both outside this change's
    footprint; a non-ok status here would flag engine-less deployments
    permanently, the exact problem this skip fixes. Configured deployments
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


async def _check_decision_record_reconcile() -> CheckResult:
    """ADVISORY — last decision_record_reconcile outcome (never gates readiness).

    The FAR-1108 chunk-8b read-only decision-record reconciliation sweep
    (``saq_worker.decision_record_reconcile``) runs hourly in the SYSTEM WORKER
    process and persists its outcome (``scanned`` + anomaly counts +
    ``last_run_at``) to ``saq:cron:stats:decision_record_reconcile``. A dead
    sweep means the decision-record corruption-detection surface has silently
    stopped; the sweep itself never mutates a record, so this is alert-only —
    a missing or >3h-stale key reports "degraded" while the app stays healthy.
    Fail-open on Redis read errors.
    """
    return await _check_sweep_stats_advisory(
        _DECISION_RECORD_RECONCILE_STATS_KEY,
        _DECISION_RECORD_RECONCILE_STALE_SECONDS,
        "scanned",
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


# FAR-1439: event-loop stall visibility.  A synchronous call on the
# readiness path — the bug class that froze the loop for ~2.4s per probe and
# produced the ~40% 504s — inflates the latency of EVERY sub-check at once,
# which names nothing.  A cooperative sampler ticks every
# ``_LOOP_LAG_TICK_SECONDS`` for the duration of a probe and records the
# worst scheduling delay (oversleep) it observes; the result is surfaced as
# the advisory ``event_loop_lag`` check so a regression is visible directly
# in the readiness payload.
_LOOP_LAG_TICK_SECONDS = 0.01
#: A tick that overslept by at least this much is a STALLED loop, not
#: scheduling jitter: a healthy loop oversleeps a 10ms tick by single-digit
#: milliseconds, while the FAR-1439 stall measured ~2400ms.
_LOOP_LAG_DEGRADED_MS = 250.0


async def _track_event_loop_lag(state: dict[str, float], stop: asyncio.Event) -> None:
    """Record the worst event-loop scheduling delay until ``stop`` is set.

    Runs concurrently with the readiness gather.  Each tick sleeps
    ``_LOOP_LAG_TICK_SECONDS`` and records how late the loop actually woke
    the task; a synchronous call anywhere on the loop shows up as one
    oversized tick.  ``state`` is shared with the caller so the recorded
    worst value survives task completion and can be read after the fact.
    """
    while not stop.is_set():
        started = time.monotonic()
        await asyncio.sleep(_LOOP_LAG_TICK_SECONDS)
        lag_ms = (time.monotonic() - started - _LOOP_LAG_TICK_SECONDS) * 1000.0
        state["worst_ms"] = max(state["worst_ms"], lag_ms)


@router.get("/healthz")
@handle_db_errors("health.liveness")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


async def evaluate_readiness() -> ReadinessResponse:
    """Evaluate the full readiness picture — the ONE implementation of the checks.

    Shared by two callers (FAR-1446):
    * the ``/healthz/ready`` HTTP route (wrapped by ``handle_db_errors``, which
      adds the 503 status code from this result's ``status``);
    * the ``health_readiness_alert`` system cron
      (``modulo.core.health_alerts``), which emails the operator when this
      evaluation transitions into a degraded/unavailable state.

    The cron imports this lazily (a ``core`` module must not import ``api`` at
    module-import time — ``api.routes.health`` imports ``core.cron_helpers``).
    Never reimplement these sub-checks elsewhere: the alert must describe
    exactly what readiness reports.

    FAR-1439: sample the loop for the duration of the probe.  Stopped with
    a plain await — never a cancel — so a tick that became due DURING a
    stall still runs and records it before the handler reads the state.
    """
    lag_state: dict[str, float] = {"worst_ms": 0.0}
    lag_stop = asyncio.Event()
    lag_task = asyncio.create_task(_track_event_loop_lag(lag_state, lag_stop))
    try:
        (
            db_check,
            redis_check,
            cp_check,
            mig_check,
            hyg_check,
            saq_check,
            cron_check,
            dr_check,
            srr_check,
            sr_check,
            hps_check,
            rwr_check,
            rms_check,
            rhp_check,
            drr_check,
        ) = await asyncio.gather(
            _check_database(),
            _check_redis(),
            _check_checkpointer(),
            _check_migrations(),
            _check_db_hygiene(),
            _check_saq_workers(),
            _check_system_crons(),
            _check_dispatcher_reconcile(),
            _check_stale_run_recovery(),
            _check_slot_reconciliation(),
            _check_hitl_park_sweep(),
            _check_runner_workspace_reconcile(),
            _check_runner_marker_sweep(),
            _check_runner_health_probe(),
            _check_decision_record_reconcile(),
        )
    finally:
        lag_stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await lag_task
    bg_check = _check_break_glass()

    # FAR-1439: worst event-loop scheduling delay observed while the probe
    # ran.  ADVISORY only — excluded from the aggregate below so a transient
    # stall alerts without gating bluegreen — but it names the failure mode
    # that previously only showed up as every sub-check reporting ~2.4s.
    worst_lag_ms = lag_state["worst_ms"]
    lag_degraded = worst_lag_ms >= _LOOP_LAG_DEGRADED_MS
    if lag_degraded:
        _log.warning(
            "health.readiness event-loop stall detected (FAR-1439)",
            extra={"worst_lag_ms": worst_lag_ms},
        )
    lag_check = CheckResult(
        status="degraded" if lag_degraded else "ok",
        latency_ms=round(worst_lag_ms, 1),
        detail=(
            f"EVENT-LOOP STALL: loop delayed up to {worst_lag_ms:.0f}ms during this probe "
            "(a synchronous call blocked the event loop — see FAR-1439)"
            if lag_degraded
            else f"event loop responsive (worst scheduling delay {worst_lag_ms:.0f}ms during probe)"
        ),
    )

    checks: dict[str, CheckResult] = {
        "database": db_check,
        "redis": redis_check,
        "checkpointer": cp_check,
        "migrations": mig_check,
        # FAR-1445: database hygiene (worst-table dead-tuple ratio + freeze
        # age). Gating at the DEGRADED tier only: the check can never return
        # "unavailable" (see _check_db_hygiene), so a bloat / high-age report
        # degrades the overall status while the endpoint stays HTTP 200 — the
        # Fly service check and every deploy gate key on the status CODE and
        # tolerate degraded, and the product's own in-app readiness alerting
        # (core/health_alerts.py) reports it through the aggregate it keys on;
        # the ADVISORY sub-checks never reach that aggregate (FAR-1571).
        # FAR-1510: a probe that did NOT complete (timeout/crash) comes back
        # advisory=True, is still listed here so the body shows it, and is
        # excluded from the aggregate below.
        "db_hygiene": hyg_check,
        "saq_workers": saq_check,
        "system_crons": cron_check,
        # ADVISORY only — excluded from the aggregate so a break-glass config
        # warning never degrades readiness (plan §3 watchdog reduction).
        "break_glass": bg_check,
        # FAR-1439 ADVISORY only — excluded from the aggregate (never gates
        # readiness): the worst event-loop scheduling delay seen during this
        # probe.  A value >= _LOOP_LAG_DEGRADED_MS means a synchronous call
        # blocked the loop mid-probe; it alerts here without blocking a
        # deploy.
        "event_loop_lag": lag_check,
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
        # ADVISORY only — excluded from the aggregate (never gates readiness).
        # FAR-1108 chunk 8b: a dead decision-record reconciliation sweep means
        # the decision-record corruption-detection surface has silently
        # stopped; the sweep mutates nothing, so it stays alert-only.
        "decision_record_reconcile": drr_check,
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
    # FAR-1445 / FAR-1510: hygiene gates only when the probe actually
    # MEASURED it. A completed reading graded over a threshold (the normal
    # _grade_db_hygiene path) contributes exactly as before — degraded, never
    # unavailable, so it can move the aggregate to degraded but can never 503
    # the endpoint on its own. A probe that timed out or crashed returns
    # advisory=True (see _check_db_hygiene): that result says "hygiene not
    # measured", which is a statement about the probe — historically an
    # event-loop stall — not about the database, so it is reported in the
    # body but excluded here instead of flipping the aggregate (and the
    # health_readiness_alert email) to degraded.
    if not hyg_check.advisory:
        statuses.append(hyg_check.status)
    # FAR-199: dispatcher_reconcile gates readiness ONLY at its "unavailable"
    # tier — reconcile stale past _RECONCILE_UNAVAILABLE_SECONDS means the
    # system worker's cron is silently dead (a wedged worker fleet that would
    # silently accumulate executor_stalled / never_dispatched runs), so
    # bluegreen must not cut over. Its "degraded" tier (stale past
    # _RECONCILE_STALE_SECONDS — 3x the 60s cadence, so a single slow or
    # missed tick never reads stale) stays advisory and is deliberately
    # excluded from the degraded aggregation — short staleness must never
    # flip readiness.
    if "unavailable" in statuses or dr_check.status == "unavailable":
        overall: Literal["ok", "degraded", "unavailable"] = "unavailable"
        unavailable_checks = sorted(name for name, check in checks.items() if check.status == "unavailable")
        degraded_checks = sorted(name for name, check in checks.items() if check.status == "degraded")
        log_service_unavailable(
            "readiness_check_unavailable",
            route="health.readiness",
            detail=f"unavailable={unavailable_checks} degraded={degraded_checks}",
        )
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


@router.get("/healthz/ready")
@handle_db_errors("health.readiness")
async def readiness(response: Response) -> ReadinessResponse:
    # Thin HTTP wrapper over evaluate_readiness (FAR-1446): the degradation
    # semantics live in the shared implementation (also called by the
    # health_readiness_alert system cron); this wrapper only maps the
    # aggregate status onto the HTTP status code the Fly service check and
    # every deploy gate key on. Deliberately NO docstring — FastAPI would
    # publish it as the OpenAPI operation description and stale
    # frontend/src/lib/api/schema.ts.
    result = await evaluate_readiness()
    if result.status == "unavailable":
        response.status_code = 503
    return result
