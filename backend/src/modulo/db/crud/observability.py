import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.organisation import Organisation


async def get_otel_config(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    result = await session.execute(select(Organisation.otel_config_json).where(Organisation.id == org_id))
    row = result.scalar_one_or_none()
    return row if row is not None else {}


async def update_otel_config(
    session: AsyncSession,
    org_id: uuid.UUID,
    config: dict[str, Any],
) -> dict[str, Any]:
    org = await session.get(Organisation, org_id)
    if org is None:
        raise ValueError("Organisation not found")
    merged = {**org.otel_config_json, **config}
    org.otel_config_json = merged
    await session.flush()
    return merged
