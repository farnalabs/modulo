from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.model_backend import ModelBackend
from modulo.db.rls import set_rls_org


async def user_has_api_key(
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    await set_rls_org(session, org_id)
    result = await session.execute(
        select(func.count(ModelBackend.id)).where(
            ModelBackend.organisation_id == org_id,
            ModelBackend.status == "active",
        )
    )
    count = result.scalar()
    return count is not None and count > 0
