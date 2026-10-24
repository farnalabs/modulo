"""FAR-2050 durable per-node completion and deadline gate tests."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from modulo.core.pipeline_execution import (
    _fail_overdue_node,
    _probe_completed_nodes,
    _wire_node_deadline_hooks,
    _write_node_completion,
)
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
async def test_completion_probe_reads_current_attempt(
    sqlite_engine: AsyncEngine, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="agent", completed=True, claim_token="claim-a")

    assert await _probe_completed_nodes(sqlite_engine, str(run_id), str(_ORG), "claim-a") is True
    assert await _probe_completed_nodes(sqlite_engine, str(run_id), str(_ORG), "claim-b") is False


@pytest.mark.asyncio
async def test_completion_writer_updates_existing_row_without_creating_duplicate(
    sqlite_engine: AsyncEngine,
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="agent", completed=False, claim_token="claim-a")

    await _write_node_completion(sqlite_engine, str(run_id), str(_ORG), "agent", "claim-a")
    await _write_node_completion(sqlite_engine, str(run_id), str(_ORG), "agent", "claim-a")

    async with sqlite_sessionmaker() as session, session.begin():
        result = await session.execute(
            select(RunNodeOutput).where(
                RunNodeOutput.run_id == run_id,
                RunNodeOutput.node_id == "agent",
            )
        )
        assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_completion_writer_logs_and_swallows_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(
            "modulo.core.pipeline_execution.async_sessionmaker",
            side_effect=RuntimeError("database unavailable"),
        ),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_execution"),
    ):
        await _write_node_completion(
            MagicMock(),
            str(uuid.uuid4()),
            str(_ORG),
            "agent",
            "claim-a",
        )

    assert "node_completion_write_failed" in caplog.text


@pytest.mark.asyncio
async def test_node_deadline_hook_records_completion_when_configured() -> None:
    executor = SimpleNamespace()
    deadlines: dict[str, tuple[float, int]] = {}
    started = asyncio.Event()
    completed = asyncio.Event()
    writer = AsyncMock()

    engine = MagicMock()
    with patch("modulo.core.pipeline_execution._write_node_completion", writer):
        _wire_node_deadline_hooks(
            executor,
            deadlines,
            started,
            completed,
            {},
            30,
            aeng=engine,
            run_id="run-1",
            org_id="org-1",
            claim_token="claim-a",
        )
        await executor.on_node_completed("agent")

    writer.assert_awaited_once_with(engine, "run-1", "org-1", "agent", "claim-a")
    assert completed.is_set()
    assert not deadlines


@pytest.mark.asyncio
async def test_node_deadline_hook_skips_completion_record_without_database_context() -> None:
    executor = SimpleNamespace()
    deadlines = {"agent": (1.0, 30)}
    started = asyncio.Event()
    completed = asyncio.Event()

    _wire_node_deadline_hooks(executor, deadlines, started, completed, {}, 30)
    await executor.on_node_completed("agent")

    assert completed.is_set()
    assert not deadlines


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
            patch("modulo.core.pipeline_execution._persist_node_deadline_triage", new_callable=AsyncMock) as triage,
            patch("modulo.core.pipeline_execution._record_derived_deadline_cost", new_callable=AsyncMock) as derived,
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
            triage.assert_awaited_once_with(sqlite_engine, str(run_id), str(_ORG), redispatched=False)
            derived.assert_awaited_once_with(
                sqlite_engine,
                str(run_id),
                str(_ORG),
                error_detail=ANY,
                derived_sandbox_wall_hours=1 / 3600.0,
                killed_node_id="sandbox",
            )
    finally:
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task


@pytest.mark.asyncio
async def test_deadline_gate_skips_triage_and_cost_when_terminal_write_is_rejected(
    sqlite_engine: AsyncEngine, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    run_id = uuid.uuid4()
    await _insert_output(sqlite_sessionmaker, run_id=run_id, node_id="sandbox", completed=False, claim_token="claim-a")
    exec_task = asyncio.create_task(asyncio.sleep(10))
    try:
        with (
            patch(
                "modulo.core.pipeline_execution.fail_run_terminal", new_callable=AsyncMock, return_value=False
            ) as fail,
            patch("modulo.core.pipeline_execution._persist_node_deadline_triage", new_callable=AsyncMock) as triage,
            patch("modulo.core.pipeline_execution._record_derived_deadline_cost", new_callable=AsyncMock) as derived,
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
        triage.assert_not_awaited()
        derived.assert_not_awaited()
    finally:
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task


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
