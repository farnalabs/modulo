"""Advisory lock service for Postgres-backed distributed locking."""

import asyncio
import hashlib
import logging
import random
import uuid
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT: float = 5.0
_POLL_INTERVAL: float = 0.1
_POLL_JITTER_MAX: float = 0.05
_INT4_BYTES: int = 4
_LOCK_ACQUIRE_TIMEOUT: float = 10.0
_LOCK_RELEASE_TIMEOUT: float = 5.0


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
    Each instance holds at most one lock at a time.
    """

    def __init__(self) -> None:
        self._owner_task_id: int | None = None
        self._resource_id: uuid.UUID | None = None

    async def acquire(self, session: AsyncSession, resource_id: uuid.UUID) -> None:
        """Attempt to acquire an advisory write lock.

        Raises ConnectorLockError when the lock cannot be acquired (already held).
        Returns None on success — use as a statement, not a boolean test.
        """
        if self._owner_task_id is not None:
            raise ConnectorLockError(
                resource_id,
                f"Already holding a lock on resource {self._resource_id} — release first",
            )
        key1, key2 = _uuid_to_lock_keys(resource_id)
        try:
            result = await asyncio.wait_for(
                session.execute(
                    text("SELECT pg_try_advisory_lock(:key1, :key2)"),
                    {"key1": key1, "key2": key2},
                ),
                timeout=_LOCK_ACQUIRE_TIMEOUT,
            )
        except TimeoutError:
            raise ConnectorLockError(resource_id, f"Timed out acquiring lock on resource {resource_id}") from None
        except SQLAlchemyError:
            raise ConnectorLockError(resource_id, f"Database error acquiring lock on resource {resource_id}") from None
        if not result.scalar_one():
            raise ConnectorLockError(resource_id)
        current_task = asyncio.current_task()
        self._owner_task_id = id(current_task) if current_task is not None else None
        self._resource_id = resource_id
        logger.info("Acquired lock on resource %s", resource_id)

    async def try_acquire(
        self,
        session: AsyncSession,
        resource_id: uuid.UUID,
        lock_timeout: float = _LOCK_TIMEOUT,
        interval: float = _POLL_INTERVAL,
    ) -> bool:
        """Acquire a lock with a polling loop and timeout.

        Returns True if the lock was acquired within *lock_timeout* seconds,
        False if the timeout expired.  Always attempts at least once even when
        *lock_timeout* is zero or negative.

        Returns False immediately if the service instance already holds a lock
        (re-entrant call).
        """
        if self._owner_task_id is not None:
            logger.warning(
                "try_acquire called while already holding lock on resource %s",
                self._resource_id,
            )
            return False
        loop = asyncio.get_running_loop()
        attempt = 0
        deadline = loop.time() + max(lock_timeout, 0.0)
        while True:
            try:
                await self.acquire(session, resource_id)
                return True
            except ConnectorLockError:
                attempt += 1
                if loop.time() >= deadline:
                    logger.warning(
                        "Timed out acquiring lock on resource %s after %.1fs (%d attempts)",
                        resource_id,
                        lock_timeout,
                        attempt,
                    )
                    return False
                jitter = random.random() * _POLL_JITTER_MAX  # noqa: S311 — jitter, not crypto
                await asyncio.sleep(interval + jitter)

    async def release(self, session: AsyncSession, resource_id: uuid.UUID) -> None:
        """Release an advisory write lock on a shared resource.

        Logs a warning if the lock is not held by this caller
        (e.g. double-release or stale release after connection reuse).

        Always attempts ``pg_advisory_unlock`` regardless of ownership to
        prevent PG-side lock leaks.
        """
        if self._resource_id is not None and resource_id != self._resource_id:
            raise ConnectorLockError(
                resource_id,
                f"Cannot release lock on resource {resource_id} — currently holding lock on {self._resource_id}",
            )
        if self._owner_task_id is not None:
            current_task = asyncio.current_task()
            current = id(current_task) if current_task is not None else None
            if current != self._owner_task_id:
                logger.warning(
                    "Release called by task %s but lock owned by task %s — possible ownership mismatch",
                    current,
                    self._owner_task_id,
                )
        key1, key2 = _uuid_to_lock_keys(resource_id)
        try:
            result = await asyncio.wait_for(
                session.execute(
                    text("SELECT pg_advisory_unlock(:key1, :key2)"),
                    {"key1": key1, "key2": key2},
                ),
                timeout=_LOCK_RELEASE_TIMEOUT,
            )
            released = result.scalar_one()
            if not released:
                logger.warning(
                    "Lock on resource %s was not held by this session — possible double-release", resource_id
                )
            else:
                logger.info("Released lock on resource %s", resource_id)
        except (TimeoutError, SQLAlchemyError) as exc:
            logger.error("Failed to release lock on resource %s: %s", resource_id, exc, exc_info=True)
            raise ConnectorLockError(resource_id, f"Failed to release lock: {exc}") from None
        finally:
            self._owner_task_id = None
            self._resource_id = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owner_task_id is not None and self._resource_id is not None:
            logger.warning(
                "AdvisoryLockService exiting context with held lock on resource %s "
                "(owner task %s) — will NOT auto-release; release in session scope",
                self._resource_id,
                self._owner_task_id,
            )


def _uuid_to_lock_keys(resource_id: uuid.UUID) -> tuple[int, int]:
    """Convert a UUID into two int4 keys for pg_try_advisory_lock.

    Uses MD5 of the UUID string to produce a stable 128-bit hash,
    split into two 32-bit signed integers. Avoids hashtext() which has a
    smaller keyspace and PG-version-dependent behaviour.
    """
    if not isinstance(resource_id, uuid.UUID):
        raise TypeError(f"Expected uuid.UUID, got {type(resource_id).__name__}")
    digest = hashlib.md5(str(resource_id).encode("ascii"), usedforsecurity=False).digest()
    key1 = int.from_bytes(digest[:_INT4_BYTES], "big", signed=True)
    key2 = int.from_bytes(digest[_INT4_BYTES : _INT4_BYTES * 2], "big", signed=True)
    return (key1, key2)
