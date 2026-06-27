"""Unit tests for db/repositories/locks.py — lock services, key hashing, factory."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.locks import (
    GenericLock,
    LockAcquireError,
    PostgresLock,
    _build_lock_service,
    _generic_locks,
    _generic_owners,
    _str_to_lock_keys,
)


@pytest.fixture(autouse=True)
def _clean_generic_lock_state() -> None:
    """Reset module-level lock state before each test."""
    for key in list(_generic_locks.keys()):
        lock = _generic_locks[key]
        if lock.locked():
            lock.release()
    _generic_locks.clear()
    _generic_owners.clear()


class TestStrToLockKeys:
    def test_returns_tuple_of_two_ints(self) -> None:
        k1, k2 = _str_to_lock_keys("pipeline:42")
        assert isinstance(k1, int)
        assert isinstance(k2, int)

    def test_deterministic_same_input(self) -> None:
        assert _str_to_lock_keys("hello") == _str_to_lock_keys("hello")

    def test_different_inputs_differ(self) -> None:
        assert _str_to_lock_keys("abc") != _str_to_lock_keys("xyz")

    def test_empty_string(self) -> None:
        k1, k2 = _str_to_lock_keys("")
        assert isinstance(k1, int)
        assert isinstance(k2, int)

    def test_unicode_input(self) -> None:
        k1, k2 = _str_to_lock_keys("héllo-世界")
        assert isinstance(k1, int)
        assert isinstance(k2, int)


class TestBuildLockService:
    def test_postgres_returns_postgres_lock(self) -> None:
        svc = _build_lock_service("postgres")
        assert isinstance(svc, PostgresLock)

    def test_sqlite_returns_generic_lock(self) -> None:
        svc = _build_lock_service("sqlite")
        assert isinstance(svc, GenericLock)

    def test_mariadb_returns_generic_lock(self) -> None:
        svc = _build_lock_service("mariadb")
        assert isinstance(svc, GenericLock)

    def test_unknown_returns_generic_lock(self) -> None:
        svc = _build_lock_service("some_unknown_backend")
        assert isinstance(svc, GenericLock)


class TestPostgresLock:
    _KEY = "test-resource"

    @pytest.fixture()
    def lock(self) -> PostgresLock:
        return PostgresLock()

    @pytest.fixture()
    def session(self) -> AsyncMock:
        s = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = True
        s.execute = AsyncMock(return_value=result)
        return s

    async def test_acquire_calls_pg_try_advisory_lock(self, lock: PostgresLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY)
        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "pg_try_advisory_lock" in call_text

    async def test_release_calls_pg_advisory_unlock(self, lock: PostgresLock, session: AsyncMock) -> None:
        await lock.release_lock(session, self._KEY)
        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "pg_advisory_unlock" in call_text

    async def test_acquire_raises_on_timeout(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = False
        session.execute = AsyncMock(return_value=result)

        lock = PostgresLock()
        with pytest.raises(LockAcquireError, match="Could not acquire lock"):
            await lock.acquire_lock(session, self._KEY, timeout=0.05)

    async def test_acquire_retries_on_contention(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        fail_result = MagicMock()
        fail_result.scalar_one.return_value = False
        ok_result = MagicMock()
        ok_result.scalar_one.return_value = True
        session.execute = AsyncMock()
        session.execute.side_effect = [fail_result, ok_result]

        lock = PostgresLock()
        await lock.acquire_lock(session, self._KEY, timeout=5.0)
        assert session.execute.await_count == 2

    async def test_release_uses_same_key_hash_as_acquire(self, lock: PostgresLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY)
        acquire_params = session.execute.await_args[0][1]
        session.execute.reset_mock()

        await lock.release_lock(session, self._KEY)
        release_params = session.execute.await_args[0][1]

        assert acquire_params == release_params


class TestGenericLock:
    _KEY = "generic-lock-key"

    @pytest.fixture()
    def lock(self) -> GenericLock:
        return GenericLock()

    @pytest.fixture()
    def session(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    async def test_acquire_and_release(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY)
        assert self._KEY in _generic_locks
        assert _generic_locks[self._KEY].locked()

        await lock.release_lock(session, self._KEY)
        assert self._KEY not in _generic_locks
        assert self._KEY not in _generic_owners

    async def test_release_non_existent_lock_is_noop(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.release_lock(session, "non-existent-key")

    async def test_acquire_with_timeout(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY, timeout=5.0)
        assert _generic_locks[self._KEY].locked()

    async def test_acquire_raises_on_timeout_when_lock_held(self, lock: GenericLock, session: AsyncMock) -> None:
        _generic_locks[self._KEY] = asyncio.Lock()
        await _generic_locks[self._KEY].acquire()
        _generic_owners[self._KEY] = 999999

        with pytest.raises(LockAcquireError, match="Could not acquire lock"):
            await lock.acquire_lock(session, self._KEY, timeout=0.05)

    async def test_ownership_prevents_cross_task_release(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY)
        assert _generic_locks[self._KEY].locked()

        _generic_owners[self._KEY] = 999999

        await lock.release_lock(session, self._KEY)
        assert _generic_locks[self._KEY].locked()

    async def test_release_cleans_up_when_lock_not_contended(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY)
        await lock.release_lock(session, self._KEY)
        assert self._KEY not in _generic_locks
        assert self._KEY not in _generic_owners

    async def test_multiple_keys_independent(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, "lock-a")
        await lock.acquire_lock(session, "lock-b")

        assert _generic_locks["lock-a"].locked()
        assert _generic_locks["lock-b"].locked()

        await lock.release_lock(session, "lock-a")
        assert "lock-a" not in _generic_locks
        assert _generic_locks["lock-b"].locked()

        await lock.release_lock(session, "lock-b")
        assert "lock-b" not in _generic_locks

    async def test_acquire_twice_same_key_blocks(self, lock: GenericLock, session: AsyncMock) -> None:
        await lock.acquire_lock(session, self._KEY)
        assert _generic_locks[self._KEY].locked()

        second_acquired = False

        async def _try_acquire() -> None:
            nonlocal second_acquired
            other_session = AsyncMock(spec=AsyncSession)
            await lock.acquire_lock(other_session, self._KEY, timeout=5.0)
            second_acquired = True

        task = asyncio.create_task(_try_acquire())
        await asyncio.sleep(0.1)
        assert not second_acquired

        await lock.release_lock(session, self._KEY)
        await asyncio.wait_for(task, timeout=2.0)
        assert second_acquired
