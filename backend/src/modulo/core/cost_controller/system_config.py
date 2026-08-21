"""Minimal ``system_config`` KV helpers for the cost subsystem.

The global ``system_config`` table has NO RLS — the app role owns it and the
probe/cooldown access is a plain global-table read/write with NO
``set_rls_org`` (spec §4.7). All functions assume an active transaction.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_config import SystemConfig

_log = logging.getLogger(__name__)

__all__ = ["acquire_kv_lock", "read_system_config", "try_acquire_kv_lock", "write_system_config"]


def _lock_keys(key: str) -> tuple[int, int]:
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys

    digest = hashlib.sha256(key.encode("utf-8")).digest()[:16]
    return _uuid_to_lock_keys(uuid.UUID(bytes=digest))


async def acquire_kv_lock(session: AsyncSession, key: str) -> None:
    """Acquire a Postgres advisory xact lock for a system_config KV key.

    The same discipline the sibling system-worker jobs use: the row
    read-modify-write happens under it, so two overlapping instances serialize
    their modify of the same key. Non-Postgres backends no-op (in-memory).
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    key1, key2 = _lock_keys(key)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": key1, "k2": key2})


async def try_acquire_kv_lock(session: AsyncSession, key: str) -> bool:
    """Non-blocking variant — True when the advisory lock was acquired."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    key1, key2 = _lock_keys(key)
    result = await session.execute(text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"), {"k1": key1, "k2": key2})
    return bool(result.scalar_one())


async def read_system_config(session: AsyncSession, key: str) -> Any:
    """Read a ``system_config`` value (``None`` when absent)."""
    result = await session.execute(select(SystemConfig.value).where(SystemConfig.key == key))
    return result.scalar_one_or_none()


async def write_system_config(session: AsyncSession, key: str, value: Any) -> None:
    """Upsert a ``system_config`` KV row (create or overwrite)."""
    result = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        session.add(SystemConfig(key=key, value=value))
    else:
        row.value = value
    await session.flush()
