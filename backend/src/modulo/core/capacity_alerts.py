"""Capacity alerting sweep (FAR-1344).

An operator learns that capacity is saturating **from a notification, before
work starts failing**, and can act without reading logs. This module owns the
alert decision only — the numbers come from FAR-1343's capacity reader
(``core/analytics/capacity.py``), which is the single source of truth for
pending depth, oldest-pending age, the binding limit and the stall/leak signal.
The per-pipeline trend is read from that report's own interval series (depth +
oldest-pending age per bucket) rather than re-derived from the run table.

Three alert families, all dispatched through the shared ``Notifier`` (so the
org/team webhook subscriptions and the per-category in-app opt-out both apply):

* **queue pressure** — an escalating ladder per pipeline. Notice at a low
  watermark (a queue is building), warning when the oldest pending run crosses
  a wait duration, critical when the queue is still climbing while its head
  ages (nothing draining).
* **backpressure engaged** — the FAR-604 dispatch gate is already refusing new
  fires for a pipeline. Evaluated with the canonical ``evaluate_backpressure``
  rather than re-derived, so the alert and the gate can never disagree.
* **capacity saturation** — an ORG-level limit (org run concurrency or org
  sandbox concurrency) has been fully held, with work queued, for longer than
  the sustain threshold.

The org layer is **signal + alert, never a refusal**: nothing here gates
admission. The alert fires whether or not ``queue_timeout`` is configured — it
is the escalation path for the queue-forever default.

Noise control (the point of this module):

* **One open alert per subject.** A subject is a pipeline (queue /
  backpressure) or the org (saturation). The open alert's high-water rung is
  held in Redis with a TTL, and a per-org Redis SET tracks which subjects are
  currently open so a subject that disappears from the report is cleared rather
  than left open forever.
* **Re-notify only on escalation.** A rung at or below the stored one is
  suppressed — a climbing queue emits one notice, one warning, one critical,
  not one per tick.
* **Auto-clear when draining resumes.** The rung falls to 0, the state is
  deleted, and (when the cleared alert was warning-or-worse) a
  ``capacity_recovered`` event closes the loop. A cleared NOTICE is retired
  silently — it is the lowest rung and its closure carries no action.

Failure posture: the sweep never raises (it runs inside ``dispatcher_reconcile``,
whose other sweeps must not be broken by it). If the Redis state store is
unavailable the sweep **skips dispatch** rather than risk storming the notifier
— dedup is the noise guard, so without it the safe direction is silence, and it
is reported (WARNING log + a ``state_unavailable`` count in the summary) rather
than swallowed.

The ``factory`` this sweep is given must be a **system-capable** session factory
(the ``modulo_system`` role): the org enumeration reads ``runs`` across every
tenant, which an org-RLS-scoped app role would silently reduce to zero rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.analytics.capacity import CapacityParams, read_capacity_report
from modulo.core.notifier import (
    EVENT_CAPACITY_BACKPRESSURE,
    EVENT_CAPACITY_RECOVERED,
    EVENT_CAPACITY_SATURATION,
    EVENT_QUEUE_PRESSURE_CRITICAL,
    EVENT_QUEUE_PRESSURE_NOTICE,
    EVENT_QUEUE_PRESSURE_WARNING,
)
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

__all__ = [
    "BACKPRESSURE_RUNG",
    "CRITICAL_RUNG",
    "NOTICE_RUNG",
    "SATURATION_RUNG",
    "WARNING_RUNG",
    "build_candidates",
    "evaluate_capacity_alerts",
    "pipeline_trend",
    "queue_pressure_rung",
    "resolve_alert_action",
    "saturation_rung",
]

# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

# Rungs are a total order so escalation is an integer comparison. 0 = clear.
CLEAR_RUNG = 0
NOTICE_RUNG = 1
WARNING_RUNG = 2
CRITICAL_RUNG = 3

# Subject kinds — the alert's identity for dedup. One open alert per subject.
_SUBJECT_QUEUE = "queue"
_SUBJECT_BACKPRESSURE = "backpressure"
_SUBJECT_SATURATION = "saturation"

# Rung → event type. The in-app severity of each rung comes from the EVENT's
# own render config (event_mapper), which is why the rungs are distinct events.
_RUNG_EVENTS: dict[int, str] = {
    NOTICE_RUNG: EVENT_QUEUE_PRESSURE_NOTICE,
    WARNING_RUNG: EVENT_QUEUE_PRESSURE_WARNING,
    CRITICAL_RUNG: EVENT_QUEUE_PRESSURE_CRITICAL,
}
_RUNG_LEVELS: dict[int, str] = {NOTICE_RUNG: "notice", WARNING_RUNG: "warning", CRITICAL_RUNG: "critical"}

# Backpressure and saturation reuse the ladder's rung integers so the state
# machine is single-sourced: both are a WARNING-class condition (rung 2).
BACKPRESSURE_RUNG = WARNING_RUNG
SATURATION_RUNG = WARNING_RUNG

# Critical additionally requires a real queue, not a single stuck run.
CRITICAL_MIN_DEPTH = 3
# Warning fires once the oldest pending run has waited this long.
WARNING_OLDEST_PENDING_SECONDS = 600
# A notice requires at least this much depth (the low watermark).
NOTICE_MIN_DEPTH = 1
# Saturation must be continuously held this long before it alerts — a momentary
# full cap during a healthy burst is not saturation.
SATURATION_SUSTAIN_SECONDS = 300

# Sweep bounds. The sweep shares the 60s ``dispatcher_reconcile`` slot with the
# other compensating sweeps, so it is wall-clock bounded and per-org bounded.
_DEFAULT_BUDGET_SECONDS = 45.0
# Slack above the graceful per-org budget before the hard ceiling cancels the
# sweep: enough for the in-flight org read to finish on a merely-slow DB, not
# enough to overrun the next 60s reconcile tick.
_HARD_TIMEOUT_SLACK_SECONDS = 15.0
MAX_ORGS_PER_TICK = 200
# Pipelines whose queue is checked against the canonical backpressure gate.
MAX_BACKPRESSURE_CHECKS_PER_ORG = 25
# Bound on one notifier dispatch (a webhook endpoint can retry with backoff).
_NOTIFY_TIMEOUT_SECONDS = 10.0

# Redis state: one JSON blob per subject, holding the high-water rung, the label
# and when it was first reached. The TTL is the hard backstop — even if the
# auto-clear path is never reached (e.g. the subject disappears while the sweep
# is down), the alert cannot stay open forever.
_STATE_KEY_PREFIX = "modulo:capacity_alert"
_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
# Separate marker for the saturation sustain clock: it must survive while the
# condition is merely "held but not yet sustained", whereas the alert state
# itself must not exist until the alert fires.
_SATURATION_MARKER = "saturation_marker"


@dataclass(frozen=True)
class Candidate:
    """One subject's current evaluation: its rung and what to send if it fires."""

    subject: str
    rung: int
    event_type: str | None
    payload: dict[str, Any] = field(default_factory=dict)


def pipeline_trend(report: dict[str, Any], pipeline_id: Any) -> tuple[int | None, float | None]:
    """Per-pipeline ``(depth_change, oldest_age_change)`` from the reader series.

    The FAR-1343 report's ``series`` carries per-bucket ``by_pipeline`` rows
    (depth + oldest-pending age). A pipeline absent from a bucket has depth 0
    there (the reader omits empty rows), so the first→last delta of each series
    is the pipeline's growth over the window — the "still climbing?" signal the
    ladder needs, measured per pipeline rather than org-wide.

    Returns ``(None, None)`` when the report has fewer than two buckets, so the
    caller falls back to the org-level signal rather than guessing.
    """
    depths: list[int] = []
    ages: list[float] = []
    for bucket in report.get("series") or []:
        row = _bucket_row(bucket, pipeline_id)
        if row is None:
            depths.append(0)
            ages.append(0.0)
        else:
            depths.append(int(row.get("pending_depth") or 0))
            ages.append(float(row.get("oldest_pending_age_seconds") or 0.0))
    if len(depths) < 2:
        return None, None
    return depths[-1] - depths[0], ages[-1] - ages[0]


def _bucket_row(bucket: dict[str, Any], pipeline_id: Any) -> dict[str, Any] | None:
    for row in bucket.get("by_pipeline") or []:
        if str(row.get("pipeline_id")) == str(pipeline_id):
            found: dict[str, Any] = row
            return found
    return None


def queue_pressure_rung(
    *,
    depth: int,
    oldest_pending_age_seconds: float | None,
    depth_change: int | None,
    oldest_age_change: float | None,
    drain_ratio: float | None,
    stall_suspected: bool,
) -> int:
    """The queue-pressure rung for one pipeline.

    Escalation order (highest wins):

    * **critical** — the reader's org-level stall/leak signature fired (the org
      cap is held by *executing* runs with ~zero completions), OR the pipeline
      still has ``CRITICAL_MIN_DEPTH`` or more queued with a genuine NO-DRAIN
      signal: FAR-1343's drain rate is zero (runs were created in the window
      and none started) and the queue is not shrinking. The window deltas are
      deliberately NOT used here: ``depth_change``/``oldest_age_change`` are
      first-bucket-vs-last-bucket deltas, so both are positive for ANY burst
      that appeared inside the window — even a healthy one whose runs start
      immediately — and a merely busy queue must never page as critical.
    * **warning** — the oldest pending run has waited at least
      ``WARNING_OLDEST_PENDING_SECONDS``.
    * **notice** — a queue exists and is growing (depth or head age rising).
    * **clear** — nothing queued, or the queue is draining.

    ``drain_ratio`` is the rate the FAR-1343 report publishes as
    ``drain.ratio`` (started/created over the window); ``None`` means there is
    no data to establish a no-drain verdict, so critical is not claimed — the
    warning rung (oldest age) still covers a stuck backlog.

    The notice rung is deliberately per-EPISODE, not forever-deduped: while a
    queue keeps building the stored state holds at the same rung, so one notice
    is emitted per build-up, never per tick. When the queue drains the notice
    state is retired silently (its closure carries no action), so the next
    build-up emits a fresh notice. Suppressing that repeat would make a
    genuinely new build-up invisible; notice is the lowest severity and one
    event per episode is the intended low-watermark signal.
    """
    if depth < NOTICE_MIN_DEPTH:
        return CLEAR_RUNG
    if stall_suspected and depth >= CRITICAL_MIN_DEPTH:
        return CRITICAL_RUNG
    if depth >= CRITICAL_MIN_DEPTH and drain_ratio is not None and drain_ratio <= 0 and (depth_change or 0) >= 0:
        return CRITICAL_RUNG
    if oldest_pending_age_seconds is not None and float(oldest_pending_age_seconds) >= WARNING_OLDEST_PENDING_SECONDS:
        return WARNING_RUNG
    if (depth_change or 0) > 0 or (oldest_age_change or 0) > 0:
        return NOTICE_RUNG
    return CLEAR_RUNG


def _org_limit_fully_held(report: dict[str, Any]) -> dict[str, Any] | None:
    """The org-level binding limit when it is fully held, else ``None``.

    Only the ORG-wide scopes count as saturation — ``pipeline`` is the queue
    ladder's territory and ``none`` means nothing binds.
    """
    binding = report.get("binding") or {}
    scope = binding.get("scope")
    if scope not in ("org_run", "sandbox"):
        return None
    if binding.get("headroom") not in (0, None):
        return None
    return binding


def saturation_rung(report: dict[str, Any], *, bound_since: datetime | None, now: datetime) -> int:
    """The saturation rung for an org whose ORG-level limit is fully held.

    Returns ``CLEAR_RUNG`` while the condition has not yet been *continuously*
    held for ``SATURATION_SUSTAIN_SECONDS`` — the caller keeps a separate
    sustain marker so the clock starts at first observation.
    """
    binding = _org_limit_fully_held(report)
    if binding is None:
        return CLEAR_RUNG
    # Work must actually be blocked on it; a fully-held cap with an empty queue
    # is a leak/stall problem, not a saturation alert (that is run_stalled's
    # territory, and alerting here would page on a healthy idle instance).
    if int(binding.get("pending_blocked") or 0) <= 0 and int((report.get("pending") or {}).get("total") or 0) <= 0:
        return CLEAR_RUNG
    if bound_since is None or (now - bound_since).total_seconds() < SATURATION_SUSTAIN_SECONDS:
        return CLEAR_RUNG
    return SATURATION_RUNG


def resolve_alert_action(prev_rung: int | None, rung: int) -> tuple[str, int | None]:
    """The dedup / escalation / auto-clear decision for one subject.

    Returns ``(action, stored_rung)`` where *action* is one of:

    * ``"notify"`` — the rung is new or has escalated; dispatch and store it.
    * ``"recover"`` — an open warning-or-worse alert cleared; dispatch the
      closure and drop the state.
    * ``"hold"`` — nothing to do (no alert, or the rung is at/below the stored
      high-water mark). *stored_rung* is what to persist (``None`` = delete).

    The high-water mark is deliberately NOT lowered on a dip: once an operator
    has been told "critical", silence until recovery is the quiet behaviour, and
    a later re-climb re-notifies from notice once the state has cleared.
    """
    if rung == CLEAR_RUNG:
        if prev_rung is None:
            return "hold", None
        if prev_rung >= WARNING_RUNG:
            return "recover", None
        # A cleared notice is retired silently: its closure carries no action.
        return "hold", None
    if prev_rung is None or rung > prev_rung:
        return "notify", rung
    return "hold", prev_rung


# ---------------------------------------------------------------------------
# Redis state
# ---------------------------------------------------------------------------


def _state_key(org_id: uuid.UUID, subject: str) -> str:
    return f"{_STATE_KEY_PREFIX}:{org_id}:{subject}"


def _open_key(org_id: uuid.UUID) -> str:
    return f"{_STATE_KEY_PREFIX}:{org_id}:open"


def _subject_for(kind: str, *, pipeline_id: uuid.UUID | None = None) -> str:
    if kind == _SUBJECT_SATURATION:
        return _SUBJECT_SATURATION
    return f"{kind}:{pipeline_id}"


def _decode(raw: Any) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


async def _read_state(redis_client: AsyncRedis, key: str) -> dict[str, Any] | None:
    raw = await redis_client.get(key)
    if raw is None:
        return None
    try:
        parsed = json.loads(_decode(raw))
    except (json.JSONDecodeError, TypeError):
        # A corrupt state blob is treated as absent so the subject can re-alert
        # (never as "an alert is open", which would silence it forever).
        _log.warning("capacity_alerts.state_unreadable key=%s", key)
        return None
    return parsed if isinstance(parsed, dict) else None


async def _write_state(redis_client: AsyncRedis, key: str, *, rung: int, label: str, now: datetime) -> None:
    body = json.dumps({"rung": rung, "label": label, "at": now.isoformat()})
    await redis_client.set(key, body, ex=_STATE_TTL_SECONDS)


async def _delete_state(redis_client: AsyncRedis, key: str) -> None:
    await redis_client.delete(key)


def _state_rung(state: dict[str, Any] | None) -> int | None:
    if state is None:
        return None
    try:
        return int(state.get("rung"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _state_at(state: dict[str, Any] | None) -> datetime | None:
    if state is None:
        return None
    raw = state.get("at")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _state_label(state: dict[str, Any] | None, fallback: str) -> str:
    if state is not None:
        label = state.get("label")
        if isinstance(label, str) and label:
            return label
    return fallback


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

_LIMIT_LABELS: dict[str, str] = {
    "pipeline": "pipeline concurrency limit",
    "org_run": "org run concurrency limit",
    "sandbox": "org sandbox concurrency limit",
    "none": "queue drain rate",
}

_SUGGESTED_ACTIONS: dict[str, str] = {
    "pipeline": "Raise the pipeline's max concurrent runs, or investigate the runs holding its slots.",
    "org_run": "Raise the org run concurrency limit, or investigate the runs holding org slots.",
    "sandbox": "Raise the org sandbox concurrency limit, or free the runner slots in use.",
    "none": "Investigate the oldest queued run, and which pipelines are holding capacity.",
}


def _limit_label(scope: str | None) -> str:
    return _LIMIT_LABELS.get(scope or "none", "queue drain rate")


def _suggested_action(scope: str | None) -> str:
    return _SUGGESTED_ACTIONS.get(scope or "none", _SUGGESTED_ACTIONS["none"])


def _oldest_minutes(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return str(int(float(value) // 60))
    except (TypeError, ValueError):
        return "n/a"


def _binding_display(value: Any) -> str:
    """Render one binding number for a notification body.

    ``None`` is the normal state while a queue builds and no cap is held
    (``scope: "none"``), so it must read as "not applicable" — never as the
    Python literal ``None`` — matching the neighbouring ``_oldest_minutes``
    convention.
    """
    return "n/a" if value is None else str(value)


def _binding_fields(report: dict[str, Any]) -> dict[str, Any]:
    binding = report.get("binding") or {}
    scope = binding.get("scope")
    return {
        "limit_label": _limit_label(scope),
        "limit_value": _binding_display(binding.get("limit")),
        "current_value": _binding_display(binding.get("in_use")),
        "suggested_action": _suggested_action(scope),
        "binding_scope": scope,
        "binding_limit": binding.get("limit"),
        "binding_in_use": binding.get("in_use"),
        "binding_headroom": binding.get("headroom"),
    }


def _queue_payload(
    *,
    pipeline: dict[str, Any],
    report: dict[str, Any],
    rung: int,
    trend: tuple[int | None, float | None],
) -> dict[str, Any]:
    """Payload for one queue-pressure rung.

    Every placeholder used by that rung's body template is present, so the
    render never falls back to ``[unknown]``.
    """
    return {
        "pipeline_id": pipeline.get("pipeline_id"),
        "pipeline_name": pipeline.get("pipeline_name") or pipeline.get("pipeline_id"),
        "pending_depth": int(pipeline.get("pending_count") or 0),
        "oldest_pending_age_seconds": pipeline.get("oldest_pending_age_seconds"),
        "oldest_pending_age_minutes": _oldest_minutes(pipeline.get("oldest_pending_age_seconds")),
        "depth_change": trend[0],
        "oldest_age_change_seconds": trend[1],
        "rung": _RUNG_LEVELS.get(rung, "notice"),
        **_binding_fields(report),
    }


def _backpressure_reason_label(reason: str) -> str:
    """Humanise the canonical reason token from ``evaluate_backpressure``.

    The token is a stable machine form (``queue_depth=8 limit=5`` /
    ``oldest_age=900s limit=3600s``); the label says what it means. An
    unrecognised token is passed through unchanged rather than described wrongly.
    """
    if reason.startswith("queue_depth"):
        return f"queue depth is over the pipeline's limit ({reason})"
    if reason.startswith("oldest_age"):
        return f"the oldest queued run has waited too long ({reason})"
    return reason or "backpressure"


def _backpressure_payload(*, pipeline: dict[str, Any], report: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "pipeline_id": pipeline.get("pipeline_id"),
        "pipeline_name": pipeline.get("pipeline_name") or pipeline.get("pipeline_id"),
        "pending_depth": int(pipeline.get("pending_count") or 0),
        "backpressure_reason": reason,
        "reason_label": _backpressure_reason_label(reason),
        **_binding_fields(report),
    }


def _saturation_payload(*, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "pending_depth": int((report.get("pending") or {}).get("total") or 0),
        "sustained_minutes": SATURATION_SUSTAIN_SECONDS // 60,
        **_binding_fields(report),
    }


def _recovered_payload(*, subject_label: str, previous_rung: int) -> dict[str, Any]:
    return {
        "subject_label": subject_label,
        "previous_level": _RUNG_LEVELS.get(previous_rung, "capacity"),
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def _build_notifier(settings: Settings) -> Any:
    """Build the shared Notifier (lazy imports keep the cron graph light)."""
    from modulo.core.cron_helpers import _get_engine
    from modulo.core.notifier import Notifier

    return Notifier(_get_engine(), settings.fernet_key)


async def _dispatch(
    notifier: Any,
    *,
    org_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    """Dispatch one alert, bounded and never raising into the sweep."""
    try:
        await asyncio.wait_for(notifier.dispatch_event(org_id, event_type, payload), timeout=_NOTIFY_TIMEOUT_SECONDS)
        summary["dispatched"] += 1
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        summary["notify_failed"] += 1
        _log.warning("capacity_alerts.notify_timeout event=%s org=%s", event_type, org_id)
    except Exception:
        summary["notify_failed"] += 1
        _log.warning("capacity_alerts.notify_failed event=%s org=%s", event_type, org_id, exc_info=True)


def _candidate_label(candidate: Candidate, subject: str) -> str:
    payload = candidate.payload
    for key in ("pipeline_name", "subject_label"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return subject


async def _apply_subject(
    redis_client: AsyncRedis,
    notifier: Any,
    *,
    org_id: uuid.UUID,
    candidate: Candidate,
    summary: dict[str, Any],
    now: datetime,
) -> str:
    """Run the dedup/escalation/auto-clear machine for one subject.

    Returns the action taken (``"notify"`` / ``"recover"`` / ``"hold"``).
    """
    key = _state_key(org_id, candidate.subject)
    state = await _read_state(redis_client, key)
    prev_rung = _state_rung(state)
    action, stored = resolve_alert_action(prev_rung, candidate.rung)

    if action == "notify" and candidate.event_type is not None:
        await _write_state(
            redis_client,
            key,
            rung=candidate.rung,
            label=_candidate_label(candidate, candidate.subject),
            now=now,
        )
        await _dispatch(
            notifier,
            org_id=org_id,
            event_type=candidate.event_type,
            payload=candidate.payload,
            summary=summary,
        )
        return "notify"
    if action == "recover":
        await _delete_state(redis_client, key)
        await _dispatch(
            notifier,
            org_id=org_id,
            event_type=EVENT_CAPACITY_RECOVERED,
            payload=_recovered_payload(
                subject_label=_state_label(state, candidate.subject),
                previous_rung=prev_rung if prev_rung is not None else WARNING_RUNG,
            ),
            summary=summary,
        )
        return "recover"
    if stored is None and prev_rung is not None:
        # A cleared notice — drop the state silently.
        await _delete_state(redis_client, key)
    return "hold"


async def _apply_org_candidates(
    redis_client: AsyncRedis,
    notifier: Any,
    *,
    org_id: uuid.UUID,
    candidates: dict[str, Candidate],
    summary: dict[str, Any],
    now: datetime,
    unverified_kinds: set[str] | None = None,
    unverified_subjects: set[str] | None = None,
) -> None:
    """Apply the state machine to this org's subjects and clear the absent ones.

    The per-org open SET is what makes auto-clear complete: a subject that was
    being alerted and has since vanished from the report (its queue drained, its
    backpressure lifted) is re-evaluated at rung 0 — which recovers a
    warning-or-worse alert — because it is never lost, only absent from the
    current candidate map.

    Absence means "evaluated clear" ONLY for a subject that was actually
    evaluated this tick. *unverified_kinds* names subject kinds whose whole
    evaluation failed (their absence is UNKNOWN, not clear) and
    *unverified_subjects* names individual subjects that were not evaluated
    (e.g. a backpressure subject truncated out by the per-org check cap). Those
    are skipped rather than auto-cleared, so a failed or truncated read can
    never emit a spurious ``capacity_recovered`` followed by a re-alert.
    """
    open_key = _open_key(org_id)
    open_subjects = {_decode(raw) for raw in await redis_client.smembers(open_key)}
    current = set(candidates)

    for subject in sorted(open_subjects - current):
        kind = subject.split(":", 1)[0]
        if unverified_kinds and kind in unverified_kinds:
            continue
        if unverified_subjects and subject in unverified_subjects:
            continue
        action = await _apply_subject(
            redis_client,
            notifier,
            org_id=org_id,
            candidate=Candidate(subject=subject, rung=CLEAR_RUNG, event_type=None),
            summary=summary,
            now=now,
        )
        if action != "notify":
            await redis_client.srem(open_key, subject)

    for subject, candidate in sorted(candidates.items()):
        action = await _apply_subject(
            redis_client, notifier, org_id=org_id, candidate=candidate, summary=summary, now=now
        )
        if action == "notify":
            await redis_client.sadd(open_key, subject)
            await redis_client.expire(open_key, _STATE_TTL_SECONDS)
        elif action == "recover":
            await redis_client.srem(open_key, subject)


def build_candidates(
    report: dict[str, Any],
    *,
    bound_since: datetime | None,
    now: datetime,
) -> dict[str, Candidate]:
    """Every non-clear capacity candidate for one org, keyed by subject."""
    out: dict[str, Candidate] = {}

    for pipeline in (report.get("pending") or {}).get("by_pipeline") or []:
        pipeline_id = _pipeline_id_or_none(pipeline.get("pipeline_id"))
        if pipeline_id is None:
            continue
        trend = pipeline_trend(report, pipeline.get("pipeline_id"))
        rung = queue_pressure_rung(
            depth=int(pipeline.get("pending_count") or 0),
            oldest_pending_age_seconds=pipeline.get("oldest_pending_age_seconds"),
            depth_change=trend[0],
            oldest_age_change=trend[1],
            drain_ratio=(report.get("drain") or {}).get("ratio"),
            stall_suspected=bool((report.get("active_health") or {}).get("stall_suspected")),
        )
        if rung == CLEAR_RUNG:
            continue
        subject = _subject_for(_SUBJECT_QUEUE, pipeline_id=pipeline_id)
        out[subject] = Candidate(
            subject=subject,
            rung=rung,
            event_type=_RUNG_EVENTS.get(rung),
            payload=_queue_payload(pipeline=pipeline, report=report, rung=rung, trend=trend),
        )

    rung = saturation_rung(report, bound_since=bound_since, now=now)
    if rung != CLEAR_RUNG:
        out[_SUBJECT_SATURATION] = Candidate(
            subject=_SUBJECT_SATURATION,
            rung=rung,
            event_type=EVENT_CAPACITY_SATURATION,
            payload=_saturation_payload(report=report),
        )
    return out


async def _add_backpressure_candidates(
    factory: async_sessionmaker[AsyncSession],
    report: dict[str, Any],
    *,
    org_id: uuid.UUID,
    candidates: dict[str, Candidate],
) -> set[str]:
    """Add the canonical backpressure hits for one org to the candidate map.

    Returns the subjects that could NOT be evaluated this tick (queued in the
    report but beyond ``MAX_BACKPRESSURE_CHECKS_PER_ORG``). Their absence from
    the candidate map means "unknown", not "clear", so the caller must not
    auto-clear an alert on them.
    """
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        hits, unevaluated = await _backpressure_hits(session, report)
        for pipeline, reason in hits:
            pipeline_id = _pipeline_id_or_none(pipeline.get("pipeline_id"))
            if pipeline_id is None:
                continue
            subject = _subject_for(_SUBJECT_BACKPRESSURE, pipeline_id=pipeline_id)
            candidates[subject] = Candidate(
                subject=subject,
                rung=BACKPRESSURE_RUNG,
                event_type=EVENT_CAPACITY_BACKPRESSURE,
                payload=_backpressure_payload(pipeline=pipeline, report=report, reason=reason),
            )
    return unevaluated


async def _backpressure_hits(
    session: AsyncSession,
    report: dict[str, Any],
) -> tuple[list[tuple[dict[str, Any], str]], set[str]]:
    """Pipelines currently refused by the canonical backpressure gate.

    Uses ``evaluate_backpressure`` itself (never a re-derivation) so the alert
    and the gate can never disagree about which pipelines are backpressured.
    Bounded to the pipelines with the deepest queues.

    Returns ``(hits, unevaluated_subjects)``: a queued pipeline beyond the
    per-org check cap was never checked, so it is reported as unverified rather
    than silently treated as clear by the auto-clear pass.
    """
    from modulo.core.run_admission import evaluate_backpressure

    hits: list[tuple[dict[str, Any], str]] = []
    by_pipeline = (report.get("pending") or {}).get("by_pipeline") or []
    queued = [p for p in by_pipeline if int(p.get("pending_count") or 0) > 0]
    checked = queued[:MAX_BACKPRESSURE_CHECKS_PER_ORG]
    unevaluated: set[str] = set()
    for pipeline in queued[MAX_BACKPRESSURE_CHECKS_PER_ORG:]:
        pipeline_id = _pipeline_id_or_none(pipeline.get("pipeline_id"))
        if pipeline_id is not None:
            unevaluated.add(_subject_for(_SUBJECT_BACKPRESSURE, pipeline_id=pipeline_id))
    for pipeline in checked:
        pipeline_id = _pipeline_id_or_none(pipeline.get("pipeline_id"))
        if pipeline_id is None:
            continue
        backpressured, reason = await evaluate_backpressure(session, pipeline_id=pipeline_id)
        if backpressured:
            hits.append((pipeline, reason))
    return hits, unevaluated


def _pipeline_id_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _org_ids_with_pending(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Orgs that currently have an unstarted pending run.

    The sweep only has work to do where work is queued; an idle instance must
    cost one cheap read, not a full capacity scan per org. Requires a
    system-capable (BYPASSRLS) factory — an org-RLS-scoped app role would
    silently return zero rows here.
    """
    async with factory() as session, session.begin():
        result = await session.execute(
            select(Run.organisation_id)
            .where(Run.status == "pending", Run.cancellation_requested.is_(False))
            .group_by(Run.organisation_id)
            .limit(MAX_ORGS_PER_TICK)
        )
        return list(result.scalars())


def _org_from_open_key(raw: str) -> uuid.UUID | None:
    """Parse the org id out of a per-org open-subject SET key, else ``None``."""
    prefix = f"{_STATE_KEY_PREFIX}:"
    suffix = ":open"
    if not (raw.startswith(prefix) and raw.endswith(suffix)):
        return None
    try:
        return uuid.UUID(raw[len(prefix) : -len(suffix)])
    except (TypeError, ValueError):
        return None


async def _org_ids_with_open_alerts(redis_client: AsyncRedis) -> list[uuid.UUID]:
    """Orgs that currently hold at least one open capacity alert.

    The pending-run scan alone is not enough. An org whose queue fully drains
    (its last pending run starts or is cancelled) drops out of that scan, so
    with only that set the org is never evaluated again: ``_apply_org_candidates``
    never runs, the open subject SET is never consulted, and the alert state
    lives out the 7-day TTL. The operator never receives ``capacity_recovered``,
    and the stale high-water rung then suppresses a fresh identical incident
    at the same rung. Unioning in the orgs with an open SET is what lets the
    auto-clear path reach a fully-drained org.
    """
    found: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    async for raw in redis_client.scan_iter(match=f"{_STATE_KEY_PREFIX}:*:open"):
        org_id = _org_from_open_key(_decode(raw))
        if org_id is not None and org_id not in seen:
            seen.add(org_id)
            found.append(org_id)
    return found


async def _sweep_org(
    *,
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    settings: Settings,
    redis_client: AsyncRedis,
    notifier: Any,
    now: datetime,
    summary: dict[str, Any],
) -> None:
    """Evaluate one org's ladder and act on it. Isolated — never raises."""
    try:
        report = await read_capacity_report(
            org_id=org_id,
            params=CapacityParams(),
            factory=factory,
            settings=settings,
            now=now,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["org_errors"] += 1
        _log.warning("capacity_alerts.report_failed org=%s", org_id, exc_info=True)
        return

    summary["orgs_evaluated"] += 1

    # Saturation sustain clock: start/extend the marker while the org limit is
    # fully held, drop it when it frees. The alert state is separate, so a held
    # -but-not-yet-sustained condition never looks like an open alert.
    marker_key = _state_key(org_id, _SATURATION_MARKER)
    held = _org_limit_fully_held(report) is not None
    if held:
        if _state_at(await _read_state(redis_client, marker_key)) is None:
            await _write_state(redis_client, marker_key, rung=CLEAR_RUNG, label=_SUBJECT_SATURATION, now=now)
    else:
        await _delete_state(redis_client, marker_key)
    bound_since = _state_at(await _read_state(redis_client, marker_key))

    candidates = build_candidates(report, bound_since=bound_since, now=now)
    unverified_subjects: set[str] = set()
    unverified_kinds: set[str] = set()
    try:
        unverified_subjects = await _add_backpressure_candidates(factory, report, org_id=org_id, candidates=candidates)
    except asyncio.CancelledError:
        raise
    except Exception:
        summary["org_errors"] += 1
        _log.warning("capacity_alerts.backpressure_failed org=%s", org_id, exc_info=True)
        # The evaluation never ran, so EVERY open backpressure subject is
        # unverified this tick: absence from the candidate map must not be read
        # as "backpressure lifted" (that would emit a spurious recovery and
        # then re-alert on the next successful tick).
        unverified_kinds.add(_SUBJECT_BACKPRESSURE)

    await _apply_org_candidates(
        redis_client,
        notifier,
        org_id=org_id,
        candidates=candidates,
        summary=summary,
        now=now,
        unverified_kinds=unverified_kinds,
        unverified_subjects=unverified_subjects,
    )


async def _run_orgs(
    *,
    factory: async_sessionmaker[AsyncSession],
    org_ids: list[uuid.UUID],
    settings: Settings,
    redis_client: AsyncRedis,
    notifier: Any,
    now: datetime,
    summary: dict[str, Any],
    budget_seconds: float,
) -> None:
    """Evaluate every candidate org, checking the wall-clock budget between orgs."""
    started = time.monotonic()
    for org_id in org_ids:
        if time.monotonic() - started >= budget_seconds:
            summary["budget_exhausted"] = True
            _log.info("capacity_alerts.budget_exhausted evaluated=%d", summary["orgs_evaluated"])
            break
        try:
            await _sweep_org(
                factory=factory,
                org_id=org_id,
                settings=settings,
                redis_client=redis_client,
                notifier=notifier,
                now=now,
                summary=summary,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            summary["org_errors"] += 1
            _log.warning("capacity_alerts.org_sweep_failed org=%s", org_id, exc_info=True)


async def evaluate_capacity_alerts(
    *,
    factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis | None,
    settings: Settings | None = None,
    notifier: Any | None = None,
    now: datetime | None = None,
    org_ids: list[uuid.UUID] | None = None,
    budget_seconds: float = _DEFAULT_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Run the capacity alerting sweep (FAR-1344). NEVER raises.

    Wired into ``dispatcher_reconcile`` (60s), so the sweep is wall-clock
    bounded and per-org isolated: one org's failure is a WARNING and an error
    count, never an abort of the whole sweep (or of the reconcile tick).

    The budget is enforced in two places because either alone is insufficient:
    a per-org check between orgs (cheap, graceful) and a hard
    ``asyncio.wait_for`` ceiling on the whole run — a single org's report read
    can block on a slow DB, and the between-org check never fires while that is
    happening. The ceiling is what keeps a degraded DB from overrunning the
    reconcile tick.
    """
    summary: dict[str, Any] = {
        "orgs_considered": 0,
        "orgs_evaluated": 0,
        "dispatched": 0,
        "notify_failed": 0,
        "org_errors": 0,
        "state_unavailable": 0,
        "budget_exhausted": False,
    }
    if redis_client is None:
        # Dedup is the noise guard; without state we cannot guarantee one alert
        # per subject, so the safe direction is silence (reported, not silent).
        summary["state_unavailable"] = 1
        _log.warning("capacity_alerts.skipped_no_redis (dedup state unavailable)")
        return summary

    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    notifier = notifier or _build_notifier(settings)

    try:
        if org_ids is not None:
            candidates = list(org_ids)
        else:
            # Union two sets: orgs with queued work AND orgs holding an open
            # alert. The second is required for closure — a fully-drained org
            # has no pending runs but may still hold an open alert that must be
            # re-evaluated so the auto-clear path can fire. Open-alert orgs are
            # ordered first so the per-tick cap cannot starve a closure.
            merged: list[uuid.UUID] = []
            seen: set[uuid.UUID] = set()
            for org_id in [*await _org_ids_with_open_alerts(redis_client), *await _org_ids_with_pending(factory)]:
                if org_id not in seen:
                    seen.add(org_id)
                    merged.append(org_id)
            candidates = merged[:MAX_ORGS_PER_TICK]
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("capacity_alerts.org_scan_failed", exc_info=True)
        return summary

    summary["orgs_considered"] = len(candidates)
    try:
        await asyncio.wait_for(
            _run_orgs(
                factory=factory,
                org_ids=candidates,
                settings=settings,
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
            "capacity_alerts.hard_timeout evaluated=%d (bound=%ss)",
            summary["orgs_evaluated"],
            budget_seconds + _HARD_TIMEOUT_SLACK_SECONDS,
        )

    if summary["dispatched"] or summary["orgs_evaluated"]:
        _log.info(
            "capacity_alerts.sweep orgs=%d dispatched=%d failed=%d errors=%d",
            summary["orgs_evaluated"],
            summary["dispatched"],
            summary["notify_failed"],
            summary["org_errors"],
        )
    return summary
