"""Unit tests for retryable transient node cancellation (NodeCancelledError).

A ``sandbox_agent`` node's E2B command wait can be cancelled from outside;
langgraph wraps the node body's ``asyncio.CancelledError`` into
``langgraph.errors.NodeCancelledError``. The executor must NOT terminal-fail
such runs: it resets the run to ``pending``, releases the E2B idempotency
fence (so the successor claim can re-dispatch), and re-raises so the SAQ job
retries — bounded by ``SAQ_RUN_RETRIES`` / the run's claim count.

Mock/fake based — no Postgres required (mirrors test_executor.py).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.errors import NodeCancelledError
from sqlalchemy.ext.asyncio import AsyncSession

import modulo.core.pipeline_execution as pe
from modulo.core.pipeline_engine.executor import PipelineExecutor

# ---------------------------------------------------------------------------
# Helpers (mirror test_executor.py)
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    status: str = "pending",
    claim_count: int = 0,
    claim_token: str | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.snapshot_id = snapshot_id or uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = status
    run.claim_count = claim_count
    run.claim_token = claim_token
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
    session.add = MagicMock()
    session.execute = _execute
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


def _mock_compiled_raising(exc: Exception) -> MagicMock:
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


async def _bypass_capacity(mock_self, **kwargs):
    run = MagicMock()
    run.status = "running"
    return run


# ---------------------------------------------------------------------------
# PipelineExecutor.execute — NodeCancelledError → retryable (reset + re-raise)
# ---------------------------------------------------------------------------


async def test_execute_resets_to_pending_and_reraises_node_cancellation():
    """A transient node cancellation under the retry cap resets the run to
    pending, releases the E2B fence with the run's claim token, and re-raises
    so the SAQ job retries — it must NOT terminal-fail via finalize_cost."""
    run = _make_run(claim_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()) as mock_update,
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
        patch("modulo.core.pipeline_execution.e2b_idempotency_enabled", return_value=True),
        patch("modulo.core.pipeline_execution.e2b_dispatch_release_fenced", new=AsyncMock()) as mock_release,
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(NodeCancelledError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    # Reset to pending so the retry claim (status='pending') succeeds.
    update_call = mock_update.await_args
    assert update_call is not None
    assert update_call.args[2] == "pending"
    assert update_call.kwargs.get("clear_error_code") is True
    # E2B fence released with the run's CURRENT claim token.
    mock_release.assert_awaited_once_with(str(run.id), "tok-claim-abc")
    # No terminal finalize — the run is NOT failed.
    mock_finalize.assert_not_awaited()
    # Cleanup ran so the retry re-entry gets a fresh broker.
    registry.close.assert_called_once_with(run.id)


async def test_execute_skips_fence_when_e2b_idempotency_disabled():
    """When the E2B idempotency knob is off the retry path still resets to
    pending and re-raises, but the fence release is skipped."""
    run = _make_run(claim_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=False)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()) as mock_update,
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
        patch("modulo.core.pipeline_execution.e2b_idempotency_enabled", return_value=False),
        patch("modulo.core.pipeline_execution.e2b_dispatch_release_fenced", new=AsyncMock()) as mock_release,
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(NodeCancelledError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert mock_update.await_args.args[2] == "pending"
    mock_release.assert_not_awaited()


async def test_execute_terminal_fails_node_cancellation_when_retries_exhausted():
    """Once the claim count reaches the SAQ retry cap the run terminal-fails
    with error_code 'node_cancelled' (NOT the raw langgraph class name) and is
    NOT reset to pending."""
    run = _make_run(claim_count=5, claim_token="tok-claim-abc")
    final_run = _make_run(
        run_id=run.id,
        status="failed",
        claim_count=5,
        claim_token="tok-claim-abc",
    )
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()) as mock_update,
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
        patch("modulo.core.pipeline_execution.e2b_idempotency_enabled", return_value=True),
        patch("modulo.core.pipeline_execution.e2b_dispatch_release_fenced", new=AsyncMock()) as mock_release,
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    call = mock_finalize.await_args
    assert call is not None
    assert call.kwargs["status"] == "failed"
    assert call.kwargs.get("error_code") == "node_cancelled"
    assert call.kwargs["is_terminal"] is True
    assert call.kwargs["error_detail"].startswith("Sandbox node cancelled (transient) after retries exhausted")
    # No reset, no fence release once retries are exhausted.
    mock_update.assert_not_awaited()
    mock_release.assert_not_awaited()


async def test_execute_non_cancellation_exception_still_terminal_fails():
    """Regression guard: a NON-cancellation exception keeps the existing
    behaviour — terminal failure with error_code = exception type name."""
    run = _make_run(claim_count=1, claim_token="tok-claim-abc")
    final_run = _make_run(
        run_id=run.id,
        status="failed",
        claim_count=1,
        claim_token="tok-claim-abc",
    )
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RuntimeError("boom"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()) as mock_update,
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
        patch("modulo.core.pipeline_execution.e2b_idempotency_enabled", return_value=True),
        patch("modulo.core.pipeline_execution.e2b_dispatch_release_fenced", new=AsyncMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    assert mock_finalize.await_args.kwargs.get("error_code") == "RuntimeError"
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    mock_update.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_executor_with_watchdog — NodeCancelledError propagates (not swallowed)
# ---------------------------------------------------------------------------


async def test_run_executor_with_watchdog_reraises_node_cancelled_error():
    """A NodeCancelledError from the executor must propagate out of the
    watchdog wrapper so the SAQ job retries — never swallowed into
    {"status": "complete"}."""
    executor = MagicMock()

    async def _boom() -> None:
        raise NodeCancelledError("node-a")

    engine = MagicMock()
    with (
        patch.object(pe, "get_settings", return_value=MagicMock(saq_setup_grace_seconds=60)),
        patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
        patch.object(pe, "fail_run_terminal", new_callable=AsyncMock),
        pytest.raises(NodeCancelledError),
    ):
        await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
            engine,
            run_id=str(uuid.uuid4()),
            org_id=str(uuid.uuid4()),
            executor=executor,
            job=None,
            execute_fn=_boom,
        )
