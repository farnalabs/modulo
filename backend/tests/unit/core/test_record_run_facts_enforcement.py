"""FAR-902: end-to-end proof that record_run_facts populates runs columns.

CRITICAL 1 regression test: ``runs.schema_validator_mode`` and
``runs.schema_validation_outcome`` must be non-NULL after ``record_run_facts``
runs on a terminal run that has enforcement records in ``run_node_outputs``.

The test must FAIL if the assignment in ``record_run_facts`` is removed — a
unit test that calls the deriver directly does NOT prove the write.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.analytics import record_run_facts
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.run_node_outputs import RunNodeOutput

_TABLE_NAMES = {"organisations", "runs", "run_node_outputs", "run_daily_facts"}

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_PIPELINE = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SNAPSHOT = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _make_run(*, run_id: uuid.UUID, status: str = "complete") -> Run:
    return Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        status=status,
        run_number=1,
        input_hash="0" * 64,
        langgraph_thread_id=f"thread-{run_id.hex}",
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


def _make_enforcement_row(
    *,
    run_id: uuid.UUID,
    node_id: str = "agent-1",
    attempt_key: str = "attempt-0",
    outcome: str = "native_decoded_and_validated",
) -> RunNodeOutput:
    return RunNodeOutput(
        organisation_id=_ORG,
        run_id=run_id,
        node_id=node_id,
        attempt_key=attempt_key,
        outputs_json={"result": "ok"},
        schema_enforcement_json={
            "outcome": outcome,
            "resolved_profile": "provider-strict",
            "native_output": True,
            "repair_attempts": 0,
            "wasted_attempts": 0,
            "validation_errors": [],
            "total_error_count": 0,
            "truncated": False,
        },
    )


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Mock the dimension reads so record_run_facts doesn't need pipelines/teams/snapshots tables.
_DIMENSION_PATCHER = patch(
    "modulo.core.analytics._snapshot_dimensions", new_callable=AsyncMock, return_value=(None, None, None)
)
_GRAPH_PATCHER = patch(
    "modulo.core.analytics._snapshot_graph_dimensions", new_callable=AsyncMock, return_value=(0, 0, None)
)


class TestRecordRunFactsPopulatesRunColumns:
    """CRITICAL 1: record_run_facts must write mode/outcome onto the Run row."""

    @pytest.mark.anyio
    async def test_strict_mode_outcome_populated(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """A run with strict enforcement records gets non-NULL mode and outcome.

        FAILS WITHOUT FIX: the assignment in record_run_facts does not exist,
        so the columns stay NULL and the API route always falls through.
        """
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(
                _make_enforcement_row(
                    run_id=run_id,
                    outcome="native_decoded_and_validated",
                )
            )
            await session.commit()

        with _DIMENSION_PATCHER, _GRAPH_PATCHER:
            async with factory() as session:
                run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await record_run_facts(session, run)
                await session.commit()

        async with factory() as session:
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert run.schema_validator_mode is not None, (
                "schema_validator_mode must be non-NULL after record_run_facts — the assignment is missing"
            )
            assert run.schema_validation_outcome is not None, (
                "schema_validation_outcome must be non-NULL after record_run_facts — the assignment is missing"
            )
            assert run.schema_validator_mode == "strict"
            assert run.schema_validation_outcome == "native_decoded_and_validated"

    @pytest.mark.anyio
    async def test_lenient_mode_populated(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """A run with a lenient bypass record gets mode='lenient'."""
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(
                _make_enforcement_row(
                    run_id=run_id,
                    outcome="lenient_validation_bypassed",
                )
            )
            await session.commit()

        with _DIMENSION_PATCHER, _GRAPH_PATCHER:
            async with factory() as session:
                run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await record_run_facts(session, run)
                await session.commit()

        async with factory() as session:
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert run.schema_validator_mode == "lenient"
            assert run.schema_validation_outcome == "lenient_validation_bypassed"

    @pytest.mark.anyio
    async def test_no_enforcement_records_stays_none(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """A run with NO enforcement records keeps NULL mode/outcome."""
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            await session.commit()

        with _DIMENSION_PATCHER, _GRAPH_PATCHER:
            async with factory() as session:
                run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await record_run_facts(session, run)
                await session.commit()

        async with factory() as session:
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert run.schema_validator_mode is None
            assert run.schema_validation_outcome is None

    @pytest.mark.anyio
    async def test_most_severe_outcome_wins(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Mixed enforcement records: the most severe outcome is chosen."""
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(
                _make_enforcement_row(
                    run_id=run_id,
                    node_id="agent-1",
                    attempt_key="attempt-0",
                    outcome="native_decoded_and_validated",
                )
            )
            session.add(
                _make_enforcement_row(
                    run_id=run_id,
                    node_id="agent-2",
                    attempt_key="attempt-0",
                    outcome="repair_exhausted",
                )
            )
            await session.commit()

        with _DIMENSION_PATCHER, _GRAPH_PATCHER:
            async with factory() as session:
                run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await record_run_facts(session, run)
                await session.commit()

        async with factory() as session:
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert run.schema_validator_mode == "strict"
            # repair_exhausted (severity 0) is more severe than native_decoded_and_validated (severity 8)
            assert run.schema_validation_outcome == "repair_exhausted"

    @pytest.mark.anyio
    async def test_run_daily_fact_also_written(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """The fact row is written with enforcement counters alongside the Run columns."""
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(
                _make_enforcement_row(
                    run_id=run_id,
                    outcome="verbatim_passed",
                )
            )
            await session.commit()

        with _DIMENSION_PATCHER, _GRAPH_PATCHER:
            async with factory() as session:
                run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await record_run_facts(session, run)
                await session.commit()

        async with factory() as session:
            fact = (await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run_id))).scalar_one()
            assert fact.enforcement_native_count == 0
            assert fact.enforcement_verbatim_count == 1
            assert fact.enforcement_repair_count == 0
            assert fact.enforcement_wasted_count == 0
