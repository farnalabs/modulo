"""Persistence contract tests for notification endpoint JSON fields."""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.db.models  # noqa: F401
from modulo.db.models.base import Base
from modulo.db.models.notification_endpoint import NotificationEndpoint


@pytest.fixture()
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    instance = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with instance.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[NotificationEndpoint.__table__],
            )
        )
    yield instance
    await instance.dispose()


async def test_events_round_trip_as_native_json_array(engine: AsyncEngine) -> None:
    expected = ["hitl_awaiting", "run_failed"]
    endpoint_id = uuid.uuid4()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        session.add(
            NotificationEndpoint(
                id=endpoint_id,
                organisation_id=uuid.uuid4(),
                url="https://hooks.example.com/notify",
                events=expected,
            )
        )
        await session.commit()

    async with AsyncSession(engine) as session:
        persisted = await session.scalar(select(NotificationEndpoint).where(NotificationEndpoint.id == endpoint_id))

    assert persisted is not None
    assert isinstance(persisted.events, list)
    assert persisted.events == expected
