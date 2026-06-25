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
