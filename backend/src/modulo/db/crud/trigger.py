"""CRUD for Trigger records."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.trigger import Trigger


async def list_triggers(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
) -> list[Trigger]:
    q = select(Trigger).where(Trigger.organisation_id == org_id)
    if pipeline_id is not None:
        q = q.where(Trigger.pipeline_id == pipeline_id)
    q = q.order_by(Trigger.created_at.desc())
    result = await session.execute(q)
    return list(result.scalars().all())
