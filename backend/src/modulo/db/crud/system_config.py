"""CRUD for SystemConfig key-value settings."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    the same key serialize on a row lock. When the key does not yet exist there is
    nothing to lock, so two concurrent first-writes can both clear the SELECT and
    race on the INSERT. The first write uses ``INSERT … ON CONFLICT DO NOTHING``:
    the winning caller's INSERT commits and the losing caller's INSERT is silently
    skipped, after which both callers ``SELECT`` the single stored row back and
    **adopt the same value** (first-write-wins).

    This first-write-wins guarantee is load-bearing for Trust-On-First-Use minting
    (see ``instance_identity.py``): under a concurrent first-mint of a key, the
    losing caller must observe the value the *winning* caller stored, not its own
    — otherwise two concurrent callers return different values for the same key
    and every reader sees whichever id was written last (last-write-wins), which
    is exactly the instability TOFU is meant to prevent.

    When the row already exists (a deliberate update, e.g. secret rotation) the
    caller's value still wins, as expected for an upsert.
    """
    existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key).with_for_update())
    existing_row = existing.scalar_one_or_none()
    if existing_row is not None:
        existing_row.value = value
        existing_row.updated_by = updated_by
        entity = existing_row
    else:
        # First write: the key is not present yet. Two concurrent first-writes
        # race the unique ``key`` INSERT. Issue the INSERT with ``ON CONFLICT
        # DO NOTHING`` so the loser's INSERT is *silently skipped* (no exception
        # to recover from, no fragile savepoint/identity-map dance) and then
        # SELECT the single stored row back. Both callers — winner and loser —
        # therefore observe the SAME stored value: the first-write-wins / TOFU
        # invariant that load-bearing Trust-On-First-Use minting depends on (see
        # ``instance_identity.py``). Under a concurrent first-mint, every caller
        # must see the value the *winning* caller stored, not its own pending
        # INSERT, otherwise two concurrent callers return different values for
        # the same key (last-write-wins), which is exactly the instability TOFU
        # is meant to prevent.
        insert_stmt = (
            pg_insert(SystemConfig)
            .values(key=key, value=value, updated_by=updated_by)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        await session.execute(insert_stmt)
        stored = (await session.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one()
        entity = stored
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
