"""CRUD for error tracking (error_events + error_groups).

All functions enforce org scoping via organisation_id filter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_group import ErrorGroup


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
    group = await get_error_group_by_fingerprint(session=session, org_id=org_id, fingerprint=fingerprint)
    if group is None:
        group = ErrorGroup(
            organisation_id=org_id,
            fingerprint=fingerprint,
            level_peak=level,
            sample_event_id=sample_event_id,
        )
        session.add(group)
    else:
        group.count = group.count + 1
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
    limit: int = 50,
    offset: int = 0,
) -> list[ErrorGroup]:
    q = select(ErrorGroup).where(ErrorGroup.organisation_id == org_id)

    if status is not None:
        q = q.where(ErrorGroup.status == status)
    if level is not None:
        q = q.where(ErrorGroup.level_peak == level)
    if source is not None:
        q = q.where(
            ErrorGroup.sample_event_id.isnot(None),
            ErrorGroup.sample_event_id.in_(
                select(ErrorEvent.id).where(
                    ErrorEvent.organisation_id == org_id,
                    ErrorEvent.source == source,
                )
            ),
        )

    q = q.order_by(ErrorGroup.last_seen.desc()).offset(offset).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())


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
    if assigned_to is not None:
        group.assigned_to = assigned_to

    await session.flush()
    return group
