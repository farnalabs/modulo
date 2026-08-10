"""Unit tests for retryable transient node cancellation (NodeCancelledError).

A ``sandbox_agent`` node's E2B command wait can be cancelled from outside;
langgraph wraps the node body's ``asyncio.CancelledError`` into
``langgraph.errors.NodeCancelledError``. The executor must NOT terminal-fail
such runs: it resets the run to ``pending``, releases the E2B idempotency
fence (so the successor claim can re-dispatch), and re-raises so the SAQ job
retries — bounded by ``SAQ_RUN_RETRIES`` / the run's node-attempt count (NOT
the claim count, which capacity-deferred / non-executing claims inflate).

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
    node_attempt_count: int = 0,
    claim_token: str | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.snapshot_id = snapshot_id or uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = status
    run.claim_count = claim_count
    run.node_attempt_count = node_attempt_count
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


def _make_session(snapshot: MagicMock, statements: list[str] | None = None) -> AsyncMock:
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

    recorded = statements if statements is not None else []

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        recorded.append(str(_args[0]) if _args else "")
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
    pending via a FENCED conditional UPDATE (claim_token + status='running') and
    re-raises so the SAQ job retries — it must NOT terminal-fail via
    finalize_cost. claim_count (10) exceeds the budget — only node_attempt_count
    gates (dist/runtime-core A1/A3)."""
    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(NodeCancelledError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    # Fenced pending-reset: a conditional UPDATE guarded by OUR claim token +
    # status='running' so a superseded original cannot demote a successor's row.
    reset_stmt = next(s for s in statements if "status='pending'" in s)
    assert "claim_token=:tok" in reset_stmt
    assert "status='running'" in reset_stmt
    assert "cancellation_requested = false" in reset_stmt
    # No terminal finalize — the run is NOT failed.
    mock_finalize.assert_not_awaited()
    # Cleanup ran so the retry re-entry gets a fresh broker.
    registry.close.assert_called_once_with(run.id)


async def test_execute_superseded_skips_pending_reset_and_reraises():
    """A superseded executor (DB token rotated by a successor) must NOT reset the
    run to pending and must NOT terminal-fail it — the successor owns the row.
    It cleans up and re-raises so the SAQ job retries (its next claim loses)."""
    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-successor-xyz")
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(NodeCancelledError):
            # Our executor holds token "tok-claim-abc" but the DB row shows the
            # successor's "tok-successor-xyz" → superseded.
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    # NO pending reset (the successor owns the run).
    assert not any("status='pending'" in s for s in statements)
    # NO terminal finalize (never fail the run out from under the successor).
    mock_finalize.assert_not_awaited()
    registry.close.assert_called_once_with(run.id)


async def test_execute_reraises_sandbox_node_failed_error():
    """The retryable sandbox-infra failure class (SandboxNodeFailedError) goes
    through the SAME retry path as NodeCancelledError — fenced reset to pending
    + re-raise (dist/runtime-core A6)."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements)
    mock_finalize.assert_not_awaited()
    registry.close.assert_called_once_with(run.id)


async def test_execute_terminal_fails_node_cancellation_when_retries_exhausted():
    """Once the node-attempt count reaches the SAQ retry cap the run
    terminal-fails with error_code 'node_cancelled' (NOT the raw langgraph
    class name), publishes a run_failed broker event for WS subscribers, and
    is NOT reset to pending."""
    run = _make_run(claim_count=20, node_attempt_count=5, claim_token="tok-claim-abc")
    final_run = _make_run(
        run_id=run.id,
        status="failed",
        claim_count=20,
        node_attempt_count=5,
        claim_token="tok-claim-abc",
    )
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    broker = registry.get_or_create.return_value
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc"
        )

    assert result is final_run
    call = mock_finalize.await_args
    assert call is not None
    assert call.kwargs["status"] == "failed"
    assert call.kwargs.get("error_code") == "node_cancelled"
    assert call.kwargs["is_terminal"] is True
    assert call.kwargs["error_detail"].startswith("Sandbox node cancelled (transient) after retries exhausted")
    # Retry exhaustion publishes a live run_failed broker event for WS
    # subscribers — consistent with every other terminal-failure path.
    publish_call = broker.publish.call_args
    assert publish_call is not None
    assert publish_call.args[0] == "run_failed"
    payload = publish_call.args[1]
    assert payload["error"] == "node_cancelled"
    assert payload["detail"].startswith("Sandbox node cancelled (transient) after retries exhausted")
    # No reset once retries are exhausted.
    assert not any("status='pending'" in s for s in statements)


async def test_execute_retry_budget_ignores_non_executing_claims():
    """Capacity-deferred / non-executing claims do NOT consume the retry
    budget. Here claim_count (5) is AT the old saq_run_retries=5 cap — a pure
    claim-count gate would terminal-fail — but only ONE real node-execution
    attempt happened (node_attempt_count=1), so the executor must still reset
    to pending and re-raise for the SAQ retry."""
    run = _make_run(claim_count=5, node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    broker = registry.get_or_create.return_value
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(NodeCancelledError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    # Retried, not terminal-failed: fenced reset to pending, no finalize, no run_failed.
    assert any("status='pending'" in s for s in statements)
    mock_finalize.assert_not_awaited()
    broker.publish.assert_not_called()


async def test_execute_non_cancellation_exception_still_terminal_fails():
    """Regression guard: a NON-cancellation exception keeps the existing
    behaviour — terminal failure with error_code = exception type name."""
    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    final_run = _make_run(
        run_id=run.id,
        status="failed",
        claim_count=10,
        node_attempt_count=1,
        claim_token="tok-claim-abc",
    )
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RuntimeError("boom"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_e2b_idempotency=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc"
        )

    assert result is final_run
    assert mock_finalize.await_args.kwargs.get("error_code") == "RuntimeError"
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert not any("status='pending'" in s for s in statements)


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
