"""Tests for the is_executable property on the Agent model."""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.agent import Agent
from modulo.db.models.base import Base

_AGENT_TABLE_NAMES = {
    "organisations", "users", "model_backends", "schema_versions",
    "library_primitives", "agents",
}


@pytest.fixture()
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _AGENT_TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture()
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


class TestAgentIsExecutable:
    async def test_default_is_true(self, session: AsyncSession) -> None:
        agent = Agent(
            organisation_id=uuid.uuid4(),
            name="Default Executable",
            created_by=uuid.uuid4(),
            input_schema_id=uuid.uuid4(),
            input_schema_version="1.0",
            output_schema_id=uuid.uuid4(),
            output_schema_version="1.0",
            prompt_template="You are a helpful assistant.",
            model_backend_id=uuid.uuid4(),
        )
        session.add(agent)
        await session.flush()
        assert agent.is_executable is True

    async def test_explicit_true(self, session: AsyncSession) -> None:
        agent = Agent(
            organisation_id=uuid.uuid4(),
            name="Executable Agent",
            created_by=uuid.uuid4(),
            is_executable=True,
            input_schema_id=uuid.uuid4(),
            input_schema_version="1.0",
            output_schema_id=uuid.uuid4(),
            output_schema_version="1.0",
            prompt_template="You are a helpful assistant.",
            model_backend_id=uuid.uuid4(),
        )
        session.add(agent)
        await session.flush()
        assert agent.is_executable is True

    async def test_explicit_false(self, session: AsyncSession) -> None:
        agent = Agent(
            organisation_id=uuid.uuid4(),
            name="Non-Executable Agent",
            created_by=uuid.uuid4(),
            is_executable=False,
            input_schema_id=uuid.uuid4(),
            input_schema_version="1.0",
            output_schema_id=uuid.uuid4(),
            output_schema_version="1.0",
            prompt_template="You are a helpful assistant.",
            model_backend_id=uuid.uuid4(),
        )
        session.add(agent)
        await session.flush()
        assert agent.is_executable is False

    async def test_persisted_in_db(self, session: AsyncSession) -> None:
        agent = Agent(
            organisation_id=uuid.uuid4(),
            name="Persist Check",
            created_by=uuid.uuid4(),
            is_executable=False,
            input_schema_id=uuid.uuid4(),
            input_schema_version="1.0",
            output_schema_id=uuid.uuid4(),
            output_schema_version="1.0",
            prompt_template="You are a helpful assistant.",
            model_backend_id=uuid.uuid4(),
        )
        session.add(agent)
        await session.flush()
        agent_id = agent.id
        session.expire_all()
        reloaded = await session.get(Agent, agent_id)
        assert reloaded is not None
        assert reloaded.is_executable is False

    async def test_can_update_to_false(self, session: AsyncSession) -> None:
        agent = Agent(
            organisation_id=uuid.uuid4(),
            name="Updatable Agent",
            created_by=uuid.uuid4(),
            is_executable=True,
            input_schema_id=uuid.uuid4(),
            input_schema_version="1.0",
            output_schema_id=uuid.uuid4(),
            output_schema_version="1.0",
            prompt_template="You are a helpful assistant.",
            model_backend_id=uuid.uuid4(),
        )
        session.add(agent)
        await session.flush()
        agent_id = agent.id
        agent.is_executable = False
        await session.flush()
        session.expire_all()
        reloaded = await session.get(Agent, agent_id)
        assert reloaded is not None
        assert reloaded.is_executable is False
