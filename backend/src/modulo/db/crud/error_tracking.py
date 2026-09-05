"""CRUD for error tracking (error_events + error_groups).

All functions enforce org scoping via organisation_id filter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import CAPACITY_MARKERS
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.trigger_event import TriggerEvent


async def create_error_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    fingerprint: str,
    level: str,
    message: str,
    source: str,
    stacktrace: str | None = None,
    context_json: dict[str, Any] | None = None,
    environment: str | None = None,
    version: str | None = None,
) -> ErrorEvent:
    event = ErrorEvent(
        organisation_id=org_id,
        fingerprint=fingerprint,
        level=level,
        message=message,
        source=source,
        stacktrace=stacktrace,
        context_json=context_json,
        environment=environment,
        version=version,
    )
    session.add(event)
    await session.flush()
    return event


async def get_error_group_by_fingerprint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    fingerprint: str,
) -> ErrorGroup | None:
    result = await session.execute(
        select(ErrorGroup).where(
            ErrorGroup.organisation_id == org_id,
            ErrorGroup.fingerprint == fingerprint,
        )
    )
    return result.scalar_one_or_none()


async def upsert_error_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    fingerprint: str,
    level: str,
    sample_event_id: uuid.UUID | None = None,
) -> ErrorGroup:
    result = await session.execute(
        select(ErrorGroup)
        .where(
            ErrorGroup.organisation_id == org_id,
            ErrorGroup.fingerprint == fingerprint,
        )
        # ErrorGroup.sample_event is lazy="joined", so this SELECT emits a LEFT
        # OUTER JOIN to error_events. Unqualified FOR UPDATE would try to lock
        # the nullable (right) side of that outer join, which Postgres rejects
        # with FeatureNotSupportedError. Lock only the error_groups row.
        .with_for_update(of=ErrorGroup)
    )
    group = result.scalar_one_or_none()
    if group is None:
        group = ErrorGroup(
            organisation_id=org_id,
            fingerprint=fingerprint,
            level_peak=level,
            sample_event_id=sample_event_id,
        )
        session.add(group)
        await session.flush()
        group.count = 1
    else:
        group.count += 1
        group.last_seen = datetime.now(UTC)
        level_rank = {"warning": 0, "error": 1, "critical": 2}
        if level_rank.get(level, 0) > level_rank.get(group.level_peak, -1):
            group.level_peak = level
        if sample_event_id is not None:
            group.sample_event_id = sample_event_id
    await session.flush()
    return group


async def get_error_groups(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    status: str | None = None,
    level: str | None = None,
    source: str | None = None,
    environment: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ErrorGroup]:
    q = select(ErrorGroup).where(ErrorGroup.organisation_id == org_id)

    if status is not None:
        q = q.where(ErrorGroup.status == status)
    if level is not None:
        q = q.where(ErrorGroup.level_peak == level)

    has_event_filters = source is not None or environment is not None or search is not None
    if has_event_filters:
        event_sub = select(ErrorEvent.fingerprint).where(
            ErrorEvent.organisation_id == org_id,
        )
        if source is not None:
            event_sub = event_sub.where(ErrorEvent.source == source)
        if environment is not None:
            event_sub = event_sub.where(ErrorEvent.environment == environment)
        if search is not None:
            event_sub = event_sub.where(ErrorEvent.message.ilike(f"%{search}%"))
        q = q.where(ErrorGroup.fingerprint.in_(event_sub))

    q = q.order_by(ErrorGroup.last_seen.desc()).offset(offset).limit(limit)
    try:
        result = await session.execute(q)
        return list(result.scalars().all())
    except ProgrammingError:
        return []


async def count_error_groups(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    status: str | None = None,
    level: str | None = None,
    source: str | None = None,
    environment: str | None = None,
    search: str | None = None,
) -> int:
    q = select(func.count(ErrorGroup.id)).where(ErrorGroup.organisation_id == org_id)

    if status is not None:
        q = q.where(ErrorGroup.status == status)
    if level is not None:
        q = q.where(ErrorGroup.level_peak == level)

    has_event_filters = source is not None or environment is not None or search is not None
    if has_event_filters:
        event_sub = select(ErrorEvent.fingerprint).where(
            ErrorEvent.organisation_id == org_id,
        )
        if source is not None:
            event_sub = event_sub.where(ErrorEvent.source == source)
        if environment is not None:
            event_sub = event_sub.where(ErrorEvent.environment == environment)
        if search is not None:
            event_sub = event_sub.where(ErrorEvent.message.ilike(f"%{search}%"))
        q = q.where(ErrorGroup.fingerprint.in_(event_sub))

    try:
        result = await session.execute(q)
        return result.scalar_one() or 0
    except ProgrammingError:
        return 0


async def get_error_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group_id: uuid.UUID,
) -> ErrorGroup | None:
    result = await session.execute(
        select(ErrorGroup).where(
            ErrorGroup.organisation_id == org_id,
            ErrorGroup.id == group_id,
        )
    )
    return result.scalar_one_or_none()


async def get_error_events_by_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[ErrorEvent]:
    group = await get_error_group(session=session, org_id=org_id, group_id=group_id)
    if group is None:
        return []

    q = (
        select(ErrorEvent)
        .where(
            ErrorEvent.organisation_id == org_id,
            ErrorEvent.fingerprint == group.fingerprint,
        )
        .order_by(ErrorEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(q)
    return list(result.scalars().all())


async def count_error_events_by_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group_id: uuid.UUID,
) -> int:
    group = await get_error_group(session=session, org_id=org_id, group_id=group_id)
    if group is None:
        return 0

    q = select(func.count(ErrorEvent.id)).where(
        ErrorEvent.organisation_id == org_id,
        ErrorEvent.fingerprint == group.fingerprint,
    )
    result = await session.execute(q)
    return result.scalar_one() or 0


async def update_error_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    status: str | None = None,
    assigned_to: uuid.UUID | None = None,
) -> ErrorGroup:
    group = await get_error_group(session=session, org_id=org_id, group_id=group_id)
    if group is None:
        raise ValueError("ErrorGroup not found")

    if status is not None:
        group.status = status
        if status == "resolved":
            group.resolved_at = datetime.now(UTC)
            await _mark_group_events_resolved(session, org_id, group.fingerprint)
    if assigned_to is not None:
        group.assigned_to = assigned_to

    await session.flush()
    return group


async def _mark_group_events_resolved(
    session: AsyncSession,
    org_id: uuid.UUID,
    fingerprint: str,
) -> None:
    """Propagate a group resolution to its events.

    Events already in a terminal state (``resolved``/``archived``) keep their
    existing state; only ``new``/``acknowledged`` events are transitioned so the
    group's events reflect the same resolution timestamp.
    """
    now = datetime.now(UTC)
    await session.execute(
        update(ErrorEvent)
        .where(
            ErrorEvent.organisation_id == org_id,
            ErrorEvent.fingerprint == fingerprint,
            ErrorEvent.status.in_(("new", "acknowledged")),
        )
        .values(status="resolved", resolved_at=now)
    )


def scheduler_starvation_age_anchor() -> Any:
    """The per-run starvation age anchor expression (FAR-604).

    ``COALESCE(MIN(trigger_events.received_at), runs.created_at)`` — the run's
    EARLIEST trigger-event receipt, falling back to ``created_at`` when no
    trigger event exists (manual runs). NOT ``created_at`` alone: a capacity
    coalescing re-delivery refreshes the pending run's ``created_at`` on the
    dispatcher's short re-dispatch cadence, which would reset a
    ``created_at``-keyed age every cycle (a days-long wedge would read as
    minutes old and the starvation banner would flap). Trigger-event receipts
    are immutable per delivery, so ``MIN(received_at)`` is the true first-seen
    instant.

    Exposed as a module function so tests can pin the anchor's shape (the
    trigger-event join must not regress to a ``created_at``-only key).
    """
    earliest_receipt = select(func.min(TriggerEvent.received_at)).where(TriggerEvent.run_id == Run.id).scalar_subquery()
    return func.coalesce(earliest_receipt, Run.created_at)


async def get_scheduler_starvation_pipelines(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    threshold: datetime,
) -> list[Any]:
    """Aggregate the org's capacity-starved pipelines (FAR-604).

    A pipeline is starved when it has unstarted pending runs
    (``status='pending'``, ``started_at IS NULL``) carrying a capacity marker
    in ``error_code`` (the SAME canonical ``CAPACITY_MARKERS`` the capacity
    sweep / dispatcher reconcile key on) whose age anchor is older than
    *threshold*. The age anchor is the run's earliest trigger-event receipt
    (see :func:`scheduler_starvation_age_anchor`) — ``created_at`` alone is
    reset by coalescing re-deliveries. One row per starved pipeline (bounded
    by the org's pipeline count); rows carry ``(pipeline_id, pipeline_name,
    pending_count, oldest_anchor)``. The caller owns RLS pinning and error
    mapping.
    """
    anchor = scheduler_starvation_age_anchor()
    stmt = (
        select(
            Run.pipeline_id,
            Pipeline.name.label("pipeline_name"),
            func.count(Run.id).label("pending_count"),
            func.min(anchor).label("oldest_created_at"),
        )
        .outerjoin(Pipeline, Pipeline.id == Run.pipeline_id)
        .where(
            Run.organisation_id == org_id,
            Run.status == "pending",
            Run.started_at.is_(None),
            Run.error_code.in_(CAPACITY_MARKERS),
            anchor < threshold,
        )
        .group_by(Run.pipeline_id, Pipeline.name)
        .order_by(func.min(anchor).asc())
    )
    rows = (await session.execute(stmt)).all()
    return list(rows)
