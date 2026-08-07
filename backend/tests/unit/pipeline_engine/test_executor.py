"""Unit tests for PipelineExecutor using mocked DB sessions."""

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    RunNotFoundError,
    _graph_contains_sandbox_agent,
    _node_output_stall_reason,
    _seed_state,
)


class _InterruptState(TypedDict, total=False):
    artifacts: list[dict[str, Any]]


def test_compute_token_costs_treats_null_counters_as_zero():
    usage: Any = {
        "node-a": {"total_tokens": None, "input_tokens": None, "output_tokens": None},
    }

    total_tokens, total_cost, result_usage = PipelineExecutor._compute_token_costs(
        usage,
        input_rate=Decimal("0.1"),
        output_rate=Decimal("0.2"),
    )

    assert total_tokens == 0
    assert total_cost == Decimal(0)
    assert result_usage == {
        "node-a": {
            "total_tokens": None,
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": 0.0,
        }
    }


def test_aggregate_sandbox_cost_sums_positive_estimates():
    """Only positive numeric cost_estimate_usd values inside node output dicts count."""
    completed_node_outputs: dict[str, Any] = {
        "node-a": {
            "output": {
                "status": "completed",
                "cost_estimate_usd": 0.5,
            }
        },
        "node-b": {
            "output": {
                "status": "completed",
                "cost_estimate_usd": 0.25,
            }
        },
        # No cost_estimate_usd key at all → contributes 0.
        "node-c": {
            "output": {
                "status": "completed",
                "summary": "no cost reported",
            }
        },
        # Zero and negative estimates must not count toward the run cost.
        "node-d": {
            "output": {
                "status": "failed",
                "cost_estimate_usd": 0,
            }
        },
        "node-e": {
            "output": {
                "status": "failed",
                "cost_estimate_usd": -1.0,
            }
        },
    }

    total = PipelineExecutor._aggregate_sandbox_cost(completed_node_outputs)

    assert total == Decimal("0.75")


def test_aggregate_sandbox_cost_ignores_non_dict():
    """Garbage entries (None, strings, missing 'output') don't crash and contribute 0."""
    completed_node_outputs: dict[str, Any] = {
        "node-a": None,
        "node-b": "some-string",
        "node-c": 42,
        "node-d": {"output": None},
        "node-e": {"output": "not-a-dict"},
        "node-f": {"output": {"cost_estimate_usd": "not-a-number"}},
        "node-g": {"output": {"cost_estimate_usd": None}},
        # Non-finite floats must not corrupt the run total.
        "node-h": {"output": {"cost_estimate_usd": float("inf")}},
        "node-i": {"output": {"cost_estimate_usd": float("nan")}},
    }

    assert PipelineExecutor._aggregate_sandbox_cost(completed_node_outputs) == Decimal(0)
    assert PipelineExecutor._aggregate_sandbox_cost(None) == Decimal(0)
    assert PipelineExecutor._aggregate_sandbox_cost({}) == Decimal(0)


def test_node_output_stall_reason_extraction():
    """_node_output_stall_reason only surfaces a non-empty stall_reason from a
    sandbox-style node output; garbage and non-stalled outputs yield None."""
    stalled = {"output": {"status": "failed", "stall_reason": "agent produced no output for 60s"}}
    assert _node_output_stall_reason(stalled) == "agent produced no output for 60s"
    assert _node_output_stall_reason({"output": {"status": "completed", "summary": "ok"}}) is None
    assert _node_output_stall_reason({"output": {"stall_reason": ""}}) is None
    assert _node_output_stall_reason({"output": None}) is None
    assert _node_output_stall_reason("not-a-dict") is None
    assert _node_output_stall_reason(None) is None
    assert _node_output_stall_reason({"output": {"stall_reason": 42}}) is None


async def test_execute_routes_completed_outputs_to_finalize_cost():
    """execute() routes the ACCUMULATED completed-node outputs through finalize_cost.

    PR A2: the executor no longer aggregates sandbox cost inline — it passes
    the accumulated ``completed_node_outputs`` to ``finalize_cost``, which
    computes the breakdown + total and runs the ledger block (§4.2).
    """
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {
                "output": {
                    "output": {"status": "completed", "cost_estimate_usd": 0.5},
                }
            },
        }
    ]
    compiled = _mock_compiled(events)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    call = mock_finalize.await_args
    assert call.kwargs["status"] == "complete"
    assert call.kwargs["is_terminal"] is True
    assert call.kwargs["node_type_map"] == {"node-a": ""}
    assert "node-a" in call.kwargs["segment_completed_node_outputs"]


async def test_resume_routes_completed_outputs_to_finalize_cost():
    """resume() mirrors execute(): the resumed segment's outputs reach finalize_cost."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_resume_session(snapshot)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {
                "output": {
                    "output": {"status": "completed", "cost_estimate_usd": 0.75},
                }
            },
        }
    ]
    compiled = _mock_compiled(events)
    compiled.aupdate_state = AsyncMock()

    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)

    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    call = mock_finalize.await_args
    assert call.kwargs["status"] == "complete"
    assert call.kwargs["is_terminal"] is True
    assert call.kwargs["node_type_map"] == {"node-a": ""}
    assert "node-a" in call.kwargs["segment_completed_node_outputs"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    status: str = "pending",
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.snapshot_id = snapshot_id or uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = status
    return run


def _make_snapshot(graph_json: dict[str, Any] | None = None) -> MagicMock:
    snap = MagicMock()
    snap.graph_json = graph_json or {
        "nodes": [{"id": "node-a", "role": None}],
        "edges": [],
    }
    snap.run_context_defaults = {"context_key": "context_val"}
    return snap


def _make_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 30
    pipeline.max_duration_seconds = 3600
    pipeline.max_steps = 100
    pipeline.token_budget = None
    return pipeline


def _make_session(snapshot: MagicMock) -> AsyncMock:
    pipeline = _make_pipeline()

    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = pipeline

    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot

    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    eval_result.scalars.return_value = scalars_mock

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    # Return pipeline first, snapshot second, then eval query, then count query
    execute_results = iter([pipeline_result, snapshot_result, eval_result, count_result])

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        try:
            return next(execute_results)
        except StopIteration:
            return count_result

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.add = MagicMock()
    session.execute = _execute
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _make_resume_session(snapshot: MagicMock) -> AsyncMock:
    """Session mock whose execute() order matches resume()'s query sequence.

    resume() queries the snapshot FIRST, then the pipeline — the opposite of
    execute(), so the shared _make_session iterator is not reusable here.
    """
    pipeline = _make_pipeline()

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipeline

    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot

    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    eval_result.scalars.return_value = scalars_mock

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    execute_results = iter([snapshot_result, pipeline_result, eval_result, count_result])

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        try:
            return next(execute_results)
        except StopIteration:
            return count_result

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = _execute
    return session


def _mock_graph_validator() -> MagicMock:
    """Return a GraphValidator class mock whose validate_for_run always succeeds."""
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


def _mock_compiled(events: list[dict[str, Any]] | None = None) -> MagicMock:
    """Return a compiled graph mock whose astream_events yields the given events."""

    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        for e in events or []:
            yield e

    c = MagicMock()
    c.astream_events = _astream
    return c


def _mock_compiled_raising(exc: Exception) -> MagicMock:
    """Return a compiled graph mock whose astream_events raises the given exception."""

    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        raise exc
        yield  # pragma: no cover  # makes this an async generator

    c = MagicMock()
    c.astream_events = _astream
    return c


def _mock_registry() -> MagicMock:
    broker = MagicMock()
    broker.publish = MagicMock()
    broker.is_closed = False
    registry = MagicMock()
    registry.get_or_create.return_value = broker
    registry.close = MagicMock()
    return registry


# ---------------------------------------------------------------------------
# _seed_state
# ---------------------------------------------------------------------------


def test_seed_state_merges_defaults_and_input():
    snap = _make_snapshot()
    snap.run_context_defaults = {"key": "default"}
    state = _seed_state(snap, {"key": "override", "extra": 1})
    assert state["run_context"]["input"] == {"key": "override", "extra": 1}
    assert state["run_context"]["cancelled"] is False
    assert state["artifacts"] == []


def test_seed_state_snapshot_defaults_present():
    snap = _make_snapshot()
    snap.run_context_defaults = {"env": "prod"}
    state = _seed_state(snap, {})
    assert state["run_context"]["env"] == "prod"


def test_seed_state_injects_pipeline_default_autonomy():
    snap = _make_snapshot()
    snap.default_autonomy_level = "fully_autonomous"
    state = _seed_state(snap, {})
    assert state["run_context"]["_pipeline_default_autonomy"] == "fully_autonomous"


def test_seed_state_skips_autonomy_when_snapshot_has_none():
    snap = _make_snapshot()
    snap.default_autonomy_level = None
    state = _seed_state(snap, {})
    assert "_pipeline_default_autonomy" not in state["run_context"]


def test_seed_state_seeds_iteration_counts():
    """The loop-edge counter must be seeded so router mutations persist.

    Without ``_iteration_counts`` in the initial LangGraph state the loop
    router's ``state.get("_iteration_counts", {})`` returns a brand-new dict
    on every call and the mutation is lost, so ``max_iterations`` never trips
    and the loop edge runs forever.
    """
    snap = _make_snapshot()
    state = _seed_state(snap, {})
    assert state["_iteration_counts"] == {}


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — happy path
# ---------------------------------------------------------------------------


async def test_execute_success_transitions_status():
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={"x": 1})

    assert result is final_run
    call = mock_finalize.await_args
    assert call.kwargs["status"] == "complete"
    assert call.kwargs.get("error_code") is None
    assert call.kwargs["is_terminal"] is True


async def test_execute_publishes_run_completed_event():
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    published_types = [call.args[0] for call in broker.publish.call_args_list]
    assert "run_completed" in published_types


async def test_execute_publishes_run_stalled_when_node_output_carries_stall_reason():
    """A sandbox-agent node output carrying stall_reason publishes run_stalled
    so the run.stalled notification advertised by FAR-98 is actually reachable."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {
                "output": {
                    "output": {
                        "status": "failed",
                        "stall_reason": "agent produced no output for 60s",
                    }
                }
            },
        }
    ]
    compiled = _mock_compiled(events)
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    stalled_calls = [call.args for call in broker.publish.call_args_list if call.args[0] == "run_stalled"]
    assert stalled_calls == [("run_stalled", {"node_id": "node-a", "stall_reason": "agent produced no output for 60s"})]


async def test_execute_does_not_publish_run_stalled_without_stall_reason():
    """A normal (non-stalled) node output never emits run_stalled."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {"output": {"output": {"status": "completed", "summary": "all good"}}},
        }
    ]
    compiled = _mock_compiled(events)
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    stalled_calls = [call.args for call in broker.publish.call_args_list if call.args[0] == "run_stalled"]
    assert stalled_calls == []


async def test_execute_seeds_state_with_run_context():
    """astream_events receives state with cancelled=False and the input_payload."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    captured_state: dict[str, Any] = {}

    async def _capture_stream(state: Any, config: Any, *, version: str = "v1") -> Any:
        captured_state.update(state)
        return
        yield  # pragma: no cover

    compiled = MagicMock()
    compiled.astream_events = _capture_stream

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={"task": "do it"})

    assert captured_state["run_context"]["cancelled"] is False
    assert captured_state["run_context"]["input"] == {"task": "do it"}
    assert captured_state["artifacts"] == []


async def test_execute_fires_on_first_progress_once():
    """_stream_graph fires on_first_progress exactly once — at the FIRST node
    dispatch — so the execute_run zombie watchdog stands down once real node
    work begins (pipeline_execution.zombie_watchdog)."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    events = [
        {"event": "on_chain_start", "name": "node-a", "data": {}},
        {"event": "on_chain_end", "name": "node-a", "data": {"output": {"status": "ok"}}},
    ]
    compiled = _mock_compiled(events)
    registry = _mock_registry()
    progress: list[str] = []

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        executor.on_first_progress = lambda: progress.append("first")
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert progress == ["first"]


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — run not found
# ---------------------------------------------------------------------------


async def test_execute_raises_when_run_not_found():
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=None),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunNotFoundError):
            await executor.execute(run_id=uuid.uuid4(), org_id=uuid.uuid4(), input_payload={})


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — graph raises exception → failed status
# ---------------------------------------------------------------------------


async def test_execute_marks_failed_on_graph_exception():
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RuntimeError("oops"))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    call = mock_finalize.await_args
    assert call.kwargs["status"] == "failed"
    assert call.kwargs.get("error_code") == "RuntimeError"
    assert call.kwargs["is_terminal"] is True


async def test_execute_error_code_matches_exception_type():
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(ValueError("bad input"))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert mock_finalize.await_args.kwargs.get("error_code") == "ValueError"


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — GraphInterrupt → awaiting_human
# ---------------------------------------------------------------------------


async def test_execute_sets_awaiting_human_on_node_interrupt():
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="awaiting_human")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(GraphInterrupt((Interrupt(value={"gate_id": "step-1"}),)))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    call = mock_finalize.await_args
    assert call.kwargs["status"] == "awaiting_human"
    assert call.kwargs["is_terminal"] is False
    # Broker NOT closed when run is awaiting_human
    registry.close.assert_not_called()


async def test_execute_publishes_hitl_awaiting_event():
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="awaiting_human")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(GraphInterrupt((Interrupt(value={"gate_id": "gate-1"}),)))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    published_types = [call.args[0] for call in broker.publish.call_args_list]
    assert "hitl_awaiting" in published_types


async def test_execute_handles_streamed_interrupt_from_real_graph():
    async def interrupting_gate(_state: _InterruptState) -> _InterruptState:
        interrupt({"gate_id": "native-gate"})
        return {}

    graph = StateGraph(_InterruptState)
    graph.add_node("native-gate", interrupting_gate)
    graph.add_edge(START, "native-gate")
    graph.add_edge("native-gate", END)
    compiled = graph.compile()

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="awaiting_human")
    snapshot = _make_snapshot({"nodes": [{"id": "native-gate", "role": None}], "edges": []})
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    hitl_manager = MagicMock()
    hitl_manager.create_gate = AsyncMock()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.HITLManager", return_value=hitl_manager),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    assert mock_finalize.await_args.kwargs["status"] == "awaiting_human"
    hitl_manager.create_gate.assert_awaited_once()
    broker = registry.get_or_create.return_value
    published_types = [call.args[0] for call in broker.publish.call_args_list]
    assert "hitl_awaiting" in published_types
    assert "run_completed" not in published_types
    registry.close.assert_not_called()


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — cache key uses graph_json_hash
# ---------------------------------------------------------------------------


async def test_execute_passes_hash_to_cache():
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    graph_json = {"nodes": [{"id": "n"}], "edges": []}
    snapshot = _make_snapshot(graph_json=graph_json)
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    captured_args: list[Any] = []

    def fake_get_or_compile(pipeline_id: Any, snapshot_id: Any, factory_fn: Any, **kwargs: Any) -> Any:
        captured_args.extend([pipeline_id, snapshot_id])
        return _mock_compiled()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.executor.get_or_compile",
            side_effect=fake_get_or_compile,
        ),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert captured_args[0] == run.pipeline_id
    assert captured_args[1] == run.snapshot_id


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — max_concurrent_runs enforcement
# ---------------------------------------------------------------------------


def _make_pipeline_with_capacity(max_concurrent_runs: int = 5) -> MagicMock:
    p = MagicMock()
    p.max_concurrent_runs = max_concurrent_runs
    p.lock_wait_timeout_seconds = 1
    return p


async def test_execute_times_out_when_at_capacity():
    """When max_concurrent_runs is exceeded, run times out with lock_timeout error."""
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    _mock_registry()

    def never_has_capacity() -> int:
        return 999

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            return_value=run,
        ),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=run,
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline",
            side_effect=never_has_capacity,
        ),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result.status == "pending"


async def test_execute_proceeds_when_under_capacity():
    """When under max_concurrent_runs, execution proceeds normally."""
    run = _make_run()
    running_run = _make_run(run_id=run.id, status="running")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            side_effect=[run, running_run, running_run, running_run],
        ),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline",
            return_value=2,
        ),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result.status == "running"


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — cancellation
# ---------------------------------------------------------------------------
async def test_execute_sets_cancelled_on_run_cancelled_error():
    from modulo.core.pipeline_engine.decorator import RunCancelledError

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="cancelled")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RunCancelledError("cancelled"))
    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=_mock_registry()),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result.status == "cancelled"
    assert mock_finalize.await_args.kwargs["status"] == "cancelled"
    assert mock_finalize.await_args.kwargs["is_terminal"] is True


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — EvalBlockedError → eval_failed
# ---------------------------------------------------------------------------


async def _bypass_capacity(mock_self, **kwargs):
    """Return a run with status='running' to bypass the capacity check."""
    run = MagicMock()
    run.status = "running"
    return run


async def test_execute_sets_eval_failed_on_eval_blocked_error():
    from modulo.core.eval_engine import EvalBlockedError

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="eval_failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(EvalBlockedError("test-eval", "score 0.3 below threshold 0.8"))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    call = mock_finalize.await_args
    assert call.kwargs["status"] == "eval_failed"
    assert call.kwargs.get("error_code") == "eval_blocked"
    assert call.kwargs["is_terminal"] is True


async def test_execute_publishes_run_failed_on_eval_blocked():
    from modulo.core.eval_engine import EvalBlockedError

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="eval_failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(EvalBlockedError("test-eval", "regex mismatch"))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    published_events = [call.args for call in broker.publish.call_args_list if call.args[0] == "run_failed"]
    assert len(published_events) == 1
    payload = published_events[0][1]
    assert payload["error"] == "eval_blocked"
    assert "regex mismatch" in payload["detail"]


async def test_execute_eval_failed_stores_error_detail():
    from modulo.core.eval_engine import EvalBlockedError

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="eval_failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(EvalBlockedError("quality-check", "failed llm judge"))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    call = mock_finalize.await_args
    assert call.kwargs.get("error_detail") is not None
    assert "failed llm judge" in call.kwargs["error_detail"]
    assert call.kwargs["status"] == "eval_failed"


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — graph compilation failure
# ---------------------------------------------------------------------------


async def test_execute_fails_on_bad_graph():
    """A graph with a cycle should raise GraphValidationError before execution."""
    from modulo.core.pipeline_engine.executor import GraphValidationError

    run = _make_run()
    snapshot = _make_snapshot(
        {
            "nodes": [{"id": "a"}],
            "edges": [{"source": "a", "target": "a", "type": "normal"}],
        }
    )
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(GraphValidationError, match=r"cycle|entry"):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — checkpointer connection failure
# ---------------------------------------------------------------------------


async def test_execute_fails_on_checkpointer_connection_error():
    """When the checkpointer can't connect, the run is marked failed."""
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()
    final_run = _make_run(run_id=run.id, status="failed")

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", side_effect=[run, final_run]),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope") as mock_scope,
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        mock_scope.side_effect = ConnectionError("db not available")

        executor = PipelineExecutor(MagicMock(), checkpointer_conn_string="postgresql://bad:5432/db")
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    # Should have been marked failed, not stuck in running
    assert mock_finalize.await_args.kwargs["status"] == "failed"


# ---------------------------------------------------------------------------
# _graph_contains_sandbox_agent — pure top-level sandbox-node detection
# ---------------------------------------------------------------------------


def test_graph_contains_sandbox_agent_false_for_none():
    assert _graph_contains_sandbox_agent(None) is False


def test_graph_contains_sandbox_agent_false_for_non_dict():
    assert _graph_contains_sandbox_agent([]) is False
    assert _graph_contains_sandbox_agent("sandbox") is False
    assert _graph_contains_sandbox_agent(42) is False


def test_graph_contains_sandbox_agent_false_when_missing_nodes():
    assert _graph_contains_sandbox_agent({"edges": []}) is False
    assert _graph_contains_sandbox_agent({}) is False


def test_graph_contains_sandbox_agent_true_for_sandbox_agent_node():
    graph = {"nodes": [{"id": "a", "node_type": "sandbox_agent"}]}
    assert _graph_contains_sandbox_agent(graph) is True


def test_graph_contains_sandbox_agent_false_for_other_node_types():
    graph = {"nodes": [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "connector"}]}
    assert _graph_contains_sandbox_agent(graph) is False


# ---------------------------------------------------------------------------
# get_sandbox_concurrency_limit — fail-open setting reader
# ---------------------------------------------------------------------------


def _org_with_settings(settings: Any) -> MagicMock:
    org = MagicMock()
    org.settings_json = settings
    return org


async def test_get_sandbox_concurrency_limit_unset_returns_none():
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    with patch("modulo.db.crud.run.get_organisation", return_value=_org_with_settings({})):
        assert await get_sandbox_concurrency_limit(AsyncMock(), uuid.uuid4()) is None


async def test_get_sandbox_concurrency_limit_returns_int():
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    org = _org_with_settings({"sandbox_concurrency_limit": 5})
    with patch("modulo.db.crud.run.get_organisation", return_value=org):
        assert await get_sandbox_concurrency_limit(AsyncMock(), uuid.uuid4()) == 5


async def test_get_sandbox_concurrency_limit_clamps_out_of_range():
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    org_high = _org_with_settings({"sandbox_concurrency_limit": 9999})
    with patch("modulo.db.crud.run.get_organisation", return_value=org_high):
        assert await get_sandbox_concurrency_limit(AsyncMock(), uuid.uuid4()) == 100
    org_low = _org_with_settings({"sandbox_concurrency_limit": 0})
    with patch("modulo.db.crud.run.get_organisation", return_value=org_low):
        assert await get_sandbox_concurrency_limit(AsyncMock(), uuid.uuid4()) == 1


@pytest.mark.parametrize(
    "bad_value",
    ["3", 3.0, True, False, [3], {"v": 3}],
)
async def test_get_sandbox_concurrency_limit_fail_open_on_bad_type(bad_value):
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    with patch(
        "modulo.db.crud.run.get_organisation",
        return_value=_org_with_settings({"sandbox_concurrency_limit": bad_value}),
    ):
        assert await get_sandbox_concurrency_limit(AsyncMock(), uuid.uuid4()) is None


async def test_get_sandbox_concurrency_limit_fail_open_on_non_dict_settings():
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    with patch("modulo.db.crud.run.get_organisation", return_value=_org_with_settings("not-a-dict")):
        assert await get_sandbox_concurrency_limit(AsyncMock(), uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# _check_capacity — org sandbox cap enforcement
# ---------------------------------------------------------------------------


def _make_capacity_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_capacity_executor(session: AsyncMock) -> PipelineExecutor:
    @asynccontextmanager
    async def _ctx():
        yield session

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = MagicMock(side_effect=lambda: _ctx())
    return executor


def _capacity_run(status: str = "pending") -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.cancellation_requested = False
    return run


def _make_update_status(run: MagicMock, calls: list[tuple[str, dict[str, Any]]]):
    async def _update_status(_session: Any, run_id: Any, status: str, **kwargs: Any) -> Any:
        run.status = status
        if kwargs.get("clear_error_code"):
            run.error_code = None
            run.error_detail = None
        if "error_code" in kwargs:
            run.error_code = kwargs["error_code"]
        if "error_detail" in kwargs:
            run.error_detail = kwargs["error_detail"]
        calls.append((status, kwargs))
        return run

    return _update_status


async def test_check_capacity_skips_org_path_when_no_sandbox_node():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    calls: list[tuple[str, dict[str, Any]]] = []
    cap_read = AsyncMock(return_value=5)
    org_count = AsyncMock(return_value=0)

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", new=org_count),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", new=cap_read),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "running"
    cap_read.assert_not_awaited()
    org_count.assert_not_awaited()


async def test_check_capacity_skips_org_count_when_cap_none():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    calls: list[tuple[str, dict[str, Any]]] = []
    cap_read = AsyncMock(return_value=None)
    org_count = AsyncMock(return_value=99)

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", new=org_count),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", new=cap_read),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "running"
    cap_read.assert_awaited_once()
    org_count.assert_not_awaited()


async def test_check_capacity_org_cap_blocks_on_org_count():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    calls: list[tuple[str, dict[str, Any]]] = []

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=2),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=2),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=10,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "pending"
    assert calls[-1][0] == "pending"
    assert calls[-1][1]["error_code"] == "org_capacity_limited"
    assert "cap 2" in calls[-1][1]["error_detail"]


async def test_check_capacity_pipeline_cap_blocks_before_org():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    calls: list[tuple[str, dict[str, Any]]] = []

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=2),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=10),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=2,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "pending"
    assert calls[-1][1]["error_code"] == "pipeline_capacity"
    assert "limit 2" in calls[-1][1]["error_detail"]


async def test_check_capacity_unlimited_pipeline_still_enforces_org_cap():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    calls: list[tuple[str, dict[str, Any]]] = []

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=3),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=3),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=0,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "pending"
    assert calls[-1][1]["error_code"] == "org_capacity_limited"


async def test_check_capacity_admission_clears_marker():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    run.error_code = "org_capacity_limited"
    calls: list[tuple[str, dict[str, Any]]] = []

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=5),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "running"
    assert calls[-1][0] == "running"
    assert calls[-1][1].get("clear_error_code") is True
    assert run.error_code is None


@pytest.mark.parametrize("terminal_status", ["complete", "failed", "cancelled", "eval_failed"])
async def test_check_capacity_never_resurrects_terminal_run(terminal_status: str):
    """A run that went terminal while a retry backed off must stay terminal."""
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run(status=terminal_status)
    calls: list[tuple[str, dict[str, Any]]] = []

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            side_effect=_make_update_status(run, calls),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=5),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == terminal_status, "terminal run must not be re-admitted"
    assert calls == [], "no status update may be issued for a terminal run"


async def test_check_capacity_fail_open_when_settings_read_raises():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()

    async def _raise_cap(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("settings boom")

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, [])),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", side_effect=_raise_cap),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "running"


async def test_check_capacity_fail_open_when_org_count_raises():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()

    async def _raise_count(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("count boom")

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, [])),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", side_effect=_raise_count),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=2),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "running"


async def test_check_capacity_fail_open_when_graph_scan_raises():
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    cap_read = AsyncMock(return_value=2)
    org_count = AsyncMock(return_value=99)

    def _raise_graph(_g: Any) -> bool:
        raise RuntimeError("graph boom")

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, [])),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", new=org_count),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", new=cap_read),
        patch("modulo.core.pipeline_engine.executor._graph_contains_sandbox_agent", side_effect=_raise_graph),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "sandbox_agent"}]},
        )

    assert result.status == "running"
    cap_read.assert_not_awaited()
    org_count.assert_not_awaited()


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — capacity-deferred (plan F3b, no _retry_pending)
# ---------------------------------------------------------------------------


async def test_execute_capacity_blocked_returns_pending_without_retry_task():
    """A capacity-blocked run is returned pending with NO in-process retry loop.

    Plan F3b removed the ``_retry_pending`` detached loop: a capacity-blocked
    run stays ``pending`` (with its reason marker) and is recovered by
    ``dispatcher_reconcile`` / ``stale_run_recovery_sweep``. execute() must
    return the pending run without spawning any retry task.
    """
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    pending_run = _make_run(run_id=run.id, status="pending")
    create_task = AsyncMock()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch.object(PipelineExecutor, "_check_capacity", new=AsyncMock(return_value=pending_run)),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor.asyncio.create_task", new=create_task),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is pending_run
    assert result.status == "pending"
    create_task.assert_not_called()


@pytest.mark.parametrize("terminal_status", ["complete", "failed", "cancelled", "eval_failed"])
async def test_execute_returns_terminal_run_without_retry_task(terminal_status: str):
    """A terminal run returned by _check_capacity is returned as-is, never resurrected.

    The old ``_retry_pending`` loop was deleted (plan F3b); execute() must not
    spawn any task for a terminal run.
    """
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    create_task = MagicMock()
    terminal_run = _make_run(run_id=run.id, status=terminal_status)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch.object(PipelineExecutor, "_check_capacity", new=AsyncMock(return_value=terminal_run)),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor.asyncio.create_task", new=create_task),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is terminal_run
    assert result.status == terminal_status
    create_task.assert_not_called()
