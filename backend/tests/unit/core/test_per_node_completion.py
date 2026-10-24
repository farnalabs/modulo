"""FAR-2050 durable per-node completion and deadline gate tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from modulo.core.pipeline_execution import _fail_overdue_node, _write_node_completion
from modulo.db.crud.run_node_outputs import attempt_key_for_node, has_completed_nodes_in_attempt
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, RunNodeOutput

_ORG = uuid.uuid4()


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=(Organisation.__table__, RunNodeOutput.__table__))
        )
    yield engine
    await engine.dispose()


@pytest.fixture
def sqlite_sessionmaker(sqlite_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)


async def _insert_output(
    maker: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    node_id: str,
    completed: bool,
    claim_token: str | None = None,
) -> None:
    async with maker() as session, session.begin():
        session.add(
            RunNodeOutput(
                run_id=run_id,
                organisation_id=_ORG,
                node_id=node_id,
                attempt_key=attempt_key_for_node(run_id=run_id, node_id=node_id, claim_token=claim_token),
                outputs_json={"result": "ok"},
                completed_at=datetime.now(UTC) if completed else None,
            )
        )


@pytest.mark.asyncio
async def test_completion_writer_persists_timestamp(
    sqlite_engine: AsyncEngine, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="agent", completed=False, claim_token="claim-a")

    await _write_node_completion(sqlite_engine, str(run_id), str(_ORG), "agent", "claim-a")

    async with sqlite_sessionmaker() as session, session.begin():
        row = await session.get(
            RunNodeOutput,
            (run_id, "agent", attempt_key_for_node(run_id=run_id, node_id="agent", claim_token="claim-a")),
        )
        assert row is not None
        assert row.completed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id", ["agent", "connector", "sandbox", "human-policy"])
async def test_all_node_families_use_same_completion_writer(
    sqlite_engine: AsyncEngine,
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
    node_id: str,
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id=node_id, completed=False, claim_token="claim-a")
    await _write_node_completion(sqlite_engine, str(run_id), str(_ORG), node_id, "claim-a")

    async with sqlite_sessionmaker() as session, session.begin():
        assert await has_completed_nodes_in_attempt(session, run_id=run_id, organisation_id=_ORG, claim_token="claim-a")


@pytest.mark.asyncio
async def test_completion_reader_requires_completed_at(sqlite_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    run_id = uuid.uuid4()
    await _insert_output(
        sqlite_sessionmaker, run_id=run_id, node_id="unfinished", completed=False, claim_token="claim-a"
    )
    async with sqlite_sessionmaker() as session, session.begin():
        assert not await has_completed_nodes_in_attempt(
            session, run_id=run_id, organisation_id=_ORG, claim_token="claim-a"
        )


@pytest.mark.asyncio
async def test_completion_reader_rejects_prior_attempt_completion(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="agent", completed=True, claim_token="old-claim")
    async with sqlite_sessionmaker() as session, session.begin():
        assert not await has_completed_nodes_in_attempt(
            session, run_id=run_id, organisation_id=_ORG, claim_token="new-claim"
        )


@pytest.mark.asyncio
async def test_completion_reader_accepts_current_attempt_completion(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(
        sqlite_sessionmaker, run_id=run_id, node_id="agent", completed=True, claim_token="current-claim"
    )
    async with sqlite_sessionmaker() as session, session.begin():
        assert await has_completed_nodes_in_attempt(
            session, run_id=run_id, organisation_id=_ORG, claim_token="current-claim"
        )


@pytest.mark.asyncio
async def test_completion_reader_ignores_sentinel_rows(sqlite_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    run_id = uuid.uuid4()
    async with sqlite_sessionmaker() as session, session.begin():
        session.add(
            RunNodeOutput(
                run_id=run_id,
                organisation_id=_ORG,
                node_id=META_NODE_ID,
                attempt_key=FINAL_ATTEMPT_KEY,
                outputs_json={"empty_outputs": False, "empty_telemetry": False},
                completed_at=datetime.now(UTC),
            )
        )
    async with sqlite_sessionmaker() as session, session.begin():
        assert not await has_completed_nodes_in_attempt(
            session, run_id=run_id, organisation_id=_ORG, claim_token="claim-a"
        )


@pytest.mark.asyncio
async def test_deadline_gate_terminal_fails_without_completion(
    sqlite_engine: AsyncEngine, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="sandbox", completed=False, claim_token="claim-a")
    exec_task = asyncio.create_task(asyncio.sleep(10))
    try:
        with (
            patch("modulo.core.pipeline_execution.fail_run_terminal", new_callable=AsyncMock) as fail,
            patch("modulo.core.pipeline_execution._persist_node_deadline_triage", new_callable=AsyncMock),
            patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", new_callable=AsyncMock),
        ):
            await _fail_overdue_node(
                sqlite_engine,
                str(run_id),
                str(_ORG),
                {"sandbox": (0.0, 1)},
                exec_task,
                asyncio.Event(),
                asyncio.Event(),
                claim_token="claim-a",
            )
            fail.assert_awaited_once()
            assert "No node completed in this attempt" in fail.await_args.kwargs["error_detail"]
    finally:
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task


@pytest.mark.asyncio
async def test_deadline_gate_allows_retry_after_completion(
    sqlite_engine: AsyncEngine, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="agent", completed=True, claim_token="claim-a")
    exec_task = asyncio.create_task(asyncio.sleep(10))
    try:
        with (
            patch(
                "modulo.core.pipeline_execution._maybe_watchdog_retry", new_callable=AsyncMock, return_value=True
            ) as retry,
            patch("modulo.core.pipeline_execution._persist_node_deadline_triage", new_callable=AsyncMock),
            patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", new_callable=AsyncMock),
        ):
            await _fail_overdue_node(
                sqlite_engine,
                str(run_id),
                str(_ORG),
                {"sandbox": (0.0, 1)},
                exec_task,
                asyncio.Event(),
                asyncio.Event(),
                retry_hook=AsyncMock(),
                claim_token="claim-a",
            )
            retry.assert_awaited_once()
            assert retry.await_args.kwargs["veto_if_delivered"] is True
    finally:
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task
