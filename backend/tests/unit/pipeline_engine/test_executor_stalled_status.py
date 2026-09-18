"""Unit tests for the stalled-sandbox-run terminal status (FAR-98 fix).

A sandbox-agent node that STALLS (idle watchdog) or TIMES OUT RETURNS a
failed output dict carrying ``stall_reason`` instead of raising. LangGraph
sees the node complete normally, so ``_stream_graph`` used to finish the
event loop and unconditionally return ``("complete", ...)`` — the run was
recorded with status ``complete``. These tests pin the fixed behaviour:
a captured node output carrying ``stall_reason`` must produce the terminal
run status ``"stalled"`` with error code ``"executor_stalled"``.
"""

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.executor import (
    _TERMINAL_STATUSES,
    PipelineExecutor,
    _node_output_stall_reason,
)
from modulo.db.crud.run import RUN_STATUS_WHITELIST, update_run_status
from modulo.db.models.base import Base
from modulo.db.models.run import TERMINAL_STATUSES, Run


def _stalled_event(stall_reason: str) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "node-a",
        "data": {
            "output": {
                "output": {
                    "status": "failed",
                    "stall_reason": stall_reason,
                }
            }
        },
    }


def _complete_event() -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "node-a",
        "data": {"output": {"output": {"status": "completed", "summary": "all good"}}},
    }


def _mock_compiled(events: list[dict[str, Any]]) -> MagicMock:
    """Compiled graph mock whose astream_events yields the given events."""

    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        for e in events:
            yield e

    c = MagicMock()
    c.astream_events = _astream
    return c


async def _run_stream_graph(events: list[dict[str, Any]]) -> tuple[Any, ...]:
    """Drive ``_stream_graph`` directly with the given fake LangGraph events."""
    executor = PipelineExecutor(MagicMock())
    completed_node_outputs: dict[str, Any] = {}
    return await executor._stream_graph(
        _mock_compiled(events),
        None,
        {"configurable": {"thread_id": str(uuid.uuid4())}},
        {"node-a"},
        MagicMock(),
        uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        completed_node_outputs=completed_node_outputs,
    )


def test_node_output_stall_reason_extracts_non_empty_reason():
    """The helper surfaces a non-empty stall_reason from a sandbox-style output."""
    stalled = {"output": {"status": "failed", "stall_reason": "agent produced no output for 60s"}}
    assert _node_output_stall_reason(stalled) == "agent produced no output for 60s"


def test_node_output_stall_reason_ignores_non_stalled_output():
    """Completed / garbage outputs never look like a stall."""
    assert _node_output_stall_reason({"output": {"status": "completed", "summary": "ok"}}) is None
    assert _node_output_stall_reason({"output": {"stall_reason": ""}}) is None
    assert _node_output_stall_reason("not-a-dict") is None


async def test_stream_graph_returns_stalled_when_node_output_carries_stall_reason():
    """The core fix: a stalled node output produces terminal status 'stalled'
    with error code 'executor_stalled', never 'complete'."""
    reason = "agent produced no output for 60s"
    final_status, error_code, error_detail, _node_token_usage = await _run_stream_graph([_stalled_event(reason)])

    assert final_status == "stalled"
    assert error_code == "executor_stalled"
    assert error_detail == reason


async def test_stream_graph_still_returns_complete_without_stall_reason():
    """Regression guard: a cleanly completed node output keeps the old path."""
    final_status, error_code, error_detail, _node_token_usage = await _run_stream_graph([_complete_event()])

    assert final_status == "complete"
    assert error_code is None
    assert error_detail is None


def test_stalled_is_a_terminal_status():
    """A stalled run is terminal — it must never be resurrected or retried."""
    assert "stalled" in _TERMINAL_STATUSES


def test_run_model_check_constraint_allows_stalled():
    """The DB CHECK constraint on runs.status accepts 'stalled'."""
    table_args = Run.__table_args__
    check_sql = " ".join(
        arg.sqltext.text for arg in table_args if isinstance(arg, CheckConstraint) and arg.name == "ck_runs_status"
    )
    assert "'stalled'" in check_sql


def test_run_status_whitelist_includes_stalled():
    """The persistence whitelist accepts 'stalled' — without this,
    update_run_status / transition_run raise ValueError and a stalled run is
    never recorded (the _stream_graph-only tests never reached this layer)."""
    assert "stalled" in RUN_STATUS_WHITELIST


def test_terminal_statuses_include_stalled():
    """The shared single-source-of-truth terminal set includes 'stalled' so
    org-deletion, the analytics backfill, and the purge treat it as terminal."""
    assert "stalled" in TERMINAL_STATUSES


@pytest.fixture
async def sqlite_runs_engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__]))
    yield eng
    await eng.dispose()


async def test_update_run_status_persists_stalled_with_completed_at(sqlite_runs_engine):
    """Persistence-layer coverage: update_run_status accepts 'stalled' and
    stamps completed_at on the real row — the end-to-end write a stalled run
    goes through in finalize_cost. Without the whitelist + completed_at
    wiring this raises ValueError or leaves completed_at NULL."""
    factory = async_sessionmaker(sqlite_runs_engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            Run(
                id=run_id,
                organisation_id=org_id,
                pipeline_id=uuid.uuid4(),
                snapshot_id=uuid.uuid4(),
                trigger_type="manual",
                status="running",
                run_number=1,
                input_hash="a" * 64,
                langgraph_thread_id=f"thread-{run_id}",
            )
        )
        await session.flush()

    async with factory() as session, session.begin():
        run = await update_run_status(session, run_id, "stalled")
        assert run is not None
        assert run.status == "stalled"
        assert run.completed_at is not None

    async with factory() as session:
        persisted = await session.execute(select(Run).where(Run.id == run_id))
        row = persisted.scalar_one_or_none()
    assert row is not None
    assert row.status == "stalled"
    assert row.completed_at is not None
