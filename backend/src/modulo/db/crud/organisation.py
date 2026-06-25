"""CRUD for Organisation records."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.organisation import Organisation


async def get_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> Organisation | None:
    result = await session.execute(select(Organisation).where(Organisation.id == org_id))
    return result.scalar_one_or_none()


async def update_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
    updates: dict[str, object],
) -> Organisation | None:
    org = await get_organisation(session, org_id)
    if org is None:
        return None
    apply_updates(org, updates)
    await session.flush()
    return org
