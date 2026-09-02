"""Unit tests for PipelineExecutor using mocked DB sessions."""

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Self, TypedDict
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.connectors._rate_bucket import SharedBudgetUnavailableError
from modulo.core.node_output_split import split_node_output
from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    RunNotFoundError,
    _accumulate_chat_model_tokens,
    _accumulate_llm_tokens,
    _failure_event_matches,
    _graph_contains_sandbox_agent,
    _node_output_stall_reason,
    _record_node_markers,
    _retry_after_policy,
    _seed_state,
    _terminal_failure,
)
from modulo.core.pipeline_engine.node_runner import (
    SANDBOX_AGENT_FAILED_SUMMARY,
    SandboxNodeFailedError,
    _build_sandbox_node_envelope,
    _SandboxNodeOutput,
)
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from modulo.core.pipeline_engine.runtime_retry import (
    COMPENSATION_FAILED_CODE,
    CompensationFailedError,
)
from modulo.otel_bridge import trace_id_for_thread


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
# FAR-189 — executor terminalization wires the work_intact reclassify
# ---------------------------------------------------------------------------


def _spy_fix3_wiring(order: list[str]) -> tuple[AsyncMock, AsyncMock]:
    """Build the FAR-189 FIX 3 spies: ``_apply_work_intact`` then
    ``_reclassify_after_work_intact``, each appending its name to *order* when
    awaited, so the test can assert the reclassify runs AFTER the work_intact
    write (the order the fix depends on)."""

    async def _apply(*_args: Any, **_kwargs: Any) -> None:
        order.append("apply_work_intact")

    async def _reclassify(*_args: Any, **_kwargs: Any) -> None:
        order.append("reclassify_after_work_intact")

    return AsyncMock(side_effect=_apply), AsyncMock(side_effect=_reclassify)


async def test_execute_wires_reclassify_after_work_intact():
    """FAR-189 FIX 3 wiring is regression-tested at the EXECUTOR integration
    point: ``execute()``'s terminalization block must await
    ``_reclassify_after_work_intact`` AFTER ``_apply_work_intact`` so the
    persisted classification record carries the real work_intact.

    Deleting the ``await _reclassify_after_work_intact(...)`` line (or
    reordering it before the work_intact write) must leave this test red — a
    direct helper-call test cannot catch a deleted wiring line (AGENTS.md
    lesson, the sweep wiring already got this treatment via
    ``test_dispatcher_reconcile_invokes_classification_sweep``).
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
    order: list[str] = []
    apply_mock, reclassify_mock = _spy_fix3_wiring(order)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.core.pipeline_engine.executor._apply_work_intact", new=apply_mock),
        patch("modulo.core.pipeline_engine.executor._reclassify_after_work_intact", new=reclassify_mock),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs["is_terminal"] is True
    assert apply_mock.await_count == 1
    assert reclassify_mock.await_count == 1
    assert order == ["apply_work_intact", "reclassify_after_work_intact"]


async def test_resume_wires_reclassify_after_work_intact():
    """FAR-189 FIX 3 wiring on the RESUME terminalization path — same
    integration-point assertion as the execute() test. The resume block is a
    separate copy of the wiring (executor.py:1992), so it needs its own test."""
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

    order: list[str] = []
    apply_mock, reclassify_mock = _spy_fix3_wiring(order)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
        patch("modulo.core.pipeline_engine.executor._apply_work_intact", new=apply_mock),
        patch("modulo.core.pipeline_engine.executor._reclassify_after_work_intact", new=reclassify_mock),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs["is_terminal"] is True
    assert apply_mock.await_count == 1
    assert reclassify_mock.await_count == 1
    assert order == ["apply_work_intact", "reclassify_after_work_intact"]


async def test_resume_wires_retry_policy_into_graph_hash_and_compile():
    """FAR-402 P5: resume() folds the pipeline retry_policy into the compile-cache
    hash and threads ``pipeline_retry_policy`` + ``node_idempotency_key`` into
    ``build_graph_from_json`` — so a checkpoint-resumed run executes with the
    SAME per-node retry / per-edge retry / compensation as a fresh run.

    This is the prove-the-fix test for the reviewer's CHANGES_REQUESTED finding 1:
    without the change, ``get_or_compile`` would be called with the base hash and
    ``build_graph_from_json`` would NOT receive ``pipeline_retry_policy`` — so this
    test FAILS on the unpatched code.
    """
    from modulo.core.pipeline_engine.executor import compute_retry_aware_topology_hash

    policy = {"on": ["failure"], "max_retries": 2}
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_resume_session(snapshot, retry_policy=policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {"output": {"output": {"status": "completed", "cost_estimate_usd": 0.0}}},
        }
    ]
    compiled = _mock_compiled(events)
    compiled.aupdate_state = AsyncMock()

    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)

    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="

    goc_calls: list[tuple[Any, Any, str | None]] = []

    def _spy_get_or_compile(pipeline_id, snapshot_id, compile_fn, *, graph_struct_hash=None, **_kwargs):
        goc_calls.append((pipeline_id, snapshot_id, graph_struct_hash))
        # Invoke the captured compile_fn so the build_graph_from_json spy records it.
        compile_fn()
        return compiled

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.get_or_compile",
            side_effect=_spy_get_or_compile,
        ),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json") as spy_build,
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    # (a) graph_struct_hash equals compute_retry_aware_topology_hash(graph_json, policy)
    assert len(goc_calls) == 1
    observed_hash = goc_calls[0][2]
    expected_hash = compute_retry_aware_topology_hash(snapshot.graph_json, policy)
    assert observed_hash == expected_hash

    # (b) build_graph_from_json received pipeline_retry_policy + node_idempotency_key
    spy_build.assert_called_once()
    build_kwargs = spy_build.call_args.kwargs
    assert build_kwargs.get("pipeline_retry_policy") == policy
    assert callable(build_kwargs.get("node_idempotency_key"))

    # (c) the retry-aware hash differs from the no-policy base hash, so a policy
    # PATCH forces a recompile rather than serving a stale (policy-less) graph.
    base_hash = compute_retry_aware_topology_hash(snapshot.graph_json, None)
    assert observed_hash != base_hash


async def test_resume_fails_open_when_retry_policy_access_raises():
    """CHANGES_REQUESTED finding 2 (MINOR): the resume path must mirror the
    execute() path and fail OPEN to no-retry when the pipeline retry_policy
    raises on access (malformed/legacy value). A bare getattr that raises must
    NOT crash resume where execute degrades to no-retry."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    # Make pipeline.retry_policy RAISE on attribute access (legacy/malformed value).
    from unittest.mock import PropertyMock

    from modulo.core.pipeline_engine.executor import compute_retry_aware_topology_hash

    pipe = _make_pipeline()
    type(pipe).retry_policy = PropertyMock(side_effect=ValueError("legacy blob"))

    # Build a resume session whose pipeline query returns our raising pipeline.
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipe
    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    eval_result.scalars.return_value = scalars_mock
    eval_result.scalar_one_or_none.return_value = pipe
    graph_json_result = MagicMock()
    graph_json_result.scalar_one_or_none.return_value = snapshot.graph_json
    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    execute_results = iter([graph_json_result, snapshot_result, pipeline_result, eval_result, count_result])

    async def _execute(*_a: Any, **_k: Any) -> Any:
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

    factory = _make_session_factory(session)
    registry = _mock_registry()
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {"output": {"output": {"status": "completed", "cost_estimate_usd": 0.0}}},
        }
    ]
    compiled = _mock_compiled(events)
    compiled.aupdate_state = AsyncMock()

    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)

    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="

    goc_calls: list[str | None] = []

    def _spy_get_or_compile(pipeline_id, snapshot_id, compile_fn, *, graph_struct_hash=None, **_kwargs):
        goc_calls.append(graph_struct_hash)
        compile_fn()
        return compiled

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", side_effect=_spy_get_or_compile),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json") as spy_build,
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        # Must NOT raise — fails open to no-retry (empty policy).
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    # No policy threaded through -> base hash (legacy value ignored safely).
    assert goc_calls == [compute_retry_aware_topology_hash(snapshot.graph_json, None)]
    spy_build.assert_called_once()
    assert not spy_build.call_args.kwargs.get("pipeline_retry_policy")


# ---------------------------------------------------------------------------
# FAR-198 — deterministic OTel trace context seeding + per-node span stamps
# ---------------------------------------------------------------------------


async def test_stream_graph_seeds_deterministic_trace_context_and_stamps_nodes():
    """_stream_graph seeds the run root span from the thread id, attaches the
    context, stamps completed-node envelopes with the trace id + span id, and
    tears everything down on exit."""
    node_run_id = uuid.uuid4()
    events = [
        {
            "event": "on_chain_end",
            "name": "node-a",
            "run_id": node_run_id,
            "data": {"output": {"output": {"status": "completed"}}},
        }
    ]
    compiled = _mock_compiled(events)
    broker = _mock_registry().get_or_create.return_value

    executor = PipelineExecutor(MagicMock())
    executor._otel_bridge = MagicMock()
    executor._otel_bridge.span_id_for_run.return_value = "0123456789abcdef"
    executor._otel_bridge.start_run_root.return_value = MagicMock()

    completed: dict[str, Any] = {}
    with (
        patch("modulo.core.pipeline_engine.executor.context_api.attach", return_value="tok") as attach,
        patch("modulo.core.pipeline_engine.executor.context_api.detach") as detach,
    ):
        status, error_code, _detail, _ntu = await executor._stream_graph(
            compiled,
            {"input": 1},
            {"configurable": {"thread_id": "org:run"}},
            {"node-a"},
            broker,
            uuid.uuid4(),
            completed_node_outputs=completed,
        )

    executor._otel_bridge.start_run_root.assert_called_once_with("org:run")
    assert status == "complete"
    assert error_code is None
    assert completed["node-a"]["otel_trace_id"] == trace_id_for_thread("org:run")
    assert completed["node-a"]["otel_span_id"] == "0123456789abcdef"
    executor._otel_bridge.end_run_root.assert_called_once()
    attach.assert_called_once()
    detach.assert_called_once()


async def test_stream_graph_detaches_context_on_exception():
    """The seeded context is detached and the run root ended even when the
    stream raises (the finally path)."""
    compiled = _mock_compiled_raising(RuntimeError("boom"))
    broker = _mock_registry().get_or_create.return_value

    executor = PipelineExecutor(MagicMock())
    executor._otel_bridge = MagicMock()
    executor._otel_bridge.start_run_root.return_value = MagicMock()

    with (
        patch("modulo.core.pipeline_engine.executor.context_api.attach", return_value="tok") as attach,
        patch("modulo.core.pipeline_engine.executor.context_api.detach") as detach,
    ):
        status, _code, _detail, _ntu = await executor._stream_graph(
            compiled,
            None,
            {"configurable": {"thread_id": "org:run"}},
            {"node-a"},
            broker,
            uuid.uuid4(),
        )

    assert status == "failed"
    executor._otel_bridge.end_run_root.assert_called_once()
    attach.assert_called_once()
    detach.assert_called_once()


async def test_stream_graph_maps_output_schema_validation_error_to_domain_code():
    """An OutputSchemaValidationError from a node resolves to the domain
    ``schema_validation_failure`` error code — never a raw exception name —
    and publishes a ``run_failed`` payload carrying that code."""
    from modulo.core.pipeline_engine.node_runner import OutputSchemaValidationError

    compiled = _mock_compiled_raising(OutputSchemaValidationError("Manual output missing required field 'name'"))
    broker = _mock_registry().get_or_create.return_value

    executor = PipelineExecutor(MagicMock())
    executor._otel_bridge = MagicMock()
    executor._otel_bridge.start_run_root.return_value = MagicMock()

    with (
        patch("modulo.core.pipeline_engine.executor.context_api.attach", return_value="tok"),
        patch("modulo.core.pipeline_engine.executor.context_api.detach") as detach,
    ):
        status, error_code, detail, _ntu = await executor._stream_graph(
            compiled,
            None,
            {"configurable": {"thread_id": "org:run"}},
            {"node-a"},
            broker,
            uuid.uuid4(),
        )

    assert status == "failed"
    assert error_code == "schema_validation_failure"
    assert error_code != "ValueError"
    assert error_code != "OutputSchemaValidationError"
    assert "missing required field 'name'" in detail

    published = [call.args for call in broker.publish.call_args_list if call.args[0] == "run_failed"]
    assert len(published) == 1
    assert published[0][1]["error"] == "schema_validation_failure"
    assert "missing required field 'name'" in published[0][1]["detail"]
    executor._otel_bridge.end_run_root.assert_called_once()
    detach.assert_called_once()


async def test_stream_graph_maps_compensation_failed_error_to_terminal_status():
    """FAR-402 P5 (§E): when a watched node's compensation edge itself fails, the
    node wrapper raises ``CompensationFailedError`` and the executor must
    terminalize the run as ``compensation_failed`` (never retry, never
    ``failed``) with the canonical ``compensation_failed`` error code. The
    runtime suite only asserts the exception type — this exercises the executor
    terminalization branch (executor.py:4467) end to end through ``_stream_graph``.
    """
    compiled = _mock_compiled_raising(
        CompensationFailedError(
            node_id="node-a",
            compensation_target="comp-n",
            cause=RuntimeError("compensation boom"),
        )
    )

    broker = _mock_registry().get_or_create.return_value

    executor = PipelineExecutor(MagicMock())
    executor._otel_bridge = MagicMock()
    executor._otel_bridge.start_run_root.return_value = MagicMock()

    with (
        patch("modulo.core.pipeline_engine.executor.context_api.attach", return_value="tok"),
        patch("modulo.core.pipeline_engine.executor.context_api.detach") as detach,
    ):
        status, error_code, detail, _ntu = await executor._stream_graph(
            compiled,
            None,
            {"configurable": {"thread_id": "org:run"}},
            {"node-a"},
            broker,
            uuid.uuid4(),
        )

    assert status == "compensation_failed"
    assert error_code == COMPENSATION_FAILED_CODE
    assert "compensation boom" in detail

    published = [call.args for call in broker.publish.call_args_list if call.args[0] == "run_failed"]
    assert len(published) == 1
    assert published[0][1]["error"] == COMPENSATION_FAILED_CODE
    executor._otel_bridge.end_run_root.assert_called_once()
    detach.assert_called_once()


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


def _make_resume_session(snapshot: MagicMock, retry_policy: dict[str, Any] | None = None) -> AsyncMock:
    """Session mock whose execute() order matches resume()'s query sequence.

    resume() queries the snapshot's graph_json FIRST (the atomic sandbox-capacity
    gate, FAR-1306), then the snapshot itself, then the pipeline — the opposite
    of execute(), so the shared _make_session iterator is not reusable here.
    """
    pipeline = _make_pipeline()
    if retry_policy is not None:
        pipeline.retry_policy = retry_policy

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipeline

    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot

    graph_json_result = MagicMock()
    graph_json_result.scalar_one_or_none.return_value = snapshot.graph_json

    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    eval_result.scalars.return_value = scalars_mock
    # The pipeline query (select(Pipeline)) resolves to this mock's
    # scalar_one_or_none(); ensure it returns the pipeline (with any configured
    # retry_policy) regardless of which iterator slot resume() consumes it from.
    eval_result.scalar_one_or_none.return_value = pipeline

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    execute_results = iter([graph_json_result, snapshot_result, pipeline_result, eval_result, count_result])

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
    assert not state["artifacts"]


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
    assert not state["_iteration_counts"]


def test_seed_state_seeds_run_overrides_from_variant_snapshot():
    """A variant run seeds ``_run_overrides`` as a TOP-LEVEL run_context key."""
    snap = _make_snapshot()
    overrides = {"model_backend_id": "backend-a", "prompt_templates": {"a": "v3"}}
    state = _seed_state(snap, {"task": "x"}, {"_run_overrides": overrides})
    assert state["run_context"]["_run_overrides"] == overrides
    # The override must never appear inside the input payload.
    assert "_run_overrides" not in state["run_context"]["input"]


def test_seed_state_ignores_run_overrides_in_caller_input():
    """A NORMAL run's caller-supplied ``_run_overrides`` in input stays DATA.

    FAR-342 injection: with no frozen variant config the executor must never
    promote a crafted ``_run_overrides`` from the input payload to the top-level
    run_context key the node_runner trusts.
    """
    snap = _make_snapshot()
    injected = {"prompt_templates": {"a": "injected prompt"}}
    state = _seed_state(snap, {"task": "x", "_run_overrides": injected})
    # Not promoted to the override boundary.
    assert "_run_overrides" not in state["run_context"]
    # The crafted dict remains inert data inside the input payload.
    assert state["run_context"]["input"]["_run_overrides"] == injected


def test_seed_state_non_dict_variant_snapshot_overrides_ignored():
    """A malformed variant snapshot override is ignored (never seeded)."""
    snap = _make_snapshot()
    state = _seed_state(snap, {"task": "x"}, {"_run_overrides": "not-a-dict"})
    assert "_run_overrides" not in state["run_context"]


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — happy path
# ---------------------------------------------------------------------------


async def test_execute_seeds_hoisted_claimed_guardrails_into_conformance_ctx():
    """FAR-215 MINOR 2: ``execute`` hoists the claimed-guardrail list ONCE per
    run and seeds it into the run-scoped conformance context, so the per-node
    check pays zero guardrail-load queries at every node start."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()

    async def _fake_load(self, org_id: Any, pipeline_id: Any):
        return ["claim-a", "claim-b"], False

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch.object(PipelineExecutor, "_load_claimed_conformance_guardrails", _fake_load),
        patch("modulo.core.pipeline_engine.executor.set_conformance_ctx") as mock_ctx,
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    mock_ctx.assert_called_once()
    args = mock_ctx.call_args.args
    assert args[4] == ["claim-a", "claim-b"]
    assert args[5] is False


async def test_resume_seeds_hoisted_claimed_guardrails_into_conformance_ctx():
    """FAR-215 MINOR 2: ``resume`` re-hoists the claimed-guardrail list (the
    manifest may have changed since the original run) and seeds the same
    run-scoped conformance context."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    session = _make_resume_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled()
    registry = _mock_registry()

    async def _fake_load(self, org_id: Any, pipeline_id: Any):
        return ["claim-a"], True

    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)

    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
        patch.object(PipelineExecutor, "_load_claimed_conformance_guardrails", _fake_load),
        patch("modulo.core.pipeline_engine.executor.set_conformance_ctx") as mock_ctx,
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    mock_ctx.assert_called_once()
    args = mock_ctx.call_args.args
    assert args[4] == ["claim-a"]
    assert args[5] is True


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={"task": "do it"})

    assert captured_state["run_context"]["cancelled"] is False
    assert captured_state["run_context"]["input"] == {"task": "do it"}
    assert not captured_state["artifacts"]


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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


async def test_dispatch_hitl_awaiting_routes_through_notifier():
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = PipelineExecutor(MagicMock(), notifier=notifier)
    run_id = uuid.uuid4()
    team_id = uuid.uuid4()
    org_id = uuid.uuid4()

    await executor._dispatch_hitl_awaiting(
        org_id=org_id,
        run_id=run_id,
        gate_id="gate-7",
        pipeline_name="My Pipeline",
        team_id=team_id,
    )

    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["org_id"] == org_id
    assert kwargs["event_type"] == "hitl_awaiting"
    assert kwargs["run_id"] == run_id
    assert kwargs["team_id"] == team_id
    assert kwargs["payload"] == {
        "run_id": str(run_id),
        "gate_id": "gate-7",
        "team_id": str(team_id),
        "pipeline_name": "My Pipeline",
    }


async def test_dispatch_hitl_awaiting_without_pipeline_name_omits_key():
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = PipelineExecutor(MagicMock(), notifier=notifier)
    run_id = uuid.uuid4()

    await executor._dispatch_hitl_awaiting(
        org_id=uuid.uuid4(),
        run_id=run_id,
        gate_id="gate-1",
        pipeline_name=None,
        team_id=None,
    )

    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["event_type"] == "hitl_awaiting"
    assert kwargs["run_id"] == run_id
    assert kwargs["team_id"] is None
    assert "pipeline_name" not in kwargs["payload"]
    assert kwargs["payload"]["gate_id"] == "gate-1"


async def test_dispatch_hitl_awaiting_skips_without_notifier():
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = PipelineExecutor(MagicMock(), notifier=notifier)
    executor._notifier = None

    await executor._dispatch_hitl_awaiting(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="gate-1",
        pipeline_name="P",
        team_id=None,
    )

    assert executor._notifier is None
    notifier.dispatch_event.assert_not_awaited()


async def test_dispatch_hitl_awaiting_failure_is_isolated():
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock(side_effect=RuntimeError("boom"))
    executor = PipelineExecutor(MagicMock(), notifier=notifier)

    with patch("modulo.core.pipeline_engine.executor._log.exception") as mock_exc:
        await executor._dispatch_hitl_awaiting(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            gate_id="gate-1",
            pipeline_name="P",
            team_id=None,
        )

    assert notifier.dispatch_event.await_count == 1
    mock_exc.assert_called_once()


async def test_execute_with_notifier_dispatches_hitl_awaiting():
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="awaiting_human")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(GraphInterrupt((Interrupt(value={"gate_id": "gate-1"}),)))
    registry = _mock_registry()
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock(), notifier=notifier)
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert notifier.dispatch_event.await_count == 1
    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["event_type"] == "hitl_awaiting"
    assert kwargs["payload"]["run_id"] == str(run.id)
    assert kwargs["payload"]["gate_id"] == "gate-1"


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
            # One result per get_run call — the FAR-510 stored-outputs read on
            # the complete path adds a fifth call to this sequence.
            side_effect=[run, running_run, running_run, running_run, running_run],
        ),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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


async def test_execute_run_failed_detail_is_sanitized():
    """A generic failure whose traceback embeds a DB URL must NOT reach the WS
    event or the persisted error_detail with the secret intact (FAR-163)."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RuntimeError("conn failed postgresql://user:supersecret@db.example/modulo"))
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    broker = registry.get_or_create.return_value
    failed = [call.args for call in broker.publish.call_args_list if call.args[0] == "run_failed"]
    assert len(failed) == 1
    ws_detail = failed[0][1]["detail"]
    assert "supersecret" not in ws_detail
    assert "<redacted>" in ws_detail

    persisted = mock_finalize.await_args.kwargs["error_detail"]
    assert "supersecret" not in persisted
    assert "<redacted>" in persisted


async def test_eval_blocked_audit_payload_error_detail_is_sanitized():
    """The eval.blocked audit payload (immutable, hash-linked) must carry the
    SANITIZED error detail — write-site redaction, never the raw message."""
    from modulo.core.eval_engine import EvalBlockedError

    run = _make_run()
    final_run = _make_run(run_id=run.id, status="eval_failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(
        EvalBlockedError("qa", "conn failed postgresql://user:supersecret@db.example/modulo")
    )
    registry = _mock_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.append_audit_event", new=AsyncMock()) as mock_audit,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    call = mock_audit.await_args
    assert call.kwargs["event_type"] == "eval.blocked"
    detail = call.kwargs["payload_json"]["error_detail"]
    assert "supersecret" not in detail
    assert "<redacted>" in detail


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(GraphValidationError, match=r"cycle|entry"):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — checkpointer connection failure
# ---------------------------------------------------------------------------


async def test_execute_fails_on_checkpointer_connection_error():
    """When the checkpointer can't connect, the run is marked failed.

    FAR-432 (derived persist policy): a checkpointer is only attached to an
    interactive pipeline, so this scenario needs a HITL gate edge in the graph
    for the checkpointer connection to be opened at all.
    """
    run = _make_run()
    snapshot = _make_snapshot(
        {
            "nodes": [{"id": "node-a", "node_type": "agent"}],
            "edges": [
                {
                    "source": "node-a",
                    "target": "node-a",
                    "type": "normal",
                    "hitl_gate_config": {"human_only": True},
                }
            ],
        }
    )
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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


async def test_check_capacity_org_run_cap_demotes_at_claim_time():
    """Major 3: the org run-concurrency cap is re-checked at claim time.

    The dispatch-time admission gate counts active runs in one transaction
    but enqueues later; newly-enqueued runs stay ``pending`` (invisible to
    the count) until a worker claims them, so a burst of dispatches can each
    see ``active < limit`` and exceed the org cap by the batch size. This
    claim-time backstop (mirroring the sandbox-cap pattern) demotes the run
    back to ``pending`` with ``org_capacity_limited``.
    """
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_org_run_concurrency_limit", return_value=2),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_org", return_value=2),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=10,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "pending"
    assert calls[-1][0] == "pending"
    assert calls[-1][1]["error_code"] == "org_capacity_limited"
    assert "cap 2" in calls[-1][1]["error_detail"]


async def test_check_capacity_org_run_cap_applies_without_sandbox_graph():
    """Major 3: the org run cap is a run-level gate — no sandbox node required."""
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_org_run_concurrency_limit", return_value=1),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_org", return_value=1),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=0,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "pending"
    assert calls[-1][1]["error_code"] == "org_capacity_limited"


async def test_check_capacity_org_run_cap_admits_when_under_cap():
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_org_run_concurrency_limit", return_value=5),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_org", return_value=2),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=10,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "running"


async def test_check_capacity_org_run_cap_fail_open_when_count_raises():
    """Major 3: a count error reads as uncapped (admit), never raises."""
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()

    async def _raise_count(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("org run count boom")

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, [])),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.get_org_run_concurrency_limit", return_value=2),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_org", side_effect=_raise_count),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "running"


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
# _check_capacity — run_started audit event (PRD §8.12)
# ---------------------------------------------------------------------------


async def test_check_capacity_admission_emits_run_started_audit():
    """A run admitted to ``running`` fires the ``run_started`` audit event once.

    PRD §8.12: pipeline runs must start with an audit event. The event is
    emitted at the pending→running claim transition — the single point where a
    run genuinely starts (the resume() path sets ``running`` directly and is
    NOT counted, so the event fires once per run, not once per resume).
    """
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    audit = AsyncMock(return_value=MagicMock())

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, [])),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.append_audit_event", new=audit),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "running"
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "run_started"
    assert kwargs["org_id"] == org_id
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == run.id
    assert kwargs["payload_json"] == {"pipeline_id": str(pipeline_id)}


async def test_check_capacity_blocked_emits_no_run_started_audit():
    """A capacity-blocked run (demoted back to pending) must NOT fire run_started."""
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()
    calls: list[tuple[str, dict[str, Any]]] = []
    audit = AsyncMock(return_value=MagicMock())

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, calls)),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=5),
        patch("modulo.core.pipeline_engine.executor.append_audit_event", new=audit),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "pending"
    assert result.error_code == "pipeline_capacity"
    audit.assert_not_awaited()


async def test_check_capacity_run_started_audit_failure_does_not_block_admission():
    """A broken audit append never blocks run admission (failure isolation)."""
    session = _make_capacity_session()
    executor = _make_capacity_executor(session)
    run = _capacity_run()

    async def _raise_audit(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("audit boom")

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", side_effect=_make_update_status(run, [])),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", return_value=0),
        patch("modulo.core.pipeline_engine.executor.append_audit_event", side_effect=_raise_audit),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=5,
            graph_json={"nodes": [{"id": "a", "node_type": "agent"}]},
        )

    assert result.status == "running"


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch.object(PipelineExecutor, "_check_capacity", new=AsyncMock(return_value=pending_run)),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor.asyncio.create_task", new=create_task),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is pending_run
    assert result.status == "pending"
    create_task.assert_not_called()


async def test_resume_at_org_sandbox_capacity_raises():
    """The atomic resume() gate (FAR-1306) must raise SandboxCapacityExceededError
    when the org's active sandbox count already meets the cap — even though the
    HITL route's fast-fail pre-check was mocked open. Regression for the reviewer
    finding: no test exercised the executor exception, only the route pre-check."""
    from modulo.core.pipeline_engine.executor import SandboxCapacityExceededError

    run = _make_run()
    snapshot = _make_snapshot()
    snapshot.graph_json = {"nodes": [{"id": "agent-a", "node_type": "sandbox_agent"}]}

    graph_json_result = MagicMock()
    graph_json_result.scalar_one_or_none.return_value = snapshot.graph_json

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=graph_json_result)

    @asynccontextmanager
    async def _ctx():
        yield session

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = MagicMock(side_effect=lambda: _ctx())

    org_id = uuid.uuid4()

    with (
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_sandbox_concurrency_limit", return_value=2),
        patch("modulo.core.pipeline_engine.executor.count_active_sandbox_runs_for_org", return_value=2),
        pytest.raises(SandboxCapacityExceededError),
    ):
        await executor.resume(run_id=run.id, org_id=org_id, resume_data={"action": "approved"})

    graph_lock_exec = session.execute.await_args_list[1]
    assert "pg_advisory_xact_lock" in graph_lock_exec.args[0].text


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
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch.object(PipelineExecutor, "_check_capacity", new=AsyncMock(return_value=terminal_run)),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor.asyncio.create_task", new=create_task),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is terminal_run
    assert result.status == terminal_status
    create_task.assert_not_called()


# ---------------------------------------------------------------------------
# FAR-296 Phase 2 — never-retryable script-mode terminal codes
# ---------------------------------------------------------------------------


def test_retry_after_policy_never_retries_script_mode_terminal_codes():
    """A ``failure`` retry_policy must NEVER retry script-mode terminal codes.

    Once a script-mode node's process started (fencing lease claimed), any
    fault is exactly-once — re-dispatching could double-execute a side effect.
    Both the canonical dotted code and the raw exception-class spelling are
    excluded at the run level (``_retry_after_policy``).
    """
    policy = {"on": ["failure"], "max_retries": 3}
    for code in (
        "script.failed",
        "script.invalid_output",
        "script.side_effect_unknown",
        "script.session_lost",
        "ScriptFailedError",
        "ScriptInvalidOutputError",
        "ScriptSideEffectUnknownError",
        "script.schema_failed",
        "script.no_output",
    ):
        assert _retry_after_policy(policy, "failed", code) is None, code


def test_retry_after_policy_still_retries_retryable_sandbox_failure():
    """A retryable sandbox-infra failure (pre-claim / LLM mode) still retries."""
    policy = {"on": ["failure"], "max_retries": 2}
    assert _retry_after_policy(policy, "failed", "sandbox.no_output_json") == 2
    assert _retry_after_policy(policy, "failed", "SandboxNodeFailedError") == 2


def test_failure_event_matches_excludes_never_retryable_script_codes():
    """The ``failure`` event matcher excludes script-mode terminal codes."""
    for code, mapped in [
        ("script.failed", "script.failed"),
        ("ScriptFailedError", "script.failed"),
        ("script.side_effect_unknown", "script.side_effect_unknown"),
        ("ScriptSideEffectUnknownError", "script.side_effect_unknown"),
        ("script.schema_failed", "contract.schema"),
        ("script.no_output", "contract.no_output"),
    ]:
        assert _failure_event_matches({"failure"}, "failed", code, mapped, None) is False, (code, mapped)


# ---------------------------------------------------------------------------
# FAR-296 Phase 3b: per-run runner-role API-key revocation at finalization
# ---------------------------------------------------------------------------


def _begin_cm() -> AsyncMock:
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    return begin_cm


def _finalize_args(run_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "org_id": org_id,
        "pipeline_id": uuid.uuid4(),
        "node_type_map": {},
        "final_status": "complete",
        "error_code": None,
        "error_detail": None,
        "node_token_usage": {},
        "completed_node_outputs": {},
        "node_ids": set(),
    }


@pytest.mark.asyncio
async def test_revoke_run_api_key_helper_uses_session_factory():
    """_revoke_run_api_key opens a session, sets RLS, and calls revoke_run_api_key.

    A failure inside the helper is swallowed (failure-isolated) — it never
    propagates to the caller.
    """
    session = AsyncMock(spec=AsyncSession)
    session.begin = MagicMock(return_value=_begin_cm())
    factory = _make_session_factory(session)
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = factory  # type: ignore[assignment]

    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with patch("modulo.auth.api_key.revoke_run_api_key", new=AsyncMock(return_value=1)) as revoke_mock:
        await executor._revoke_run_api_key(run_id=run_id, org_id=org_id)

    revoke_mock.assert_awaited_once()
    assert revoke_mock.await_args.kwargs["run_id"] == run_id
    assert revoke_mock.await_args.kwargs["org_id"] == org_id


@pytest.mark.asyncio
async def test_revoke_run_api_key_helper_failure_is_swallowed():
    """A revocation failure is logged and swallowed — it never propagates."""

    async def _raise_boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("revoke boom")

    session = AsyncMock(spec=AsyncSession)
    session.begin = MagicMock(return_value=_begin_cm())
    factory = _make_session_factory(session)
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = factory  # type: ignore[assignment]

    with patch("modulo.auth.api_key.revoke_run_api_key", new=_raise_boom):
        await executor._revoke_run_api_key(run_id=uuid.uuid4(), org_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_finalize_run_after_stream_revokes_run_api_key():
    """Finalization revokes the per-run runner-role API key (FAR-296 Phase 3b)."""
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.begin = MagicMock(return_value=_begin_cm())
    factory = _make_session_factory(session)
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = factory  # type: ignore[assignment]

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()) as revoke_mock,
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=MagicMock())),
    ):
        final_run = await executor._finalize_run_after_stream(**_finalize_args(run_id, org_id))

    assert final_run is not None
    revoke_mock.assert_awaited_once()
    assert revoke_mock.await_args.kwargs["run_id"] == run_id
    assert revoke_mock.await_args.kwargs["org_id"] == org_id


@pytest.mark.asyncio
async def test_finalize_run_after_stream_revocation_failure_is_isolated():
    """A revocation failure never crashes finalization — the run still returns."""

    async def _raise_boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("revoke boom")

    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.begin = MagicMock(return_value=_begin_cm())
    factory = _make_session_factory(session)
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = factory  # type: ignore[assignment]

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=_raise_boom),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=MagicMock())),
    ):
        final_run = await executor._finalize_run_after_stream(**_finalize_args(run_id, org_id))

    assert final_run is not None


# ---------------------------------------------------------------------------
# FAR-510 — masked sandbox-agent failure downgrade at finalization
# ---------------------------------------------------------------------------


def _finalize_executor_with_session() -> tuple[PipelineExecutor, MagicMock]:
    executor = PipelineExecutor(MagicMock())
    session = AsyncMock(spec=AsyncSession)
    session.begin = MagicMock(return_value=_begin_cm())
    executor._session_factory = _make_session_factory(session)  # type: ignore[assignment]
    return executor, session


async def _finalize_with_patched_tail(
    executor: PipelineExecutor,
    args: dict[str, Any],
) -> tuple[AsyncMock, Any]:
    """Drive ``_finalize_run_after_stream`` with its DB-facing tail patched out.

    Returns the ``finalize_cost`` mock (the assertion seam for the terminal
    status/error fields) and the final run row.
    """
    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=MagicMock())),
    ):
        final_run = await executor._finalize_run_after_stream(**args)
    return mock_finalize, final_run


def _generic_exception_envelope(node_id: str = "node-x") -> dict[str, Any]:
    """The REAL generic-exception failure envelope, stamped exactly as the
    runner's ``except Exception`` path builds it (FAR-510)."""
    return _build_sandbox_node_envelope(
        node_id=node_id,
        output=_SandboxNodeOutput(
            status="failed",
            summary=SANDBOX_AGENT_FAILED_SUMMARY,
            exit_code=-1,
            wall_clock_time_ms=1234,
            cost_estimate_usd=0.0,
            attempt_key="attempt-1",
            error_type="RuntimeError",
            error_message="boom",
            sandbox_id="sb-1",
            modulo_synthetic_failure=True,
        ),
        exclude_from_output=frozenset({"error_type", "error_message"}),
    )


def _schema_validation_envelope(node_id: str = "node-x", business_json: dict[str, Any] | None = None) -> dict[str, Any]:
    """The REAL schema-validation failure envelope, stamped exactly as the
    runner's schema-rejection path builds it (FAR-510)."""
    rejected = business_json if business_json is not None else {"summary": "done"}
    return _build_sandbox_node_envelope(
        node_id=node_id,
        output=_SandboxNodeOutput(
            status="failed",
            summary="Output failed schema validation: 'status' is a required property",
            exit_code=0,
            wall_clock_time_ms=1234,
            cost_estimate_usd=0.0,
            cost_source=rejected,
            output_json=rejected,
            attempt_key="attempt-1",
            modulo_synthetic_failure=True,
        ),
    )


def _split_stored_columns(envelope: dict[str, Any], node_id: str = "node-x") -> tuple[Any, dict[str, Any]]:
    """The ``(outputs_json, node_telemetry_json)`` pair the P1b writer
    persists for *envelope* — driven through the production splitter."""
    return split_node_output(envelope, "sandbox_agent", None)


@pytest.mark.asyncio
async def test_finalize_downgrades_masked_sandbox_agent_failure():
    """A ``complete`` run whose completed sandbox-agent output is the stamped
    generic-exception failure envelope finalizes ``failed`` with the
    ``sandbox.agent_failed`` code."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {"node-x": _generic_exception_envelope()}

    mock_finalize, final_run = await _finalize_with_patched_tail(executor, args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "Sandbox agent node(s) failed: node-x"


@pytest.mark.asyncio
async def test_finalize_downgrades_stamped_schema_validation_envelope():
    """The schema-validation synthetic failure path is stamped too — its live
    envelope downgrades on the marker, independent of the summary text."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {"node-x": _schema_validation_envelope()}

    mock_finalize, _final_run = await _finalize_with_patched_tail(executor, args)

    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "Sandbox agent node(s) failed: node-x"


@pytest.mark.asyncio
async def test_finalize_keeps_complete_for_clean_sandbox_output():
    """A normal successful sandbox output finalizes ``complete`` unchanged."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {
        "node-x": _build_sandbox_node_envelope(
            node_id="node-x",
            output=_SandboxNodeOutput(
                status="completed",
                summary="all good",
                exit_code=0,
                wall_clock_time_ms=100,
                cost_estimate_usd=0.0,
                output_json={"summary": "all good"},
                attempt_key="attempt-1",
            ),
        ),
    }

    mock_finalize, _final_run = await _finalize_with_patched_tail(executor, args)

    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs["error_code"] is None
    assert mock_finalize.await_args.kwargs["error_detail"] is None


@pytest.mark.asyncio
async def test_finalize_ignores_failed_envelope_on_non_sandbox_node():
    """A stamped failed envelope on a non-sandbox node type is NOT downgraded."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "llm"}
    args["completed_node_outputs"] = {"node-x": _generic_exception_envelope()}

    mock_finalize, _final_run = await _finalize_with_patched_tail(executor, args)

    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs["error_code"] is None
    assert mock_finalize.await_args.kwargs["error_detail"] is None


@pytest.mark.asyncio
async def test_finalize_leaves_already_failed_run_unchanged():
    """A run already finalizing ``failed`` passes through untouched (no-op)."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["final_status"] = "failed"
    args["error_code"] = "agent.failed"
    args["error_detail"] = "agent self-reported failure"
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {
        "node-x": {"status": "failed", "summary": SANDBOX_AGENT_FAILED_SUMMARY, "exit_code": -1},
    }

    mock_finalize, _final_run = await _finalize_with_patched_tail(executor, args)

    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "agent.failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "agent self-reported failure"


def test_work_intact_false_for_downgraded_sandbox_agent_failure():
    """FAR-510 — a downgraded run (``failed`` + ``sandbox.agent_failed``) is
    forced work_intact=False. The synthetic failure envelope has a summary
    string, so without the forced-False rule ``compute_work_intact`` would
    count it as a valid artifact and return True for an agent that died."""
    executor, _session = _finalize_executor_with_session()
    synthetic = {
        "status": "failed",
        "summary": SANDBOX_AGENT_FAILED_SUMMARY,
        "exit_code": -1,
        "modulo_synthetic_failure": True,
    }
    # The completed set equals the full DAG node set and every output has a
    # summary — exactly the shape that would compute work_intact=True.
    assert (
        executor._compute_run_work_intact("failed", "sandbox.agent_failed", {"node-x": synthetic}, {"node-x"}) is False
    )
    assert executor._compute_run_work_intact("failed", "sandbox.agent_failed", {}, set()) is False


def _stored_run_with_outputs(outputs_json: Any, node_telemetry_json: Any | None = None) -> MagicMock:
    run = MagicMock()
    run.outputs_json = outputs_json
    run.node_telemetry_json = node_telemetry_json
    return run


@pytest.mark.asyncio
async def test_finalize_downgrades_stored_telemetry_failure_from_prior_segment_on_resume():
    """Resume parity (REAL post-P1b shape) — the live segment emits nothing
    (a HITL resume re-emits only the resumed segment), and the stored columns
    hold the SPLIT shapes: ``outputs_json[node]`` is ``None`` (the
    generic-exception path has no agent return) and the envelope fields
    (``status``/``summary`` + the synthetic-failure marker) live ONLY in
    ``node_telemetry_json``. The stored telemetry view drives the downgrade."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {}
    return_value, telemetry = _split_stored_columns(_generic_exception_envelope())
    assert return_value is None
    assert telemetry["status"] == "failed"
    stored_run = _stored_run_with_outputs({"node-x": return_value}, {"node-x": telemetry})

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=stored_run)),
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "Sandbox agent node(s) failed: node-x"


@pytest.mark.asyncio
async def test_finalize_downgrades_stored_schema_validation_failure():
    """FAR-510 — the schema-validation synthetic failure path is stamped on the
    STORED shape too: the rejected business JSON lands in ``outputs_json`` (the
    pure return) while the stamped envelope fields land in telemetry, and the
    telemetry view drives the downgrade."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {}
    rejected = {"summary": "done", "pr_url": "https://example.com/pr/1"}
    return_value, telemetry = _split_stored_columns(_schema_validation_envelope(business_json=rejected))
    assert return_value == rejected
    stored_run = _stored_run_with_outputs({"node-x": return_value}, {"node-x": telemetry})

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=stored_run)),
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "Sandbox agent node(s) failed: node-x"


@pytest.mark.asyncio
async def test_finalize_keeps_complete_for_failed_status_without_synthetic_marker():
    """Precision boundary — a sandbox node output self-reporting ``failed``
    WITHOUT the runner's synthetic-failure marker is an honest failure shape
    (real exit-code failure / agent-authored business JSON), not the masked
    dispatch-failure marker: no downgrade."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {}
    honest = _build_sandbox_node_envelope(
        node_id="node-x",
        output=_SandboxNodeOutput(
            status="failed",
            summary="genuinely failed differently",
            exit_code=1,
            wall_clock_time_ms=1234,
            cost_estimate_usd=0.0,
            output_json={"status": "failed", "summary": "genuinely failed differently"},
            attempt_key="attempt-1",
        ),
    )
    return_value, telemetry = _split_stored_columns(honest)
    stored_run = _stored_run_with_outputs({"node-x": return_value}, {"node-x": telemetry})

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=stored_run)),
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs["error_code"] is None
    assert mock_finalize.await_args.kwargs["error_detail"] is None


@pytest.mark.asyncio
async def test_finalize_downgrade_checks_live_and_stored_views_without_clobbering():
    """Dedup premise — stored ``outputs_json`` and the live envelope hold
    DIFFERENT shapes for the same node by design, so no view clobbers another:
    a clean stored business JSON must not mask a stamped live envelope."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {"node-x": _generic_exception_envelope()}
    stored_run = _stored_run_with_outputs({"node-x": {"summary": "clean business json"}})

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=stored_run)),
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "Sandbox agent node(s) failed: node-x"


@pytest.mark.asyncio
async def test_finalize_keeps_complete_for_pre_p1b_legacy_row_without_marker():
    """Legacy (pre-P1b) stored rows — the mixed envelope self-reports
    status/summary but never carries the synthetic-failure marker, so a legacy
    row never downgrades (the marker-based predicate is the contract, and the
    legacy-safe telemetry accessor still resolves the view)."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {}
    legacy = {
        "artifacts": [
            {
                "node_id": "node-x",
                "status": "failed",
                "output": {"status": "failed", "summary": SANDBOX_AGENT_FAILED_SUMMARY, "exit_code": -1},
            }
        ],
        "output": {"status": "failed", "summary": SANDBOX_AGENT_FAILED_SUMMARY},
    }
    stored_run = _stored_run_with_outputs({"node-x": legacy})

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=stored_run)),
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs["error_code"] is None
    assert mock_finalize.await_args.kwargs["error_detail"] is None


@pytest.mark.asyncio
async def test_finalize_downgrade_lists_all_matching_nodes_sorted():
    """Multi-node — every matching node is listed, sorted, in error_detail."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-b": "sandbox_agent", "node-a": "sandbox_agent", "node-c": "llm"}
    args["completed_node_outputs"] = {
        "node-b": _generic_exception_envelope("node-b"),
        "node-a": _generic_exception_envelope("node-a"),
        "node-c": _generic_exception_envelope("node-c"),
    }

    mock_finalize, _final_run = await _finalize_with_patched_tail(executor, args)

    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"
    assert mock_finalize.await_args.kwargs["error_detail"] == "Sandbox agent node(s) failed: node-a, node-b"


@pytest.mark.asyncio
async def test_finalize_stored_output_read_failure_falls_back_to_live_scan():
    """Failure isolation — a stored-outputs read failure is swallowed (warn +
    empty dict) and the downgrade still scans the live dict; finalization
    never crashes."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["node_type_map"] = {"node-x": "sandbox_agent"}
    args["completed_node_outputs"] = {"node-x": _generic_exception_envelope()}
    # First get_run (stored read) explodes; the second (final fetch) succeeds.
    get_run_mock = AsyncMock(side_effect=[RuntimeError("stored read boom"), MagicMock()])

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.get_run", new=get_run_mock),
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "sandbox.agent_failed"


@pytest.mark.asyncio
async def test_finalize_skips_stored_output_read_when_not_complete():
    """Performance contract — the extra stored-outputs run-row read happens on
    the complete path only; a failed run issues exactly one get_run (the
    final-row fetch)."""
    executor, _session = _finalize_executor_with_session()
    args = _finalize_args(uuid.uuid4(), uuid.uuid4())
    args["final_status"] = "failed"
    args["error_code"] = "agent.failed"
    args["error_detail"] = "agent self-reported failure"

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_revoke_run_api_key", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=MagicMock())) as get_run_mock,
    ):
        final_run = await executor._finalize_run_after_stream(**args)

    assert final_run is not None
    assert get_run_mock.await_count == 1


# ---------------------------------------------------------------------------
# S3776 decomposition helpers (FAR-310) — direct coverage for extracted helpers
# ---------------------------------------------------------------------------


class TestRecordNodeMarkers:
    def test_stall_reason_publishes_run_stalled(self) -> None:
        broker = MagicMock()
        output = {"output": {"status": "failed", "stall_reason": "idle for 60s"}}
        stall, agent_failure, session_lost = _record_node_markers(output, broker, "node-a")
        assert stall == "idle for 60s"
        assert agent_failure is None
        assert session_lost is None
        broker.publish.assert_called_once_with("run_stalled", {"node_id": "node-a", "stall_reason": "idle for 60s"})

    def test_agent_failure_returns_reason_without_publish(self) -> None:
        broker = MagicMock()
        output = {"output": {"agent_status": "failed", "error": "agent crashed"}}
        stall, agent_failure, session_lost = _record_node_markers(output, broker, "node-a")
        assert stall is None
        assert agent_failure == "agent crashed"
        assert session_lost is None
        broker.publish.assert_not_called()

    def test_session_lost_marker_returns_reason(self) -> None:
        broker = MagicMock()
        output = {"output": {"sandbox_session_lost": True, "summary": "opencode session died"}}
        stall, agent_failure, session_lost = _record_node_markers(output, broker, "node-a")
        assert stall is None
        assert agent_failure is None
        assert session_lost == "opencode session died"
        broker.publish.assert_not_called()

    def test_clean_output_returns_no_markers(self) -> None:
        broker = MagicMock()
        output = {"output": {"status": "completed", "summary": "all good"}}
        assert _record_node_markers(output, broker, "node-a") == (None, None, None)
        broker.publish.assert_not_called()


class TestTerminalFailure:
    def test_publishes_run_failed_and_returns_tuple(self) -> None:
        broker = MagicMock()
        usage = {"node-a": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        result = _terminal_failure(broker, "failed", "executor_stalled", "detail-here", usage)
        assert result == ("failed", "executor_stalled", "detail-here", usage)
        broker.publish.assert_called_once_with("run_failed", {"error": "executor_stalled", "detail": "detail-here"})

    def test_empty_or_none_usage_normalizes_to_none(self) -> None:
        broker = MagicMock()
        assert _terminal_failure(broker, "failed", "code", "detail", None)[3] is None
        assert _terminal_failure(broker, "failed", "code", "detail", {})[3] is None


class TestAccumulateChatModelTokens:
    @staticmethod
    def _event(node: str, output: Any) -> dict[str, Any]:
        return {"metadata": {"langgraph_node": node}, "data": {"output": output}}

    def test_accumulates_usage_metadata_from_base_message(self) -> None:
        usage: dict[str, dict[str, int]] = {}
        msg = AIMessage("hi", usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12})
        _accumulate_chat_model_tokens(self._event("node-a", msg), usage, None, None)
        assert usage == {"node-a": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}}

    def test_accumulates_legacy_llm_output_token_usage(self) -> None:
        usage: dict[str, dict[str, int]] = {}
        output = {"llm_output": {"token_usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}}
        _accumulate_chat_model_tokens(self._event("node-a", output), usage, None, None)
        assert usage == {"node-a": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}

    def test_cumulative_across_events(self) -> None:
        usage: dict[str, dict[str, int]] = {}
        output = {"llm_output": {"token_usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}}
        _accumulate_chat_model_tokens(self._event("node-a", output), usage, None, None)
        _accumulate_chat_model_tokens(self._event("node-a", output), usage, None, None)
        assert usage["node-a"]["input_tokens"] == 20
        assert usage["node-a"]["output_tokens"] == 8
        assert usage["node-a"]["total_tokens"] == 28

    def test_records_cumulative_tokens_on_guard(self) -> None:
        guard = RunawayGuard()
        output = {"llm_output": {"token_usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}}
        _accumulate_chat_model_tokens(self._event("node-a", output), {}, guard, None)
        assert guard._token_count == 7

    def test_node_budget_breach_raises_runaway_run_error(self) -> None:
        output = {"llm_output": {"token_usage": {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100}}}
        with pytest.raises(RunawayRunError) as excinfo:
            _accumulate_chat_model_tokens(self._event("node-a", output), {}, None, {"node-a": 50})
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 100
        assert excinfo.value.limit == 50

    def test_at_budget_does_not_raise(self) -> None:
        output = {"llm_output": {"token_usage": {"prompt_tokens": 50, "completion_tokens": 0, "total_tokens": 50}}}
        usage: dict[str, dict[str, int]] = {}
        # Exactly at budget (50 == 50) must NOT raise RunawayRunError (strictly-greater check).
        _accumulate_chat_model_tokens(self._event("node-a", output), usage, None, {"node-a": 50})
        assert usage["node-a"]["total_tokens"] == 50
        # Second call at a higher budget also accumulates without raising.
        _accumulate_chat_model_tokens(self._event("node-a", output), usage, None, {"node-a": 100})
        assert usage["node-a"]["total_tokens"] == 100

    def test_ignores_events_without_node_name(self) -> None:
        usage: dict[str, dict[str, int]] = {}
        _accumulate_chat_model_tokens({"metadata": {}, "data": {"output": {}}}, usage, None, None)
        assert usage == {}


class TestAccumulateLlmTokens:
    @staticmethod
    def _event(node: str, output: Any) -> dict[str, Any]:
        return {"metadata": {"langgraph_node": node}, "data": {"output": output}}

    def test_accumulates_llm_output_token_usage(self) -> None:
        usage: dict[str, dict[str, int]] = {}
        output = {"llm_output": {"token_usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}}
        _accumulate_llm_tokens(self._event("node-a", output), usage, None, None)
        assert usage == {"node-a": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}}

    def test_node_budget_breach_raises_runaway_run_error(self) -> None:
        output = {"llm_output": {"token_usage": {"prompt_tokens": 60, "completion_tokens": 0, "total_tokens": 60}}}
        with pytest.raises(RunawayRunError) as excinfo:
            _accumulate_llm_tokens(self._event("node-a", output), {}, None, {"node-a": 50})
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 60
        assert excinfo.value.limit == 50

    def test_ignores_events_without_node_name(self) -> None:
        usage: dict[str, dict[str, int]] = {}
        _accumulate_llm_tokens({"metadata": {}, "data": {"output": {}}}, usage, None, None)
        assert usage == {}


class TestTransientFailureDetail:
    @staticmethod
    def _executor() -> PipelineExecutor:
        return PipelineExecutor(MagicMock())

    def test_script_lease_unknown_is_needs_human_code(self) -> None:
        code, detail = TestTransientFailureDetail._executor()._transient_failure_detail(
            exc=NodeCancelledError("node-a"),
            script_lease_ok=False,
            graph_idempotent=True,
            node_attempt_count=1,
            retries=1,
        )
        assert code == "script.side_effect_unknown"
        assert "side effect unknown" in detail
        assert "needs human review" in detail

    def test_node_cancelled_error_detail(self) -> None:
        code, detail = TestTransientFailureDetail._executor()._transient_failure_detail(
            exc=NodeCancelledError("killed"),
            script_lease_ok=True,
            graph_idempotent=True,
            node_attempt_count=1,
            retries=1,
        )
        assert code == "node_cancelled"
        assert detail.startswith("Sandbox node cancelled (transient) after retries exhausted:")
        assert "killed" in detail

    def test_sandbox_node_failed_error_detail(self) -> None:
        exc = SandboxNodeFailedError("stalled", node_id="node-a")
        code, detail = TestTransientFailureDetail._executor()._transient_failure_detail(
            exc=exc, script_lease_ok=True, graph_idempotent=True, node_attempt_count=1, retries=1
        )
        assert code == "node_cancelled"
        assert "Sandbox node failed (transient) after retries exhausted: stalled" in detail

    def test_non_idempotent_graph_retry_suppression_detail(self) -> None:
        exc = SandboxNodeFailedError("side-effect fail", node_id="node-a")
        code, detail = TestTransientFailureDetail._executor()._transient_failure_detail(
            exc=exc, script_lease_ok=True, graph_idempotent=False, node_attempt_count=0, retries=1
        )
        assert code == "node_cancelled"
        assert "retry suppressed because a node in the graph is non-idempotent" in detail
        assert "idempotent=false" in detail


def _make_connector_init_session(rows: list[Any]) -> AsyncMock:
    """Session whose execute() yields non-empty ConnectorInstance rows for _init_connector_hub."""
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result.scalars.return_value = scalars_mock
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = result
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class _FakeConnectorHub:
    """Stand-in ConnectorHub whose initialise fails closed with a shared-budget error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialise(self, _rows: list[Any], allowed_connectors: list[Any] | None = None) -> None:
        raise self._exc


async def test_init_connector_hub_propagates_shared_budget_error():
    """FAR-439: _init_connector_hub must FAIL CLOSED (re-raise) on SharedBudgetUnavailableError.

    A configured-but-unconstructable shared Redis budget — or a settings-read failure on
    the executor path — raises SharedBudgetUnavailableError from ``hub.initialise``.
    Swallowing it and returning None would let the connector node vacuously "succeed"
    with the ``no connector hub`` fallback, silently no-op'ing the remote integration and
    finalising the run GREEN. The raise must propagate so the run fails loudly at startup.
    """
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory(  # type: ignore[assignment]
        _make_connector_init_session([MagicMock()])
    )
    hub = _FakeConnectorHub(
        SharedBudgetUnavailableError("shared rate-limit Redis client is configured but could not be constructed")
    )

    with (
        patch("modulo.core.connector_hub.ConnectorHub", return_value=hub),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="key")),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        pytest.raises(SharedBudgetUnavailableError),
    ):
        await executor._init_connector_hub(org_id)


async def test_init_connector_hub_raises_when_configured_but_build_fails():
    """FAR-439 root cause: a run WITH connectors configured must fail loudly.

    A construction/settings failure (here ``get_settings`` raising) on a run that
    HAS active connector rows must RAISE out of ``_init_connector_hub`` — never
    return None. Returning None would let the node_runner vacuously "succeed"
    with the ``no connector hub`` fallback, silently no-op'ing the configured
    remote integration and finalising the run GREEN.
    """
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory(  # type: ignore[assignment]
        _make_connector_init_session([MagicMock()])
    )

    with (
        patch("modulo.settings.get_settings", side_effect=RuntimeError("settings unavailable")),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        pytest.raises(RuntimeError),
    ):
        await executor._init_connector_hub(org_id)


async def test_init_connector_hub_returns_none_when_no_connectors_configured():
    """FAR-439: a run with NO active connector bindings returns None (vacuous success).

    Vacuous success is only correct when no connector work is expected — the
    ``hub=None`` fallback must never be reached for a configured run (see the
    sibling fail-loudly tests). With zero rows the hub is never constructed so
    the None return is the safe, correct result.
    """
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory(  # type: ignore[assignment]
        _make_connector_init_session([])
    )

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
    ):
        result = await executor._init_connector_hub(org_id)

    assert result is None


def _make_connector_read_failing_session(exc: Exception) -> AsyncMock:
    """Session whose execute() raises for a _init_connector_hub connector-row read."""
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = exc
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


async def test_init_connector_hub_fails_closed_on_read_error():
    """FAR-439: a connector-row READ failure must re-raise (fail closed), not return None.

    A session / set_rls / ``session.execute`` failure while reading the
    ConnectorInstance rows means we CANNOT determine whether connectors are
    configured. Failing open (returning None) would let the connector node
    vacuously "succeed" with the ``no connector hub`` fallback, silently no-op'ing
    a possibly-configured remote integration and finalising the run GREEN. The
    read error must propagate so the run fails loudly at startup — it is NOT
    evidence that no connectors are configured.
    """
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory(  # type: ignore[assignment]
        _make_connector_read_failing_session(RuntimeError("db read failed"))
    )

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        pytest.raises(RuntimeError, match="db read failed"),
    ):
        await executor._init_connector_hub(org_id)


class _FakeConnectorHubWithMarkers:
    """Stand-in ConnectorHub whose initialise populates skipped/healthy like the real hub."""

    def __init__(
        self,
        skipped: dict[uuid.UUID, str] | None = None,
        healthy: set[uuid.UUID] | None = None,
    ) -> None:
        self._skipped = skipped or {}
        self._healthy = healthy or set()
        self.skipped: dict[uuid.UUID, str] = {}
        self.healthy: set[uuid.UUID] = set()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialise(self, _rows: list[Any], allowed_connectors: list[Any] | None = None) -> None:
        self.skipped = dict(self._skipped)
        self.healthy = set(self._healthy)


def _make_nested_savepoint_cm() -> AsyncMock:
    """begin_nested() savepoint stand-in whose __aexit__ records the rollback path."""
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    return nested_cm


async def test_init_connector_hub_persists_degraded_markers():
    """FAR-495: skipped instances get a degraded marker, healthy ones get cleared.

    After ``hub.initialise`` populates ``skipped``/``healthy``, the executor must
    persist the marker writes inside a ``begin_nested()`` savepoint:
    ``mark_instances_degraded`` for the skipped instance ids and
    ``clear_degraded_markers`` for the healthy ones, then still register the hub.
    """
    org_id = uuid.uuid4()
    skipped_id = uuid.uuid4()
    healthy_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    session = _make_connector_init_session([MagicMock()])
    nested_cm = _make_nested_savepoint_cm()
    session.begin_nested = MagicMock(return_value=nested_cm)
    executor._session_factory = _make_session_factory(session)  # type: ignore[assignment]
    hub = _FakeConnectorHubWithMarkers(skipped={skipped_id: "stub backend unavailable"}, healthy={healthy_id})
    mark_mock = AsyncMock()
    clear_mock = AsyncMock()

    with (
        patch("modulo.core.connector_hub.ConnectorHub", return_value=hub),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="key")),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub") as set_hub_mock,
        patch("modulo.db.crud.connector_instance.mark_instances_degraded", mark_mock),
        patch("modulo.db.crud.connector_instance.clear_degraded_markers", clear_mock),
    ):
        result = await executor._init_connector_hub(org_id)

    assert result is hub
    mark_mock.assert_awaited_once_with(session, {skipped_id: "stub backend unavailable"})
    clear_mock.assert_awaited_once_with(session, {healthy_id})
    nested_cm.__aenter__.assert_awaited_once()
    set_hub_mock.assert_called_once_with(hub)


async def test_init_connector_hub_marker_failure_is_swallowed():
    """FAR-495: a degraded-marker write failure must never fail the run start.

    ``mark_instances_degraded`` raising inside the savepoint must be swallowed
    (best-effort wiring): the savepoint's ``__aexit__`` is exercised with the
    exception (the rollback path), the sibling clear is never attempted, and
    ``_init_connector_hub`` still completes successfully and registers the hub.
    """
    org_id = uuid.uuid4()
    skipped_id = uuid.uuid4()
    healthy_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    session = _make_connector_init_session([MagicMock()])
    nested_cm = _make_nested_savepoint_cm()
    session.begin_nested = MagicMock(return_value=nested_cm)
    executor._session_factory = _make_session_factory(session)  # type: ignore[assignment]
    hub = _FakeConnectorHubWithMarkers(skipped={skipped_id: "secrets unavailable"}, healthy={healthy_id})
    clear_mock = AsyncMock()

    with (
        patch("modulo.core.connector_hub.ConnectorHub", return_value=hub),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="key")),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub") as set_hub_mock,
        patch(
            "modulo.db.crud.connector_instance.mark_instances_degraded",
            AsyncMock(side_effect=RuntimeError("marker write failed")),
        ),
        patch("modulo.db.crud.connector_instance.clear_degraded_markers", clear_mock),
    ):
        result = await executor._init_connector_hub(org_id)

    assert result is hub
    nested_cm.__aenter__.assert_awaited_once()
    nested_cm.__aexit__.assert_awaited_once()
    exc_type, _exc, _tb = nested_cm.__aexit__.await_args.args
    assert exc_type is RuntimeError
    clear_mock.assert_not_awaited()
    set_hub_mock.assert_called_once_with(hub)


async def test_init_connector_hub_no_marker_calls_when_clean():
    """FAR-495: with nothing skipped and nothing healthy, marker wiring is a no-op.

    An empty ``skipped``/``healthy`` outcome must not open a savepoint or invoke
    either crud function — the guard skips the whole marker block.
    """
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    session = _make_connector_init_session([MagicMock()])
    session.begin_nested = MagicMock()
    executor._session_factory = _make_session_factory(session)  # type: ignore[assignment]
    hub = _FakeConnectorHubWithMarkers()
    mark_mock = AsyncMock()
    clear_mock = AsyncMock()

    with (
        patch("modulo.core.connector_hub.ConnectorHub", return_value=hub),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="key")),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub"),
        patch("modulo.db.crud.connector_instance.mark_instances_degraded", mark_mock),
        patch("modulo.db.crud.connector_instance.clear_degraded_markers", clear_mock),
    ):
        result = await executor._init_connector_hub(org_id)

    assert result is hub
    mark_mock.assert_not_awaited()
    clear_mock.assert_not_awaited()
    session.begin_nested.assert_not_called()


class _FakeModelBackendHub:
    """Stand-in ModelBackendHub whose __aexit__ is awaited by ``_teardown_hub``."""

    def __init__(self) -> None:
        self.exited = False

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True


async def test_teardown_model_backend_hub_when_connector_hub_init_raises_pre_stream():
    """FAR-439: a pre-stream connector-hub raise must tear down the model-backend hub.

    ``_init_run_environment`` acquires model_backend_hub (via ``__aenter__`` +
    ``set_model_backend_hub``) BEFORE ``_init_connector_hub``. On a configured-path
    raise that propagates out of execute() before the post-stream try/finally runs,
    the hub would be leaked (ContextVar dangling, async client never awaited). The
    raise must await the hub's ``__aexit__`` and clear the ContextVars before
    propagating so no async client is left dangling.
    """
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._otel_bridge = MagicMock()

    model_hub = _FakeModelBackendHub()
    with (
        patch.object(executor, "_init_model_backend_hub", new=AsyncMock(return_value=model_hub)),
        patch.object(
            executor,
            "_init_connector_hub",
            new=AsyncMock(side_effect=RuntimeError("connector hub init failed")),
        ),
        patch("modulo.core.pipeline_engine.executor.set_model_backend_hub", new=MagicMock()) as set_mb,
        patch("modulo.core.pipeline_engine.executor.set_connector_hub", new=MagicMock()) as set_ch,
        patch("modulo.core.pipeline_engine.executor.set_cancellation_check", new=MagicMock()) as set_cc,
        patch("modulo.core.pipeline_engine.executor.set_audit_hook", new=MagicMock()) as set_ah,
        patch("modulo.core.pipeline_engine.executor.get_registry") as get_registry_mock,
    ):
        registry = MagicMock()
        get_registry_mock.return_value = registry
        with pytest.raises(RuntimeError, match="connector hub init failed"):
            await executor._init_run_environment(
                org_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                graph_json={"nodes": []},
            )

    assert model_hub.exited is True
    set_mb.assert_called_once_with(None)
    set_ch.assert_called_once_with(None)
    assert set_cc.call_args_list[-1] == call(None)
    assert set_ah.call_args_list[-1] == call(None)
    registry.close.assert_called_once_with(run_id)
