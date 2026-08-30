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
from modulo.core.pipeline_engine.executor import PipelineExecutor, _node_output_has_idempotency_gate, _should_skip_retry

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
    run.idempotency_key = None
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
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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


async def test_execute_non_idempotent_graph_suppresses_transient_retry():
    """FAR-295: a graph declaring a node with ``idempotent: false`` must NOT be
    re-dispatched on a transient NodeCancelledError even while retry budget
    remains — the re-run would re-execute that node's external side effect. The
    run terminal-fails via the single finalization path (no re-raise, no fenced
    pending-reset) with error_code node_cancelled and an error_detail that names
    the idempotency suppression; the run_failed publish carries the reason."""
    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot(graph_json={"nodes": [{"id": "node-a", "idempotent": False}], "edges": []})
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(NodeCancelledError("node-a"))
    registry = _mock_registry()
    broker = registry.get_or_create.return_value
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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

    # Terminal-failed (execute returned normally) — NOT re-raised for retry.
    assert result is run
    # No fenced pending-reset was issued (no re-dispatch).
    assert not any("status='pending'" in s for s in statements)
    # Finalized with the transient code and an error_detail that explains the
    # idempotency suppression (the retry budget was NOT exhausted).
    call = mock_finalize.await_args
    assert call is not None
    assert call.kwargs["status"] == "failed"
    assert call.kwargs.get("error_code") == "node_cancelled"
    detail = call.kwargs["error_detail"]
    assert "idempotent=false" in detail
    assert "retry suppressed" in detail
    # Live run_failed publish for WS subscribers.
    run_failed = [c.args for c in broker.publish.call_args_list if c.args and c.args[0] == "run_failed"]
    assert run_failed
    assert run_failed[0][1]["error"] == "node_cancelled"


async def test_execute_terminal_fail_roundtrips_sandbox_diagnostics():
    """FAR-197 review fix (PR #1317 CHANGES_REQUESTED): the retry-exhausted
    terminal-fail write surface used to truncate the error_detail at 500 chars,
    cutting the stderr tail AND the E2B log tail (the only place the kill reason
    lives) for large-output failures. The write cap is raised to 5000 (matching
    the sanitizer + String(5000) column); this round-trips a realistic large-output
    SandboxNodeFailedError through the executor AND the run-detail read surface
    and asserts stderr + log-tail markers survive."""
    from modulo.core.pipeline_engine.error_codes import present_error
    from modulo.core.pipeline_engine.node_runner import (
        SandboxNodeFailedError,
        _build_no_output_message,
    )

    # Realistic large-output failure: stdout floods, the agent's fatal error sits
    # at the END of stderr, the kill reason at the END of the sandbox log tail —
    # the exact case the 500-char cap previously destroyed.
    stderr_marker = "AGENT_FATAL_AT_END"
    log_marker = "KILL_REASON_AT_END"
    diagnostic = _build_no_output_message(
        exit_code=137,
        stdout_raw="o" * 50_000,
        stderr_raw=("e" * 10_000) + stderr_marker,
        sandbox_id="sbx-roundtrip",
        read_raw="",
        log_tail=("l" * 8_000) + log_marker,
    )
    # The builder already keeps the message under the 5000-char surface cap.
    assert len(diagnostic) <= 5000
    assert stderr_marker in diagnostic
    assert log_marker in diagnostic

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
    compiled = _mock_compiled_raising(SandboxNodeFailedError(diagnostic))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
    persisted = mock_finalize.await_args.kwargs["error_detail"]
    assert persisted.startswith("Sandbox node failed (transient) after retries exhausted")
    assert "no parseable output.json" in persisted
    # The stderr tail (agent error) and the E2B log tail (kill reason) SURVIVE
    # the executor's terminal write surface — the prove-the-fix assertion.
    assert stderr_marker in persisted
    assert log_marker in persisted
    assert len(persisted) <= 5000
    # And they survive the run-detail READ surface (present_error limit=5000).
    _code, presented = present_error("node_cancelled", persisted, limit=5000)
    assert stderr_marker in presented
    assert log_marker in presented
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
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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


# ---------------------------------------------------------------------------
# FAR-228 guard B — idempotency gate retry-suppression
# ---------------------------------------------------------------------------


def _sandbox_snapshot() -> MagicMock:
    """A SINGLE sandbox_agent node graph — the only shape the gate operates on."""
    return _make_snapshot(
        {
            "nodes": [{"id": "node-a", "node_type": "sandbox_agent", "role": None}],
            "edges": [],
        }
    )


def _delivery_markers(run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    key = f"run:{run_id}:node:node-a:1"
    return {key: {"_modulo_marker": True, "delivery_done": True, "attempt_key": key}}


class TestShouldSkipRetryDecision:
    """The shared pure decision helper — delimiter-safe, dict-guarded."""

    def test_correct_node_fires(self) -> None:
        run_id = uuid.uuid4()
        assert _should_skip_retry("node-a", _delivery_markers(run_id), str(run_id)) is True

    def test_other_node_does_not_fire(self) -> None:
        run_id = uuid.uuid4()
        assert _should_skip_retry("node-b", _delivery_markers(run_id), str(run_id)) is False

    def test_non_dict_markers_ignored(self) -> None:
        run_id = uuid.uuid4()
        assert _should_skip_retry("node-a", "not-a-dict", str(run_id)) is False
        assert _should_skip_retry("node-a", MagicMock(), str(run_id)) is False

    def test_delimiter_trap_node(self) -> None:
        """:node:n1: must NOT match :node:n11: (delimiter trap)."""
        run_id = uuid.uuid4()
        key = f"run:{run_id}:node:n11:1"
        markers = {key: {"_modulo_marker": True, "delivery_done": True, "attempt_key": key}}
        assert _should_skip_retry("n1", markers, str(run_id)) is False
        assert _should_skip_retry("n11", markers, str(run_id)) is True

    def test_delimiter_trap_run(self) -> None:
        """run:run-1: must NOT match run:run-11:."""
        markers = {
            "run:run-11:node:node-a:1": {
                "_modulo_marker": True,
                "delivery_done": True,
                "attempt_key": "run:run-11:node:node-a:1",
            }
        }
        assert _should_skip_retry("node-a", markers, "run-1") is False
        assert _should_skip_retry("node-a", markers, "run-11") is True

    def test_marker_without_delivery_done_never_fires(self) -> None:
        run_id = uuid.uuid4()
        key = f"run:{run_id}:node:node-a:1"
        markers = {key: {"_modulo_marker": True, "delivery_done": False, "attempt_key": key}}
        assert _should_skip_retry("node-a", markers, str(run_id)) is False


class TestNodeOutputHasIdempotencyGate:
    """The fire_agent_signal one-rule skip — keyed on the marker only."""

    def test_gated_envelope_true(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _idempotency_gate_skipped_envelope

        assert _node_output_has_idempotency_gate(_idempotency_gate_skipped_envelope("node-a")) is True

    def test_template_error_skip_still_fires(self) -> None:
        """A status=skipped envelope WITHOUT the idempotency_gate marker must
        NOT be suppressed (template-error skips fire today)."""
        envelope = {"artifacts": [{"node_id": "node-a", "status": "skipped", "output": {"status": "skipped"}}]}
        assert _node_output_has_idempotency_gate(envelope) is False

    def test_non_dict_is_false(self) -> None:
        assert _node_output_has_idempotency_gate(None) is False
        assert _node_output_has_idempotency_gate("nope") is False


async def test_execute_gate_suppresses_retry_when_delivery_marked():
    """FAR-228 guard B red witness (WITH gate): a transient SandboxNodeFailedError
    for a node whose marker carries delivery_done=True is SUPPRESSED — the run
    completes COMPLETE with error_code harness.idempotency_gate, no pending-reset,
    no run_failed publish, no re-raise, run_completed published exactly once, and
    node_attempt_count stays 1 (no phantom retry)."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    run.raw_output_markers = _delivery_markers(run.id)
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    broker = registry.get_or_create.return_value
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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

    # Gate fired: complete + harness.idempotency_gate, NOT failed.
    assert result is run
    call = mock_finalize.await_args
    assert call is not None
    assert call.kwargs["status"] == "complete"
    assert call.kwargs.get("error_code") == "harness.idempotency_gate"
    assert call.kwargs.get("error_detail") == "delivery already sent; transient retry suppressed by idempotency gate"
    # No pending-reset (the run was NOT demoted for a retry).
    assert not any("status='pending'" in s for s in statements)
    # No run_failed publish; run_completed published exactly once.
    publish_args = [c.args for c in broker.publish.call_args_list]
    assert not any(a[0] == "run_failed" for a in publish_args)
    assert [a[0] for a in publish_args].count("run_completed") == 1
    # The completed-node output is the SKIPPED ENVELOPE (work_intact True).
    assert call.kwargs["segment_completed_node_outputs"]["node-a"]["artifacts"][0]["status"] == "skipped"


async def test_execute_gate_suppresses_retry_when_retries_exhausted():
    """FAR-228 guard B retries-exhausted case: node_attempt_count (5) is AT the
    SAQ retry cap — WITHOUT the gate this terminal-fails; WITH a delivery marker
    the gate completes the run instead of failing it."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=20, node_attempt_count=5, claim_token="tok-claim-abc")
    run.raw_output_markers = _delivery_markers(run.id)
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    broker = registry.get_or_create.return_value
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
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

    assert result is run
    assert mock_finalize.await_args.kwargs["status"] == "complete"
    assert mock_finalize.await_args.kwargs.get("error_code") == "harness.idempotency_gate"
    assert not any("status='pending'" in s for s in statements)
    publish_args = [c.args for c in broker.publish.call_args_list]
    assert not any(a[0] == "run_failed" for a in publish_args)
    assert [a[0] for a in publish_args].count("run_completed") == 1


async def test_execute_gate_magicmock_markers_skips_gate():
    """FAR-228 MagicMock compatibility: a MagicMock raw_output_markers (not a
    dict) is ignored by the isinstance guard — the gate never fires and the
    existing retry path (pending-reset + re-raise) is untouched."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    run.raw_output_markers = MagicMock()  # NOT a dict -> gate must stay silent
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements), "gate silent -> existing reset + re-raise"
    mock_finalize.assert_not_awaited()


async def test_execute_gate_kill_switch_off_disables_gate():
    """FAR-228 kill-switch: modulo_idempotency_gate_enabled=False means the gate
    never fires even with a delivery marker — the transient retry proceeds."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    run.raw_output_markers = _delivery_markers(run.id)
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=False)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements), "flag off -> gate must not fire"
    mock_finalize.assert_not_awaited()


async def test_execute_gate_node_id_none_disables_gate():
    """FAR-228: a SandboxNodeFailedError WITHOUT node_id disables guard B —
    the existing retry path is preserved for every pre-FAR-228 raise site."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    run.raw_output_markers = _delivery_markers(run.id)
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled"))  # NO node_id
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements), "node_id None -> gate must not fire"
    mock_finalize.assert_not_awaited()


async def test_execute_gate_marker_absent_preserves_retry_path():
    """FAR-228 guard B gate_ok=False (marker ABSENT): a run whose markers dict
    contains NO delivery_done marker for the failing node is NOT suppressed —
    the exact pre-gate retry path (fenced pending-reset + re-raise) runs."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    run.raw_output_markers = {}  # dict, but no delivery_done marker for node-a
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements), "no marker -> gate must not fire, reset + re-raise"
    mock_finalize.assert_not_awaited()


async def test_execute_gate_superseded_never_suppresses():
    """FAR-228 guard B gate_ok=False (superseded): even with a delivery marker
    present, a superseded executor (DB token rotated by a successor) must NOT
    suppress the retry AND must NOT reset/terminal-fail — the successor owns the
    run. The gate is explicitly disabled by the ``not superseded`` term."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-successor-xyz")
    run.raw_output_markers = _delivery_markers(run.id)
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            # Our executor holds "tok-claim-abc" but the DB row shows the
            # successor's "tok-successor-xyz" -> superseded -> gate never fires.
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert not any("status='pending'" in s for s in statements), "superseded -> never demote the successor's row"
    mock_finalize.assert_not_awaited()


async def test_execute_gate_multi_node_inert():
    """FAR-228 guard B single-node scope: on a graph with TWO sandbox_agent nodes
    the gate is INERT — a transient failure of node-b (with a delivery marker)
    is NOT suppressed and retries exactly as before (pending-reset + re-raise)."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    key = f"run:{run.id}:node:node-b:1"
    run.raw_output_markers = {key: {"_modulo_marker": True, "delivery_done": True, "attempt_key": key}}
    run.cancellation_requested = False
    snapshot = _make_snapshot(
        {
            "nodes": [
                {"id": "node-a", "node_type": "sandbox_agent", "role": None},
                {"id": "node-b", "node_type": "sandbox_agent", "role": None},
            ],
            "edges": [],
        }
    )
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-b"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements), "multi-node -> gate must not fire, reset + re-raise"
    mock_finalize.assert_not_awaited()


async def test_execute_gate_prove_the_fix_suppression_requires_decision():
    """FAR-228 prove-the-fix RED witness: the gate suppression is DRIVEN by
    _should_skip_retry. Patching the decision to False (with the marker present,
    kill-switch on, single node, no supersede/stall/cancel) flips the outcome:
    the transient retry proceeds (pending-reset + re-raise), NOT complete +
    harness.idempotency_gate. The green twin is
    test_execute_gate_suppresses_retry_when_delivery_marked — together they prove
    the suppression is not an artifact of the surrounding patch set."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(claim_count=10, node_attempt_count=1, claim_token="tok-claim-abc")
    run.raw_output_markers = _delivery_markers(run.id)
    run.cancellation_requested = False
    snapshot = _sandbox_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("stalled", node_id="node-a"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, modulo_idempotency_gate_enabled=True)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
        patch("modulo.core.pipeline_engine.executor._should_skip_retry", return_value=False),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(SandboxNodeFailedError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert any("status='pending'" in s for s in statements), "decision False -> gate must not fire, reset + re-raise"
    mock_finalize.assert_not_awaited()
