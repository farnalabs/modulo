"""Unit tests for AdvisoryLockService."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.connector_hub.locking import AdvisoryLockService, ConnectorLockError


@pytest.fixture()
def lock_service():
    return AdvisoryLockService()


async def test_advisory_lock_acquires(lock_service):
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result_mock)

    acquired = await lock_service.acquire(session, uuid.uuid4())
    assert acquired is True


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
    session.execute = AsyncMock(return_value=result_mock)

    rid = uuid.uuid4()
    await lock_service.release(session, rid)
    session.execute.assert_called_once()


async def test_advisory_lock_acquire_release_round_trip(lock_service):
    """Full acquire → release round-trip with the same resource ID."""
    session = AsyncMock(spec=AsyncSession)
    acquire_result = MagicMock()
    acquire_result.scalar_one.return_value = True  # Lock acquired
    session.execute = AsyncMock(return_value=acquire_result)

    rid = uuid.uuid4()
    acquired = await lock_service.acquire(session, rid)
    assert acquired is True

    session.execute.reset_mock()

    await lock_service.release(session, rid)
    session.execute.assert_called_once()


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

    # Second attempt — success
    ok_result = MagicMock()
    ok_result.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=ok_result)

    acquired = await lock_service.acquire(session, rid)
    assert acquired is True

    # Release
    await lock_service.release(session, rid)
    assert session.execute.call_count >= 1


async def test_uuid_to_lock_keys_round_trip():
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


async def test_uuid_to_lock_keys_different_uuids_differ():
    """Different UUIDs produce different lock key pairs."""
    from modulo.core.connector_hub.locking import _uuid_to_lock_keys

    rid1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    rid2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    k1a, k2a = _uuid_to_lock_keys(rid1)
    k1b, k2b = _uuid_to_lock_keys(rid2)
    # At least one of the keys should differ
    assert not (k1a == k1b and k2a == k2b)
