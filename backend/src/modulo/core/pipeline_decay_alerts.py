"""Pipeline-decay alerting sweep (FAR-2116).

Readiness checks answer "is the app up". They do not answer "is a pipeline
still doing its job": the Branch Fixer degraded for ~5 days (success rate
53% → 42% → 51% → 35% → 70% over 18-22 Oct, the node-deadline watchdog firing
24-56 times a day) while the application stayed healthy, and nothing alerted.
This sweep closes that gap from the existing ``dispatcher_reconcile`` cron
(every 60s) — no new scheduler, no new channel: every alert is dispatched
through the shared ``Notifier`` (org webhook subscriptions, in-app
notifications and any subscribed pager), exactly like the FAR-1344 capacity
sweep next door.

What is measured
----------------

For each **webhook-triggered** pipeline, over a rolling window
(:data:`WINDOW_MINUTES`, 2 hours):

* ``success_rate`` — delivered terminal runs / **all** terminal runs, on the
  same numerator/denominator shape the ``/analytics`` dashboard publishes
  (``run_daily_facts.success_rate``: complete / (all terminal), so
  ``cancelled`` counts against the rate). The reported incidences reproduce
  exactly under this definition (e.g. 2026-10-21: 38 complete of 110 = 34.6%).
  One deliberate divergence: /analytics drops BUSINESS-EXPECTED (``benign``)
  rows from its denominator (FAR-707), and that marker lives on the daily facts
  row rather than on the run — so this 2-hour window cannot exclude it and a
  pipeline with benign outcomes can read a little lower than the panel. The
  window is short and the threshold is 80%, so the bounded difference cannot
  manufacture an alert on its own; the honest statement is "the same shape",
  not "the identical number".
* ``node_deadline_watchdog_fired_count`` — summed across the same window's
  runs. This is the fingerprint of "the agent keeps blowing its per-attempt
  deadline": during the outage it fired 24-56 times/day, and 0 after the fix.

The pipeline is **degraded** when either condition holds:

* ``success_rate < DEGRADED_SUCCESS_RATE_PERCENT`` (80%) over at least
  :data:`MIN_SAMPLE_RUNS` (5) terminal runs, or
* ``watchdog_firings >= WATCHDOG_FIRINGS_DEGRADED`` (5) in the window.

Why a single flaky failure can never fire (the ticket's hard requirement):

* the rate condition needs 5 terminal runs AND strictly less than 80%, so
  1 failure out of 5 is exactly 80% — healthy — and 0 failures can never be
  below 80%. The minimum possible alerting rate is 2 failures in 5 runs (60%);
* the watchdog condition counts firings, not runs, and 5 firings inside one
  window is by definition repeated, not a single blip.

Noise control (mirrors the capacity sweep, which this module is modelled on):

* **One open alert per pipeline.** The open state lives in Redis (survives a
  process restart), keyed ``modulo:pipeline_decay:<org>:<pipeline>`` with a
  7-day TTL as the hard backstop. When a pipeline recovers the state is
  deleted and a single ``pipeline_decay_recovered`` event closes the loop.
* **No re-notification while the condition persists.** The rung is
  high-water: a later tick at the same rung holds silently.
* **Hysteresis against boundary flapping.** The alert ENTERS below 80% and
  only CLEARS at/above :data:`RECOVERY_SUCCESS_RATE_PERCENT` (90%) — or when
  the window no longer holds :data:`MIN_SAMPLE_RUNS` runs, at which point
  there is no evidence of ongoing degradation. The watchdog signal enters at
  5 firings and clears at/below 2, so 3-4 firings maintain an open alert
  instead of ping-ponging one.

Failure posture: the sweep NEVER raises (it runs inside ``dispatcher_reconcile``
where the other sweeps must not be broken by it) and is wall-clock bounded.
If the Redis dedup store is unavailable the sweep SKIPS dispatch rather than
risking a storm — dedup is the noise guard, so without it the safe direction
is silence — and reports that as ``state_unavailable`` plus a WARNING log
(never silently). A failed notifier dispatch is a WARNING and a counter, never
a raise: alerting is best-effort and must not take a reconcile tick down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.alert_dispatch import build_notifier, read_alert_state
from modulo.core.capacity_alerts import WARNING_RUNG, resolve_alert_action
from modulo.core.notifier import EVENT_PIPELINE_DECAY, EVENT_PIPELINE_DECAY_RECOVERED
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import SUCCESS_STATUSES, TERMINAL_STATUSES, Run
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

__all__ = [
    "DECAY_STATUSES",
    "DEGRADED_SUCCESS_RATE_PERCENT",
    "MIN_SAMPLE_RUNS",
    "RECOVERY_SUCCESS_RATE_PERCENT",
    "WATCHDOG_FIRINGS_DEGRADED",
    "WINDOW_MINUTES",
    "PipelineHealth",
    "build_decay_payload",
    "build_recovered_payload",
    "evaluate_pipeline_decay_alerts",
    "pipeline_degraded",
    "success_rate_percent",
]

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Rolling window both signals are measured over (2 hours — the ticket's
#: suggested window; long enough that a single slow run cannot dominate it).
WINDOW_MINUTES = 120

#: A rate condition needs this many terminal runs in the window. Below it the
#: sample is too small to distinguish "degraded" from "two runs, one failed".
MIN_SAMPLE_RUNS = 5

#: Enter the alert below this success rate (percent). Strictly below: 4 of 5
#: complete is exactly 80% and is NOT an alert.
DEGRADED_SUCCESS_RATE_PERCENT = 80.0

#: Clear the alert at/above this success rate (percent) — the hysteresis band
#: (80-90%) holds an open alert instead of flapping it.
RECOVERY_SUCCESS_RATE_PERCENT = 90.0

#: Enter the alert at/above this many node-deadline watchdog firings in the
#: window. The 2026-10-18..22 incident produced 24-56 firings/day (bursty), and
#: 0 once fixed; 5 in one 2-hour window is unambiguous repeated killing.
WATCHDOG_FIRINGS_DEGRADED = 5

#: Clear the watchdog half of the alert only at/below this count, so 3-4
#: firings maintain an open alert rather than flapping it.
WATCHDOG_FIRINGS_RECOVERY = 2

#: The terminal statuses the sweep counts. This is the full terminal
#: vocabulary (``cancelled`` INCLUDED, matching the /analytics success_rate
#: definition) and it is also the exact predicate of the supporting partial
#: index ``ix_runs_webhook_decay_sweep`` (migration 0334). Adding a terminal
#: status to ``TERMINAL_STATUSES`` without widening that index strands the new
#: status outside it; ``tests/unit/db/test_migration_0334_runs_webhook_decay_sweep_index.py``
#: pins the three-way agreement.
DECAY_STATUSES: tuple[str, ...] = tuple(sorted(TERMINAL_STATUSES))

#: Only webhook-triggered pipelines are in scope (the ticket's scope: a
#: webhook pipeline that runs constantly but fails most of the time). A manual
#: or cron pipeline has different expectations and is deliberately excluded.
DECAY_TRIGGER_TYPE = "webhook"

# Bounds. The sweep shares the 60s reconcile slot with the other compensating
# sweeps, so it is wall-clock bounded and per-tick bounded.
_DEFAULT_BUDGET_SECONDS = 30.0
_HARD_TIMEOUT_SLACK_SECONDS = 15.0
MAX_PIPELINES_PER_TICK = 200
_NOTIFY_TIMEOUT_SECONDS = 10.0

# Redis state: one JSON document per degraded pipeline, holding the rung, the
# label and when the alert opened. The TTL is the hard backstop — even if the
# recovery path is never reached (e.g. the pipeline disappears while the sweep
# is down), the alert cannot stay open forever.
_STATE_KEY_PREFIX = "modulo:pipeline_decay"
_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
# The alert is WARNING-class, so it reuses the capacity sweep's warning rung:
# the shared resolve_alert_action only emits a RECOVERY edge for a
# warning-or-worse state (a cleared notice is retired silently), and an
# operator must be told when a degradation ends.
_DEGRADED_RUNG = WARNING_RUNG


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineHealth:
    """One pipeline's measured window, as read from ``runs``."""

    organisation_id: uuid.UUID
    pipeline_id: uuid.UUID
    pipeline_name: str | None
    runs_total: int
    runs_complete: int
    watchdog_firings: int

    @property
    def success_rate(self) -> float | None:
        """``complete / total`` as a percentage, or ``None`` with no runs."""
        return success_rate_percent(self.runs_complete, self.runs_total)


def success_rate_percent(complete: int, total: int) -> float | None:
    """The percentage of terminal runs that completed, or ``None`` (no data).

    Rounded to one decimal so the alert body, the webhook payload and the
    /analytics panel render the same figure.
    """
    if total <= 0:
        return None
    return round(complete * 100.0 / total, 1)


def _raw_rate(health: PipelineHealth) -> float | None:
    """The UNROUNDED rate the thresholds compare against.

    Comparisons use the unrounded value so a true 79.96% cannot silently round
    up to the 80% threshold and escape the alert; only the displayed figure is
    rounded.
    """
    if health.runs_total <= 0:
        return None
    return health.runs_complete * 100.0 / health.runs_total


def pipeline_degraded(health: PipelineHealth, *, currently_active: bool) -> bool:
    """Whether *health* is sustainedly degraded.

    *currently_active* is whether an alert for this pipeline is already open
    (its Redis state exists). It selects the hysteresis band: an open alert
    stays open until the rate reaches :data:`RECOVERY_SUCCESS_RATE_PERCENT`
    (or the sample falls below :data:`MIN_SAMPLE_RUNS`), and until watchdog
    firings drop to :data:`WATCHDOG_FIRINGS_RECOVERY` or below.

    A pipeline that is not currently alerting enters only on the strict
    thresholds — never on a single failure (see the module docstring).
    """
    rate = _raw_rate(health)
    has_sample = health.runs_total >= MIN_SAMPLE_RUNS
    if has_sample and rate is not None and rate < DEGRADED_SUCCESS_RATE_PERCENT:
        return True
    if health.watchdog_firings >= WATCHDOG_FIRINGS_DEGRADED:
        return True
    if not currently_active:
        return False
    # Hysteresis: an open alert survives the band between the entry and
    # recovery thresholds. No sample means no evidence of ongoing degradation,
    # so the rate half clears (the watchdog half still has its own floor).
    if has_sample and rate is not None and rate < RECOVERY_SUCCESS_RATE_PERCENT:
        return True
    return health.watchdog_firings > WATCHDOG_FIRINGS_RECOVERY


def _degradation_reason(health: PipelineHealth) -> str:
    """The token naming what is measured wrong: ``success_rate`` / ``watchdog``.

    Only called when the pipeline is entering the alert (a fresh state), so at
    least one entry condition holds; ``success_rate`` is preferred as the
    headline because it is the figure an operator can reproduce on /analytics.
    """
    rate = _raw_rate(health)
    if health.runs_total >= MIN_SAMPLE_RUNS and rate is not None and rate < DEGRADED_SUCCESS_RATE_PERCENT:
        return "success_rate"
    return "watchdog"


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def _rate_label(health: PipelineHealth) -> str:
    rate = health.success_rate
    return "n/a" if rate is None else f"{rate:.1f}%"


def _reason_label(health: PipelineHealth, reason: str) -> str:
    """Human sentence naming the measured figure and the window.

    This is what makes the alert actionable without a lookup: the failing
    condition, the number, the threshold and the window are all in the text.
    """
    if reason == "success_rate":
        return (
            f"success rate {_rate_label(health)} over the last {WINDOW_MINUTES} minutes "
            f"({health.runs_complete} of {health.runs_total} terminal webhook runs completed, "
            f"threshold {DEGRADED_SUCCESS_RATE_PERCENT:.0f}% with at least {MIN_SAMPLE_RUNS} runs)"
        )
    return (
        f"{health.watchdog_firings} node-deadline watchdog firing(s) over the last "
        f"{WINDOW_MINUTES} minutes (threshold {WATCHDOG_FIRINGS_DEGRADED})"
    )


def _common_payload(health: PipelineHealth) -> dict[str, Any]:
    """Fields both the alert and the recovery payload carry.

    Every key referenced by either body template is always present, so the
    in-app render can never fall back to ``[unknown]``.
    """
    rate = health.success_rate
    return {
        "pipeline_id": str(health.pipeline_id),
        "pipeline_name": health.pipeline_name or str(health.pipeline_id),
        "trigger_type": DECAY_TRIGGER_TYPE,
        "window_minutes": WINDOW_MINUTES,
        "runs_total": health.runs_total,
        "runs_complete": health.runs_complete,
        "runs_failed": health.runs_total - health.runs_complete,
        "success_rate_percent": "n/a" if rate is None else f"{rate:.1f}",
        "watchdog_firings": health.watchdog_firings,
        "threshold_percent": f"{DEGRADED_SUCCESS_RATE_PERCENT:.0f}",
        "recovery_threshold_percent": f"{RECOVERY_SUCCESS_RATE_PERCENT:.0f}",
        "watchdog_threshold": WATCHDOG_FIRINGS_DEGRADED,
        "min_sample_runs": MIN_SAMPLE_RUNS,
    }


def build_decay_payload(health: PipelineHealth) -> dict[str, Any]:
    """Payload for the ``pipeline_decay`` alert.

    Carries the measured rate, the run counts, the watchdog firing count and
    the window in structured fields (so a webhook consumer can act on them)
    plus a ``reason_label`` sentence for the human-readable render.

    The rate counts ``cancelled`` in the denominator (matching the shape the
    /analytics panel publishes) and does not exclude FAR-707 ``benign`` rows —
    that marker is not on the run row; see the module docstring.
    """
    reason = _degradation_reason(health)
    payload = _common_payload(health)
    payload["reason"] = reason
    payload["reason_label"] = _reason_label(health, reason)
    return payload


def build_recovered_payload(health: PipelineHealth) -> dict[str, Any]:
    """Payload for the ``pipeline_decay_recovered`` closure event."""
    payload = _common_payload(health)
    payload["reason"] = "recovered"
    payload["reason_label"] = (
        f"success rate {_rate_label(health)} over the last {WINDOW_MINUTES} minutes "
        f"({health.runs_complete} of {health.runs_total} terminal webhook runs completed), "
        f"{health.watchdog_firings} node-deadline watchdog firing(s)"
    )
    return payload


# ---------------------------------------------------------------------------
# Redis state
# ---------------------------------------------------------------------------


def _state_key(org_id: uuid.UUID, pipeline_id: uuid.UUID) -> str:
    return f"{_STATE_KEY_PREFIX}:{org_id}:{pipeline_id}"


def _decode(raw: Any) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


def _parse_state_key(key: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Parse ``<prefix>:<org>:<pipeline>`` back into ids, else ``None``."""
    parts = key.split(":")
    if len(parts) != 4 or ":".join(parts[:2]) != _STATE_KEY_PREFIX:
        return None
    try:
        return uuid.UUID(parts[2]), uuid.UUID(parts[3])
    except ValueError:
        return None


async def _read_state(redis_client: AsyncRedis, key: str) -> dict[str, Any] | None:
    # FAR-2116: the state read is shared with the capacity sweep so the
    # "corrupt blob means absent" decision cannot drift between the two.
    return await read_alert_state(
        redis_client,
        key,
        logger=_log,
        event="pipeline_decay_alerts",
    )


async def _write_state(
    redis_client: AsyncRedis,
    key: str,
    *,
    label: str,
    reason: str,
    now: datetime,
) -> None:
    body = json.dumps({"rung": _DEGRADED_RUNG, "label": label, "reason": reason, "at": now.isoformat()})
    await redis_client.set(key, body, ex=_STATE_TTL_SECONDS)


async def _open_states(redis_client: AsyncRedis) -> dict[str, dict[str, Any]]:
    """Every open decay state, keyed by its Redis key.

    A scan over the state prefix is how a pipeline that has *recovered* is
    still reached: once its runs leave the window it is absent from the
    measurement, so without this it would never be evaluated at rung 0 and the
    operator would never receive the closure event.
    """
    found: dict[str, dict[str, Any]] = {}
    async for raw_key in redis_client.scan_iter(match=f"{_STATE_KEY_PREFIX}:*"):
        key = _decode(raw_key)
        state = await _read_state(redis_client, key)
        if state is not None:
            found[key] = state
    return found


# ---------------------------------------------------------------------------
# The measurement query
# ---------------------------------------------------------------------------


def build_health_query(*, since: datetime) -> Any:
    """The window aggregate: one row per webhook pipeline with terminal runs.

    Single-sourced so the supporting partial index (migration 0334), the
    model's index declaration and the migration's predicate can be pinned
    against the SAME expressions by the structural test.

    Runs under a system-capable session factory: it groups across every
    tenant, and an org-RLS-scoped app role would silently see zero rows.
    """
    return (
        select(
            Run.organisation_id,
            Run.pipeline_id,
            Pipeline.name.label("pipeline_name"),
            func.count().label("runs_total"),
            func.count().filter(Run.status.in_(SUCCESS_STATUSES)).label("runs_complete"),
            func.coalesce(func.sum(Run.node_deadline_watchdog_fired_count), 0).label("watchdog_firings"),
        )
        .join(Pipeline, Pipeline.id == Run.pipeline_id)
        .where(
            Run.trigger_type == DECAY_TRIGGER_TYPE,
            Run.status.in_(DECAY_STATUSES),
            Run.completed_at.is_not(None),
            Run.completed_at >= since,
        )
        .group_by(Run.organisation_id, Run.pipeline_id, Pipeline.name)
        # Busiest pipelines first, so the per-tick cap can never starve the
        # pipelines most likely to be degraded.
        .order_by(func.count().desc())
        .limit(MAX_PIPELINES_PER_TICK)
    )


def health_from_row(row: Any) -> PipelineHealth:
    """Adapt one aggregate row into a :class:`PipelineHealth`."""
    return PipelineHealth(
        organisation_id=row.organisation_id,
        pipeline_id=row.pipeline_id,
        pipeline_name=row.pipeline_name,
        runs_total=int(row.runs_total or 0),
        runs_complete=int(row.runs_complete or 0),
        watchdog_firings=int(row.watchdog_firings or 0),
    )


async def read_pipeline_health(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> list[PipelineHealth]:
    """Read the rolling-window aggregate for every in-scope pipeline."""
    since = now - timedelta(minutes=WINDOW_MINUTES)
    async with factory() as session, session.begin():
        result = await session.execute(build_health_query(since=since))
        return [health_from_row(row) for row in result.all()]


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def _build_notifier(settings: Settings) -> Any:
    """Build the shared Notifier (one definition, shared with capacity alerts)."""
    return build_notifier(settings)


async def _dispatch(
    notifier: Any,
    *,
    org_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    summary: dict[str, Any],
) -> bool:
    """Dispatch one alert, bounded, and never raising into the sweep.

    Returns True when the notifier accepted the event. The caller uses that to
    decide whether the dedup state may be written: a FAILED dispatch must not
    leave a state claiming the operator was told, because the next tick would
    then hold silently and the alert would be lost until the pipeline recovered
    and degraded again.
    """
    try:
        await asyncio.wait_for(
            notifier.dispatch_event(org_id, event_type, payload),
            timeout=_NOTIFY_TIMEOUT_SECONDS,
        )
        summary["dispatched"] += 1
        return True
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        summary["notify_failed"] += 1
        _log.warning(
            "pipeline_decay_alerts.notify_timeout",
            extra={"event_type": event_type, "org_id": str(org_id)},
        )
        return False
    except Exception:
        summary["notify_failed"] += 1
        _log.warning(
            "pipeline_decay_alerts.notify_failed",
            extra={"event_type": event_type, "org_id": str(org_id)},
            exc_info=True,
        )
        return False


def _label(state: dict[str, Any] | None, health: PipelineHealth | None) -> str:
    if health is not None:
        return health.pipeline_name or str(health.pipeline_id)
    if state is not None:
        label = state.get("label")
        if isinstance(label, str) and label:
            return label
    return "unknown pipeline"


async def _apply(
    redis_client: AsyncRedis,
    notifier: Any,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    health: PipelineHealth | None,
    state: dict[str, Any] | None,
    summary: dict[str, Any],
    now: datetime,
) -> str:
    """Run the notify / hold / recover machine for one pipeline.

    A ``health`` of ``None`` means the pipeline produced no in-scope runs this
    tick — no evidence of continuing degradation, so it evaluates healthy and
    any open alert is closed with the closure event.
    """
    currently_active = state is not None
    degraded = health is not None and pipeline_degraded(health, currently_active=currently_active)
    prev_rung = _DEGRADED_RUNG if currently_active else None
    action, stored = resolve_alert_action(prev_rung, _DEGRADED_RUNG if degraded else 0)
    key = _state_key(org_id, pipeline_id)

    if action == "notify" and health is not None:
        payload = build_decay_payload(health)
        # Dispatch BEFORE writing the dedup state: a failed dispatch must be
        # retried on the next tick, not remembered as delivered.
        delivered = await _dispatch(
            notifier,
            org_id=org_id,
            event_type=EVENT_PIPELINE_DECAY,
            payload=payload,
            summary=summary,
        )
        if not delivered:
            # The DECISION was to notify; the next tick retries because no
            # state was written. Counted as an alert so the summary reflects
            # the decision, with the delivery failure in notify_failed.
            return "notify"
        await _write_state(
            redis_client,
            key,
            label=payload["pipeline_name"],
            reason=str(payload["reason"]),
            now=now,
        )
        _log.warning(
            "pipeline_decay_alerts.degraded",
            extra={
                "pipeline_id": str(pipeline_id),
                "org_id": str(org_id),
                "reason": payload["reason"],
                "success_rate_percent": payload["success_rate_percent"],
                "runs_total": health.runs_total,
                "watchdog_firings": health.watchdog_firings,
                "window_minutes": WINDOW_MINUTES,
            },
        )
        return "notify"

    if action == "recover":
        recovered = health or PipelineHealth(
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            pipeline_name=_label(state, None),
            runs_total=0,
            runs_complete=0,
            watchdog_firings=0,
        )
        delivered = await _dispatch(
            notifier,
            org_id=org_id,
            event_type=EVENT_PIPELINE_DECAY_RECOVERED,
            payload=build_recovered_payload(recovered),
            summary=summary,
        )
        if not delivered:
            # Keep the state so the closure is retried rather than lost.
            return "recover"
        await redis_client.delete(key)
        _log.info(
            "pipeline_decay_alerts.recovered",
            extra={"pipeline_id": str(pipeline_id), "org_id": str(org_id)},
        )
        return "recover"

    if degraded and stored is not None:
        # Still degraded at the stored rung: hold silently (one alert per
        # incident). The state must NOT be rewritten — that would push the
        # 7-day TTL forward forever — so nothing to do here.
        return "hold"

    if stored is None and currently_active:
        # Belt-and-braces: a state that resolves to "clear" without the
        # recovery edge (only possible if the rung encoding changes) is
        # dropped rather than left to expire.
        await redis_client.delete(key)
    return "hold"


async def evaluate_pipeline_decay_alerts(
    *,
    factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis | None,
    settings: Settings | None = None,
    notifier: Any | None = None,
    now: datetime | None = None,
    budget_seconds: float = _DEFAULT_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Run the pipeline-decay alerting sweep (FAR-2116). NEVER raises.

    Wired into ``dispatcher_reconcile`` (60s), so it is wall-clock bounded and
    isolated: a failure here is a WARNING and a counter, never an abort of the
    reconcile tick (or of any other sweep in it).
    """
    summary: dict[str, Any] = {
        "pipelines_measured": 0,
        "alerts": 0,
        "recoveries": 0,
        "dispatched": 0,
        "notify_failed": 0,
        "state_unavailable": 0,
        "read_failed": 0,
        "budget_exhausted": False,
    }
    if redis_client is None:
        # Dedup is the noise guard; without state we cannot guarantee one
        # alert per incident, so the safe direction is silence — reported.
        summary["state_unavailable"] = 1
        _log.warning("pipeline_decay_alerts.skipped_no_redis (dedup state unavailable)")
        return summary

    now = now or datetime.now(UTC)
    settings = settings or get_settings()
    notifier = notifier or _build_notifier(settings)

    try:
        await asyncio.wait_for(
            _run_sweep(
                factory=factory,
                redis_client=redis_client,
                notifier=notifier,
                now=now,
                summary=summary,
                budget_seconds=budget_seconds,
            ),
            timeout=budget_seconds + _HARD_TIMEOUT_SLACK_SECONDS,
        )
    except TimeoutError:
        summary["budget_exhausted"] = True
        _log.warning(
            "pipeline_decay_alerts.hard_timeout (bound=%ss)",
            budget_seconds + _HARD_TIMEOUT_SLACK_SECONDS,
        )

    if summary["alerts"] or summary["recoveries"] or summary["read_failed"]:
        _log.info("pipeline_decay_alerts.sweep", extra=dict(summary))
    return summary


async def _run_sweep(
    *,
    factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    notifier: Any,
    now: datetime,
    summary: dict[str, Any],
    budget_seconds: float,
) -> None:
    """Measure, then act on every measured pipeline and every open state."""
    started = time.monotonic()
    try:
        health_rows = await read_pipeline_health(factory, now=now)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A failed read is UNKNOWN, not "everything recovered": skipping the
        # tick leaves open alerts open instead of emitting a spurious closure
        # followed by a re-alert.
        summary["read_failed"] += 1
        _log.warning("pipeline_decay_alerts.read_failed", exc_info=True)
        return

    summary["pipelines_measured"] = len(health_rows)
    health_by_pipeline = {(h.organisation_id, h.pipeline_id): h for h in health_rows}

    try:
        open_states = await _open_states(redis_client)
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["state_unavailable"] = 1
        _log.warning("pipeline_decay_alerts.state_read_failed", exc_info=True)
        return

    # Open states first: a pipeline that has recovered (and therefore dropped
    # out of the window) must still be closed even when the measurement is at
    # its per-tick cap.
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for key, state in sorted(open_states.items()):
        if time.monotonic() - started >= budget_seconds:
            summary["budget_exhausted"] = True
            return
        parsed = _parse_state_key(key)
        if parsed is None:
            _log.warning("pipeline_decay_alerts.state_key_unparseable", extra={"key": key})
            continue
        org_id, pipeline_id = parsed
        seen.add((org_id, pipeline_id))
        action = await _apply(
            redis_client,
            notifier,
            org_id=org_id,
            pipeline_id=pipeline_id,
            health=health_by_pipeline.get((org_id, pipeline_id)),
            state=state,
            summary=summary,
            now=now,
        )
        _count_action(summary, action)

    for (org_id, pipeline_id), health in health_by_pipeline.items():
        if (org_id, pipeline_id) in seen:
            continue
        if time.monotonic() - started >= budget_seconds:
            summary["budget_exhausted"] = True
            return
        action = await _apply(
            redis_client,
            notifier,
            org_id=org_id,
            pipeline_id=pipeline_id,
            health=health,
            state=None,
            summary=summary,
            now=now,
        )
        _count_action(summary, action)


def _count_action(summary: dict[str, Any], action: str) -> None:
    if action == "notify":
        summary["alerts"] += 1
    elif action == "recover":
        summary["recoveries"] += 1
