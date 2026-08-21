"""Unit tests for AdvisoryLockService."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.connector_hub.locking import AdvisoryLockService, ConnectorLockError


@pytest.fixture
def lock_service():
    return AdvisoryLockService()


async def test_advisory_lock_acquires(lock_service):
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    assert await lock_service.acquire(session, uuid.uuid4()) is None


async def test_advisory_lock_raises_on_contention(lock_service):
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = False  # Lock already held
    session.execute = AsyncMock(return_value=result_mock)

    resource_id = uuid.uuid4()
    with pytest.raises(ConnectorLockError, match="Could not acquire"):
        await lock_service.acquire(session, resource_id)


async def test_advisory_unlock_runs(lock_service):
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    await lock_service.release(session, rid)
    session.execute.assert_called_once()
    assert "pg_advisory_unlock" in session.execute.call_args.args[0].text
    result_mock.scalar_one.assert_called_once()


async def test_advisory_lock_acquire_release_round_trip(lock_service):
    """Full acquire → release round-trip with the same resource ID."""
    session = AsyncMock(spec=AsyncSession)
    acquire_result = MagicMock()
    acquire_result.scalar_one.return_value = True  # Lock acquired
    session.execute = AsyncMock(return_value=acquire_result)

    rid = uuid.uuid4()
    await lock_service.acquire(session, rid)
    # acquire() returns None on success — no exception means success

    session.execute.reset_mock()

    await lock_service.release(session, rid)
    session.execute.assert_called_once()
    assert lock_service._owner_task_id is None
    assert lock_service._resource_id is None


async def test_advisory_lock_acquire_contention_then_release(lock_service):
    """Lock contention raises, then a different call succeeds and releases."""
    session = AsyncMock(spec=AsyncSession)

    # First attempt — contention
    fail_result = MagicMock()
    fail_result.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=fail_result)

    rid = uuid.uuid4()
    with pytest.raises(ConnectorLockError, match="Could not acquire"):
        await lock_service.acquire(session, rid)
    # A failed acquire must not leave the service holding a lock.
    assert lock_service._owner_task_id is None
    assert lock_service._resource_id is None

    # Second attempt — success
    ok_result = MagicMock()
    ok_result.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=ok_result)

    await lock_service.acquire(session, rid)
    # acquire() returns None on success — no exception means success
    assert lock_service._resource_id == rid

    # Release
    await lock_service.release(session, rid)
    # Two statements on this mock: try-lock (success) then unlock.
    assert session.execute.call_count == 2
    assert "pg_advisory_unlock" in session.execute.call_args_list[1].args[0].text
    assert lock_service._owner_task_id is None
    assert lock_service._resource_id is None


def test_uuid_to_lock_keys_round_trip():
    """_uuid_to_lock_keys produces stable int4 keys for the same UUID."""
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys

    rid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    key1, key2 = _uuid_to_lock_keys(rid)
    assert isinstance(key1, int)
    assert isinstance(key2, int)
    # Re-running must produce the same result
    key1b, key2b = _uuid_to_lock_keys(rid)
    assert key1 == key1b
    assert key2 == key2b


def test_uuid_to_lock_keys_different_uuids_differ():
    """Different UUIDs produce different lock key pairs."""
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys

    rid1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    rid2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    k1a, k2a = _uuid_to_lock_keys(rid1)
    k1b, k2b = _uuid_to_lock_keys(rid2)
    # At least one of the keys should differ
    assert not (k1a == k1b and k2a == k2b)


def test_uuid_to_lock_keys_rejects_non_uuid():
    """_uuid_to_lock_keys raises TypeError for non-UUID input."""
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys

    with pytest.raises(TypeError, match=r"Expected uuid\.UUID"):
        _uuid_to_lock_keys("not-a-uuid")  # type: ignore[arg-type]


def test_uuid_to_lock_keys_within_int4_range():
    """_uuid_to_lock_keys produces signed 32-bit keys accepted by pg_try_advisory_lock."""
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys

    for _ in range(64):
        key1, key2 = _uuid_to_lock_keys(uuid.uuid4())
        assert -(2**31) <= key1 <= 2**31 - 1
        assert -(2**31) <= key2 <= 2**31 - 1


async def test_acquire_records_owner_state(lock_service):
    """acquire records the owning task and resource; release clears them."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    await lock_service.acquire(session, rid)

    assert lock_service._resource_id == rid
    assert lock_service._owner_task_id is not None

    await lock_service.release(session, rid)
    assert lock_service._resource_id is None
    assert lock_service._owner_task_id is None


async def test_acquire_while_holding_raises(lock_service):
    """Calling acquire() a second time without release raises ConnectorLockError."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    await lock_service.acquire(session, rid)
    with pytest.raises(ConnectorLockError, match="Already holding"):
        await lock_service.acquire(session, rid)


async def test_acquire_timeout_raises(lock_service):
    """A timeout acquiring the lock raises ConnectorLockError with a 'Timed out' message."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=TimeoutError)

    rid = uuid.uuid4()
    with pytest.raises(ConnectorLockError, match="Timed out"):
        await lock_service.acquire(session, rid)


async def test_acquire_db_error_raises(lock_service):
    """A database error acquiring the lock raises ConnectorLockError."""
    from sqlalchemy.exc import SQLAlchemyError

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))

    rid = uuid.uuid4()
    with pytest.raises(ConnectorLockError, match="Database error"):
        await lock_service.acquire(session, rid)


async def test_try_acquire_returns_true(lock_service):
    """try_acquire returns True when the lock is acquired on the first attempt."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    assert await lock_service.try_acquire(session, uuid.uuid4()) is True


async def test_try_acquire_polls_until_timeout(lock_service):
    """try_acquire returns False when the lock stays contended past the deadline."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=result_mock)

    with patch("modulo.core.connector_hub.locking.asyncio.sleep", AsyncMock()):
        assert await lock_service.try_acquire(session, uuid.uuid4(), lock_timeout=0.01) is False


async def test_try_acquire_returns_false_when_holding(lock_service):
    """try_acquire returns False immediately when the service already holds a lock."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    await lock_service.acquire(session, rid)
    assert await lock_service.try_acquire(session, rid) is False


async def test_try_acquire_retries_then_succeeds(lock_service):
    """try_acquire polls and eventually acquires the lock when contention clears."""
    session = AsyncMock(spec=AsyncSession)

    calls = {"count": 0}

    def _side_effect(*args, **kwargs):
        calls["count"] += 1
        result = MagicMock()
        # Fail the first poll, succeed on the second
        result.scalar_one.return_value = calls["count"] > 1
        return result

    session.execute = AsyncMock(side_effect=_side_effect)

    with patch("modulo.core.connector_hub.locking.asyncio.sleep", AsyncMock()):
        assert await lock_service.try_acquire(session, uuid.uuid4(), lock_timeout=5.0) is True
    assert calls["count"] == 2


async def test_release_different_resource_raises(lock_service):
    """Releasing a different resource than the one held raises ConnectorLockError."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    held = uuid.uuid4()
    other = uuid.uuid4()
    await lock_service.acquire(session, held)
    with pytest.raises(ConnectorLockError, match="currently holding"):
        await lock_service.release(session, other)


async def test_release_ownership_mismatch_warns(lock_service, caplog):
    """Releasing from a different task logs an ownership mismatch warning but still releases."""
    import logging

    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    await lock_service.acquire(session, rid)
    lock_service._owner_task_id = -1  # simulate ownership by a different task

    with caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub.locking"):
        await lock_service.release(session, rid)

    assert any("ownership mismatch" in rec.message for rec in caplog.records)


async def test_release_not_held_warns(lock_service, caplog):
    """Releasing a lock that PG reports as not held logs a double-release warning."""
    import logging

    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=result_mock)

    with caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub.locking"):
        await lock_service.release(session, uuid.uuid4())

    assert any("double-release" in rec.message for rec in caplog.records)


async def test_release_db_error_raises(lock_service):
    """A database error releasing the lock raises ConnectorLockError."""
    from sqlalchemy.exc import SQLAlchemyError

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))

    with pytest.raises(ConnectorLockError, match="Failed to release"):
        await lock_service.release(session, uuid.uuid4())


async def test_context_manager_aenter_returns_self(lock_service):
    """__aenter__ returns the service instance for use as an async context manager."""
    async with lock_service as svc:
        assert svc is lock_service


async def test_context_manager_aexit_warns_on_held_lock(lock_service, caplog):
    """Exiting the context manager while holding a lock logs a warning (no auto-release)."""
    import logging

    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    async with lock_service:
        await lock_service.acquire(session, rid)
        with caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub.locking"):
            await lock_service.__aexit__(None, None, None)
    assert any("exiting context with held lock" in rec.message for rec in caplog.records)


async def test_context_manager_aexit_no_held_lock_no_warning(lock_service, caplog):
    """Exiting the context manager without holding a lock logs nothing."""
    import logging

    with caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub.locking"):
        await lock_service.__aexit__(None, None, None)

    assert not caplog.records
