"""CRUD for Trigger records."""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.team_scope import team_scope_clause
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.trigger import Trigger


async def list_triggers(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = 20,
    team_id: uuid.UUID | None = None,
) -> PageResult[Trigger]:
    q = (
        select(Trigger)
        .join(Pipeline, Trigger.pipeline_id == Pipeline.id)
        .where(
            Trigger.organisation_id == org_id,
            Pipeline.deleted_at.is_(None),
            Trigger.deleted_at.is_(None),
        )
    )
    if pipeline_id is not None:
        q = q.where(Trigger.pipeline_id == pipeline_id)
    if team_id is not None:
        # A team-scoped caller sees triggers for its own team's pipelines plus
        # org-level pipelines (no owner team) — the same boundary the MCP
        # guard applies.
        q = q.where(team_scope_clause(Pipeline.owner_team_id, team_id))
    q = q.order_by(Trigger.created_at.desc())

    if cursor is not None:
        paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
        cp = await paginator.paginate(
            session,
            q,
            cursor=cursor,
            limit=limit,
            model=Trigger,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=1,
            page_size=limit,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    result = await session.execute(q)
    items = list(result.scalars().all())
    total = len(items)
    return PageResult(items=items, total=total, page=1, page_size=limit, has_more=False)


async def soft_delete_trigger(
    session: AsyncSession,
    trigger_id: uuid.UUID,
) -> Trigger | None:
    """Mark a trigger as deleted (soft delete). Returns None if not found or already deleted."""
    result = await session.execute(
        update(Trigger)
        .where(Trigger.id == trigger_id, Trigger.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .returning(Trigger)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_trigger(
    session: AsyncSession,
    trigger_id: uuid.UUID,
) -> Trigger | None:
    """Restore a soft-deleted trigger. Returns None if not found."""
    result = await session.execute(
        update(Trigger)
        .where(Trigger.id == trigger_id, Trigger.deleted_at.is_not(None))
        .values(deleted_at=None)
        .returning(Trigger)
    )
    await session.flush()
    return result.scalar_one_or_none()
