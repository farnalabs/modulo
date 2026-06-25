"""Advisory lock service for Postgres-backed distributed locking."""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ConnectorLockError(Exception):
    """Raised when an advisory write lock cannot be acquired."""

    pass


class AdvisoryLockService:
    """Manages Postgres advisory write locks for shared resources.

    Uses pg_try_advisory_lock with a two-key (int4, int4) strategy to minimise
    collision probability and avoid PG-version-dependent hashtext() behaviour.
    """

    async def acquire(self, session: AsyncSession, resource_id: uuid.UUID) -> bool:
        """Attempt to acquire an advisory write lock.

        Raises ConnectorLockError when the lock cannot be acquired (already held).
        """
        key1, key2 = _uuid_to_lock_keys(resource_id)
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not result.scalar_one():
            raise ConnectorLockError(f"Could not acquire write lock on resource {resource_id}")
        return True

    async def release(self, session: AsyncSession, resource_id: uuid.UUID) -> None:
        """Release an advisory write lock on a shared resource."""
        key1, key2 = _uuid_to_lock_keys(resource_id)
        await session.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )


def _uuid_to_lock_keys(resource_id: uuid.UUID) -> tuple[int, int]:
    """Convert a UUID into two int4 keys for pg_try_advisory_lock.

    Uses MD5 of the UUID string to produce a stable 128-bit hash,
    split into two 32-bit signed integers. Avoids hashtext() which has a
    smaller keyspace and PG-version-dependent behaviour.
    """
    digest = hashlib.md5(str(resource_id).encode("ascii"), usedforsecurity=False).digest()
    key1 = int.from_bytes(digest[:4], "big", signed=True)
    key2 = int.from_bytes(digest[4:8], "big", signed=True)
    return (key1, key2)
