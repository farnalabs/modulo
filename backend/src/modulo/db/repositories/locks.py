"""Lock abstraction — distributed (Postgres) / in-memory (generic).

Usage:
    lock_svc = _build_lock_service("postgres")
    async with session.begin():
        await lock_svc.acquire_lock(session, "pipeline:42", lock_timeout=10.0)
        try:
            ...
        finally:
            await lock_svc.release_lock(session, "pipeline:42")
"""

import asyncio
import contextlib
import hashlib
import logging
from abc import ABC, abstractmethod

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.05
_DEFAULT_LOCK_TIMEOUT = 300.0
_TIMEOUT_ERR = "Could not acquire lock {key!r} within {timeout}s"


class LockAcquireError(RuntimeError):
    """Raised when a lock cannot be acquired within the requested timeout."""


class BaseLockService(ABC):
    """Abstract lock provider.

    Postgres implementations use ``pg_advisory_lock`` / ``pg_advisory_unlock``
    (distributed, session-scoped).  Generic implementations use in-memory
    ``asyncio.Lock`` instances (single-process only).
    """

    @abstractmethod
    async def acquire_lock(
        self,
        session: AsyncSession,
        key: str,
        lock_timeout: float | None = None,
    ) -> None: ...

    @abstractmethod
    async def release_lock(self, session: AsyncSession, key: str) -> None: ...


class PostgresLock(BaseLockService):
    """Distributed lock backed by Postgres advisory locks.

    Locks are scoped to the database session — they are automatically
    released when the session ends or the connection is closed.

    The caller MUST wrap acquire/release inside an explicit transaction
    (``async with session.begin():``) so that both calls use the same
    database connection; advisory locks are connection-scoped.
    """

    async def acquire_lock(
        self,
        session: AsyncSession,
        key: str,
        lock_timeout: float | None = None,
    ) -> None:
        key1, key2 = _str_to_lock_keys(key)
        actual_timeout = lock_timeout if lock_timeout is not None else _DEFAULT_LOCK_TIMEOUT
        deadline = asyncio.get_running_loop().time() + actual_timeout

        while True:
            result = await session.execute(
                text("SELECT pg_try_advisory_lock(:key1, :key2)"),
                {"key1": key1, "key2": key2},
            )
            if result.scalar_one():
                return

            if asyncio.get_running_loop().time() >= deadline:
                raise LockAcquireError(_TIMEOUT_ERR.format(key=key, timeout=actual_timeout))

            await asyncio.sleep(_POLL_INTERVAL)

    async def release_lock(self, session: AsyncSession, key: str) -> None:
        key1, key2 = _str_to_lock_keys(key)
        result = await session.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not result.scalar_one():
            logger.warning(
                "pg_advisory_unlock returned false for key=%r — lock was not held by this session",
                key,
            )


_generic_locks: dict[str, asyncio.Lock] = {}
_generic_owners: dict[str, int] = {}
_generic_dict_lock = asyncio.Lock()


class GenericLock(BaseLockService):
    """In-memory lock for single-process backends (SQLite, MariaDB).

    Uses an ``asyncio.Lock`` per key (shared across all instances),
    tracking ownership by task ID so that timeout / error paths cannot
    release another task's lock.  The *session* parameter is accepted
    for interface compatibility but is not used.
    """

    async def acquire_lock(
        self,
        _session: AsyncSession,
        key: str,
        lock_timeout: float | None = None,
    ) -> None:
        async with _generic_dict_lock:
            if key not in _generic_locks:
                _generic_locks[key] = asyncio.Lock()
            lock = _generic_locks[key]

        actual_timeout = lock_timeout if lock_timeout is not None else _DEFAULT_LOCK_TIMEOUT
        try:
            await asyncio.wait_for(lock.acquire(), timeout=actual_timeout)
        except TimeoutError as exc:
            raise LockAcquireError(_TIMEOUT_ERR.format(key=key, timeout=actual_timeout)) from exc

        try:
            owner = id(asyncio.current_task())
            async with _generic_dict_lock:
                _generic_owners[key] = owner
        except asyncio.CancelledError:
            with contextlib.suppress(RuntimeError):
                lock.release()
            raise

    async def release_lock(self, _session: AsyncSession, key: str) -> None:
        owner = id(asyncio.current_task())
        async with _generic_dict_lock:
            actual = _generic_owners.get(key)
            if actual is None:
                logger.warning(
                    "release_lock called for key=%r but no owner was recorded — double-release?",
                    key,
                )
                return
            if actual != owner:
                logger.warning(
                    "release_lock called for key=%r by task %d but owner is task %d",
                    key,
                    owner,
                    actual,
                )
                return
            lock = _generic_locks.get(key)
            if lock is not None:
                try:
                    lock.release()
                except RuntimeError:
                    logger.warning(
                        "release_lock: lock.release() failed for key=%r — already released?",
                        key,
                        exc_info=True,
                    )
            del _generic_owners[key]
            _generic_locks.pop(key, None)


def _str_to_lock_keys(key: str) -> tuple[int, int]:
    """Hash an arbitrary string into two signed 32-bit integers.

    Uses MD5 for stable, uniform distribution over the int32 range.
    Matches the convention used by ``modulo.core.connector_hub.locking``.
    """
    digest = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).digest()
    k1 = int.from_bytes(digest[:4], "big", signed=True)
    k2 = int.from_bytes(digest[4:8], "big", signed=True)
    return (k1, k2)


def _build_lock_service(db_type: str) -> BaseLockService:
    match db_type:
        case "postgres":
            return PostgresLock()
        case _:
            logger.warning("Unknown db_type=%r — falling back to GenericLock", db_type)
            return GenericLock()
