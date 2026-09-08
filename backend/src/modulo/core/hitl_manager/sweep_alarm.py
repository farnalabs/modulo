"""Approve-sweep anomaly alarm (FAR-611).

On 2026-09-05 a single account claim+approved 22+ HITL gates across 5+
pipelines in ~80 seconds and nothing flagged it. This module runs the
detection on every committed approve decision:

* count that actor's committed HITL decisions (``hitl.output_delivered``
  approve events AND ``hitl.manual_delivery`` events — a manual delivery
  resumes the run past the gate with caller-supplied output, exactly the
  impact an approve has, so a sweep mixing the two must trip the same
  way) in the last ``SWEEP_WINDOW_SECONDS`` (org-scoped — the
  ``hitl_claims.account_id`` column suggested by the original plan is
  NULLed by ``_decide`` at decision time, so the audit chain is the only
  per-actor decision record; the count joins ``hitl_claims`` on the audit
  ``resource_id`` to resolve each decided gate's pipeline);
* when the count exceeds ``SWEEP_COUNT_THRESHOLD`` AND the decisions span
  more than one pipeline, emit the alarm: an audit event
  (``hitl_approve_sweep_suspected``) and a fire-and-forget webhook
  dispatch through the shared :class:`Notifier` — whose
  ``dispatch_event`` also creates the in-app admin notification itself
  (the same sibling pattern the ``hitl_overdue`` job uses, so the alarm
  never writes the notification row twice).

Cost note: the detection runs one aggregate SELECT per approve, filtered
by the org, the indexed ``event_type``/``account_id`` columns and a
60-second ``created_at`` window, then joins on the ``hitl_claims``
primary key. There is no composite index on
``(organisation_id, event_type, account_id, created_at)`` — a migration
was deliberately out of scope (FAR-611). At these volumes the
org-narrowed subset is tiny (approves are rare relative to other audit
traffic) and the planner can drive the join from the ``audit_events``
event_type/account_id indexes; if approve volume ever grows enough for
this query to show up in pg_stat_statements, add the composite index as
a follow-up.

Suppression: the alarm itself is rate-limited — at most ONE alarm per
(org, actor) per ``SWEEP_ALARM_COOLDOWN`` via a bounded in-process
marker dict checked BEFORE the detection query, so an ongoing sweep
never re-notifies on every approve. Per-process (not cross-worker) by
design: a multi-replica deployment may emit up to one alarm per replica
per hour, which is the correct envelope for an anomaly page. The marker
is bounded (drop-oldest beyond ``_ALARM_MARK_MAX_KEYS``) so it can never
grow unbounded.

Failure isolation: :func:`maybe_alarm_approve_sweep` is no-throw by
contract (only ``asyncio.CancelledError`` propagates) — a broken alarm
must never fail the human's decision. The detection SELECT and the
emission audit write each run inside a SAVEPOINT so a DB error on either
rolls back only that work, leaving the surrounding decision transaction
healthy. Callers (``HITLManager.approve`` / ``approve_with_modification``)
invoke it right after the decision's audit events are committed to the
session, so the just-made decision is visible to the detection query
within the same transaction.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.core.notifier import EVENT_HITL_APPROVE_SWEEP
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.hitl_claim import HitlClaim

_log = logging.getLogger(__name__)

# Detection window and thresholds. count must EXCEED SWEEP_COUNT_THRESHOLD
# (i.e. the 6th approve inside the window crosses the line) and the decisions
# must span more than one pipeline — a single run with many gates in a minute
# is unusual but not cross-pipeline.
SWEEP_WINDOW_SECONDS = 60
SWEEP_COUNT_THRESHOLD = 5
SWEEP_MIN_DISTINCT_PIPELINES = 2

# At most one alarm per (org, actor) per hour.
SWEEP_ALARM_COOLDOWN = timedelta(hours=1)

# Audit + notification event type (one literal, audit and notifier agree).
AUDIT_EVENT_TYPE = "hitl_approve_sweep_suspected"

# Decision event types counted by the detection query. One approve (plain or
# with modification) writes exactly one ``hitl.output_delivered`` audit event
# and one manual delivery writes exactly one ``hitl.manual_delivery`` event,
# so the count of these events == the count of committed approve/manual
# decisions (FAR-611 review fix: deliver_manual resumes the run past the gate
# with caller-supplied output — equal sweep signal to an approve).
_DECISION_EVENT_TYPES = ("hitl.output_delivered", "hitl.manual_delivery")
_RESOURCE_TYPE = "hitl_claim"

# Bounded in-process suppression marker: (org_id, actor_id) -> last alarm time.
_alarm_marks: dict[tuple[uuid.UUID, uuid.UUID], datetime] = {}
_ALARM_MARK_MAX_KEYS = 1024

# Strong references to in-flight webhook dispatch tasks (mirrors
# hitl_email_alerts._PENDING_DISPATCH_TASKS — an unreferenced task can be
# garbage-collected mid-flight).
_PENDING_SWEEP_TASKS: set[asyncio.Task[None]] = set()

__all__ = [
    "AUDIT_EVENT_TYPE",
    "SWEEP_ALARM_COOLDOWN",
    "SWEEP_COUNT_THRESHOLD",
    "SWEEP_MIN_DISTINCT_PIPELINES",
    "SWEEP_WINDOW_SECONDS",
    "count_recent_approves",
    "maybe_alarm_approve_sweep",
    "reset_alarm_state",
]


def reset_alarm_state() -> None:
    """Drop the suppression markers (tests and reconfiguration)."""
    _alarm_marks.clear()


def _suppressed(key: tuple[uuid.UUID, uuid.UUID], now: datetime) -> bool:
    """Whether the alarm for *key* is still inside the cooldown window."""
    last = _alarm_marks.get(key)
    return last is not None and now - last < SWEEP_ALARM_COOLDOWN


def _mark_alarmed(key: tuple[uuid.UUID, uuid.UUID], now: datetime) -> None:
    """Stamp the marker; bounded — drop-oldest on overflow (no sweeper)."""
    if len(_alarm_marks) >= _ALARM_MARK_MAX_KEYS and key not in _alarm_marks:
        oldest = next(iter(_alarm_marks))
        del _alarm_marks[oldest]
    _alarm_marks[key] = now


async def count_recent_approves(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    window_start: datetime,
) -> tuple[int, int]:
    """Count the actor's committed HITL decisions since *window_start* (org-scoped).

    Counts BOTH decision surfaces — ``hitl.output_delivered`` (approve) and
    ``hitl.manual_delivery`` — so a sweep mixing approves and manual
    deliveries trips the same aggregate threshold. Returns
    ``(decision_count, distinct_pipeline_count)``. The count reads the
    audit chain (the only per-actor decision record —
    ``hitl_claims.account_id`` is NULLed at decision time) joined to
    ``hitl_claims`` on the audit ``resource_id`` so each decided gate's
    pipeline is resolved without a second query.
    """
    stmt = (
        select(func.count(), func.count(func.distinct(HitlClaim.pipeline_id)))
        .select_from(AuditEvent)
        .join(
            HitlClaim,
            (AuditEvent.resource_id == HitlClaim.id)
            & (AuditEvent.resource_type == _RESOURCE_TYPE)
            & (AuditEvent.organisation_id == HitlClaim.organisation_id),
        )
        .where(
            AuditEvent.organisation_id == org_id,
            AuditEvent.event_type.in_(_DECISION_EVENT_TYPES),
            AuditEvent.account_id == actor_id,
            AuditEvent.created_at.is_not(None),
            AuditEvent.created_at >= window_start,
        )
    )
    row = (await session.execute(stmt)).one()
    return int(row[0]), int(row[1])


def _alarm_payload(
    *,
    actor_id: uuid.UUID,
    approve_count: int,
    distinct_pipelines: int,
) -> dict[str, Any]:
    return {
        "actor": str(actor_id),
        "approve_count": approve_count,
        "distinct_pipeline_count": distinct_pipelines,
        "window_seconds": SWEEP_WINDOW_SECONDS,
    }


def _schedule_sweep_webhook(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Fire-and-forget the webhook dispatch (hitl_overdue dispatch pattern).

    The webhook dispatch does its own DB + HTTP work, so it must never run
    inside the decision transaction; it ALSO creates the in-app admin
    notification (``dispatch_event`` → ``create_from_event``), so the alarm
    itself writes no notification row — writing one here too produced a
    duplicate per alarm (FAR-611 review fix). Like ``hitl_email_alerts`` the
    task is scheduled on the running loop with a strong reference retained
    until it completes; it opens its OWN session on the shared engine. Never
    raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning("hitl.sweep_alarm.no_running_loop")
        return
    task = loop.create_task(_run_sweep_webhook(org_id, payload), name=f"hitl-approve-sweep-{org_id}")
    _PENDING_SWEEP_TASKS.add(task)
    task.add_done_callback(_PENDING_SWEEP_TASKS.discard)


async def _run_sweep_webhook(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Background task body: dispatch the webhook via a Notifier on the shared engine.

    Never raises — a broken webhook path must never surface anywhere.
    """
    try:
        from modulo.core.notifier import Notifier
        from modulo.db.session import get_shared_engine
        from modulo.settings import get_settings

        notifier = Notifier(get_shared_engine(), get_settings().fernet_key)
        await notifier.dispatch_event(org_id=org_id, event_type=EVENT_HITL_APPROVE_SWEEP, payload=payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            "hitl.sweep_alarm.webhook_dispatch_failed: %s",
            exc,
            extra={"org_id": str(org_id), "event_type": EVENT_HITL_APPROVE_SWEEP},
        )


async def maybe_alarm_approve_sweep(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    gate: HitlClaim,
    now: datetime | None = None,
) -> bool:
    """Detect and alarm an approve sweep for *actor_id*. Returns True when alarmed.

    Runs after a decision (approve plain/with-modification, or a manual
    delivery once the manager wires it) has committed its decision audit
    event on *session*, so the detection count includes the just-made
    decision. The count covers ``hitl.output_delivered`` AND
    ``hitl.manual_delivery`` events. No-throw by contract: every failure
    is logged and swallowed so the decision itself is never affected
    (``asyncio.CancelledError`` still propagates).

    The cooldown marker is checked BEFORE the detection query, so a suppressed
    actor pays no query cost on subsequent approves during an ongoing sweep.
    """
    if actor_id is None:
        return False
    try:
        at = now or datetime.now(UTC)
        key = (org_id, actor_id)
        if _suppressed(key, at):
            return False

        window_start = at - timedelta(seconds=SWEEP_WINDOW_SECONDS)
        # The detection SELECT runs in its OWN savepoint: a DB-level failure
        # on the query would otherwise abort the surrounding transaction
        # (Postgres 25P02: every subsequent statement — including the
        # decision's commit — fails once the transaction is aborted). The
        # savepoint rolls back only the detection; the approve proceeds, no
        # alarm is raised, and no cooldown marker is armed.
        async with session.begin_nested():
            decision_count, distinct_pipelines = await count_recent_approves(
                session, org_id=org_id, actor_id=actor_id, window_start=window_start
            )
        if decision_count <= SWEEP_COUNT_THRESHOLD or distinct_pipelines < SWEEP_MIN_DISTINCT_PIPELINES:
            return False

        payload = _alarm_payload(
            actor_id=actor_id,
            approve_count=decision_count,
            distinct_pipelines=distinct_pipelines,
        )
        # The marker is stamped only AFTER a successful emission: a transient
        # audit failure must never suppress the next real crossing for an
        # hour. Concurrent decisions may then double-emit — two alarms on a
        # real sweep beats zero alarms on a transient blip.
        #
        # The emission audit write runs inside a SAVEPOINT so a DB error on
        # it rolls back only the emission, leaving the surrounding decision
        # transaction healthy; the cooldown marker (below) is not stamped, so
        # the next real crossing re-emits. The in-app admin notification is
        # NOT written here — the fire-and-forget webhook dispatch
        # (``dispatch_event``) creates it in its own transaction, exactly the
        # ``hitl_overdue`` sibling pattern; a direct write here duplicated
        # every notification.
        async with session.begin_nested():
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=AUDIT_EVENT_TYPE,
                actor_user_id=actor_id,
                resource_type=_RESOURCE_TYPE,
                resource_id=gate.id,
                payload_json=payload,
            )
        _mark_alarmed(key, at)
        _schedule_sweep_webhook(org_id, payload)
        _log.warning(
            "hitl.sweep_alarm.suspected",
            extra={
                "org_id": str(org_id),
                "actor": str(actor_id),
                "approve_count": decision_count,
                "distinct_pipeline_count": distinct_pipelines,
                "window_seconds": SWEEP_WINDOW_SECONDS,
            },
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            "hitl.sweep_alarm.detection_failed: %s",
            exc,
            extra={"org_id": str(org_id), "actor": str(actor_id) if actor_id else None},
        )
        return False
