"""Shared fixtures for the ``cleanup_jobs`` unit tests.

``autobegin_false_factory`` is the REAL ``autobegin=False`` session factory
(the F1 system-session convention) that the cron transaction-contract
regressions drive against an in-memory SQLite schema. It is hoisted here so
the webhook-dedup and trigger-events cron suites share one definition instead
of carrying byte-identical copies that can drift apart.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.db.models.base import Base
from modulo.db.models.trigger_event import TriggerEvent


@pytest.fixture
async def autobegin_false_factory() -> AsyncGenerator[async_sessionmaker, None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[TriggerEvent.__table__]))
        # The trigger_events FK targets (organisations/triggers/runs) are
        # not created here; foreign-key enforcement is disabled for the test.
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    try:
        yield factory
    finally:
        await engine.dispose()
