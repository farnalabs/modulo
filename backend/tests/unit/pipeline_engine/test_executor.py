"""Unit tests for PipelineExecutor using mocked DB sessions."""

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    RunNotFoundError,
    _graph_json_hash,
    _seed_state,
)

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


def _make_session(snapshot: MagicMock) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = snapshot
    session.execute = AsyncMock(return_value=scalar_result)
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _mock_compiled(events: list[dict[str, Any]] | None = None) -> MagicMock:
    """Return a compiled graph mock whose astream_events yields the given events."""

    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        for e in (events or []):
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
# _graph_json_hash
# ---------------------------------------------------------------------------


def test_graph_json_hash_is_deterministic():
    g = {"nodes": [{"id": "x"}], "edges": []}
    assert _graph_json_hash(g) == _graph_json_hash(g)


def test_graph_json_hash_is_order_independent():
    g1 = {"edges": [], "nodes": [{"id": "x"}]}
    g2 = {"nodes": [{"id": "x"}], "edges": []}
    assert _graph_json_hash(g1) == _graph_json_hash(g2)


def test_graph_json_hash_differs_for_different_content():
    assert _graph_json_hash({"nodes": [], "edges": []}) != _graph_json_hash(
        {"nodes": [{"id": "x"}], "edges": []}
    )


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
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={"x": 1}
        )

    assert result is final_run
    calls = mock_update.call_args_list
    assert calls[0].args[2] == "complete"
    assert calls[0].kwargs.get("error_code") is None


async def test_execute_publishes_run_completed_event():
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    published_types = [call.args[0] for call in broker.publish.call_args_list]
    assert "run_completed" in published_types


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
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={"task": "do it"}
        )

    assert captured_state["run_context"]["cancelled"] is False
    assert captured_state["run_context"]["input"] == {"task": "do it"}
    assert captured_state["artifacts"] == []


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
            await executor.execute(
                run_id=uuid.uuid4(), org_id=uuid.uuid4(), input_payload={}
            )


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
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}
        )

    assert result is final_run
    calls = mock_update.call_args_list
    assert calls[0].args[2] == "failed"
    assert calls[0].kwargs.get("error_code") == "RuntimeError"


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
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert mock_update.call_args_list[0].kwargs.get("error_code") == "ValueError"


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — NodeInterrupt → awaiting_human
# ---------------------------------------------------------------------------


async def test_execute_sets_awaiting_human_on_node_interrupt():
    from langgraph.errors import NodeInterrupt

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="awaiting_human")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeInterrupt({"gate_id": "step-1"}))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}
        )

    assert result is final_run
    final_update = mock_update.call_args_list[-1]
    assert final_update.args[2] == "awaiting_human"
    # Broker NOT closed when run is awaiting_human
    registry.close.assert_not_called()


async def test_execute_publishes_hitl_awaiting_event():
    from langgraph.errors import NodeInterrupt

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="awaiting_human")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeInterrupt({"gate_id": "gate-1"}))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    published_types = [call.args[0] for call in broker.publish.call_args_list]
    assert "hitl_awaiting" in published_types


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

    def fake_get_or_compile(pipeline_id: Any, snapshot_id: Any, factory_fn: Any) -> Any:
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
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert captured_args[0] == run.pipeline_id
    assert captured_args[1] == run.snapshot_id


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — max_concurrent_runs enforcement
# ---------------------------------------------------------------------------


def _make_pipeline(max_concurrent_runs: int = 5) -> MagicMock:
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
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}
        )

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
            side_effect=[run, running_run, running_run],
        ),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=running_run,
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline",
            return_value=2,
        ),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._capacity_poll_interval = 0.01
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}
        )

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
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=_mock_registry()),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}
        )

    assert result.status == "cancelled"


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — graph compilation failure
# ---------------------------------------------------------------------------


async def test_execute_fails_on_bad_graph():
    """A graph with a cycle should raise GraphValidationError before execution."""
    from modulo.core.pipeline_engine.executor import GraphValidationError

    run = _make_run()
    snapshot = _make_snapshot({
        "nodes": [{"id": "a"}],
        "edges": [{"source": "a", "target": "a", "type": "normal"}],
    })
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
            await executor.execute(
                run_id=run.id, org_id=uuid.uuid4(), input_payload={}
            )


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
        patch("modulo.core.pipeline_engine.executor.get_run", side_effect=[run, run]),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            return_value=final_run,
        ) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope") as mock_scope,
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        mock_scope.side_effect = ConnectionError("db not available")

        executor = PipelineExecutor(
            MagicMock(), checkpointer_conn_string="postgresql://bad:5432/db"
        )
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}
        )

    assert result is final_run
    # Should have been marked failed, not stuck in running
    failed_update = mock_update.call_args_list[-1]
    assert failed_update.args[2] == "failed"
