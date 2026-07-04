"""Advisory lock service for Postgres-backed distributed locking."""

import asyncio
import hashlib
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ConnectorLockError(Exception):
    """Raised when an advisory write lock cannot be acquired."""

    def __init__(self, resource_id: uuid.UUID, message: str = "") -> None:
        self.resource_id = resource_id
        msg = message or f"Could not acquire write lock on resource {resource_id}"
        super().__init__(msg)


class AdvisoryLockService:
    """Manages Postgres advisory write locks for shared resources.

    Uses pg_try_advisory_lock with a two-key (int4, int4) strategy to minimise
    collision probability and avoid PG-version-dependent hashtext() behaviour.

    Not thread-safe for the same session — use one service instance per locking domain.
    """

    def __init__(self) -> None:
        self._owner_task_id: int | None = None

    async def acquire(self, session: AsyncSession, resource_id: uuid.UUID) -> None:
        """Attempt to acquire an advisory write lock.

        Raises ConnectorLockError when the lock cannot be acquired (already held).
        Returns None on success — use as a statement, not a boolean test.
        """
        key1, key2 = _uuid_to_lock_keys(resource_id)
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not result.scalar_one():
            raise ConnectorLockError(resource_id)
        self._owner_task_id = id(asyncio.current_task())
        logger.info("Acquired lock on resource %s", resource_id)

    async def try_acquire(
        self, session: AsyncSession, resource_id: uuid.UUID, lock_timeout: float = 5.0, interval: float = 0.1
    ) -> bool:
        """Acquire a lock with a polling loop and timeout.

        Returns True if the lock was acquired within *lock_timeout* seconds,
        False if the timeout expired.
        """
        deadline = asyncio.get_event_loop().time() + lock_timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await self.acquire(session, resource_id)
                return True
            except ConnectorLockError:
                await asyncio.sleep(interval)
        logger.warning("Timed out acquiring lock on resource %s after %.1fs", resource_id, lock_timeout)
        return False

    async def release(self, session: AsyncSession, resource_id: uuid.UUID) -> None:
        """Release an advisory write lock on a shared resource.

        Logs a warning if the lock is not held by this caller
        (e.g. double-release or stale release after connection reuse).
        """
        if self._owner_task_id is not None:
            current = id(asyncio.current_task())
            if current != self._owner_task_id:
                logger.warning(
                    "Release called by task %s but lock owned by task %s — possible ownership mismatch",
                    current,
                    self._owner_task_id,
                )
        key1, key2 = _uuid_to_lock_keys(resource_id)
        result = await session.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        released = result.scalar_one()
        if not released:
            logger.warning("Lock on resource %s was not held by this session — possible double-release", resource_id)
        else:
            logger.info("Released lock on resource %s", resource_id)
        self._owner_task_id = None

    async def __aenter__(self) -> "AdvisoryLockService":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


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
