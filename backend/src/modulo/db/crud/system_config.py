"""CRUD for SystemConfig key-value settings."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_config import SystemConfig


async def get_config(session: AsyncSession, key: str) -> SystemConfig | None:
    result = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
    return result.scalar_one_or_none()


async def set_config(
    session: AsyncSession,
    key: str,
    value: Any,
    updated_by: uuid.UUID | None = None,
) -> SystemConfig:
    existing = await session.execute(
        select(SystemConfig).where(SystemConfig.key == key).with_for_update()
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row is not None:
        existing_row.value = value
        existing_row.updated_by = updated_by
        entity = existing_row
    else:
        entity = SystemConfig(key=key, value=value, updated_by=updated_by)
        session.add(entity)
    await session.flush()
    return entity


async def list_config(session: AsyncSession) -> list[SystemConfig]:
    result = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
    return list(result.scalars().all())


async def delete_config(session: AsyncSession, key: str) -> bool:
    existing = await get_config(session, key)
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True
