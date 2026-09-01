"""FAR-190 — ongoing-trigger no-delivery streak engine.

The engine walks an ongoing trigger's terminal run-classification records
(FAR-189) and auto-deactivates the trigger after N consecutive no-delivery
runs, then notifies. It runs as a system sweep (``enforce_no_delivery_streaks``,
wired into ``cron_helpers.dispatcher_reconcile`` every 60s) — NEVER inline in
terminalization. The streak is bounded by ``GREATEST(last_delivery_at,
streak_epoch)`` and the deactivation is a guarded atomic UPDATE (count folded
into the WHERE), so a re-enabled trigger or a stale tick can never be hit.

The engine was extracted from ``cron_helpers`` (which had grown to ~4400 lines)
so the scheduler helpers and the streak engine evolve independently. It imports
the shared scheduler helpers (``_set_rls_org`` / ``_open_factory`` /
``_log_ongoing_event`` / ``_ingest_saq_error`` / ``_get_engine`` /
``_clear_ongoing_failure``) lazily from ``cron_helpers`` at call time — never at
module import time — which keeps the ``cron_helpers -> trigger_streak`` wiring
free of a circular import regardless of which module is imported first.

Every ``active=True`` transition of an ongoing trigger MUST re-anchor
``streak_epoch`` (migration backfill anchors at deploy; this anchors at
create + re-enable) — see ``anchor_trigger_streak_epoch``, routed through all
active-write sites (triggers.py update/toggle/restore, mcp_server, the
cost_controller circuit-breaker reset). There is no un-epoch'd activation
path: a row whose epoch is NULL (rolling-deploy skew) COALESCEs to now() and
therefore can never be deactivated until re-anchored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.pipeline_engine.classify import RunClassificationValue
from modulo.db.models.run import TERMINAL_STATUSES

_log = logging.getLogger(__name__)


def _ch() -> Any:
    """Lazy import of the shared scheduler helpers (breaks the import cycle)."""
    from modulo.core import cron_helpers

    return cron_helpers


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# FAR-190 — ongoing-trigger no-delivery streak engine.
#
# An ongoing trigger that produces N consecutive no-delivery terminal runs
# (run_classification value 'no_delivery' — including empty-backlog completes
# and infra/sandbox failures elevated to failed, PO decision) is auto-deactivated
# so the operator investigates the quiet stretch. The streak walks newest ->
# oldest terminal runs and stops at the first delivered OR excluded run (an
# unclassified run — a missing/broken classification record — ALSO stops the
# walk fail-closed, so deactivation can never ride on uncertain evidence). The
# walk boundary is ``GREATEST(last_delivery_at, streak_epoch)``.
#
# Config (per-trigger ``config_json``): ``max_no_delivery_streak`` (threshold),
# falling back to the legacy ``max_consecutive_failures`` key for one release;
# ``no_delivery_min_window_hours`` (wall-clock window). Product default is N=5
# WITH a 24h minimum wall-clock window so other customers' quiet stretches never
# self-deactivate (the boundary must be at least the window old before the
# streak can fire); a deployment overrides the window to 0 via
# MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS so a fast-moving repo's quiet stretch
# still stops the pool.
ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT = 5
ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT = 24
# Per-org per-hour deactivation cap (mass-cascade guard): a single org cannot
# have more than this many auto-deactivations per rolling hour — a burst is
# deferred to the next tick rather than cascading. The per-trigger atomic
# UPDATE (count folded into the WHERE) remains the hard correctness bound; this
# cap is the soft operational throttle.
ONGOING_STREAK_DEACTIVATE_MAX_PER_ORG_PER_HOUR = 10
# Mass-cascade alert: when an org has deactivated >= this many triggers in the
# last 24h, a critical alert fires (infra outage guard) — once per window.
ONGOING_STREAK_MASS_CASCADE_ALERT_THRESHOLD = 5
ONGOING_STREAK_MASS_CASCADE_ALERT_WINDOW_HOURS = 24
# Kill switch for the deactivate+notify side-effect. Classification ALWAYS
# persists (the reconcile sweep is independent); this only gates the sweep's
# deactivation + notification. Documented constant default, overridable at
# runtime via MODULO_STREAK_DEACTIVATE_KILL_SWITCH.
STREAK_DEACTIVATE_ENABLED_DEFAULT = True

# Audit event types for the streak engine lifecycle records (append-only).
STREAK_DEACTIVATION_EVENT_TYPE = "ongoing_trigger.auto_deactivated"
STREAK_NOTIFY_FAILED_EVENT_TYPE = "ongoing_trigger.deactivation_notify_failed"
STREAK_MASS_CASCADE_EVENT_TYPE = "ongoing_trigger.mass_cascade_alert"

# ``deactivated_by`` sentinel values carried in the deactivation audit payload
# (append-only, never renamed). The on-demand reader maps the payload field to
# the surfaced ``deactivated_reason``.
STREAK_DEACTIVATED_BY_STREAK = "no_delivery_streak"
STREAK_DEACTIVATED_BY_CONFIG_FAILURE = "config_failure"

# Redis markers: per-org pending deactivation-notification retry set (a failed
# dispatch is retried on the next scheduler tick; the member carries the full
# sanitised payload) + the once-per-window mass-cascade alert marker.
_STREAK_NOTIFY_PENDING_PREFIX = "saq:streak:notify_pending"
_STREAK_MASS_CASCADE_ALERT_PREFIX = "saq:streak:mass_cascade_alerted"
_STREAK_PENDING_MARKER_TTL = 7 * 24 * 3600  # 7d — long enough to retry across an outage

# Notification payload reason allow-list — identifiers/titles + these reason
# fields only, never tokens or raw output (FAR-190 payload-sanitisation rule).
_STREAK_PAYLOAD_ALLOWED_REASONS = frozenset({"no_work", "needs_human", "source_error", "parse_error", "no_delivery"})

# Health-critical cron bounds. The sweep runs inside dispatcher_reconcile (120s
# SAQ timeout); the deactivation UPDATE is the durable truth and the
# notification must never be able to kill the tick.
_STREAK_SWEEP_BUDGET_SECONDS = 45.0  # per-tick wall-clock budget for the whole sweep
_STREAK_NOTIFY_TIMEOUT_SECONDS = 10.0  # per-dispatch bound (asyncio.wait_for)
_STREAK_NOTIFY_MAX_PER_TICK = 10  # max inline notify attempts per tick; the rest defer to the pending-retry path
_STREAK_RETRY_COOLDOWN_SECONDS = 15 * 60  # retry a pending member at most once per 15 min

# Terminal-status IN-list derived from the single source of truth
# (``modulo.db.models.run.TERMINAL_STATUSES``) so a new terminal status can
# never silently change the walk. Assembled into the SQL constants via
# ``__STATUSES__`` placeholders + ``str.replace`` (bandit S608 flags
# f-string/format SQL; these are static frozensets, never user input).
_STREAK_STATUSES_SQL = ",".join(f"'{s}'" for s in sorted(TERMINAL_STATUSES))
# Classification values derived from the single source of truth
# (``RunClassificationValue``) so a new value can never silently change the
# walk's countable / stop predicates.
_STREAK_NO_DELIVERY_VALUE = RunClassificationValue.no_delivery.value
_STREAK_DELIVERED_VALUE = RunClassificationValue.delivered.value
_STREAK_STOP_VALUES_SQL = ",".join(
    f"'{v.value}'"
    for v in (RunClassificationValue.delivered, RunClassificationValue.excluded, RunClassificationValue.unclassified)
)

# The streak boundary: GREATEST(last delivered run's completed_at, streak_epoch).
# ``last_delivery_at`` is derived from the classification log (single source of
# truth). The epoch is read from the live trigger row inside a self-contained
# scalar subquery — NOT a bare ``triggers.`` column reference — so this fragment
# is portable to a runs-only FROM (the reason query) AND stays atomic inside the
# guarded UPDATE (the row lock serialises a concurrent re-anchor, so the count
# always sees the post-re-anchor epoch). ``COALESCE((subquery), now())`` fails
# SAFE on a NULL epoch (rolling-deploy skew): the boundary becomes "now", no run
# counts, and the trigger cannot deactivate until the row is re-anchored. All
# tenant/identity references are bind parameters (``:oid`` / ``:tid``), so the
# raw text() fragments are never cross-tenant.
_STREAK_BOUNDARY_SQL = (
    "GREATEST("  # nosec B608 - static constant fragments, never user input
    "(SELECT max(r2.completed_at) FROM runs r2 "
    " WHERE r2.organisation_id = :oid "
    "   AND r2.trigger_id = :tid "
    "   AND r2.completed_at IS NOT NULL "
    "   AND r2.run_classification ->> 'value' = '__DELIVERED__'),"
    "COALESCE((SELECT tr.streak_epoch FROM triggers tr "
    " WHERE tr.id = :tid AND tr.organisation_id = :oid), now()))"
).replace("__DELIVERED__", _STREAK_DELIVERED_VALUE)

# The consecutive no-delivery streak: countable terminal runs (value
# 'no_delivery', at/after the boundary) with NO newer terminal run that is
# delivered/excluded/unclassified OR that has no classification record at all
# (fail-closed: uncertain evidence stops the walk). The ``r3.id > r.id``
# tie-break makes equal-completed_at runs deterministic (equal-completed_at
# ordering — the count is order-independent but the STOP predicate needs a total
# order). Terminal-only predicates keep in-flight runs drained (never counted,
# never breaking the walk — deactivation never cancels).
_STREAK_COUNT_SQL = (
    (
        "(SELECT count(*) FROM ("  # nosec B608 - static constant fragments, never user input
        "SELECT r.id FROM runs r "
        "WHERE r.organisation_id = :oid "
        "  AND r.trigger_id = :tid "
        "  AND r.status IN (__STATUSES__) "
        "  AND r.completed_at IS NOT NULL "
        "  AND r.completed_at >= __BOUNDARY__ "
        "  AND r.run_classification ->> 'value' = '__NO_DELIVERY__' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM runs r3 "
        "    WHERE r3.organisation_id = r.organisation_id "
        "      AND r3.trigger_id = r.trigger_id "
        "      AND r3.status IN (__STATUSES__) "
        "      AND r3.completed_at IS NOT NULL "
        "      AND (r3.completed_at > r.completed_at "
        "           OR (r3.completed_at = r.completed_at AND r3.id > r.id)) "
        "      AND (r3.run_classification IS NULL "
        "           OR r3.run_classification ->> 'value' IN (__STOP__))"
        ") ) streak_count)"
    )
    .replace("__STATUSES__", _STREAK_STATUSES_SQL)
    .replace("__BOUNDARY__", _STREAK_BOUNDARY_SQL)
    .replace("__NO_DELIVERY__", _STREAK_NO_DELIVERY_VALUE)
    .replace("__STOP__", _STREAK_STOP_VALUES_SQL)
)

# Guarded atomic deactivation (FAR-190 spec item 7): the streak is computed
# INSIDE the UPDATE's WHERE from the live trigger row (``active`` +
# ``streak_epoch`` via the scalar-subquery boundary) and the classification log,
# so a re-enabled trigger (active=false already, or epoch re-anchored) and a
# stale tick can never be hit — concurrent ticks produce one rowcount=1 and the
# second is a no-op. The boundary must also be at least ``window_cutoff`` old
# (the 24h wall-clock window, 0 for a deployment — a cutoff of "now" is trivially
# satisfied). ``RETURNING`` carries the streak value (same correlated subquery)
# for the audit record, so no second walk is needed. The explicit
# ``organisation_id = :oid`` predicate keeps the raw text() statement scoped to
# the org (raw text bypasses the non-Postgres tenant-filter listener).
_NO_DELIVERY_DEACTIVATE_SQL = (
    (
        "UPDATE triggers SET active = false "  # nosec B608 - static constant fragments, never user input
        "WHERE id = :tid "
        "  AND organisation_id = :oid "
        "  AND active "
        "  AND __STREAK_COUNT__ >= :threshold "
        "  AND __BOUNDARY__ <= :window_cutoff "
        "RETURNING id, pipeline_id, organisation_id, config_json, __STREAK_COUNT__ AS streak"
    )
    .replace("__STREAK_COUNT__", _STREAK_COUNT_SQL)
    .replace("__BOUNDARY__", _STREAK_BOUNDARY_SQL)
)

# Newest countable no-delivery run's classification reason — the audit + notify
# reason for a deactivation. Mirrors the count predicate (boundary + stop) so it
# returns a run actually inside the streak. Uses the SAME interpolated boundary
# fragment (which carries its own scalar-subquery FROM, so it is valid in this
# runs-only query — the reason query has no ``triggers`` relation in scope).
_STREAK_NEWEST_REASON_SQL = (
    (
        "SELECT r.run_classification ->> 'reason' AS reason "  # nosec B608 - static constant fragments, never user input
        "FROM runs r "
        "WHERE r.organisation_id = :oid AND r.trigger_id = :tid "
        "  AND r.status IN (__STATUSES__) "
        "  AND r.completed_at IS NOT NULL "
        "  AND r.completed_at >= __BOUNDARY__ "
        "  AND r.run_classification ->> 'value' = '__NO_DELIVERY__' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM runs r3 "
        "    WHERE r3.organisation_id = r.organisation_id "
        "      AND r3.trigger_id = r.trigger_id "
        "      AND r3.status IN (__STATUSES__) "
        "      AND r3.completed_at IS NOT NULL "
        "      AND (r3.completed_at > r.completed_at "
        "           OR (r3.completed_at = r.completed_at AND r3.id > r.id)) "
        "      AND (r3.run_classification IS NULL "
        "           OR r3.run_classification ->> 'value' IN (__STOP__))"
        ") "
        "ORDER BY r.completed_at DESC, r.id DESC LIMIT 1"
    )
    .replace("__STATUSES__", _STREAK_STATUSES_SQL)
    .replace("__BOUNDARY__", _STREAK_BOUNDARY_SQL)
    .replace("__NO_DELIVERY__", _STREAK_NO_DELIVERY_VALUE)
    .replace("__STOP__", _STREAK_STOP_VALUES_SQL)
)


# FAR-191 — read-only on-demand streak read. The SAME walk constant the sweep
# uses, wrapped in a bare SELECT so the current streak is read WITHOUT the
# deactivation UPDATE's active/threshold/window guards. Single source of truth
# for "what IS the current streak" — the on-demand read and the deactivation
# sweep can never disagree. Used only for reads, never for a write.
_STREAK_STATUS_COUNT_SQL = (
    "SELECT __STREAK_COUNT__ AS streak"  # nosec B608 - static constant fragments, never user input
).replace("__STREAK_COUNT__", _STREAK_COUNT_SQL)


# ---------------------------------------------------------------------------
# Activation anchor + re-enable helpers
# ---------------------------------------------------------------------------


async def anchor_trigger_streak_epoch(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    """FAR-190 shared activation anchor — reset a trigger's no-delivery streak
    boundary on any active=True transition (create / update / toggle / restore /
    re-enable). UPDATE-based and idempotent; matches ONLY rows currently active
    and not soft-deleted, so a half-applied transition can never be epoch-anchored
    in the inactive state and a re-enabled trigger's streak restarts from its
    re-enable moment. Call INSIDE the write transaction after ``active = True``
    is set — the epoch must commit atomically with the flip so a concurrent
    sweep tick can never observe active=True with a stale epoch.
    """
    from sqlalchemy import update

    from modulo.db.models.trigger import Trigger

    await session.execute(
        update(Trigger)
        .where(Trigger.id == trigger_id, Trigger.active.is_(True), Trigger.deleted_at.is_(None))
        .values(streak_epoch=now or datetime.now(UTC))
    )


async def clear_trigger_streak_after_reenable(trigger_id: uuid.UUID) -> None:
    """Post-commit re-enable side-effect (FAR-190): clear the FAR-158
    config-failure Redis counter. MUST be called only after the trigger's
    active=True transaction committed (over-clearing is safe, under-clearing is
    not — a stale counter would otherwise re-deactivate a re-enabled trigger on
    its next failure). Best-effort and NEVER raises: a missing/empty REDIS_URL
    or any Redis failure leaves a stale counter that self-heals on the next
    successful top-up.
    """
    ch = _ch()
    try:
        settings = ch.get_settings()
        if not settings.redis_url:
            return
        redis_client = AsyncRedis.from_url(settings.redis_url, socket_connect_timeout=5)
        try:
            await ch._clear_ongoing_failure(redis_client, trigger_id)
        finally:
            with ch._suppress_aclose():
                await redis_client.aclose()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.clear_streak_after_reenable failed trigger=%s", trigger_id)


def _streak_deactivate_enabled() -> bool:
    """Kill switch for the deactivate+notify side-effect (FAR-190 item 9).

    Classification ALWAYS persists (the reconcile sweep is independent); this
    only gates the sweep's deactivation + notification. Documented constant
    default (``STREAK_DEACTIVATE_ENABLED_DEFAULT``) overridable at runtime via
    the ``MODULO_STREAK_DEACTIVATE_KILL_SWITCH`` env var ('0'/'false'/'off'/'no'
    disables).
    """
    raw = os.environ.get("MODULO_STREAK_DEACTIVATE_KILL_SWITCH")
    if raw is None:
        return STREAK_DEACTIVATE_ENABLED_DEFAULT
    return raw.strip().lower() not in ("0", "false", "off", "no")


def _streak_min_window_hours_default() -> int:
    """Product default minimum wall-clock window before a no-delivery streak
    fires (24h — a quiet stretch must not self-deactivate within the first day
    after a delivery). A deployment overrides this to 0 via
    ``MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS`` so a fast-moving repo's quiet
    stretch still stops the pool. Per-trigger ``no_delivery_min_window_hours``
    config overrides both.
    """
    raw = os.environ.get("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS")
    if raw is None:
        return ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT


def _streak_config(config: dict[str, Any] | None) -> tuple[int, int]:
    """Resolve (threshold, min_window_hours) from a trigger's ``config_json``.

    Threshold: ``max_no_delivery_streak``, falling back to the legacy
    ``max_consecutive_failures`` key (read for one release) — else a deployment
    default (5). Window: per-trigger ``no_delivery_min_window_hours``, else the
    env default (24h product; 0 for a deployment). Only genuine ``int`` values are
    accepted — a boolean (``int(True) == 1``) or a float is rejected so a
    mis-typed config can never fire instantly or silently truncate. Invalid
    values fall back to the defaults — a mis-typed config can never disable the
    guard or fire instantly.
    """
    cfg = config or {}
    raw_threshold = cfg.get("max_no_delivery_streak")
    if raw_threshold is None:
        raw_threshold = cfg.get("max_consecutive_failures")  # legacy key fallback
    if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, int):
        parsed: int | None = None
    else:
        parsed = raw_threshold
    threshold = (
        # A missing, non-int, zero, or negative threshold is invalid — fall
        # back to the default (never fire instantly on a mis-typed config).
        ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT if parsed is None or parsed < 1 else parsed
    )
    raw_window = cfg.get("no_delivery_min_window_hours")
    if raw_window is None or isinstance(raw_window, bool) or not isinstance(raw_window, int):
        window = _streak_min_window_hours_default()
    else:
        window = max(0, raw_window)
    return threshold, window


# ---------------------------------------------------------------------------
# Read-only streak status (FAR-191) — on-demand read for the API surface
# ---------------------------------------------------------------------------


def _resolve_streak_threshold(trigger: Any, config_threshold: int | None) -> int:
    """Resolve the threshold: caller-supplied wins, else the trigger's config."""
    if config_threshold is not None:
        return int(config_threshold)
    threshold, _ = _streak_config(trigger.config_json)
    return threshold


async def _read_streak_count(session: AsyncSession, org_id: uuid.UUID, trigger_id: uuid.UUID) -> int:
    """Read the current no-delivery streak (the same walk the sweep uses)."""
    result = await session.execute(
        text(_STREAK_STATUS_COUNT_SQL),
        {"oid": str(org_id), "tid": str(trigger_id)},
    )
    return int(result.scalar_one() or 0)


async def _read_streak_outcomes(
    session: AsyncSession, org_id: uuid.UUID, trigger_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Read the trigger's last terminal-classified runs (newest first, <=5)."""
    from modulo.db.models.run import Run

    rows = (
        await session.execute(
            select(Run.id, Run.run_classification, Run.completed_at)
            .where(
                Run.organisation_id == org_id,
                Run.trigger_id == trigger_id,
                Run.completed_at.is_not(None),
                Run.completed_at >= text(_STREAK_BOUNDARY_SQL),
            )
            .order_by(Run.completed_at.desc(), Run.id.desc())
            .limit(5),
            {"oid": str(org_id), "tid": str(trigger_id)},
        )
    ).all()
    last_outcomes: list[dict[str, Any]] = []
    for run_id, classification, completed_at in rows:
        if not isinstance(classification, dict):
            continue
        last_outcomes.append(
            {
                "run_id": str(run_id),
                "classification": classification.get("value"),
                "reason": classification.get("reason"),
                "completed_at": completed_at.isoformat() if completed_at is not None else None,
            }
        )
    return last_outcomes


async def _read_streak_deactivation_reason(
    session: AsyncSession,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    trigger: Any,
) -> str | None:
    """Read the newest auto-deactivation reason SINCE the last active=True."""
    from modulo.db.models.audit_event import AuditEvent

    streak_epoch = getattr(trigger, "streak_epoch", None)
    epoch_cutoff = streak_epoch if streak_epoch is not None else datetime.now(UTC)
    audit_row = (
        await session.execute(
            select(AuditEvent.payload_json)
            .where(
                AuditEvent.organisation_id == org_id,
                AuditEvent.resource_type == "trigger",
                AuditEvent.resource_id == trigger_id,
                AuditEvent.event_type == STREAK_DEACTIVATION_EVENT_TYPE,
                AuditEvent.created_at >= epoch_cutoff,
            )
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
    ).first()
    if audit_row is None:
        return None
    payload = audit_row[0] or {}
    deactivated_by = payload.get("deactivated_by") if isinstance(payload, dict) else None
    return (
        STREAK_DEACTIVATED_BY_CONFIG_FAILURE
        if deactivated_by == STREAK_DEACTIVATED_BY_CONFIG_FAILURE
        else STREAK_DEACTIVATED_BY_STREAK
    )


async def get_trigger_streak_status(
    session: AsyncSession,
    trigger: Any,
    config_threshold: int | None = None,
) -> dict[str, Any]:
    """FAR-191 — READ-ONLY on-demand streak status for ONE trigger.

    Computes the current no-delivery streak / threshold / state for the trigger
    detail + list serializers so the operator can see how close an ongoing
    trigger is to auto-deactivation, whether it HAS been auto-deactivated (and
    why), and the last-N outcome summary. Reuses the sweep's SQL walk constants
    (``_STREAK_COUNT_SQL`` via ``_STREAK_STATUS_COUNT_SQL``) so the on-demand
    read and the deactivation sweep count the SAME streak.

    NEVER deactivates, NEVER writes, NEVER triggers the mass-cascade/cap/notify
    machinery. Best-effort and NEVER raises: any failure is swallowed and logged
    and the caller receives ``{enabled: False, state: 'unconfigured'}`` so the
    API degrades gracefully instead of 500ing the trigger list.

    ``enabled`` means the streak engine is active for this trigger (ongoing type
    + the deactivate+notify kill switch on). It deliberately does NOT mean
    ``active=True``: an auto-deactivated trigger still reports its deactivation
    state and the streak that caused it.

    Returns::

        {
            "enabled": bool,                 # engine active for this trigger
            "streak": int,                   # current consecutive no-delivery count
            "threshold": int,                # configured max_no_delivery_streak
            "state": "ok" | "deactivated" | "unconfigured",
            "deactivated_reason": "no_delivery_streak" | "config_failure" | None,
            "last_outcomes": [{run_id, classification, reason, completed_at}],  # <=5, newest first
        }

    The reader degrades PER SUB-READ, never as one big try/except: a failure
    reading the streak count collapses to the bare unconfigured base (nothing
    computable), but a failure reading only the outcomes or only the audit
    reason keeps everything already computed — a deactivated trigger must never
    be reported as 'unconfigured' just because the reason read hiccuped.
    """
    base = _streak_status_base()
    if getattr(trigger, "trigger_type", None) != "ongoing":
        return base
    org_id = getattr(trigger, "organisation_id", None)
    trigger_id = getattr(trigger, "id", None)
    if org_id is None or trigger_id is None:
        return base

    # Threshold resolution is part of the reader's self-contained contract: a
    # caller-supplied ``config_threshold`` (resolved by the serializer) wins,
    # otherwise resolve from the trigger's own config_json.
    threshold, degraded = _resolve_streak_status_threshold(trigger, config_threshold)
    if degraded is not None:
        return degraded

    # 1) The current streak — the SAME walk the sweep uses, read without the
    # deactivation guards. ``text()`` raw SQL bypasses the tenant-filter
    # listener, so the walk's ``organisation_id = :oid`` predicates scope it
    # (the count fragment carries its own org/trigger bind params). A failure
    # here degrades to the bare base (no streak computable).
    streak, ok = await _read_streak_status_count(session, org_id, trigger_id)
    if not ok:
        return base

    # 2) Last-N outcome summary — the trigger's own terminal classified runs
    # newest first, bounded by the SAME streak boundary the walk uses (so the
    # panel can never contradict the badge after a re-enable re-anchors the
    # epoch). In-flight runs (completed_at NULL) are excluded. A failure here
    # keeps the computed streak + threshold and degrades the outcomes to [].
    last_outcomes = await _read_streak_status_outcomes(session, org_id, trigger_id)

    # 3) Deactivation reason — an auto-deactivated trigger (active=False) whose
    # NEWEST auto-deactivation audit record SINCE the last activation says
    # config_failure vs the no-delivery-streak default. A manually-paused
    # trigger has no such record and reports state 'ok' with
    # deactivated_reason None.
    #
    # Audit aging (FIX 1): the append-only log keeps every
    # ``ongoing_trigger.auto_deactivated`` record forever, and re-enable only
    # re-anchors ``streak_epoch``. Constrain the query to
    # ``created_at >= streak_epoch`` so ONLY deactivations since the last
    # active=True transition count — a re-enabled -> manually-paused trigger
    # must never surface the OLD pre-re-enable deactivation record as a false
    # 'deactivated' badge. A NULL epoch (rolling-deploy skew) COALESCEs to now()
    # so nothing pre-anchor counts.
    deactivated_reason: str | None = None
    reason_read_failed = False
    if not getattr(trigger, "active", True):
        # A reason-read failure on an INACTIVE trigger must never collapse to
        # 'unconfigured': surface 'deactivated' with the reason unknown so the
        # operator sees something needs attention.
        deactivated_reason, reason_read_failed = await _read_streak_status_reason(session, org_id, trigger_id, trigger)

    state = "deactivated" if deactivated_reason is not None or reason_read_failed else "ok"

    return {
        "enabled": _streak_deactivate_enabled(),
        "streak": streak,
        "threshold": threshold,
        "state": state,
        "deactivated_reason": deactivated_reason,
        "last_outcomes": last_outcomes,
    }


def _streak_status_base() -> dict[str, Any]:
    """Bare unconfigured reader response (nothing computable yet)."""
    return {
        "enabled": False,
        "streak": 0,
        "threshold": 0,
        "state": "unconfigured",
        "deactivated_reason": None,
        "last_outcomes": [],
    }


def _resolve_streak_status_threshold(trigger: Any, config_threshold: int | None) -> tuple[int, dict[str, Any] | None]:
    """Resolve the reader threshold; a failure degrades to the bare base.

    Returns ``(threshold, None)`` on success or ``(0, base)`` when the
    caller-supplied / config threshold cannot be resolved.
    """
    base = _streak_status_base()
    try:
        return _resolve_streak_threshold(trigger, config_threshold), None
    except Exception:
        _log.warning("streak.threshold_resolve_failed trigger=%s", getattr(trigger, "id", None), exc_info=True)
        return 0, base


async def _read_streak_status_count(
    session: AsyncSession, org_id: uuid.UUID, trigger_id: uuid.UUID
) -> tuple[int, bool]:
    """Read the current streak; ``(streak, ok)`` — ``False`` means degrade."""
    try:
        return await _read_streak_count(session, org_id, trigger_id), True
    except Exception:
        _log.warning("streak.status_count_failed trigger=%s", trigger_id, exc_info=True)
        return 0, False


async def _read_streak_status_outcomes(
    session: AsyncSession, org_id: uuid.UUID, trigger_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Read the last-N outcome summary; ``[]`` on failure (keeps streak+threshold)."""
    try:
        return await _read_streak_outcomes(session, org_id, trigger_id)
    except Exception:
        _log.warning("streak.status_outcomes_failed trigger=%s", trigger_id, exc_info=True)
        return []


async def _read_streak_status_reason(
    session: AsyncSession,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    trigger: Any,
) -> tuple[str | None, bool]:
    """Read the deactivation reason; ``(reason, failed)`` on outcome.

    A read failure surfaces ``(None, True)`` so the caller reports
    ``deactivated`` with the reason unknown rather than collapsing to
    'unconfigured'.
    """
    try:
        return await _read_streak_deactivation_reason(session, org_id, trigger_id, trigger), False
    except Exception:
        _log.warning("streak.status_reason_failed trigger=%s", trigger_id, exc_info=True)
        return None, True


# ---------------------------------------------------------------------------
# Audit / lifecycle records
# ---------------------------------------------------------------------------


async def record_ongoing_deactivation_lifecycle(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
    streak: int = 0,
    threshold: int | None = None,
    reason: str = "no_delivery",
    deactivated_by: str = STREAK_DEACTIVATED_BY_STREAK,
) -> None:
    """Shared deactivation lifecycle ceremony (FAR-190 / FAR-158): the
    append-only AuditEvent (actor=system) is the primary record; the TriggerEvent
    row keeps the fire-outcome log consistent. Run in the SAME transaction as the
    ``active=False`` write. Used by the no-delivery streak deactivation AND the
    config-failure deactivation (``cron_helpers._bump_ongoing_failure``) so both
    auto-deactivation paths leave identical, searchable audit records.
    """
    from types import SimpleNamespace

    from modulo.core.audit_logger import append_audit_event

    ch = _ch()
    streak = int(streak or 0)
    await append_audit_event(
        session,
        org_id=org_id,
        event_type=STREAK_DEACTIVATION_EVENT_TYPE,
        actor_user_id=None,  # system
        resource_type="trigger",
        resource_id=trigger_id,
        payload_json={
            "trigger_id": str(trigger_id),
            "pipeline_id": str(pipeline_id) if pipeline_id else "",
            "streak": streak,
            "threshold": int(threshold) if threshold is not None else None,
            "reason": reason or "no_delivery",
            "trigger_type": "ongoing",
            "deactivated_by": deactivated_by,
        },
    )
    if deactivated_by == STREAK_DEACTIVATED_BY_STREAK:
        detail = f"auto-deactivated after {streak} consecutive no-delivery runs (threshold {int(threshold or 0)})"
    else:
        detail = f"auto-deactivated after {streak} consecutive failures (config-failure guard)"
    await ch._log_ongoing_event(
        session,
        trigger=SimpleNamespace(id=trigger_id),
        org_id=org_id,
        result="auto_deactivated",
        error_detail=detail,
    )


async def _record_streak_deactivation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    data: dict[str, Any],
    threshold: int,
    reason: str | None,
) -> None:
    """Lifecycle records for one no-delivery deactivation — thin wrapper over the
    shared :func:`record_ongoing_deactivation_lifecycle`.
    """
    await record_ongoing_deactivation_lifecycle(
        session,
        org_id=org_id,
        trigger_id=data["id"],
        pipeline_id=data.get("pipeline_id") or None,
        streak=int(data.get("streak") or 0),
        threshold=int(threshold),
        reason=reason or "no_delivery",
        deactivated_by=STREAK_DEACTIVATED_BY_STREAK,
    )


# ---------------------------------------------------------------------------
# Deactivation walk helpers
# ---------------------------------------------------------------------------


async def _count_recent_streak_deactivations(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    hours: int,
) -> int:
    """Count an org's streak auto-deactivations in the last *hours* (audit chain
    is the primary lifecycle record, so the count reads audit_events). Best-
    effort: a count failure reads 0 (the per-trigger atomic UPDATE remains the
    hard correctness bound; the cap is a soft operational throttle).
    """
    from sqlalchemy import func

    from modulo.db.models.audit_event import AuditEvent

    ch = _ch()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    try:
        async with factory() as session, session.begin():
            await ch._set_rls_org(session, org_id)
            result = await session.execute(
                select(func.count()).where(
                    AuditEvent.organisation_id == org_id,
                    AuditEvent.event_type == STREAK_DEACTIVATION_EVENT_TYPE,
                    AuditEvent.created_at >= cutoff,
                )
            )
            return int(result.scalar_one() or 0)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.deactivation_count_failed org=%s", org_id)
        return 0


async def _select_active_ongoing_triggers(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    max_triggers: int = 100,
    after_id: uuid.UUID | None = None,
) -> list[Any]:
    """One keyset page of the org's active ongoing triggers for the sweep.

    ``after_id`` is the exclusive ``id > after_id`` cursor: the sweep walks an
    org in ``max_triggers``-sized pages, so an org with more triggers than the
    page size is NEVER starved (FAR-190 qa FIX 7 — the old ``LIMIT 100`` with no
    rotation skipped every org's triggers past the first page forever).
    """
    from modulo.db.models.trigger import Trigger

    ch = _ch()
    async with factory() as session, session.begin():
        await ch._set_rls_org(session, org_id)
        stmt = select(Trigger).where(
            Trigger.trigger_type == "ongoing",
            Trigger.active.is_(True),
            Trigger.deleted_at.is_(None),
        )
        if after_id is not None:
            stmt = stmt.where(Trigger.id > after_id)
        result = await session.execute(stmt.order_by(Trigger.id).limit(max_triggers))
        return list(result.scalars().all())


async def _newest_streak_no_delivery_reason(
    session: AsyncSession,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> str | None:
    """The newest countable no-delivery run's classification reason — the audit
    + notification reason for a deactivation. Mirrors the count predicate
    (boundary + stop) so it returns a run actually inside the streak.
    """
    result = await session.execute(
        text(_STREAK_NEWEST_REASON_SQL),
        {"oid": str(org_id), "tid": str(trigger_id)},
    )
    row = result.first()
    if row is None:
        return None
    return row[0] if row[0] is not None else None


async def _deactivate_trigger_on_no_delivery_streak(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    threshold: int,
    window_cutoff: datetime,
) -> dict[str, Any] | None:
    """Guarded atomic deactivation for ONE ongoing trigger (FAR-190).

    The streak is computed INSIDE the UPDATE's WHERE from the live trigger row
    (``active`` + ``streak_epoch``) and the classification log, so a re-enabled
    trigger or a stale tick can never be hit (no TOCTOU; concurrent ticks
    produce one rowcount=1 and the second is a no-op). Runs in its OWN
    transaction (per-trigger isolation): the deactivation UPDATE, the AuditEvent
    lifecycle record, and the fire-outcome TriggerEvent commit together, and a
    failure for this trigger can never stop the other triggers in the sweep.
    Returns ``{id, pipeline_id, organisation_id, config_json, streak, reason}``
    when deactivated, else ``None`` (below threshold / inside the wall-clock
    window / already inactive).
    """
    ch = _ch()
    async with factory() as session, session.begin():
        await ch._set_rls_org(session, org_id)
        result = await session.execute(
            text(_NO_DELIVERY_DEACTIVATE_SQL),
            {
                "tid": str(trigger_id),
                "oid": str(org_id),
                "threshold": threshold,
                "window_cutoff": window_cutoff,
            },
        )
        row = result.first()
        if row is None:
            return None
        data: dict[str, Any] = {
            "id": trigger_id,
            "pipeline_id": row[1],
            "organisation_id": row[2],
            "config_json": row[3] or {},
            "streak": int(row[4] or 0),
        }
        reason = await _newest_streak_no_delivery_reason(session, org_id, trigger_id)
        data["reason"] = reason or "no_delivery"
        await _record_streak_deactivation(session, org_id=org_id, data=data, threshold=threshold, reason=reason)
        _log.warning(
            "streak.deactivated org=%s trigger=%s streak=%s threshold=%s",
            org_id,
            trigger_id,
            data["streak"],
            threshold,
        )
        return data


# ---------------------------------------------------------------------------
# Notification helpers — pending-retry set + bounded dispatch
# ---------------------------------------------------------------------------


def _streak_notify_pending_key(org_id: uuid.UUID) -> str:
    return f"{_STREAK_NOTIFY_PENDING_PREFIX}:{org_id}"


def _streak_pending_member(
    data: dict[str, Any],
    *,
    threshold: int,
    pipeline_name: str,
    retry_count: int = 0,
    last_retry_at: int | None = None,
) -> str:
    return json.dumps(
        {
            "trigger_id": str(data["id"]),
            "pipeline_id": str(data.get("pipeline_id") or ""),
            "streak": int(data.get("streak") or 0),
            "threshold": int(threshold),
            "reason": data.get("reason") or "no_delivery",
            "pipeline_name": pipeline_name or "",
            "retry_count": int(retry_count),
            "last_retry_at": last_retry_at,
        },
        separators=(",", ":"),
    )


async def _write_streak_notify_pending(
    redis_client: AsyncRedis | None,
    org_id: uuid.UUID,
    *,
    data: dict[str, Any],
    threshold: int,
    pipeline_name: str,
    retry_count: int = 0,
    last_retry_at: int | None = None,
) -> None:
    """Persist a failed deactivation notification as a per-org Redis SET member so
    the next scheduler tick retries the dispatch. Best-effort — never raises.
    Refreshes the SET's TTL on every write (a re-enqueued retry keeps the retry
    window alive).
    """
    if redis_client is None:
        return
    key = _streak_notify_pending_key(org_id)
    try:
        # redis.asyncio stubs type the set ops as ``Union[Awaitable, value]``
        # (dual sync/async); we always hold the async client, so cast.
        await cast(
            Awaitable[int],
            redis_client.sadd(
                key,
                _streak_pending_member(
                    data,
                    threshold=threshold,
                    pipeline_name=pipeline_name,
                    retry_count=retry_count,
                    last_retry_at=last_retry_at,
                ),
            ),
        )
        await cast(Awaitable[int], redis_client.expire(key, _STREAK_PENDING_MARKER_TTL))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.notify_pending_write_failed org=%s", org_id)


async def _record_streak_notify_failed(
    org_id: uuid.UUID,
    *,
    data: dict[str, Any],
    threshold: int,
    reason: str,
) -> None:
    """Critical audit entry when the deactivation notifier fails on the FIRST
    attempt (FAR-190 item 10). Best-effort — a failed audit write must never
    raise out of the sweep. Subsequent retries deliberately do NOT re-enter this
    path (the per-member pending set is the retry state; an audit row per retry
    tick would flood the chain).
    """
    ch = _ch()
    try:
        from modulo.core.audit_logger import append_audit_event

        async with ch._open_factory()() as session, session.begin():
            await ch._set_rls_org(session, org_id)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=STREAK_NOTIFY_FAILED_EVENT_TYPE,
                actor_user_id=None,  # system
                resource_type="trigger",
                resource_id=data["id"],
                payload_json={
                    "trigger_id": str(data["id"]),
                    "pipeline_id": str(data.get("pipeline_id") or ""),
                    "streak": int(data.get("streak") or 0),
                    "threshold": int(threshold),
                    "reason": reason,
                    "trigger_type": "ongoing",
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.notify_failed_audit_write_failed org=%s", org_id)


async def _pend_streak_notify_retry(
    org_id: uuid.UUID,
    *,
    data: dict[str, Any],
    threshold: int,
    reason: str,
    pipeline_name: str,
    redis_client: AsyncRedis | None,
    retry_count: int = 0,
) -> bool:
    """Record a first-attempt audit failure and pend a Redis retry.

    Shared tail of every deactivation-notify failure path: first-attempt
    failures (``retry_count == 0``) get a CRITICAL audit row via
    :func:`_record_streak_notify_failed`; every failure gets a per-org pending
    marker via :func:`_write_streak_notify_pending` so the next scheduler tick
    retries. Returns ``False`` so callers can ``return False`` directly.
    """
    if retry_count == 0:
        await _record_streak_notify_failed(org_id, data=data, threshold=threshold, reason=reason)
    await _write_streak_notify_pending(
        redis_client,
        org_id,
        data=data,
        threshold=threshold,
        pipeline_name=pipeline_name,
        retry_count=retry_count,
    )
    return False


async def _notify_streak_deactivation(
    org_id: uuid.UUID,
    *,
    data: dict[str, Any],
    threshold: int,
    reason: str,
    pipeline_name: str,
    redis_client: AsyncRedis | None,
    retry_count: int = 0,
) -> bool:
    """Best-effort post-commit deactivation notification (FAR-190 item 10).

    Reuses the existing notifier surface (constants in ``notifier/__init__``,
    event config in ``event_mapper._EVENT_CONFIG``, AVAILABLE_EVENTS in
    admin_notifications). Payload is sanitised: identifiers + titles +
    allow-listed reason fields only — never tokens or raw output.

    Bound: the dispatch runs under ``asyncio.wait_for`` (10s) so a hung endpoint
    can never blow the enclosing 120s dispatcher_reconcile tick — the
    deactivation (audit + trigger_events) is the durable truth. Failure
    handling:

    * dispatch raises / times out: CRITICAL log + critical audit on the FIRST
      attempt (``retry_count == 0``); later retries log WARNING only — then a
      per-org Redis pending marker so the next scheduler tick retries.
    * dispatch returns results where any endpoint ``dead_lettered`` (the notifier
      dead-letters internally after 4 attempts and does NOT raise): treated as a
      delivery failure — WARNING log (retryable, NOT critical) + pending marker.
    * no subscribed endpoints (empty result list): success — nothing to retry.

    Never raises.
    """
    safe_reason = reason if reason in _STREAK_PAYLOAD_ALLOWED_REASONS else "no_delivery"
    payload = {
        "trigger_id": str(data["id"]),
        "pipeline_name": pipeline_name or "",
        "trigger_type": "ongoing",
        "streak": int(data.get("streak") or 0),
        "threshold": int(threshold),
        "reason": safe_reason,
        # In-flight runs are drained (never cancelled); a delivery landing after
        # deactivation is surfaced here as a contract field and never silently
        # re-activates the trigger (stays inactive until the operator re-enables).
        "delivered_after_deactivation": False,
        "mass_cascade_alert": False,
    }
    ch = _ch()
    try:
        from modulo.core.notifier import EVENT_TRIGGER_DEACTIVATED, Notifier

        notifier = Notifier(ch._get_engine(), ch.get_settings().fernet_key)
        results = await asyncio.wait_for(
            notifier.dispatch_event(org_id, EVENT_TRIGGER_DEACTIVATED, payload),
            timeout=_STREAK_NOTIFY_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _log.warning(
            "streak.deactivation_notify_timeout org=%s trigger=%s (bound=%ss)",
            org_id,
            data["id"],
            _STREAK_NOTIFY_TIMEOUT_SECONDS,
        )
        if retry_count == 0:
            _log.critical("streak.deactivation_notify_failed org=%s trigger=%s (timeout)", org_id, data["id"])
        return await _pend_streak_notify_retry(
            org_id,
            data=data,
            threshold=threshold,
            reason=safe_reason,
            pipeline_name=pipeline_name,
            redis_client=redis_client,
            retry_count=retry_count,
        )
    except Exception:
        _log.critical(
            "streak.deactivation_notify_failed org=%s trigger=%s",
            org_id,
            data["id"],
            exc_info=True,
        )
        return await _pend_streak_notify_retry(
            org_id,
            data=data,
            threshold=threshold,
            reason=safe_reason,
            pipeline_name=pipeline_name,
            redis_client=redis_client,
            retry_count=retry_count,
        )
    # dispatch_event does NOT raise on per-endpoint delivery failure (it
    # dead-letters internally after 4 attempts) — inspect the results.
    if results and any(getattr(r, "status", "") == "dead_lettered" for r in results):
        _log.warning(
            "streak.deactivation_notify_dead_lettered org=%s trigger=%s",
            org_id,
            data["id"],
        )
        return await _pend_streak_notify_retry(
            org_id,
            data=data,
            threshold=threshold,
            reason=safe_reason,
            pipeline_name=pipeline_name,
            redis_client=redis_client,
            retry_count=retry_count,
        )
    return True


async def _trigger_active_state(org_id: uuid.UUID, trigger_id: uuid.UUID) -> bool | None:
    """Read a trigger's live active state for the pending-retry re-check.

    Returns ``True`` (active), ``False`` (inactive / soft-deleted / missing), or
    ``None`` when the read fails (the caller then skips the member this tick
    rather than risking a stale notification).
    """
    from modulo.db.models.trigger import Trigger

    ch = _ch()
    try:
        async with ch._open_factory()() as session, session.begin():
            await ch._set_rls_org(session, org_id)
            result = await session.execute(
                select(Trigger.active).where(Trigger.id == trigger_id, Trigger.organisation_id == org_id)
            )
            val = result.scalar_one_or_none()
            if val is None:
                return False
            return bool(val)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.trigger_active_read_failed org=%s trigger=%s", org_id, trigger_id)
        return None


async def _srem_streak_member(redis_client: AsyncRedis, key: str, raw: str) -> None:
    try:
        await cast(Awaitable[int], redis_client.srem(key, raw))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.notify_pending_remove_failed key=%s", key)


def _build_deactivation_payload(
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID | None,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Build the deactivation payload forwarded to the notifier."""
    return {
        "id": trigger_id,
        "pipeline_id": pipeline_id,
        "streak": int(data.get("streak") or 0),
        "reason": data.get("reason") or "no_delivery",
    }


async def _reenqueue_streak_notify_member(
    redis_client: AsyncRedis,
    org_id: uuid.UUID,
    key: str,
    raw: str,
    deactivation: dict[str, Any],
    threshold: int,
    pipeline_name: str,
    retry_count: int,
) -> None:
    """Drop the member from the pending set and re-enqueue it with a bumped
    retry_count + cooldown stamp (the SET's TTL is refreshed by the write), so
    the member is retried at most once per 15 min and never floods the audit
    chain. Best-effort — never raises.
    """
    await _srem_streak_member(redis_client, key, raw)
    await _write_streak_notify_pending(
        redis_client,
        org_id,
        data=deactivation,
        threshold=threshold,
        pipeline_name=pipeline_name,
        retry_count=retry_count + 1,
        last_retry_at=int(time.time()),
    )


async def _retry_one_pending_member(
    org_id: uuid.UUID,
    redis_client: AsyncRedis,
    key: str,
    raw: str,
    attempted: int,
    max_retries: int,
) -> tuple[str, int]:
    """Retry one pending notifier member; returns ``(status, attempted)``.

    ``status`` is one of:

    * ``"ok"``      — notified; caller counts a retry.
    * ``"failed"``  — notifier failed again; re-enqueued with a bumped retry.
    * ``"skip"``    — cooldown / corrupt / activity-read-failure; leave pending.
    * ``"dropped"`` — trigger re-enabled; member removed from the pending set.
    * ``"stop"``    — per-tick dispatch cap reached; caller stops the pass.

    Never raises out: unparseable/corrupt members are srem'd so they are not
    retried forever.
    """
    data = _decode_pending_member(raw)
    if data is None:
        # corrupt / unparseable member — srem'd, never retried forever.
        _log.warning("streak.notify_pending_retry_failed org=%s", org_id)
        await _srem_streak_member(redis_client, key, raw)
        return "failed", attempted
    if _streak_member_in_cooldown(data):
        return "skip", attempted  # per-member cooldown — retry at most once per 15 min
    try:
        trigger_id = uuid.UUID(data["trigger_id"])
        pipeline_id = uuid.UUID(data["pipeline_id"]) if data.get("pipeline_id") else None
        threshold = int(data.get("threshold") or 0)
        retry_count = int(data.get("retry_count") or 0)
        # Re-check the trigger's active state before dispatching. A pending
        # member exists precisely because the trigger was JUST auto-
        # deactivated (active=False), so dispatch while it stays deactivated;
        # drop only when it has been re-enabled (active=True) — a re-enabled
        # trigger must NOT receive a stale "auto-deactivated" notification.
        # A read failure (None) skips the member this tick without dropping.
        active = await _trigger_active_state(org_id, trigger_id)
        if active is None:
            return "skip", attempted  # read failure — skip, don't drop
        if active:
            await _srem_streak_member(redis_client, key, raw)
            _log.warning(
                "streak.notify_pending_dropped org=%s trigger=%s (re-enabled)",
                org_id,
                trigger_id,
            )
            return "dropped", attempted
        attempted += 1
        if attempted > max_retries:
            return "stop", attempted  # per-tick dispatch cap reached — leave the rest pending
        return await _dispatch_pending_member_notify(
            org_id,
            redis_client,
            key,
            raw,
            data,
            trigger_id,
            pipeline_id,
            threshold,
            retry_count,
            attempted,
        )
    except Exception:
        _log.warning("streak.notify_pending_retry_failed org=%s", org_id)
        await _srem_streak_member(redis_client, key, raw)
        return "failed", attempted


def _decode_pending_member(raw: str) -> dict[str, Any] | None:
    """Parse a pending notifier member; ``None`` on corrupt / unparseable."""
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


async def _dispatch_pending_member_notify(
    org_id: uuid.UUID,
    redis_client: AsyncRedis,
    key: str,
    raw: str,
    data: dict[str, Any],
    trigger_id: uuid.UUID,
    pipeline_id: uuid.UUID | None,
    threshold: int,
    retry_count: int,
    attempted: int,
) -> tuple[str, int]:
    """Dispatch one pending member's deactivation notification.

    Returns ``("ok", attempted)`` when the notifier accepted (member srem'd) or
    ``("failed", attempted)`` after re-enqueueing with a bumped ``retry_count``
    + cooldown stamp (the SET's TTL is refreshed by the write), so the member is
    retried at most once per 15 min and never floods the audit chain.
    """
    deactivation = _build_deactivation_payload(trigger_id, pipeline_id, data)
    ok = await _notify_streak_deactivation(
        org_id,
        data=deactivation,
        threshold=threshold,
        reason=deactivation["reason"],
        pipeline_name=data.get("pipeline_name") or "",
        # None: the retry path owns the re-enqueue (bumped retry_count +
        # cooldown) below — never a bare re-add.
        redis_client=None,
        retry_count=retry_count,
    )
    if ok:
        await _srem_streak_member(redis_client, key, raw)
        return "ok", attempted
    # Re-enqueue with a bumped retry_count + cooldown stamp (the SET's TTL is
    # refreshed by the write) so the member is retried at most once per 15 min
    # and never floods the audit chain.
    await _reenqueue_streak_notify_member(
        redis_client,
        org_id,
        key,
        raw,
        deactivation,
        threshold,
        data.get("pipeline_name") or "",
        retry_count,
    )
    return "failed", attempted


def _streak_member_in_cooldown(data: dict[str, Any]) -> bool:
    """True when this pending member is inside its retry cooldown window."""
    last_retry_at = data.get("last_retry_at")
    if not isinstance(last_retry_at, (int, float)):
        return False
    return time.time() - float(last_retry_at) < _STREAK_RETRY_COOLDOWN_SECONDS


async def _read_streak_pending_members(redis_client: AsyncRedis, org_id: uuid.UUID) -> set[str] | None:
    """Read the per-org pending notify set; ``None`` on failure (skip the pass)."""
    try:
        # redis.asyncio stubs type the set ops as ``Union[Awaitable, value]``
        # (dual sync/async); we always hold the async client, so cast.
        return await cast(Awaitable[set[str]], redis_client.smembers(_streak_notify_pending_key(org_id)))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.notify_pending_read_failed org=%s", org_id)
        return None


async def _retry_pending_streak_notifications(
    org_id: uuid.UUID,
    redis_client: AsyncRedis | None,
    *,
    deadline: float | None = None,
    max_retries: int = _STREAK_NOTIFY_MAX_PER_TICK,
) -> int:
    """Retry deactivation notifications whose first dispatch failed (persisted
    notified_at + retry on scheduler tick). Reads the per-org pending SET,
    re-dispatches each, removes on success. Bounded (cooldown) + deduplicated
    (single critical audit on first failure) + deadline-gated: the pass runs
    only while the sweep's wall-clock budget remains (``deadline``) and
    dispatches at most ``max_retries`` members per tick, so a mass-cascade
    retry pass can never blow the enclosing 120s tick. Best-effort — never
    raises.
    """
    if redis_client is None:
        return 0
    if deadline is not None and time.monotonic() > deadline:
        return 0  # sweep budget already exhausted — skip the pass entirely
    members = await _read_streak_pending_members(redis_client, org_id)
    if members is None:
        return 0
    key = _streak_notify_pending_key(org_id)
    retried = 0
    attempted = 0
    for raw in members or []:
        if deadline is not None and time.monotonic() > deadline:
            break  # budget exhausted mid-pass — truncate, never drop
        status, attempted = await _retry_one_pending_member(org_id, redis_client, key, raw, attempted, max_retries)
        if status == "ok":
            retried += 1
        if status == "stop":
            break
    return retried


# ---------------------------------------------------------------------------
# Mass-cascade guard (DB-derived dedup)
# ---------------------------------------------------------------------------


async def _record_streak_mass_cascade(org_id: uuid.UUID, count: int) -> None:
    """Critical audit entry for a mass-cascade alert (FAR-190 item 9). Best-effort."""
    ch = _ch()
    try:
        from modulo.core.audit_logger import append_audit_event

        async with ch._open_factory()() as session, session.begin():
            await ch._set_rls_org(session, org_id)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=STREAK_MASS_CASCADE_EVENT_TYPE,
                actor_user_id=None,  # system
                resource_type="organisation",
                resource_id=org_id,
                payload_json={
                    "deactivated_count": int(count),
                    "window_hours": ONGOING_STREAK_MASS_CASCADE_ALERT_WINDOW_HOURS,
                    "threshold": ONGOING_STREAK_MASS_CASCADE_ALERT_THRESHOLD,
                    "trigger_type": "ongoing",
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.mass_cascade_audit_write_failed org=%s", org_id)


async def _streak_mass_cascade_alerted_this_window(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
) -> bool:
    """DB-derived dedup for the mass-cascade alert: has this org already fired a
    mass-cascade alert within the window? The audit chain is the source of truth
    (the deactivation count already comes from audit_events), so there is NO
    Redis dependency — a Redis outage can never suppress the alert.
    """
    from sqlalchemy import func

    from modulo.db.models.audit_event import AuditEvent

    ch = _ch()
    cutoff = datetime.now(UTC) - timedelta(hours=ONGOING_STREAK_MASS_CASCADE_ALERT_WINDOW_HOURS)
    try:
        async with factory() as session, session.begin():
            await ch._set_rls_org(session, org_id)
            result = await session.execute(
                select(func.count()).where(
                    AuditEvent.organisation_id == org_id,
                    AuditEvent.event_type == STREAK_MASS_CASCADE_EVENT_TYPE,
                    AuditEvent.created_at >= cutoff,
                )
            )
            return int(result.scalar_one() or 0) > 0
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.mass_cascade_dedup_check_failed org=%s", org_id)
        return False  # fail-open: cannot confirm a prior alert -> alert


async def _maybe_alert_mass_cascade(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
) -> bool:
    """Mass-cascade guard (FAR-190 item 9): when an org has deactivated >= 5
    triggers within 24h (an infra-outage signature), raise a critical alert —
    once per window. The alert side-effects (critical log + critical audit +
    ops error ingestion) run UNCONDITIONALLY once the threshold is crossed; the
    DB audit chain (never Redis) dedups the once-per-window firing, so a Redis
    outage degrades to a duplicate alert (noisy), never a suppressed one. The
    per-trigger atomic UPDATE remains the hard bound. Best-effort — never raises.
    """
    try:
        count = await _count_recent_streak_deactivations(
            factory, org_id, hours=ONGOING_STREAK_MASS_CASCADE_ALERT_WINDOW_HOURS
        )
        if count < ONGOING_STREAK_MASS_CASCADE_ALERT_THRESHOLD:
            return False
        if await _streak_mass_cascade_alerted_this_window(factory, org_id):
            return False  # already alerted this window
        _log.critical(
            "streak.mass_cascade org=%s deactivated_24h=%d threshold=%d — suspected infra outage",
            org_id,
            count,
            ONGOING_STREAK_MASS_CASCADE_ALERT_THRESHOLD,
        )
        await _record_streak_mass_cascade(org_id, count)
        await _ch()._ingest_saq_error(
            cast(AsyncSession, None),  # session param is vestigial (the helper opens its own)
            org_id,
            function="enforce_no_delivery_streaks",
            message=(
                f"streak engine mass cascade: {count} ongoing triggers auto-deactivated in the last "
                f"{ONGOING_STREAK_MASS_CASCADE_ALERT_WINDOW_HOURS}h"
            ),
            context={"org_id": str(org_id), "deactivated_24h": count},
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.mass_cascade_check_failed org=%s", org_id)
        return False


# ---------------------------------------------------------------------------
# Sweep entry
# ---------------------------------------------------------------------------


async def _pipeline_name(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID | None,
) -> str:
    """Best-effort pipeline name for the notification payload (titles only)."""
    if pipeline_id is None:
        return ""
    try:
        from modulo.db.models.pipeline import Pipeline
        from modulo.db.rls import set_rls_execution_context

        ch = _ch()
        async with factory() as session, session.begin():
            await ch._set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            result = await session.execute(select(Pipeline.name).where(Pipeline.id == pipeline_id))
            name = result.scalar_one_or_none()
            return str(name) if name is not None else ""
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("streak.pipeline_name_read_failed pipeline=%s", pipeline_id)
        return ""


_SWEEP_SUMMARY_KEYS = (
    "scanned",
    "deactivated",
    "capped",
    "alerts",
    "notify_failed",
    "notify_retried",
    "notify_deferred",
    "errors",
)


async def _handle_trigger_in_sweep(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    trigger: Any,
    *,
    delta: dict[str, Any],
    notify_budget: int,
    redis_client: AsyncRedis | None,
    recent_deactivations: int,
    deactivated_this_tick: int,
) -> tuple[int, int]:
    """Deactivate + notify ONE trigger; returns ``(notify_budget, deactivated_this_tick)``.

    Capacity-capped triggers are counted and skipped without deactivating. A
    guarded atomic deactivation below threshold is a no-op. On a deactivation,
    the inline-notification budget is honoured (deferred to the pending-retry
    path once exhausted) and the mass-cascade alert is checked. Isolated: any
    per-trigger failure is swallowed (WARNING + error count) and cannot break
    the enclosing sweep.
    """
    if recent_deactivations + deactivated_this_tick >= ONGOING_STREAK_DEACTIVATE_MAX_PER_ORG_PER_HOUR:
        delta["capped"] += 1
        _log.warning("streak.capped org=%s trigger=%s", org_id, trigger.id)
        return notify_budget, deactivated_this_tick
    threshold, window_hours = _streak_config(trigger.config_json)
    window_cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    try:
        deactivated = await _deactivate_trigger_on_no_delivery_streak(
            factory,
            org_id=org_id,
            trigger_id=trigger.id,
            threshold=threshold,
            window_cutoff=window_cutoff,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        delta["errors"] += 1
        _log.warning("streak.walk_failed org=%s trigger=%s", org_id, trigger.id, exc_info=True)
        return notify_budget, deactivated_this_tick
    if deactivated is None:
        return notify_budget, deactivated_this_tick
    deactivated_this_tick += 1
    delta["deactivated"] += 1
    pipeline_name = await _pipeline_name(factory, org_id, deactivated["pipeline_id"])
    reason = deactivated.get("reason") or "no_delivery"
    if notify_budget > 0:
        notify_budget -= 1
        notified = await _notify_streak_deactivation(
            org_id,
            data=deactivated,
            threshold=threshold,
            reason=reason,
            pipeline_name=pipeline_name,
            redis_client=redis_client,
        )
        if not notified:
            delta["notify_failed"] += 1
    else:
        # Inline notification budget exhausted — defer this dispatch to the
        # pending-retry path (never inline).
        delta["notify_deferred"] += 1
        _log.warning(
            "streak.notify_budget_exceeded org=%s trigger=%s",
            org_id,
            deactivated["id"],
        )
        await _write_streak_notify_pending(
            redis_client,
            org_id,
            data=deactivated,
            threshold=threshold,
            pipeline_name=pipeline_name,
        )
    if await _maybe_alert_mass_cascade(factory, org_id):
        delta["alerts"] += 1
    return notify_budget, deactivated_this_tick


async def _sweep_org(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    redis_client: AsyncRedis | None,
    max_triggers_per_tick: int,
    deadline: float,
    notify_budget: int,
) -> tuple[dict[str, Any], int]:
    """Walk one org's active ongoing triggers (keyset pages) and deactivate.

    Folds each trigger's streak count into a guarded atomic deactivation UPDATE
    via :func:`_handle_trigger_in_sweep`, keyed by ``deactivated_this_tick`` +
    the per-org per-hour cap. Returns ``(delta, remaining_notify_budget)`` where
    ``delta`` aggregates the org's contribution to the sweep summary.
    """
    delta: dict[str, Any] = dict.fromkeys(_SWEEP_SUMMARY_KEYS, 0)
    recent_deactivations = await _count_recent_streak_deactivations(factory, org_id, hours=1)
    deactivated_this_tick = 0
    after_id: uuid.UUID | None = None
    while time.monotonic() <= deadline:
        page = await _select_active_ongoing_triggers(
            factory,
            org_id,
            max_triggers=max_triggers_per_tick,
            after_id=after_id,
        )
        if not page:
            break
        for trigger in page:
            if time.monotonic() > deadline:
                break
            delta["scanned"] += 1
            notify_budget, deactivated_this_tick = await _handle_trigger_in_sweep(
                factory,
                org_id,
                trigger,
                delta=delta,
                notify_budget=notify_budget,
                redis_client=redis_client,
                recent_deactivations=recent_deactivations,
                deactivated_this_tick=deactivated_this_tick,
            )
        if len(page) < max_triggers_per_tick:
            break
        after_id = page[-1].id
    delta["notify_retried"] += await _retry_pending_streak_notifications(org_id, redis_client, deadline=deadline)
    return delta, notify_budget


async def enforce_no_delivery_streaks(
    *,
    org_ids: list[uuid.UUID] | None = None,
    redis_client: AsyncRedis | None = None,
    max_triggers_per_tick: int = 100,
    budget_seconds: float = _STREAK_SWEEP_BUDGET_SECONDS,
) -> dict[str, Any]:
    """FAR-190 sweep — auto-deactivate ongoing triggers on no-delivery streaks.

    Runs as a system sweep (from ``dispatcher_reconcile``, every 60s — NEVER
    inline in terminalization). Per org: walks the active ongoing triggers in
    keyset pages (an org with more than ``max_triggers_per_tick`` triggers is
    fully scanned, page by page — never starved) and, for each, folds the streak
    count into a guarded atomic deactivation UPDATE. Per-org per-hour cap + the
    24h mass-cascade alert guard against an infra outage cascading. The kill
    switch (``_streak_deactivate_enabled``) gates ONLY the deactivate+notify
    side-effect; classification persists regardless (the reconcile sweep is
    independent).

    Bounds: the whole sweep runs under ``budget_seconds`` (default 45s, inside
    the 120s tick), each notification under ``_STREAK_NOTIFY_TIMEOUT_SECONDS``,
    and inline notifications are capped at ``_STREAK_NOTIFY_MAX_PER_TICK`` —
    beyond the cap the notification is deferred to the pending-retry path.

    The sweep NEVER raises — every per-trigger step is isolated, so a walk
    failure for one trigger is swallowed (WARNING) and can never break the
    enclosing tick's ongoing top-up, and can never stop the other triggers in
    the sweep. Returns a summary dict.
    """
    summary: dict[str, Any] = dict.fromkeys(_SWEEP_SUMMARY_KEYS, 0)
    try:
        if not _streak_deactivate_enabled():
            summary["kill_switch"] = "off"
            return summary
        ch = _ch()
        factory = ch._open_factory()
        if org_ids is None:
            org_ids = await _sweep_all_org_ids(factory)
        if not org_ids:
            return summary
        deadline = time.monotonic() + budget_seconds
        return await _run_sweep_orgs(
            factory,
            org_ids,
            redis_client,
            max_triggers_per_tick,
            deadline,
            summary,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("enforce_no_delivery_streaks sweep_failed", exc_info=True)
        summary["errors"] += 1
        return summary


async def _run_sweep_orgs(
    factory: async_sessionmaker[AsyncSession],
    org_ids: Sequence[uuid.UUID],
    redis_client: AsyncRedis | None,
    max_triggers_per_tick: int,
    deadline: float,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Sweep every org within the wall-clock budget; returns the summary.

    Stops as soon as the deadline elapses (``budget_exceeded`` is recorded so
    the caller can distinguish a truncated pass from a clean one). Each org is
    isolated via :func:`_enforce_org_streak`.
    """
    notify_budget = _STREAK_NOTIFY_MAX_PER_TICK
    for org_id in org_ids:
        if time.monotonic() > deadline:
            summary["budget_exceeded"] = True
            break
        notify_budget = await _enforce_org_streak(
            factory,
            org_id,
            redis_client=redis_client,
            max_triggers_per_tick=max_triggers_per_tick,
            deadline=deadline,
            notify_budget=notify_budget,
            summary=summary,
        )
    return summary


async def _sweep_all_org_ids(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Read every org id (system context) to sweep when none are passed in."""
    from modulo.db.models.organisation import Organisation

    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        return list(result.scalars())


async def _enforce_org_streak(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    redis_client: AsyncRedis | None,
    max_triggers_per_tick: int,
    deadline: float,
    notify_budget: int,
    summary: dict[str, Any],
) -> int:
    """Sweep one org's active ongoing triggers and fold its delta in.

    Isolated: a per-org sweep failure is swallowed (WARNING + an error count in
    ``summary``) and can never break the enclosing sweep. Returns the remaining
    inline-notification budget for the next org.
    """
    try:
        delta, notify_budget = await _sweep_org(
            factory,
            org_id,
            redis_client=redis_client,
            max_triggers_per_tick=max_triggers_per_tick,
            deadline=deadline,
            notify_budget=notify_budget,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["errors"] += 1
        _log.warning("enforce_no_delivery_streaks org_sweep_failed org=%s", org_id, exc_info=True)
        return notify_budget
    for key in _SWEEP_SUMMARY_KEYS:
        summary[key] += delta[key]
    return notify_budget
