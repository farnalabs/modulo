"""Read helper for the durable, org-independent ``system_audit_events`` ledger.

FAR-1517 made org-lifecycle records survive a hard delete; FAR-1538 gives a
system admin a read surface for them. The table is deliberately OUTSIDE the
RLS regime (no ``organisation_id`` column, no FK to ``organisations``), so this
helper never sets an RLS org context and never scopes a query by the caller's
org: the whole point of the ledger is that an operator can still read what
happened to an organisation long after that organisation is gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_audit_event import SystemAuditEvent

__all__ = [
    "LIST_DEFAULT_PAGE_SIZE",
    "LIST_MAX_PAGE_SIZE",
    "list_system_audit_events",
]

LIST_DEFAULT_PAGE_SIZE = 50
LIST_MAX_PAGE_SIZE = 200


def _apply_filters(
    query: Select[Any],
    *,
    event_type: str | None = None,
    org_id: uuid.UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> Select[Any]:
    """Attach the optional typed filters. Absent filter means "no restriction"."""
    if event_type:
        query = query.where(SystemAuditEvent.event_type == event_type)
    if org_id is not None:
        query = query.where(SystemAuditEvent.org_id == org_id)
    if from_date is not None:
        query = query.where(SystemAuditEvent.created_at >= from_date)
    if to_date is not None:
        query = query.where(SystemAuditEvent.created_at <= to_date)
    return query


async def list_system_audit_events(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = LIST_DEFAULT_PAGE_SIZE,
    event_type: str | None = None,
    org_id: uuid.UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> tuple[list[SystemAuditEvent], int]:
    """One offset-paginated page of durable org-lifecycle records, plus a total.

    ``page`` is 1-based and clamped to ``>= 1``; ``page_size`` is clamped to
    ``[1, LIST_MAX_PAGE_SIZE]``. Rows are ordered ``created_at DESC, id DESC``
    so the newest lifecycle record comes first and ties are broken stably by
    the primary key.

    Offset pagination is the right trade here: these records are rare (org
    deletion request/confirm/cancel and friends), so the ledger stays small,
    and a page number is far simpler for a human-facing admin table than an
    opaque keyset cursor. The caller runs this inside its own transaction
    (the DI session factory is ``autobegin=False``).
    """
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, LIST_MAX_PAGE_SIZE))

    filtered = _apply_filters(
        select(SystemAuditEvent),
        event_type=event_type,
        org_id=org_id,
        from_date=from_date,
        to_date=to_date,
    )

    count_result = await session.execute(select(func.count()).select_from(filtered.subquery()))
    total = count_result.scalar_one_or_none() or 0

    stmt = (
        filtered.order_by(SystemAuditEvent.created_at.desc(), SystemAuditEvent.id.desc())
        .offset((safe_page - 1) * safe_page_size)
        .limit(safe_page_size)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all()), total
