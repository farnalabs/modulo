"""CRUD for EvalDefinition records."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval_definition import EvalDefinition


async def list_eval_definitions(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
) -> list[EvalDefinition]:
    q = select(EvalDefinition).where(EvalDefinition.organisation_id == org_id)
    if pipeline_id is not None:
        q = q.where(EvalDefinition.pipeline_id == pipeline_id)
    q = q.order_by(EvalDefinition.name)
    result = await session.execute(q)
    return list(result.scalars().all())
