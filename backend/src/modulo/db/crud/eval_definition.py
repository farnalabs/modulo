"""CRUD for EvalDefinition records."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.eval_definition import EvalDefinition


async def list_eval_definitions(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> PageResult[EvalDefinition]:
    q = select(EvalDefinition).where(EvalDefinition.organisation_id == org_id)
    if pipeline_id is not None:
        q = q.where(EvalDefinition.pipeline_id == pipeline_id)
    q = q.order_by(EvalDefinition.name)

    if cursor is not None:
        paginator = CursorPaginator(sort_field="name", sort_dir="asc")
        cp = await paginator.paginate(
            session,
            q,
            cursor=cursor,
            limit=limit,
            model=EvalDefinition,
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
