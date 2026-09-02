"""Unit tests for the pipeline retry_policy decision logic in the executor.

The primary contract is ``_retry_after_policy`` â€” the pure decision function
that maps a terminal (final_status, error_code) outcome to a retry budget.
These tests assert the matching rules directly; the execute() integration path
(reset-to-pending + re-raise) is covered by the executor's fenced-retry tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import executor as executor_module
from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    RunRetryPolicyError,
    _graph_is_idempotent,
    _retry_after_policy,
    _retry_backoff_seconds,
)


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    status: str = "running",
    node_attempt_count: int = 1,
    claim_token: str | None = "tok-claim-abc",
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.snapshot_id = snapshot_id or uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = status
    run.node_attempt_count = node_attempt_count
    run.claim_token = claim_token
    run.claim_count = 10
    return run


def _make_snapshot(graph_json: dict[str, Any] | None = None) -> MagicMock:
    snap = MagicMock()
    snap.graph_json = graph_json or {
        "nodes": [{"id": "node-a", "role": None}],
        "edges": [],
    }
    snap.run_context_defaults = {"context_key": "context_val"}
    return snap


def _make_pipeline(*, retry_policy: Any = None) -> MagicMock:
    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 30
    pipeline.max_duration_seconds = 3600
    pipeline.max_steps = 100
    pipeline.token_budget = None
    pipeline.node_timeout_seconds = 300
    pipeline.retry_policy = retry_policy if retry_policy is not None else {}
    return pipeline


def _make_session(snapshot: MagicMock, statements: list[str] | None = None, retry_policy: Any = None) -> AsyncMock:
    pipeline = _make_pipeline(retry_policy=retry_policy)

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

    session = AsyncMock(spec=object)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.execute = _execute
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx() -> AsyncIterator[AsyncMock]:
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    mock_cls.check_retry_policy = MagicMock()
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
# _retry_after_policy â€” pure decision logic (the extensible matching contract)
# ---------------------------------------------------------------------------


def test_retry_after_policy_stall_match():
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "stalled", "executor_stalled") == 2


def test_retry_after_policy_stall_does_not_match_timeout():
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "failed", "node_timeout") is None


def test_retry_after_policy_timeout_match():
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 1}, "failed", "node_timeout") == 1


def test_retry_after_policy_timeout_error_code_match():
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 1}, "failed", "TimeoutError") == 1


def test_retry_after_policy_failure_match():
    assert _retry_after_policy({"on": ["failure"], "max_retries": 1}, "failed", "some_other_error") == 1


def test_retry_after_policy_no_policy_returns_none():
    assert _retry_after_policy({}, "stalled", "executor_stalled") is None
    assert _retry_after_policy(None, "failed", "boom") is None


def test_retry_after_policy_malformed_on_returns_none():
    assert _retry_after_policy({"on": ["bogus"], "max_retries": 2}, "failed", "boom") is None
    assert _retry_after_policy({"on": "stall", "max_retries": 2}, "stalled", "executor_stalled") is None


def test_retry_after_policy_zero_budget_returns_none():
    assert _retry_after_policy({"on": ["failure"], "max_retries": 0}, "failed", "boom") is None


def test_retry_after_policy_failure_excludes_timeout_outcome():
    # A "failure"-only policy must not retry a node_timeout outcome.
    assert _retry_after_policy({"on": ["failure"], "max_retries": 3}, "failed", "node_timeout") is None


def test_retry_after_policy_failure_excludes_hang_death():
    # FAR-136 Gap 2 (prove-the-fix): a sandbox-agent hang death terminalizes as
    # error_code="node_cancelled" with "likely hung" in error_detail. A
    # "failure"-only policy must NOT re-dispatch it â€” each re-dispatch burns a
    # full node timeout with zero recovery probability.
    hang_detail = (
        "Sandbox agent command produced no output within 1200s. No stdout/stderr "
        "was captured â€” the agent likely hung before writing any result."
    )
    assert _retry_after_policy({"on": ["failure"], "max_retries": 5}, "failed", "node_cancelled", hang_detail) is None


def test_retry_after_policy_failure_excludes_hang_death_dotted_code():
    # The dotted alias (node.cancelled) behaves identically to the legacy code.
    assert (
        _retry_after_policy(
            {"on": ["failure"], "max_retries": 5},
            "failed",
            "node.cancelled",
            "the agent likely hung before writing any result",
        )
        is None
    )


def test_retry_after_policy_failure_retries_transient_node_cancelled():
    # FAR-136 Gap 2 (both directions): a TRANSIENT node_cancelled (no "likely
    # hung" marker) stays retryable via the "failure" event â€” the exclusion
    # must stay surgical.
    assert (
        _retry_after_policy(
            {"on": ["failure"], "max_retries": 3},
            "failed",
            "node_cancelled",
            "Sandbox node cancelled (transient): command wait interrupted",
        )
        == 3
    )


def test_retry_after_policy_failure_still_matches_with_detail_present():
    # A generic failure with an error_detail (no hang marker) still matches the
    # "failure" event â€” the new error_detail param must not change the
    # pre-existing generic-failure behaviour.
    assert _retry_after_policy({"on": ["failure"], "max_retries": 2}, "failed", "boom", "some detail") == 2


def test_retry_after_policy_stall_error_code_on_failed_status():
    # A stall surfaces as status "failed" with error_code "executor_stalled"
    # (the zombie watchdog path) â€” the "stall" event must still match it.
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "failed", "executor_stalled") == 2


# ---------------------------------------------------------------------------
# FAR-503 â€” the "eval_failed" event and the node-deadline "timeout" alias
# ---------------------------------------------------------------------------


def test_retry_after_policy_eval_failed_match():
    # FAR-503: an eval-blocked run terminalizes as final_status "eval_failed"
    # with raw error_code "eval_blocked" â€” the new event re-dispatches it.
    assert _retry_after_policy({"on": ["eval_failed"], "max_retries": 1}, "eval_failed", "eval_blocked") == 1


def test_retry_after_policy_eval_failed_dotted_code_match():
    # The canonical dotted spelling (eval.blocked) behaves identically.
    assert _retry_after_policy({"on": ["eval_failed"], "max_retries": 2}, "eval_failed", "eval.blocked") == 2


def test_retry_after_policy_eval_failed_stays_surgical():
    # The new event must not hijack the pre-existing events: a "eval_failed"-
    # only policy never retries a stall/timeout/generic-failure outcome, and a
    # "failure"-only policy never retries an eval-blocked run (eval_failed is a
    # distinct terminal status, not a generic failure).
    assert _retry_after_policy({"on": ["eval_failed"], "max_retries": 1}, "failed", "boom") is None
    assert _retry_after_policy({"on": ["eval_failed"], "max_retries": 1}, "failed", "node_timeout") is None
    assert _retry_after_policy({"on": ["eval_failed"], "max_retries": 1}, "stalled", "executor_stalled") is None
    assert _retry_after_policy({"on": ["failure"], "max_retries": 1}, "eval_failed", "eval_blocked") is None


def test_retry_after_policy_timeout_matches_node_deadline_exceeded():
    # FAR-369: the absolute node-deadline watchdog terminalizes with
    # "node_deadline_exceeded" â€” a {on: ["timeout"]} policy must re-dispatch it
    # (the live gap: the deadline code was not in the timeout alias set).
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 1}, "failed", "node_deadline_exceeded") == 1


def test_retry_after_policy_timeout_matches_dotted_deadline_code():
    # The dotted registry spelling resolves identically through map_legacy_code.
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 2}, "failed", "node.deadline_exceeded") == 2


# ---------------------------------------------------------------------------
# _graph_is_idempotent â€” FAR-295 (idempotent flag gates every retry path)
# ---------------------------------------------------------------------------


def _graph_with_idempotent(*values: bool | None) -> dict[str, Any]:
    """Build a graph whose nodes carry the given ``idempotent`` values."""
    nodes = []
    for i, value in enumerate(values):
        node: dict[str, Any] = {"id": f"node-{i}", "node_type": "agent", "role": None}
        if value is not None:
            node["idempotent"] = value
        nodes.append(node)
    return {"nodes": nodes, "edges": []}


def test_graph_is_idempotent_when_field_missing():
    # Legacy graphs (persisted before the field existed) must stay retryable.
    assert _graph_is_idempotent(_graph_with_idempotent()) is True
    assert _graph_is_idempotent(_graph_with_idempotent(None)) is True
    assert _graph_is_idempotent(_graph_with_idempotent(None, None)) is True


def test_graph_is_idempotent_when_all_explicit_true():
    assert _graph_is_idempotent(_graph_with_idempotent(True, True)) is True


def test_graph_is_idempotent_false_when_any_node_non_idempotent():
    assert _graph_is_idempotent(_graph_with_idempotent(False)) is False
    assert _graph_is_idempotent(_graph_with_idempotent(True, False)) is False
    assert _graph_is_idempotent(_graph_with_idempotent(False, True, None)) is False


def test_graph_is_idempotent_defaults_for_malformed_input():
    assert _graph_is_idempotent(None) is True
    assert _graph_is_idempotent({}) is True
    assert _graph_is_idempotent({"nodes": None}) is True
    assert _graph_is_idempotent({"nodes": ["not-a-dict"]}) is True


async def test_execute_retry_policy_never_redispatch_non_idempotent_graph():
    """FAR-295: a graph containing a node with idempotent=false is NEVER
    re-dispatched by the run-level retry_policy even when the policy would
    otherwise retry the failure â€” re-running would double-execute the
    side-effecting node. Mirrors the FAR-210 correction-run exclusion.

    Prove-the-fix: without the ``graph_idempotent`` guard at the dispatch site,
    execute() re-raises RunRetryPolicyError and resets the run to pending; with
    the guard, the run terminal-fails via the single finalization path (no
    pending-reset, no backoff sleep)."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot(
        graph_json={
            "nodes": [
                {"id": "node-a", "node_type": "sandbox_agent", "idempotent": False},
            ],
            "edges": [],
        }
    )
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        # The non-idempotent graph is excluded, so the backoff sleep must NOT fire.
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id,
            org_id=uuid.uuid4(),
            input_payload={},
            claim_token="tok-claim-abc",
        )
    # No re-dispatch: no RunRetryPolicyError escaped, no backoff sleep ran.
    sleep_mock.assert_not_awaited()
    # No fenced pending-reset was issued.
    resets = [s for s in statements if "status='pending'" in s]
    assert resets == []
    # Terminal failure via the single finalization path.
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "_GenericFailureError"
    assert result is not None


async def test_execute_retry_policy_still_redispatch_idempotent_graph():
    """FAR-295 sanity: a graph whose nodes are all idempotent (or carry no
    idempotent flag) keeps the pre-existing retry_policy re-dispatch behaviour â€”
    the guard must stay surgical and not disable retries for normal graphs."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot(
        graph_json={
            "nodes": [
                {"id": "node-a", "node_type": "agent", "idempotent": True},
            ],
            "edges": [],
        }
    )
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=AsyncMock()))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await executor.execute(
                run_id=run.id,
                org_id=uuid.uuid4(),
                input_payload={},
                claim_token="tok-claim-abc",
            )
    assert exc_info.value.status == "failed"
    # Fenced pending-reset issued (retry still happens for an idempotent graph).
    reset_stmt = next(s for s in statements if "status='pending'" in s)
    assert "claim_token=:tok" in reset_stmt
    mock_finalize.assert_not_awaited()


async def test_execute_transient_retry_suppressed_on_non_idempotent_graph():
    """FAR-295: a transient SandboxNodeFailedError on a graph containing a
    non-idempotent node terminal-fails instead of fenced-resetting + requeueing.
    The error_detail explains that the retry was suppressed by idempotency, and
    no pending-reset / SAQ re-raise occurs."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot(
        graph_json={
            "nodes": [
                {"id": "node-a", "node_type": "sandbox_agent", "idempotent": False},
            ],
            "edges": [],
        }
    )
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _mock_compiled_raising(SandboxNodeFailedError("sandbox infra boom"))

    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id,
            org_id=uuid.uuid4(),
            input_payload={},
            claim_token="tok-claim-abc",
        )
    # No fenced pending-reset â€” the transient retry was suppressed.
    resets = [s for s in statements if "status='pending'" in s]
    assert resets == []
    # Terminal failure with a message that names the idempotency suppression.
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "node_cancelled"
    detail = mock_finalize.await_args.kwargs["error_detail"]
    assert "idempotent=false" in detail
    assert "retry suppressed" in detail
    assert result is not None


# ---------------------------------------------------------------------------
# RunRetryPolicyError â€” subclasses NodeCancelledError (transient to the caller)
# ---------------------------------------------------------------------------


def test_run_retry_policy_error_subclasses_node_cancelled():
    from langgraph.errors import NodeCancelledError

    exc = RunRetryPolicyError("failed", 2)
    assert isinstance(exc, NodeCancelledError)
    assert exc.status == "failed"
    assert exc.max_retries == 2


# ---------------------------------------------------------------------------
# PipelineExecutor.execute â€” terminal failure under a retry policy resets the
# run to pending and re-raises RunRetryPolicyError (not a terminal fail)
# ---------------------------------------------------------------------------


async def test_execute_retry_policy_resets_pending_and_reraises():
    """A terminal 'failed' outcome that the pipeline's retry_policy says to
    retry (with budget remaining) resets the run to pending via the fenced
    conditional UPDATE and re-raises RunRetryPolicyError so SAQ re-dispatches."""

    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)

    # _stream_graph raises a transient NodeCancelledError â€” the run's terminal
    # state then goes through the retry-policy decision point. But a
    # NodeCancelledError with retry budget left is handled by the EXISTING
    # NodeCancelledError path, not the retry-policy path. To exercise the
    # retry-policy path we must produce a terminal (final_status, error_code)
    # tuple WITHOUT a NodeCancelledError â€” a generic exception whose class name
    # becomes the error_code, matching "failure".
    class _GenericFailureError(Exception):
        pass

    compiled = _mock_compiled_raising(_GenericFailureError("boom"))

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
        # FAR-136 backoff: never actually sleep in the retry tests.
        patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=AsyncMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    assert exc_info.value.status == "failed"
    assert exc_info.value.max_retries == 2
    # Fenced pending-reset issued (claim_token guarded).
    reset_stmt = next(s for s in statements if "status='pending'" in s)
    assert "claim_token=:tok" in reset_stmt
    # The run must NOT be terminal-failed via finalize_cost.
    mock_finalize.assert_not_awaited()
    # The broker is closed so the retry re-entry gets a fresh one.
    registry.close.assert_called()


def test_graph_non_idempotent_flag_semantics():
    """FAR-295: the graph helper flags ONLY an explicit boolean False â€” anything
    else (absent, True, or a malformed non-bool) stays idempotent so a retry is
    never fail-opened by a type mixup."""
    from modulo.core.pipeline_engine.executor import _graph_is_idempotent

    assert _graph_is_idempotent(None) is True
    assert _graph_is_idempotent({"nodes": []}) is True
    assert _graph_is_idempotent({"nodes": [{"id": "node-a"}]}) is True
    assert _graph_is_idempotent({"nodes": [{"id": "node-a", "idempotent": True}]}) is True
    assert _graph_is_idempotent({"nodes": [{"id": "node-a", "idempotent": False}]}) is False
    mixed = {"nodes": [{"id": "node-a", "idempotent": True}, {"id": "node-b", "idempotent": False}]}
    assert _graph_is_idempotent(mixed) is False
    # A malformed non-bool value must NOT count as non-idempotent â€” it stays
    # retryable here (save-time validation rejects the graph before it runs).
    assert _graph_is_idempotent({"nodes": [{"id": "node-a", "idempotent": "false"}]}) is True


async def test_execute_retry_policy_suppressed_for_non_idempotent_graph():
    """FAR-295: a pipeline retry_policy must NOT re-dispatch a run whose graph
    declares a node with ``idempotent: false`` â€” the re-run would re-execute that
    node's external side effect. The run terminal-fails via the single
    finalization path with the generic failure code: no RunRetryPolicyError, no
    fenced pending-reset."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot(graph_json={"nodes": [{"id": "node-a", "idempotent": False}], "edges": []})
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

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
        patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=AsyncMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc"
        )

    # No re-dispatch: execute() returned normally (no RunRetryPolicyError) and
    # no fenced pending-reset was issued.
    assert result is not None
    assert not any("status='pending'" in s for s in statements)
    # Terminal failure via the single finalization path with the generic code â€”
    # the non-idempotent graph is excluded from re-dispatch, not remapped to a
    # dedicated error code.
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "_GenericFailureError"


class _GenericFailureError(Exception):
    """A non-transient generic failure â€” its class name becomes the error_code,
    matching the "failure" retry event (unlike NodeCancelledError subclasses)."""


def _make_failure_compiled() -> MagicMock:
    return _mock_compiled_raising(_GenericFailureError("boom"))


def _enter_execute_patches(
    stack: ExitStack,
    factory: MagicMock,
    run: MagicMock,
    compiled: MagicMock,
    registry: MagicMock,
    settings: MagicMock,
) -> AsyncMock:
    """Enter every execute() patch needed for the retry-budget sequence tests.

    Returns the ``finalize_cost`` mock so tests can assert on the terminal
    finalization path.
    """
    finalize_mock = AsyncMock()
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.get_run", return_value=run))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.finalize_cost", new=finalize_mock))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.set_rls_org"))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()))
    stack.enter_context(patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity))
    stack.enter_context(patch("modulo.settings.get_settings", return_value=settings))
    # FAR-136 backoff: never actually sleep in the retry-budget tests.
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=AsyncMock()))
    return finalize_mock


async def _run_single_retry_attempt(
    *,
    node_attempt_count: int,
    retry_policy: dict[str, Any],
    compiled: MagicMock | None = None,
) -> tuple[str, Any, list[str], AsyncMock]:
    """Run ONE execute() against a streaming _GenericFailureError with a FRESH
    mock session.

    Each real execute() invocation starts from a fresh DB session (the
    session_factory creates a new session per call), so each simulated attempt
    must build a fresh session mock â€” reusing one session mock across attempts
    exhausts its canned result iterator and silently corrupts later attempts.
    ``compiled`` overrides the default _GenericFailureError graph (e.g. an
    EvalBlockedError graph for the FAR-503 eval_failed tests).

    Returns ``(outcome, payload, statements, finalize_mock)`` where outcome is
    ``"retry"`` (RunRetryPolicyError raised â€” the run was reset to pending and
    re-dispatched) or ``"terminal"`` (execute() returned normally).
    """
    run = _make_run(node_attempt_count=node_attempt_count, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    if compiled is None:
        compiled = _make_failure_compiled()

    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        executor = PipelineExecutor(MagicMock())
        try:
            result = await executor.execute(
                run_id=run.id,
                org_id=uuid.uuid4(),
                input_payload={},
                claim_token="tok-claim-abc",
            )
        except RunRetryPolicyError as exc:
            return "retry", exc, statements, mock_finalize
        return "terminal", result, statements, mock_finalize


async def test_execute_retry_policy_max_retries_1_retries_exactly_once():
    """max_retries=1 means exactly ONE retry (two execution attempts).

    Attempt 1 (node_attempt_count == 1 <= budget 1) re-dispatches; attempt 2
    (node_attempt_count == 2 > budget 1) terminal-fails. This is the off-by-one
    regression test: the old ``node_attempt_count < retry_budget`` comparison
    yielded ZERO retries for max_retries=1.
    """

    policy = {"on": ["failure"], "max_retries": 1}

    # Attempt 1 (count == budget): re-dispatch via RunRetryPolicyError.
    kind, exc, statements, mock_finalize = await _run_single_retry_attempt(node_attempt_count=1, retry_policy=policy)
    assert kind == "retry", f"attempt 1 should retry; got {kind}"
    assert exc.status == "failed"
    assert exc.max_retries == 1
    resets = [s for s in statements if "status='pending'" in s]
    assert len(resets) == 1
    assert "claim_token=:tok" in resets[0]
    mock_finalize.assert_not_awaited()

    # Attempt 2 (count > budget): terminal fail â€” no re-dispatch.
    kind2, result, statements2, mock_finalize2 = await _run_single_retry_attempt(
        node_attempt_count=2, retry_policy=policy
    )
    assert kind2 == "terminal", f"attempt 2 should be terminal; got {kind2}"
    resets2 = [s for s in statements2 if "status='pending'" in s]
    assert resets2 == []
    mock_finalize2.assert_awaited_once()
    assert mock_finalize2.await_args.kwargs["status"] == "failed"
    assert result is not None


async def test_execute_retry_policy_max_retries_5_retries_all_five():
    """max_retries=5 means exactly FIVE retries (six execution attempts).

    Attempts 1..5 (count 1..5 <= budget 5) re-dispatch; attempt 6 (count 6 > 5)
    terminal-fails. The old ``<`` comparison terminal-failed after the 5th
    attempt, yielding only four retries.
    """

    policy = {"on": ["failure"], "max_retries": 5}
    retried = 0
    terminal_result = None
    for attempt in range(1, 7):
        kind, payload, statements, mock_finalize = await _run_single_retry_attempt(
            node_attempt_count=attempt, retry_policy=policy
        )
        if attempt <= 5:
            assert kind == "retry", f"attempt {attempt} should retry; got {kind}"
            assert payload.max_retries == 5
            resets = [s for s in statements if "status='pending'" in s]
            assert len(resets) == 1
            mock_finalize.assert_not_awaited()
            retried += 1
        else:
            assert kind == "terminal", f"attempt 6 should be terminal; got {kind}"
            mock_finalize.assert_awaited_once()
            assert mock_finalize.await_args.kwargs["status"] == "failed"
            terminal_result = payload

    assert retried == 5
    assert terminal_result is not None


async def test_execute_retry_policy_exhausted_boundary_terminal_no_redispatch():
    """EXHAUSTED boundary: once the attempt count exceeds the retry budget
    (count == budget + 1 after the budgeted retries are consumed) the run
    terminal-fails â€” no fenced pending-reset, no RunRetryPolicyError. This
    covers the terminal branch the existing tests only skirt.
    """

    kind, result, statements, mock_finalize = await _run_single_retry_attempt(
        node_attempt_count=2, retry_policy={"on": ["failure"], "max_retries": 1}
    )
    assert kind == "terminal"
    # No re-dispatch: no fenced pending-reset was issued.
    resets = [s for s in statements if "status='pending'" in s]
    assert resets == []
    # Terminal failure via the single finalization path.
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "_GenericFailureError"
    assert result is not None


# ---------------------------------------------------------------------------
# FAR-503 â€” eval_failed re-dispatch, budget exhaustion, deadline alias,
# and the delivery-sentinel composition (guard A)
# ---------------------------------------------------------------------------


def _eval_blocked_compiled() -> MagicMock:
    """A compiled graph raising EvalBlockedError â€” terminalizes as
    final_status "eval_failed" / error_code "eval_blocked"."""
    from modulo.core.eval_engine import EvalBlockedError

    return _mock_compiled_raising(EvalBlockedError("quality", "score 0.3 below threshold 0.8"))


async def test_execute_retry_policy_eval_failed_redispatches():
    """FAR-503: a run terminalized eval_failed (EvalBlockedError) under
    {on: ["eval_failed"], max_retries: 1} is re-dispatched â€” fenced
    pending-reset + RunRetryPolicyError, no terminal finalization."""
    kind, exc, statements, mock_finalize = await _run_single_retry_attempt(
        node_attempt_count=1,
        retry_policy={"on": ["eval_failed"], "max_retries": 1},
        compiled=_eval_blocked_compiled(),
    )
    assert kind == "retry", f"eval_failed attempt 1 should retry; got {kind}"
    assert exc.status == "eval_failed"
    assert exc.max_retries == 1
    resets = [s for s in statements if "status='pending'" in s]
    assert len(resets) == 1
    assert "claim_token=:tok" in resets[0]
    mock_finalize.assert_not_awaited()


async def test_execute_retry_policy_eval_failed_budget_exhaustion_terminalizes():
    """FAR-503: max_retries counts RETRIES â€” attempt 1 (count <= budget 1)
    re-dispatches; attempt 2 (count > budget) terminal-fails as
    eval_failed/eval_blocked and stays failed (no second re-dispatch)."""
    policy = {"on": ["eval_failed"], "max_retries": 1}

    kind, exc, _statements, _mock_finalize = await _run_single_retry_attempt(
        node_attempt_count=1, retry_policy=policy, compiled=_eval_blocked_compiled()
    )
    assert kind == "retry"
    assert exc.status == "eval_failed"

    kind2, result, statements2, mock_finalize2 = await _run_single_retry_attempt(
        node_attempt_count=2, retry_policy=policy, compiled=_eval_blocked_compiled()
    )
    assert kind2 == "terminal", f"attempt 2 should be terminal; got {kind2}"
    assert not any("status='pending'" in s for s in statements2)
    mock_finalize2.assert_awaited_once()
    assert mock_finalize2.await_args.kwargs["status"] == "eval_failed"
    assert mock_finalize2.await_args.kwargs["error_code"] == "eval_blocked"
    assert result is not None


async def test_execute_retry_policy_node_deadline_exceeded_redispatches_under_timeout():
    """FAR-369 / FAR-503: a terminal outcome carrying the deadline code
    re-dispatches under {on: ["timeout"]} â€” the code resolves into the timeout
    event's alias set.

    The generic-catch publishes ``type(exc).__name__`` as the error_code, so an
    exception class renamed to the raw watchdog spelling exercises the exact
    alias chain a deadline outcome produces (``node_deadline_exceeded`` ->
    ``map_legacy_code`` -> ``node.deadline_exceeded`` -> timeout event). NOTE:
    the watchdog itself currently terminal-fails the run directly and bypasses
    this decision (see the known-limitation note on ``_retry_after_policy``) â€”
    this test pins the alias chain, not the watchdog wiring."""

    class _NodeDeadlineExceededError(Exception):
        """Fake: renamed so its class name IS the raw FAR-369 watchdog code."""

    # Rename the class so type(exc).__name__ == the raw watchdog code.
    _NodeDeadlineExceededError.__name__ = "node_deadline_exceeded"

    kind, exc, statements, mock_finalize = await _run_single_retry_attempt(
        node_attempt_count=1,
        retry_policy={"on": ["timeout"], "max_retries": 1},
        compiled=_mock_compiled_raising(_NodeDeadlineExceededError("node blew its absolute deadline")),
    )
    assert kind == "retry", f"deadline death attempt 1 should retry; got {kind}"
    assert exc.status == "failed"
    assert exc.max_retries == 1
    resets = [s for s in statements if "status='pending'" in s]
    assert len(resets) == 1
    assert "claim_token=:tok" in resets[0]
    mock_finalize.assert_not_awaited()

    # Budget exhaustion terminalizes (max_retries counts RETRIES).
    kind2, result, statements2, mock_finalize2 = await _run_single_retry_attempt(
        node_attempt_count=2,
        retry_policy={"on": ["timeout"], "max_retries": 1},
        compiled=_mock_compiled_raising(_NodeDeadlineExceededError("node blew its absolute deadline")),
    )
    assert kind2 == "terminal"
    assert not any("status='pending'" in s for s in statements2)
    mock_finalize2.assert_awaited_once()
    assert mock_finalize2.await_args.kwargs["error_code"] == "node_deadline_exceeded"
    assert result is not None


async def test_execute_retry_policy_eval_failed_with_delivery_marker_guard_a_composition():
    """FAR-503 sentinel composition: a run whose node marker already carries
    delivery_done=True that terminally fails eval_failed IS re-dispatched by
    the policy (unlike a transient cancellation, which guard B suppresses), and
    on re-execution guard A returns the SKIPPED envelope for that marker â€” the
    delivered node never re-executes its side effect (no duplicate delivery).

    The executor-level half proves the re-dispatch happens with the marker
    present; the guard-A half proves the skipped envelope the re-executed node
    returns (end-to-end guard-A behaviour is covered by
    test_sandbox_output_retention.py::test_guard_a_skips_*)."""
    from modulo.core.pipeline_engine.node_runner import (
        _idempotency_gate_skipped_envelope,
        _marker_delivery_done_for_node,
    )

    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    # A prior attempt of node-a already delivered: the marker carries
    # delivery_done=True under the canonical attempt_key shape.
    attempt_key = f"run:{run.id}:node:node-a:1"
    run.raw_output_markers = {attempt_key: {"_modulo_marker": True, "delivery_done": True, "attempt_key": attempt_key}}
    snapshot = _make_snapshot(
        {
            "nodes": [{"id": "node-a", "node_type": "sandbox_agent", "role": None}],
            "edges": [],
        }
    )
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements, retry_policy={"on": ["eval_failed"], "max_retries": 1})
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)

    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, _eval_blocked_compiled(), registry, settings)
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    # The eval_failed outcome IS re-dispatched despite the delivery marker
    # (guard B only suppresses TRANSIENT cancellations, never the terminal
    # retry-policy path).
    assert exc_info.value.status == "eval_failed"
    assert exc_info.value.max_retries == 1
    resets = [s for s in statements if "status='pending'" in s]
    assert len(resets) == 1
    assert "claim_token=:tok" in resets[0]
    mock_finalize.assert_not_awaited()
    # On re-execution, guard A reads THIS marker and returns the skipped
    # envelope â€” no sandbox provisioning, no duplicate side effect.
    assert _marker_delivery_done_for_node(run.raw_output_markers, str(run.id), "node-a") is True
    envelope = _idempotency_gate_skipped_envelope("node-a")
    assert envelope["artifacts"][0]["status"] == "skipped"
    assert envelope["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "email_sent"
    assert envelope["artifacts"][0]["output"]["output_json"]["delivery_done"] is True


# ---------------------------------------------------------------------------
# FAR-136 Gap 1 â€” jittered, capped exponential backoff between re-dispatches
# ---------------------------------------------------------------------------


def test_retry_backoff_schedule_grows_with_attempt(monkeypatch):
    """The deterministic schedule (jitter pinned to 0) grows with attempt_n."""
    # Pin jitter to 0 so the pure exponential schedule is exact.
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    base = executor_module._RETRY_BACKOFF_BASE_SECONDS
    cap = executor_module._RETRY_BACKOFF_CAP_SECONDS
    # base=45: 45, 90, 180, 300(capped), 300, ...
    expected = [45.0, 90.0, 180.0, 300.0, 300.0, 300.0, 300.0]
    for attempt_n, exp in enumerate(expected, start=1):
        assert _retry_backoff_seconds(attempt_n, base=base, cap=cap) == exp


def test_retry_backoff_never_exceeds_cap(monkeypatch):
    """Even with max jitter the delay never exceeds the cap."""
    # Pin jitter to its max (b == the upper bound of uniform's range).
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: b)
    cap = executor_module._RETRY_BACKOFF_CAP_SECONDS
    for attempt_n in range(1, 30):
        assert _retry_backoff_seconds(attempt_n, cap=cap) <= cap


def test_retry_backoff_jitter_spreads_schedule(monkeypatch):
    """Jitter spreads the schedule between the exponential value and the
    jittered ceiling, while never exceeding the cap."""
    cap = executor_module._RETRY_BACKOFF_CAP_SECONDS
    base = executor_module._RETRY_BACKOFF_BASE_SECONDS
    # Deterministic jitter sweep: uniform(a, b) returns a fixed fraction of b.
    for fraction in (0.0, 0.5, 1.0):
        monkeypatch.setattr(executor_module.random, "uniform", lambda a, b, f=fraction: b * f)
        delay = _retry_backoff_seconds(1, base=base, cap=cap)
        # attempt 1 exponential = base (45), jitter up to fraction*base*0.25.
        assert base <= delay <= cap
    # With jitter enabled the delay for attempt 1 differs from the pure
    # exponential (the spread is real, not a no-op).
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: b)
    with_jitter = _retry_backoff_seconds(1, base=base, cap=cap)
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    without_jitter = _retry_backoff_seconds(1, base=base, cap=cap)
    assert with_jitter > without_jitter
    assert with_jitter <= cap


def test_retry_backoff_bounded_by_max_retries():
    """The schedule is bounded by the retry budget: the delay for the LAST
    allowed attempt (attempt == max_retries) never schedules beyond the cap,
    and attempts beyond the budget stay at the cap (never grow unbounded)."""
    max_retries = executor_module._RETRY_POLICY_MAX_RETRIES  # 5
    cap = executor_module._RETRY_BACKOFF_CAP_SECONDS
    for attempt_n in range(1, max_retries + 1):
        delay = _retry_backoff_seconds(attempt_n, cap=cap)
        assert delay <= cap
    # Attempts beyond the budget remain bounded by the cap.
    for attempt_n in range(max_retries + 1, max_retries + 10):
        assert _retry_backoff_seconds(attempt_n, cap=cap) <= cap


async def test_execute_retry_policy_applies_backoff_delay():
    """FAR-136 Gap 1 wiring: the computed backoff delay is actually awaited
    (via asyncio.sleep) before the RunRetryPolicyError re-raise â€” the retry
    is not re-dispatched back-to-back."""
    run = _make_run(node_attempt_count=2, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 3}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    assert exc_info.value.max_retries == 3
    # The executor slept for a positive backoff delay before re-raising.
    sleep_mock.assert_awaited_once()
    delay = sleep_mock.await_args.args[0]
    assert delay > 0
    assert delay <= executor_module._RETRY_BACKOFF_CAP_SECONDS
    # The fenced pending-reset still fired before the re-raise.
    reset_stmt = next(s for s in statements if "status='pending'" in s)
    assert "claim_token=:tok" in reset_stmt
    # No terminal failure â€” the retry path never finalizes.
    mock_finalize.assert_not_awaited()


async def test_execute_retry_policy_hang_death_terminal_no_redispatch():
    """FAR-136 Gap 2 end-to-end wiring: a sandbox-agent HANG death that
    exhausts the SAQ retry budget reaches the retry-policy decision as
    error_code "node_cancelled" with "likely hung" in error_detail â€” and is NOT
    re-dispatched. The run terminal-fails via finalize_cost (no
    RunRetryPolicyError, no fenced pending-reset, no backoff sleep).

    This proves the hang exclusion is wired at the execute() call site â€” that
    ``error_detail`` is actually passed into ``_retry_after_policy``. A
    regression that dropped ``error_detail`` at the call site would keep the
    pure-function hang tests green while real hang deaths resumed retrying."""
    from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError

    hang_msg = (
        "Sandbox agent command produced no output within 1200s. "
        "No stdout/stderr was captured - the agent likely hung before writing any result."
    )
    run = _make_run(node_attempt_count=5, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 5}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _mock_compiled_raising(SandboxNodeFailedError(hang_msg))

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        # The hang is excluded, so the backoff sleep must NOT fire at all.
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id,
            org_id=uuid.uuid4(),
            input_payload={},
            claim_token="tok-claim-abc",
        )
    # No re-dispatch: no RunRetryPolicyError escaped, and no backoff sleep ran.
    sleep_mock.assert_not_awaited()
    # Terminal failure via the single finalization path.
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "node_cancelled"
    # No fenced pending-reset was issued for a hang death.
    resets = [s for s in statements if "status='pending'" in s]
    assert resets == []
    assert result is not None


async def test_execute_retry_policy_never_redispatch_correction_run():
    """FAR-210: a correction-trigger run is EXCLUDED from retry_policy
    re-dispatch even when the policy would otherwise retry the failure.

    The single-node correction path owns its own bounded retry budget; the
    pipeline retry policy must never re-dispatch a correction run (no chained
    corrections). Prove-the-fix: without the ``not is_correction_run`` guard at
    the dispatch site, execute() re-raises RunRetryPolicyError and resets the
    run to pending; with the guard, the correction run terminal-fails via the
    single finalization path (no pending-reset, no backoff sleep)."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    run.trigger_type = "correction"
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        # The correction run is excluded, so the backoff sleep must NOT fire.
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        result = await executor.execute(
            run_id=run.id,
            org_id=uuid.uuid4(),
            input_payload={},
            claim_token="tok-claim-abc",
        )
    # No re-dispatch: no RunRetryPolicyError escaped, no backoff sleep ran.
    sleep_mock.assert_not_awaited()
    # No fenced pending-reset was issued for the correction run.
    resets = [s for s in statements if "status='pending'" in s]
    assert resets == []
    # Terminal failure via the single finalization path.
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["status"] == "failed"
    assert mock_finalize.await_args.kwargs["error_code"] == "_GenericFailureError"
    assert result is not None


# ---------------------------------------------------------------------------
# FAR-525 â€” configurable run-level retry backoff (backoff_schedule)
# ---------------------------------------------------------------------------


def test_retry_backoff_multiplier_default_matches_legacy_doubling(monkeypatch):
    """multiplier defaults to 2.0: present-with-defaults == absent == today."""
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    for attempt in range(1, 7):
        assert _retry_backoff_seconds(attempt) == _retry_backoff_seconds(attempt, multiplier=2.0)
        assert _retry_backoff_seconds(attempt) == min(45.0 * 2.0 ** (attempt - 1), 300.0)


def test_retry_backoff_multiplier_fixed_and_growth_tables(monkeypatch):
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    # multiplier 1.0 = FIXED delay (no growth).
    assert [_retry_backoff_seconds(n, base=30, multiplier=1.0) for n in range(1, 6)] == [30.0] * 5
    # multiplier 3.0 grows x3 per attempt until the cap.
    assert [_retry_backoff_seconds(n, base=10, multiplier=3.0) for n in range(1, 5)] == [10.0, 30.0, 90.0, 270.0]


def test_retry_backoff_cap_clamps_computed_value(monkeypatch):
    """The 300s cap clamps the COMPUTED value: delay=300 x M=10 pins at 300
    from attempt 1 â€” the cap is code-held, not user-configurable."""
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    for n in range(1, 8):
        assert _retry_backoff_seconds(n, base=300, multiplier=10.0) == 300.0


def test_retry_backoff_jitter_bounds_via_pinned_seam(monkeypatch):
    """Pinned random.uniform seam: max jitter = +25% of the pre-jitter value,
    total never exceeds the cap."""
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: b)
    assert _retry_backoff_seconds(1, base=45) == 45.0 * 1.25  # 56.25
    assert _retry_backoff_seconds(2, base=45) == 90.0 * 1.25  # 112.5


def test_retry_backoff_jitter_dead_zone_at_cap(monkeypatch):
    """Computed >= cap clamps to EXACTLY cap with ZERO jitter spread â€”
    reachable at attempt 1 with delay_seconds=300 (dead zone called out in
    ADR 028's fleet math)."""
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: b)
    assert _retry_backoff_seconds(1, base=300) == 300.0
    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    assert _retry_backoff_seconds(1, base=300) == 300.0
    assert _retry_backoff_seconds(1, base=300) == _retry_backoff_seconds(1, base=300, jitter_fraction=0.0)


def test_retry_backoff_cap_constants_pinned():
    """Characterization: the THREE pre-existing cap constants are unchanged by
    FAR-525 (executor node-retry cap, authoring-layer cap, runtime-retry cap)."""
    from modulo.core.pipeline_engine import retry_compensation as rc_mod
    from modulo.core.pipeline_engine import runtime_retry as rr_mod

    assert executor_module._RETRY_BACKOFF_CAP_SECONDS == 300.0
    assert rc_mod.RETRY_BACKOFF_CAP_SECONDS == 300.0
    assert rr_mod._BACKOFF_CAP_SECONDS == 300.0
    # The new schedule cap mirrors the same 300s code-held bound.
    assert rc_mod.RETRY_SCHEDULE_MAX_DELAY_SECONDS == 300
    assert rc_mod.RETRY_SCHEDULE_MIN_DELAY_SECONDS == 1
    assert rc_mod.RETRY_SCHEDULE_MIN_MULTIPLIER == 1.0
    assert rc_mod.RETRY_SCHEDULE_MAX_MULTIPLIER == 10.0


def test_retry_backoff_defaults_single_sourced_from_retry_compensation():
    """FAR-525 qa gate: the executor's default base/multiplier are
    SINGLE-SOURCED to retry_compensation.RETRY_SCHEDULE_DEFAULT_* — the same
    constants the runtime resolver uses on the absent path. Raising one layer
    without the other must be impossible without this pin failing."""
    import inspect

    from modulo.core.pipeline_engine import retry_compensation as rc

    assert executor_module._RETRY_BACKOFF_BASE_SECONDS == rc.RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS
    assert rc.RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS == 45.0
    assert executor_module._RETRY_BACKOFF_DEFAULT_MULTIPLIER == rc.RETRY_SCHEDULE_DEFAULT_MULTIPLIER
    assert rc.RETRY_SCHEDULE_DEFAULT_MULTIPLIER == 2.0
    # The live parameter defaults reference the rc constants directly.
    sig = inspect.signature(executor_module._retry_backoff_seconds)
    assert sig.parameters["base"].default == rc.RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS
    assert sig.parameters["multiplier"].default == rc.RETRY_SCHEDULE_DEFAULT_MULTIPLIER


async def test_execute_retry_policy_schedule_fixed_delay_wiring():
    """Wiring: the awaited sleep arg == the resolved schedule delay == the log
    field (ONE jitter draw â€” no re-resolution between log and sleep)."""
    import logging as _logging

    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 30, "multiplier": 1.0}}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_retry_delay=77)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        with _capture_executor_logs(_logging.WARNING) as records, pytest.raises(RunRetryPolicyError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    sleep_mock.assert_awaited_once()
    sleep_arg = sleep_mock.await_args.args[0]
    # deterministic component 30 (+ up to 25% jitter; the same single draw as logged)
    assert 30.0 <= sleep_arg <= 37.5
    log_record = next(r for r in records if r.getMessage() == "pipeline.retry_policy")
    assert log_record.schedule_state == "valid"
    assert log_record.sleep_seconds == pytest.approx(sleep_arg, abs=1e-3)  # log field is round(..., 3)
    assert log_record.saq_retry_delay == 77
    assert log_record.sleep_seconds >= 30.0
    assert mock_finalize.assert_not_awaited() is None


async def test_execute_retry_policy_schedule_fail_open_end_to_end():
    """An invalid schedule re-dispatches with the HARDCODED default schedule
    (45 x 2.0 pinned via the jitter seam) + a structured fail-open warning
    with a sanitized offending-value snippet."""
    import logging as _logging

    run = _make_run(node_attempt_count=2, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 3, "backoff_schedule": {"delay_seconds": 0, "api_key": "sk-xyz"}}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5, saq_retry_delay=60)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        stack.enter_context(patch.object(executor_module.random, "uniform", lambda a, b: 0.0))
        executor = PipelineExecutor(MagicMock())
        with _capture_executor_logs(_logging.WARNING) as records, pytest.raises(RunRetryPolicyError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    # Fail-open schedule at attempt 2: 45 * 2^(2-1) = 90 (exactly, jitter pinned to 0).
    sleep_mock.assert_awaited_once_with(90.0)
    fail_record = next(r for r in records if r.getMessage() == "pipeline.retry_policy_schedule_fail_open")
    assert "delay_seconds" in fail_record.offending
    assert "sk-xyz" not in fail_record.offending  # token-like keys redacted
    policy_record = next(r for r in records if r.getMessage() == "pipeline.retry_policy")
    assert policy_record.schedule_state == "failopen"
    assert policy_record.sleep_seconds == 90.0
    assert mock_finalize.assert_not_awaited() is None


async def test_execute_retry_policy_resolver_defect_cannot_strand_a_reset():
    """The pure schedule resolution happens BEFORE the fenced pending-reset:
    a resolver defect raises before the run is demoted to pending."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        stack.enter_context(
            patch(
                "modulo.core.pipeline_engine.retry_compensation.resolve_backoff_schedule",
                side_effect=RuntimeError("resolver defect"),
            )
        )
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RuntimeError, match="resolver defect"):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    # NO fenced pending-reset was issued â€” the reset cannot be stranded.
    assert not any("status='pending'" in s for s in statements)
    sleep_mock.assert_not_awaited()


async def test_execute_retry_policy_eval_failed_with_schedule_same_path():
    """eval_failed re-dispatch honours backoff_schedule via the SAME
    _maybe_retry_after_policy call site (FAR-503 event + FAR-525 pacing)."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {
        "on": ["eval_failed"],
        "max_retries": 1,
        "backoff_schedule": {"delay_seconds": 20, "multiplier": 1.0},
    }
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _eval_blocked_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    assert exc_info.value.status == "eval_failed"
    # 20 (+ up to 25% jitter) â€” the schedule, not the 45s default.
    sleep_arg = sleep_mock.await_args.args[0]
    assert 20.0 <= sleep_arg <= 25.0
    assert mock_finalize.assert_not_awaited() is None


async def test_execute_retry_policy_counter_emitted_on_confirmed_reset():
    """The OTel counter fires AFTER a confirmed fenced reset with the full
    attribute dict (fake-meter seam)."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 45, "multiplier": 1.0}}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    fake_counter = MagicMock()
    fake_meter = MagicMock()
    fake_meter.create_counter.return_value = fake_counter
    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        stack.enter_context(patch.object(executor_module, "_get_otel_meter", return_value=fake_meter))
        stack.enter_context(patch.object(executor_module.random, "uniform", lambda a, b: 0.0))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    fake_meter.create_counter.assert_called_once()
    assert fake_meter.create_counter.call_args.kwargs["name"] == "runs_retry_redispatch_total"
    fake_counter.add.assert_called_once_with(
        1,
        {"reason": "failed", "schedule_state": "valid", "delay_bucket": "10-60s"},
    )


async def test_execute_retry_policy_no_counter_on_fence_lost():
    """Fence-lost (rowcount 0): still sleeps + re-raises, but NO counter emit."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    fake_counter = MagicMock()
    fake_meter = MagicMock()
    fake_meter.create_counter.return_value = fake_counter
    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        stack.enter_context(patch.object(executor_module, "_get_otel_meter", return_value=fake_meter))
        stack.enter_context(patch.object(PipelineExecutor, "_fenced_pending_reset", new=AsyncMock(return_value=0)))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    fake_counter.add.assert_not_called()
    sleep_mock.assert_awaited_once()


async def test_execute_retry_policy_counter_no_op_without_meter_provider():
    """No meter provider -> the counter is a silent no-op; the re-raise still
    happens (the retry itself is never lost to telemetry)."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        stack.enter_context(patch.object(executor_module, "_get_otel_meter", return_value=None))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    sleep_mock.assert_awaited_once()


async def test_execute_retry_policy_counter_never_blocks_the_reraise():
    """A metrics failure inside the emit is swallowed (warning-logged) â€” the
    RunRetryPolicyError still re-raises."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    retry_policy = {"on": ["failure"], "max_retries": 2}
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    boom_meter = MagicMock()
    boom_meter.create_counter.side_effect = RuntimeError("otel down")
    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        stack.enter_context(patch.object(executor_module, "_get_otel_meter", return_value=boom_meter))
        executor = PipelineExecutor(MagicMock())
        with pytest.raises(RunRetryPolicyError):
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")
    sleep_mock.assert_awaited_once()


def test_schedule_reresolution_reads_current_policy_per_attempt():
    """Per-attempt re-resolution characterization: resolution reads the policy
    dict fresh each attempt (each retry is a new SAQ job re-reading the row),
    so a mid-run PATCH takes effect on the NEXT attempt."""
    from modulo.core.pipeline_engine import retry_compensation as rc_mod

    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 10, "multiplier": 1.0}}
    _present, delay, _mult, _reason = rc_mod.resolve_backoff_schedule(policy)
    assert delay == 10.0
    # "Mid-run PATCH" = mutate the captured dict; the NEXT attempt's resolve
    # (same call, same dict object) sees the new schedule.
    policy["backoff_schedule"]["delay_seconds"] = 30
    _present, delay, _mult, _reason = rc_mod.resolve_backoff_schedule(policy)
    assert delay == 30.0


def _real_check_graph_validator() -> MagicMock:
    """A GraphValidator class mock whose run-start checks are the REAL
    staticmethods (so the run-start validation layering is exercised)."""
    from modulo.core.graph_validator import GraphValidator as RealGV

    validation = MagicMock()
    validation.is_valid = True
    cls = MagicMock()
    cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    cls.check_retry_policy = RealGV.check_retry_policy
    cls.check_retry_policy_schedule = RealGV.check_retry_policy_schedule
    return cls


async def _execute_with_real_runstart_checks(retry_policy: dict[str, Any]):
    """execute() with the REAL check_retry_policy / check_retry_policy_schedule
    staticmethods at run start (everything else harness-mocked)."""
    run = _make_run(node_attempt_count=1, claim_token="tok-claim-abc")
    snapshot = _make_snapshot()
    statements: list[str] = []
    session = _make_session(snapshot, statements=statements, retry_policy=retry_policy)
    factory = _make_session_factory(session)
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)
    compiled = _make_failure_compiled()

    sleep_mock = AsyncMock()
    with ExitStack() as stack:
        mock_finalize = _enter_execute_patches(stack, factory, run, compiled, registry, settings)
        stack.enter_context(
            patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_real_check_graph_validator())
        )
        stack.enter_context(patch("modulo.core.pipeline_engine.executor.asyncio.sleep", new=sleep_mock))
        executor = PipelineExecutor(MagicMock())
        try:
            result = await executor.execute(
                run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc"
            )
        except Exception as exc:
            return "raised", exc, statements, sleep_mock, mock_finalize
        return "returned", result, statements, sleep_mock, mock_finalize


async def test_run_start_malformed_core_policy_still_raises_graph_validation_error():
    """A malformed CORE policy (bad max_retries) STILL raises at run start â€”
    the FAR-525 schedule layering must not soften the core check."""
    _kind, exc, _statements, _sleep, _finalize = await _execute_with_real_runstart_checks(
        {"on": ["failure"], "max_retries": 99, "backoff_schedule": {"delay_seconds": 45}}
    )
    from modulo.core.pipeline_engine import GraphValidationError

    assert isinstance(exc, GraphValidationError)
    assert any("max_retries" in i.message for i in exc.issues)


async def test_run_start_malformed_schedule_warns_and_run_proceeds():
    """A malformed schedule does NOT block the run: it warns (explicit issue
    log) and the retry path proceeds with the hardcoded default schedule."""
    import logging as _logging

    with _capture_executor_logs(_logging.WARNING) as records:
        kind, exc, _statements, sleep_mock, mock_finalize = await _execute_with_real_runstart_checks(
            {"on": ["failure"], "max_retries": 1, "backoff_schedule": {"delay_seconds": 0}}
        )
    from modulo.core.pipeline_engine import GraphValidationError

    assert not isinstance(exc, GraphValidationError)
    assert kind == "raised" and isinstance(exc, RunRetryPolicyError)
    mock_finalize.assert_not_awaited()
    malformed = [r for r in records if r.getMessage() == "pipeline.retry_policy_schedule_malformed"]
    assert malformed, records
    assert "delay_seconds" in malformed[0].detail
    # Fail-open default: 45 x 2^(1-1) = 45 with pinned jitter -> in [45, 56.25].
    assert 45.0 <= sleep_mock.await_args.args[0] <= 56.25


async def test_run_start_valid_schedule_no_schedule_warning():
    import logging as _logging

    with _capture_executor_logs(_logging.WARNING) as records:
        _kind, exc, _statements, _sleep, _finalize = await _execute_with_real_runstart_checks(
            {"on": ["failure"], "max_retries": 1, "backoff_schedule": {"delay_seconds": 45}}
        )
    assert isinstance(exc, RunRetryPolicyError)
    assert not any(r.getMessage() == "pipeline.retry_policy_schedule_malformed" for r in records)


@contextmanager
def _capture_executor_logs(level: int):
    """Capture executor log RECORDS (incl. `extra` attrs) without caplog."""
    import logging as _logging

    logger = _logging.getLogger("modulo.core.pipeline_engine.executor")
    records: list = []

    class _Handler(_logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level=level)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
