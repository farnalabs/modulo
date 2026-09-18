"""Unit tests for FAR-104 per-agent token budget enforcement.

Covers ``derive_node_agent_map`` (node→agent attribution from the snapshot
graph), ``_accumulate_agent_tokens`` (per-agent accumulation across the run's
nodes), and ``_enforce_agent_token_budgets`` (the terminal override to
``budget_exceeded`` with the PRD error message).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.cost_controller.finalize import (
    _accumulate_agent_tokens,
    _enforce_agent_token_budgets,
    derive_node_agent_map,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# derive_node_agent_map
# ---------------------------------------------------------------------------


def test_derive_node_agent_map_reads_graph_nodes() -> None:
    graph = {
        "nodes": [
            {"id": "node-a", "agent_id": "aaaaaaaa-0000-0000-0000-000000000001"},
            {"id": "node-b", "agent_id": "bbbbbbbb-0000-0000-0000-000000000002"},
            {"id": "node-gate"},  # HITL gate — no agent_id
        ]
    }
    assert derive_node_agent_map(graph) == {
        "node-a": "aaaaaaaa-0000-0000-0000-000000000001",
        "node-b": "bbbbbbbb-0000-0000-0000-000000000002",
    }


def test_derive_node_agent_map_malformed_degrades_empty() -> None:
    assert not derive_node_agent_map(None)
    assert not derive_node_agent_map({"nodes": "not-a-list"})
    assert not derive_node_agent_map({"nodes": [{"id": "a"}]})
    assert not derive_node_agent_map("not-a-dict")


# ---------------------------------------------------------------------------
# _accumulate_agent_tokens
# ---------------------------------------------------------------------------


def test_accumulate_agent_tokens_sums_per_agent_across_nodes() -> None:
    agent_a = "aaaaaaaa-0000-0000-0000-000000000001"
    agent_b = "bbbbbbbb-0000-0000-0000-000000000002"
    usage = {
        "node-a1": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "node-a2": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        "node-b": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    node_agent_map = {"node-a1": agent_a, "node-a2": agent_a, "node-b": agent_b}
    per_agent = _accumulate_agent_tokens(usage, node_agent_map)
    assert per_agent == {agent_a: 165, agent_b: 2}


def test_accumulate_agent_tokens_unmapped_and_malformed_nodes_contribute_zero() -> None:
    agent_a = "aaaaaaaa-0000-0000-0000-000000000001"
    usage = {
        "node-a": {"total_tokens": 10},
        "node-gate": {"total_tokens": 999},  # no agent_id
        "node-junk": "not-a-dict",
    }
    per_agent = _accumulate_agent_tokens(usage, {"node-a": agent_a})
    assert per_agent == {agent_a: 10}


def test_accumulate_agent_tokens_falls_back_to_input_plus_output() -> None:
    agent_a = "aaaaaaaa-0000-0000-0000-000000000001"
    usage = {"node-a": {"input_tokens": 30, "output_tokens": 12}}  # no total_tokens
    per_agent = _accumulate_agent_tokens(usage, {"node-a": agent_a})
    assert per_agent == {agent_a: 42}


# ---------------------------------------------------------------------------
# _enforce_agent_token_budgets
# ---------------------------------------------------------------------------


def _mock_run() -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.organisation_id = _ORG_ID
    return run


def _mock_session(graph_json: dict[str, Any], agent_rows: list[tuple[Any, int | None]]) -> AsyncMock:
    session = AsyncMock()
    graph_result = MagicMock()
    graph_result.scalar_one_or_none.return_value = graph_json
    agent_result = MagicMock()
    agent_result.all.return_value = agent_rows
    session.execute = AsyncMock(side_effect=[graph_result, agent_result])
    return session


async def test_enforce_budget_exceeded_returns_terminal_override() -> None:
    """110000 accumulated against a 100000 budget → budget_exceeded override."""
    agent_id = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    run = _mock_run()
    session = _mock_session(
        graph_json={"nodes": [{"id": "node-writer", "agent_id": str(agent_id)}]},
        agent_rows=[(agent_id, 100000)],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={"node-writer": {"input_tokens": 110000, "output_tokens": 0, "total_tokens": 110000}},
    )
    assert override == ("budget_exceeded", "budget_exceeded", "This run exceeded its token budget.")


async def test_enforce_budget_within_limit_returns_none() -> None:
    agent_id = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    run = _mock_run()
    session = _mock_session(
        graph_json={"nodes": [{"id": "node-writer", "agent_id": str(agent_id)}]},
        agent_rows=[(agent_id, 100000)],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={"node-writer": {"input_tokens": 90000, "output_tokens": 0, "total_tokens": 90000}},
    )
    assert override is None


async def test_enforce_no_agent_budget_returns_none() -> None:
    """Agents without a configured token_budget are never enforced."""
    agent_id = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    run = _mock_run()
    session = _mock_session(
        graph_json={"nodes": [{"id": "node-writer", "agent_id": str(agent_id)}]},
        agent_rows=[(agent_id, None)],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={"node-writer": {"input_tokens": 10**9, "output_tokens": 0, "total_tokens": 10**9}},
    )
    assert override is None


async def test_enforce_no_agent_nodes_returns_none() -> None:
    run = _mock_run()
    session = _mock_session(
        graph_json={"nodes": [{"id": "node-gate"}]},
        agent_rows=[],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={"node-gate": {"input_tokens": 10**9, "output_tokens": 0, "total_tokens": 10**9}},
    )
    assert override is None


async def test_enforce_check_failure_is_fail_open() -> None:
    """A budget-check DB failure degrades to NO override (never-fail envelope)."""
    run = _mock_run()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={"node-writer": {"input_tokens": 10**9, "output_tokens": 0, "total_tokens": 10**9}},
    )
    assert override is None


async def test_enforce_multiple_nodes_accumulate_per_agent() -> None:
    """Two nodes referencing the SAME agent sum together; one node is within
    budget alone, but the SUM crosses the budget."""
    agent_id = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    run = _mock_run()
    session = _mock_session(
        graph_json={
            "nodes": [
                {"id": "node-a1", "agent_id": str(agent_id)},
                {"id": "node-a2", "agent_id": str(agent_id)},
            ]
        },
        agent_rows=[(agent_id, 100000)],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={
            "node-a1": {"input_tokens": 60000, "output_tokens": 0, "total_tokens": 60000},
            "node-a2": {"input_tokens": 60000, "output_tokens": 0, "total_tokens": 60000},
        },
    )
    assert override is not None
    assert override[0] == "budget_exceeded"


async def test_enforce_multiple_agents_independent() -> None:
    """Agent A exceeds its budget while Agent B stays under — the run trips."""
    agent_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    agent_b = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
    run = _mock_run()
    session = _mock_session(
        graph_json={
            "nodes": [
                {"id": "node-a", "agent_id": str(agent_a)},
                {"id": "node-b", "agent_id": str(agent_b)},
            ]
        },
        agent_rows=[(agent_a, 100000), (agent_b, 100000)],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={
            "node-a": {"input_tokens": 150000, "output_tokens": 0, "total_tokens": 150000},
            "node-b": {"input_tokens": 1000, "output_tokens": 0, "total_tokens": 1000},
        },
    )
    assert override is not None
    assert override[0] == "budget_exceeded"
    assert override[2] == "This run exceeded its token budget."


async def test_enforce_all_within_budget_returns_none() -> None:
    agent_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    agent_b = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
    run = _mock_run()
    session = _mock_session(
        graph_json={
            "nodes": [
                {"id": "node-a", "agent_id": str(agent_a)},
                {"id": "node-b", "agent_id": str(agent_b)},
            ]
        },
        agent_rows=[(agent_a, 100000), (agent_b, 50000)],
    )
    override = await _enforce_agent_token_budgets(
        session,
        run=run,
        usage={
            "node-a": {"input_tokens": 90000, "output_tokens": 0, "total_tokens": 90000},
            "node-b": {"input_tokens": 40000, "output_tokens": 0, "total_tokens": 40000},
        },
    )
    assert override is None


# ---------------------------------------------------------------------------
# Integration of the override through the terminal write path (finalize_cost)
# ---------------------------------------------------------------------------


async def test_finalize_cost_budget_exceeded_overrides_terminal_write() -> None:
    """A budget-exceeded run finalizes with status='budget_exceeded' and the
    PRD error message through update_run_status — the SAME atomic write."""
    from unittest.mock import patch

    from modulo.core.cost_controller.finalize import finalize_cost

    run = MagicMock()
    run.id = uuid.uuid4()
    run.organisation_id = _ORG_ID
    run.owner_team_id = None
    run.node_token_usage = {"node-writer": {"input_tokens": 110000, "output_tokens": 0, "total_tokens": 110000}}
    run.outputs_json = {"node-writer": {"summary": "did the thing"}}
    run.node_telemetry_json = {"node-writer": {"status": "completed", "wall_clock_time_ms": 1000}}
    run.started_at = None
    run.snapshot_id = uuid.uuid4()
    run.ledger_written = False
    run.ledger_refused_at = None
    run.cancellation_requested = False

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))

    with (
        patch("modulo.core.cost_controller.finalize.load_live_components", new=AsyncMock(return_value=[])),
        patch("modulo.core.cost_controller.finalize.build_cost_breakdown", return_value=([], Decimal(0))),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
    ):

        async def _fake_enforce(session: Any, *, run: Any, usage: Any) -> tuple[str, str, str] | None:
            return ("budget_exceeded", "budget_exceeded", "This run exceeded its token budget.")

        with patch(
            "modulo.core.cost_controller.finalize._enforce_agent_token_budgets",
            new=AsyncMock(side_effect=_fake_enforce),
        ):
            await finalize_cost(
                session,
                run_id=run.id,
                org_id=_ORG_ID,
                status="complete",
                segment_node_token_usage=run.node_token_usage,
                segment_completed_node_outputs=run.outputs_json,
                node_type_map={"node-writer": "agent"},
                is_terminal=True,
            )
    mock_urs.assert_awaited_once()
    assert mock_urs.await_args.args[2] == "budget_exceeded"
    kwargs = mock_urs.await_args.kwargs
    assert kwargs["error_code"] == "budget_exceeded"
    assert kwargs["error_detail"] == "This run exceeded its token budget."


# ---------------------------------------------------------------------------
# budget_exceeded is a first-class terminal status (mirroring the stalled
# precedent: model constraint, TERMINAL_STATUSES, RUN_STATUS_WHITELIST, and
# the persistence write)
# ---------------------------------------------------------------------------


def test_budget_exceeded_is_a_terminal_status() -> None:
    from modulo.db.models.run import TERMINAL_STATUSES

    assert "budget_exceeded" in TERMINAL_STATUSES


def test_run_model_check_constraint_allows_budget_exceeded() -> None:
    from sqlalchemy import CheckConstraint

    from modulo.db.models.run import Run

    table_args = Run.__table_args__
    check_sql = " ".join(
        arg.sqltext.text for arg in table_args if isinstance(arg, CheckConstraint) and arg.name == "ck_runs_status"
    )
    assert "'budget_exceeded'" in check_sql


def test_run_status_whitelist_includes_budget_exceeded() -> None:
    from modulo.db.crud.run import RUN_STATUS_WHITELIST

    assert "budget_exceeded" in RUN_STATUS_WHITELIST


@pytest.fixture
async def sqlite_runs_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    from modulo.db.models.base import Base
    from modulo.db.models.run import Run

    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__]))
    yield eng
    await eng.dispose()


async def test_update_run_status_persists_budget_exceeded_with_completed_at(sqlite_runs_engine) -> None:
    """Persistence-layer coverage: update_run_status accepts 'budget_exceeded'
    and stamps completed_at on the real row — the end-to-end write a
    budget-exceeded run goes through in finalize_cost. Without the whitelist +
    completed_at wiring this raises ValueError or leaves completed_at NULL."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from modulo.db.crud.run import update_run_status
    from modulo.db.models.run import Run

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
        run = await update_run_status(session, run_id, "budget_exceeded")
        assert run is not None
        assert run.status == "budget_exceeded"
        assert run.completed_at is not None

    async with factory() as session:
        persisted = await session.execute(select(Run).where(Run.id == run_id))
        row = persisted.scalar_one_or_none()
    assert row is not None
    assert row.status == "budget_exceeded"
    assert row.completed_at is not None
