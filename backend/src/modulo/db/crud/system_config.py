"""CRUD for SystemConfig key-value settings."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
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
    """Insert-or-update a SystemConfig row (upsert).

    Reads the existing row with ``SELECT … FOR UPDATE`` so concurrent updates to
    the same key serialize on a row lock. When the key does not yet exist there
    is nothing to lock, so two concurrent first-writes can both clear the SELECT
    and race on the INSERT. That race surfaces as an ``IntegrityError`` on the
    unique ``key`` constraint — we roll back to a savepoint, re-select the
    winning row, and adopt its value, so the stored value converges to the value
    we intended to write.
    """
    existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key).with_for_update())
    existing_row = existing.scalar_one_or_none()
    if existing_row is not None:
        existing_row.value = value
        existing_row.updated_by = updated_by
        entity = existing_row
    else:
        entity = SystemConfig(key=key, value=value, updated_by=updated_by)
        session.add(entity)
        # Wrap the INSERT in a savepoint so a concurrent first-write's unique
        # violation rolls back only this scope. ``begin_nested`` requires an
        # active transaction; under autocommit-style usage (or a mock session in
        # unit tests) there is no transaction to nest, so fall back to a plain
        # flush — there is no real concurrency to guard against in that context.
        savepoint = None
        try:
            savepoint = await session.begin_nested()
        except (TypeError, AttributeError, InvalidRequestError):
            savepoint = None
        try:
            await session.flush()
        except IntegrityError:
            if savepoint is not None:
                await savepoint.rollback()
            existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key).with_for_update())
            entity = existing.scalar_one()
            entity.value = value
            entity.updated_by = updated_by
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
