"""CRUD for SystemConfig key-value settings."""

import uuid
from typing import Any

from sqlalchemy import func, select
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
    """Trust-On-First-Use mint of a SystemConfig row — first-write-wins.

    Always issues ``INSERT … ON CONFLICT DO NOTHING`` and then re-``SELECT``s
    the single stored row back, returning it. This is **unconditional**: there
    is no existence-check-gated ``UPDATE`` branch, so the function can never
    adopt a caller's own value while silently clobbering an existing row.

    The first-write-wins / TOFU invariant holds deterministically for every
    interleaving:

    * *Concurrent first-mint* — two callers race the unique ``key`` INSERT. The
      winner's INSERT commits; the loser's INSERT is *silently skipped* by
      ``ON CONFLICT DO NOTHING`` and the loser re-``SELECT``s the **winner's**
      stored row and adopts it. Both callers observe the same value.
    * *Sequential second write* — a caller that runs ``set_config`` after the
      key was already committed also hits the ``ON CONFLICT DO NOTHING`` path:
      its INSERT is skipped and it re-``SELECT``s and returns the already-stored
      value, never overwriting it.

    This guarantee is load-bearing for Trust-On-First-Use minting (see
    ``instance_identity.py``): under a concurrent first-mint of a key the losing
    caller must observe the value the *winning* caller stored, not its own —
    otherwise two concurrent callers return different values for the same key
    and every reader sees whichever id was written last (last-write-wins), which
    is exactly the instability TOFU is meant to prevent.

    ``set_config`` therefore NEVER overwrites a value that already exists. A
    deliberate update or rotation (e.g. secret rotation, admin edit) must use
    :func:`update_config`, which exists specifically for that purpose.
    """
    # Unconditional first-write path: ``INSERT … ON CONFLICT DO NOTHING`` then
    # re-SELECT. No existence-check-gated UPDATE branch exists, so the TOFU
    # invariant above holds for every caller, concurrent or sequential.
    insert_stmt = (
        pg_insert(SystemConfig)
        .values(key=key, value=value, updated_by=updated_by)
        .on_conflict_do_nothing(index_elements=["key"])
    )
    await session.execute(insert_stmt)
    stored = (await session.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one()
    await session.flush()
    return stored


async def update_config(
    session: AsyncSession,
    key: str,
    value: Any,
    updated_by: uuid.UUID | None = None,
) -> SystemConfig:
    """Deliberate overwrite (upsert) of a SystemConfig row — last-writer-wins.

    Unlike :func:`set_config` (which is Trust-On-First-Use and never
    overwrites), this path is for *intentional* changes: secret rotation,
    admin edits, monitor-config updates, per-instance sequence numbers, and so
    on. It uses a single atomic ``INSERT … ON CONFLICT DO UPDATE`` statement, so
    the write is applied and locked in one round-trip — there is no separate
    stale-read window between an existence check and the mutation (which would
    let a concurrent committed update slip in and be silently clobbered). A
    brand-new key is inserted; an existing key is overwritten in place.

    This is intentionally **not** first-write-wins: a deliberate rotation or
    admin edit must take effect even when the key already exists.
    """
    # Single atomic statement: ``INSERT … ON CONFLICT DO UPDATE``. No separate
    # existence SELECT precedes the mutation, so a concurrent committed update
    # cannot be lost between reads; Postgres applies the conflict-target update
    # under its own row lock.
    stmt = (
        pg_insert(SystemConfig)
        .values(key=key, value=value, updated_by=updated_by)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": value, "updated_by": updated_by, "updated_at": func.current_timestamp()},
        )
    )
    await session.execute(stmt)
    stored = (await session.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one()
    await session.flush()
    return stored


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
