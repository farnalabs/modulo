"""FAR-2076 — ONE retry budget: the durable execution counter is the authority.

A run must be executed at most ``1 + retry_policy.max_retries`` times. Before
this ticket every path that could grant an execution counted a different thing
(``node_attempt_count``, ``max(node_attempt_count, claim_count - 1)``, the SAQ
transport setting, or nothing at all), so a pipeline with
``{"on": ["failure"], "max_retries": 2}`` reached a fourth execution.

The first attempt at the fix derived the measure from ``claim_count``; the
pre-PR gate rejected it because ``claim_count`` also grows on capacity-demoted
claims (the attempt never began) and on HITL resume claims (a checkpoint
continuation, not a graph execution), so healthy runs were terminal-failed
with budget left. The authority is ``runs.execution_attempt_count``, incremented
exactly once per admitted ``execute_run`` attempt.

These tests pin, in order:

1. the policy shapes and the ``1 + max_retries`` ceiling;
2. the dispatch-side clamp END TO END (what actually reaches ``_enqueue_saq``);
3. the composed transient decision (a real dict policy, not a mock);
4. the reconcile backstop's effects (summary / terminalized ids / writer args);
5. the capacity-marker exclusion (both markers);
6. the increment's placement (admit = spend, demote = do not, resume = do not,
   pre-node death = spend) and the exactly-ceiling boundary;
7. the read-boundary registration of the terminal error code.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.errors import NodeCancelledError

from modulo.core import cron_helpers as ch
from modulo.core import dispatch
from modulo.core import pipeline_execution as pe
from modulo.core.pipeline_engine import PipelineExecutor
from modulo.core.pipeline_engine import executor as ex
from modulo.core.pipeline_engine import watchdog_retry as wr
from modulo.core.pipeline_engine.error_codes import (
    _CODE_HARNESS_RETRY_BUDGET_EXHAUSTED,
    ERROR_CODE_REGISTRY,
    LEGACY_ALIASES,
    class_for,
    known_error_codes,
    map_legacy_code,
)
from modulo.core.retry_budget import (
    RETRY_POLICY_MAX_RETRIES,
    clamp_saq_retries,
    retry_budget_exhausted,
    retry_executions_consumed,
    retry_max_executions,
    retry_policy_max_retries,
)
from modulo.core.worker_handoff import WORKER_LOST_ERROR_CODE, _node_started_policy_allows

ORG = uuid.uuid4()

#: ``max_retries`` 2 => at most 3 executions.
_POLICY = {"on": ["failure"], "max_retries": 2}
_CEILING = 3


# ---------------------------------------------------------------------------
# 1. Policy shapes and the ceiling
# ---------------------------------------------------------------------------


def test_no_policy_imposes_no_ceiling() -> None:
    """Today's behaviour: absent / empty policy grants nothing to gate."""
    assert retry_policy_max_retries(None) is None
    assert retry_policy_max_retries({}) is None
    assert retry_max_executions(None) is None
    assert retry_max_executions({}) is None
    assert retry_budget_exhausted(None, execution_attempt_count=99) is False
    assert retry_budget_exhausted({}, execution_attempt_count=99) is False


def test_ceiling_is_one_plus_max_retries() -> None:
    assert retry_max_executions(_POLICY) == _CEILING
    assert retry_max_executions({"on": ["stall"], "max_retries": 5}) == 6


def test_a_non_empty_policy_without_max_retries_imposes_one_execution() -> None:
    """The fail-open this closes: ``{"on": ["failure"]}`` used to return NO
    ceiling, so the run-level path treated it as no retry while every other
    path granted without limit."""
    assert retry_policy_max_retries({"on": ["failure"]}) == 0
    assert retry_max_executions({"on": ["failure"]}) == 1
    assert retry_budget_exhausted({"on": ["failure"]}, execution_attempt_count=0) is False
    assert retry_budget_exhausted({"on": ["failure"]}, execution_attempt_count=1) is True


def test_an_explicit_zero_budget_imposes_one_execution() -> None:
    assert retry_max_executions({"on": ["failure"], "max_retries": 0}) == 1
    assert retry_budget_exhausted({"on": [], "max_retries": 0}, execution_attempt_count=1) is True


@pytest.mark.parametrize("budget", ["2", True, 6, -1, None])
def test_a_malformed_budget_fails_closed_to_one_execution(budget: Any) -> None:
    """A value the validator would reject must never WIDEN a budget."""
    policy = {"on": ["failure"], "max_retries": budget}
    assert retry_policy_max_retries(policy) == 0
    assert retry_max_executions(policy) == 1


def test_consumed_coerces_garbage_to_zero() -> None:
    assert retry_executions_consumed(execution_attempt_count=3) == 3
    assert retry_executions_consumed(execution_attempt_count=-2) == 0
    assert retry_executions_consumed(execution_attempt_count=None) == 0
    assert retry_executions_consumed(execution_attempt_count="3") == 0
    assert retry_executions_consumed(execution_attempt_count=True) == 0


def test_the_validator_bound_is_the_shared_constant() -> None:
    """A second hard-coded bound in the validator would make a raised bound
    read every stored policy as malformed and silently disable every ceiling."""
    from modulo.core import graph_validator

    assert graph_validator.RETRY_POLICY_MAX_RETRIES == RETRY_POLICY_MAX_RETRIES


# ---------------------------------------------------------------------------
# 2. dispatch_run — the transport allowance END TO END
# ---------------------------------------------------------------------------


class _FakeBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeDispatchSession:
    """Minimal AsyncSession double for the dispatch admission transaction.

    ``get_bind()`` reports sqlite so the RLS / row-lock helpers take their
    no-op branches; the only statement the flow executes is the pipeline-policy
    SELECT of ``_saq_retries_for_run``.
    """

    def __init__(self, pipeline: Any) -> None:
        self.info: dict[str, Any] = {}
        self._pipeline = pipeline
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        return self._bind

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self._pipeline, rowcount=1, fetchone=lambda: ("id",))

    async def close(self) -> None:
        return None


def _patch_dispatch_run(*, run: Any, pipeline: Any) -> tuple[Any, Any]:
    """Patch the seams of ``dispatch_run``; return (enqueue mock, session cache)."""
    sessions: list[_FakeDispatchSession] = []

    def _open() -> _FakeDispatchSession:
        session = _FakeDispatchSession(pipeline)
        sessions.append(session)
        return session

    enqueue = AsyncMock(return_value=("job-1", False))
    patches = [
        patch.object(dispatch, "_open_session", _open),
        patch.object(dispatch, "get_settings", return_value=SimpleNamespace(saq_runs_queue="runs", saq_run_retries=5)),
        patch.object(dispatch, "_capacity_deferred", AsyncMock(return_value=False)),
        patch.object(dispatch, "_org_capacity_deferred", AsyncMock(return_value=False)),
        patch.object(dispatch, "_slot_saturated", AsyncMock(return_value=False)),
        patch.object(dispatch, "_enqueue_saq", enqueue),
        patch("modulo.db.crud.run.get_run", AsyncMock(return_value=run)),
    ]
    return patches, enqueue, sessions


async def _dispatch_retries(*, run: Any, pipeline: Any, job_type: str = "execute_run") -> int:
    """Run the REAL ``dispatch_run`` and return the ``retries`` it enqueued."""
    patches, enqueue, _sessions = _patch_dispatch_run(run=run, pipeline=pipeline)
    for seam in patches:
        seam.start()
    try:
        outcome, job_id = await dispatch.dispatch_run(uuid.uuid4(), uuid.uuid4(), job_type=job_type)
    finally:
        for seam in reversed(patches):
            seam.stop()
    assert outcome == "enqueued"
    assert job_id == "job-1"
    return int(enqueue.await_args.kwargs["retries"])


def _run(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "pipeline_id": uuid.uuid4(),
        "status": "pending",
        "execution_attempt_count": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_dispatch_at_the_ceiling_grants_zero_transport_retries() -> None:
    retries = await _dispatch_retries(
        run=_run(execution_attempt_count=3),
        pipeline=SimpleNamespace(retry_policy=_POLICY),
    )
    assert retries == 0


async def test_dispatch_after_one_execution_grants_one_transport_retry() -> None:
    retries = await _dispatch_retries(
        run=_run(execution_attempt_count=1),
        pipeline=SimpleNamespace(retry_policy=_POLICY),
    )
    assert retries == 1


async def test_dispatch_does_not_shrink_retries_without_a_policy() -> None:
    retries = await _dispatch_retries(
        run=_run(execution_attempt_count=9),
        pipeline=SimpleNamespace(retry_policy=None),
    )
    assert retries == 5


async def test_resume_dispatch_is_never_clamped() -> None:
    """A resume continues from a checkpoint — it is not a graph execution, so
    the policy must not bound its transport retries."""
    retries = await _dispatch_retries(
        run=_run(execution_attempt_count=3),
        pipeline=SimpleNamespace(retry_policy=_POLICY),
        job_type="resume_run",
    )
    assert retries == 5


def test_clamp_is_the_shared_helper() -> None:
    assert clamp_saq_retries(_POLICY, execution_attempt_count=0, configured=5) == 2
    assert clamp_saq_retries(_POLICY, execution_attempt_count=1, configured=5) == 1
    assert clamp_saq_retries(_POLICY, execution_attempt_count=2, configured=5) == 0
    assert clamp_saq_retries(_POLICY, execution_attempt_count=3, configured=5) == 0
    assert clamp_saq_retries(None, execution_attempt_count=3, configured=5) == 5


# ---------------------------------------------------------------------------
# 3. The composed transient decision (real dict policies)
# ---------------------------------------------------------------------------


def _transient_executor(*, execution_attempt_count: int, claim_token: str = "tok") -> Any:
    """A ``PipelineExecutor`` with only the transient-decision seams stubbed."""
    executor = object.__new__(PipelineExecutor)
    executor._claim_token = claim_token
    executor._load_transient_state = AsyncMock(
        return_value=(execution_attempt_count, claim_token, None, False, "pipeline:1")
    )
    executor._idempotency_gate_ok = MagicMock(return_value=False)
    executor._script_lease_ok = AsyncMock(return_value=True)
    executor._fenced_pending_reset = AsyncMock(return_value=1)
    executor._cleanup_run_resources = AsyncMock()
    return executor


async def _transient_decision(executor: Any, policy: Any) -> dict[str, Any]:
    broker = MagicMock()
    with patch("modulo.settings.get_settings", return_value=SimpleNamespace(saq_run_retries=5)):
        return await executor._decide_transient_failure(
            exc=NodeCancelledError("transient sandbox failure"),
            run_id=uuid.uuid4(),
            org_id=ORG,
            graph_json={"nodes": [{"id": "n1"}], "edges": []},
            graph_idempotent=True,
            single_sandbox_node=False,
            model_backend_hub=None,
            connector_hub=None,
            broker=broker,
            stall_requested=None,
            pipeline_retry_policy=policy,
        )


async def test_transient_requeues_below_the_policy_ceiling() -> None:
    executor = _transient_executor(execution_attempt_count=1)
    decision = await _transient_decision(executor, _POLICY)
    assert decision["decision"] == "requeue"
    executor._fenced_pending_reset.assert_awaited_once()


async def test_transient_terminalises_at_the_policy_ceiling() -> None:
    executor = _transient_executor(execution_attempt_count=3)
    decision = await _transient_decision(executor, _POLICY)
    assert decision["decision"] == "terminal"
    executor._fenced_pending_reset.assert_not_awaited()


async def test_transient_still_requeues_without_a_policy() -> None:
    """No policy => the configured transport allowance is unchanged."""
    executor = _transient_executor(execution_attempt_count=4)
    decision = await _transient_decision(executor, None)
    assert decision["decision"] == "requeue"


def test_transient_allowance_is_min_of_transport_and_ceiling() -> None:
    assert ex._transient_retry_allowance(5, _POLICY) == _CEILING
    assert ex._transient_retry_allowance(5, None) == 5
    assert ex._transient_retry_allowance(5, {"on": ["failure"], "max_retries": 1}) == 2


# ---------------------------------------------------------------------------
# 4. The watchdog + handback boundaries
# ---------------------------------------------------------------------------


def _watchdog_executor(execution_attempt_count: int) -> MagicMock:
    executor = MagicMock()
    executor._read_retry_attempt_state = AsyncMock(return_value=(execution_attempt_count, "tok", None))
    executor._node_timeouts = {}
    return executor


async def _watchdog_attempt_count(execution_attempt_count: int, *, retry_budget: int = 2) -> int | None:
    return await wr._attempt_count_if_retryable(
        _watchdog_executor(execution_attempt_count),
        run_id=uuid.uuid4(),
        org_id=ORG,
        trigger_type="manual",
        graph_json={"nodes": [], "edges": []},
        claim_token="tok",
        retry_budget=retry_budget,
        final_status="failed",
        error_code=WORKER_LOST_ERROR_CODE,
    )


async def test_watchdog_allows_execution_two_and_refuses_execution_three() -> None:
    """Exactly ``1 + max_retries`` executions: with max_retries=2 the watchdog
    may re-dispatch after execution 1 and 2, and must refuse after 3 (which is
    already the ceiling). This REPLACES the old
    ``test_watchdog_allows_exactly_ceiling_executions``, which encoded
    ``claim_count - 1`` and therefore permitted a fourth execution."""
    assert await _watchdog_attempt_count(1) == 1
    assert await _watchdog_attempt_count(2) == 2
    assert await _watchdog_attempt_count(3) is None


def _handback_row(execution_attempt_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        retry_policy=_POLICY,
        execution_attempt_count=execution_attempt_count,
        claim_count=99,
        node_attempt_count=99,
    )


def test_handback_grants_the_third_execution_and_refuses_the_fourth() -> None:
    allowed, reason = _node_started_policy_allows(_handback_row(2))
    assert allowed
    assert reason == ""

    refused, reason = _node_started_policy_allows(_handback_row(3))
    assert not refused
    assert "budget exhausted" in reason


# ---------------------------------------------------------------------------
# 5. The reconcile backstop — EFFECTS, not mock calls
# ---------------------------------------------------------------------------


def _pending_row(
    *,
    execution_attempt_count: int,
    claim_count: int = 1,
    node_attempt_count: int = 1,
    retry_policy: Any = _POLICY,
    error_code: str | None = None,
    pipeline_row_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    """A pending, node-started, fenced-reset-shaped row (the backstop's class)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        pipeline_row_id=pipeline_row_id if pipeline_row_id is not None else uuid.uuid4(),
        status="pending",
        dispatcher="saq",
        dispatch_phase="first_node_dispatched",
        error_code=error_code,
        execution_attempt_count=execution_attempt_count,
        node_attempt_count=node_attempt_count,
        claim_count=claim_count,
        retry_policy=retry_policy,
        node_token_usage=None,
        outputs_absent=False,
        checkpoints_absent=False,
        started_at=None,
        dispatched_at=MagicMock(),
        heartbeat_at=None,
        enqueue_failed_at=None,
        sandbox_dispatch_state=None,
        guardrail_override_resume_pending=None,
    )


async def _run_one_row(
    row: SimpleNamespace,
    *,
    writer_result: bool = True,
) -> tuple[AsyncMock, AsyncMock, dict[str, Any], list[tuple[uuid.UUID, uuid.UUID]]]:
    """Drive the REAL ``_reconcile_one_row``; return the two mocks + effects."""
    dispatch_mock = AsyncMock(return_value=0)
    budget_fail_mock = AsyncMock(return_value=writer_result)
    summary = ch._dispatcher_summary()
    terminalized: list[tuple[uuid.UUID, uuid.UUID]] = []
    with (
        patch.object(ch, "_read_reconcile_job", AsyncMock(return_value=(None, True))),
        patch.object(ch, "_resolve_hitl_resume_or_skip", AsyncMock(return_value=(False, None))),
        patch.object(ch, "_re_dispatch_reconciled_run", dispatch_mock),
        patch.object(ch, "_fail_retry_budget_exhausted_run", budget_fail_mock),
    ):
        await ch._reconcile_one_row(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            ORG,
            row,
            30,
            0,
            summary,
            terminalized,
            early_detect_minutes=None,
            floor_nodeless_window=None,
            stale_window=1800,
        )
    return dispatch_mock, budget_fail_mock, summary, terminalized


async def test_reconcile_terminalises_a_pending_row_beyond_budget() -> None:
    row = _pending_row(execution_attempt_count=_CEILING)

    dispatch_mock, budget_fail_mock, summary, terminalized = await _run_one_row(row)

    assert summary["retry_budget_exhausted"] == 1
    assert terminalized == [(row.id, ORG)]
    budget_fail_mock.assert_awaited_once()
    assert budget_fail_mock.await_args is not None
    assert budget_fail_mock.await_args.args[1] == row.id
    assert budget_fail_mock.await_args.args[2] == ORG
    assert budget_fail_mock.await_args.kwargs["ceiling"] == _CEILING
    assert budget_fail_mock.await_args.kwargs["execution_attempts"] == _CEILING
    dispatch_mock.assert_not_awaited()


async def test_reconcile_still_recovers_a_within_budget_pending_row() -> None:
    row = _pending_row(execution_attempt_count=2)

    dispatch_mock, budget_fail_mock, summary, terminalized = await _run_one_row(row)

    assert summary["retry_budget_exhausted"] == 0
    assert terminalized == []
    budget_fail_mock.assert_not_awaited()
    dispatch_mock.assert_awaited_once()
    assert dispatch_mock.await_args is not None
    assert dispatch_mock.await_args.args[4] == "execute_run"


async def test_reconcile_recovery_counts_when_the_guarded_write_matches_nothing() -> None:
    """The writer returns ``False`` when the row advanced between the scan and
    the write: no dispatch, one ``skipped``, no terminalized id."""
    row = _pending_row(execution_attempt_count=_CEILING)

    dispatch_mock, _budget_fail_mock, summary, terminalized = await _run_one_row(row, writer_result=False)

    assert summary["retry_budget_exhausted"] == 0
    assert summary["skipped"] == 1
    assert terminalized == []
    dispatch_mock.assert_not_awaited()


async def test_reconcile_terminalises_a_handed_back_row_beyond_budget() -> None:
    """The FAR-1627 handback stamps ``harness.worker_lost`` — still bounded."""
    row = _pending_row(execution_attempt_count=_CEILING, error_code=WORKER_LOST_ERROR_CODE)

    dispatch_mock, budget_fail_mock, _summary, _terminalized = await _run_one_row(row)

    budget_fail_mock.assert_awaited_once()
    dispatch_mock.assert_not_awaited()


@pytest.mark.parametrize("marker", ["org_capacity_limited", "pipeline_capacity"])
async def test_reconcile_never_budget_gates_a_capacity_marked_row(marker: str) -> None:
    """A capacity demotion consumes a claim but executes nothing — it also
    never increments the execution counter, so the gate must leave it alone."""
    row = _pending_row(execution_attempt_count=0, claim_count=30, error_code=marker)

    dispatch_mock, budget_fail_mock, _summary, terminalized = await _run_one_row(row)

    assert terminalized == []
    budget_fail_mock.assert_not_awaited()
    dispatch_mock.assert_awaited_once()


async def test_reconcile_without_policy_preserves_the_old_recovery() -> None:
    row = _pending_row(execution_attempt_count=30, retry_policy=None)

    dispatch_mock, budget_fail_mock, _summary, _terminalized = await _run_one_row(row)

    budget_fail_mock.assert_not_awaited()
    dispatch_mock.assert_awaited_once()


async def test_reconcile_logs_loudly_when_the_pipeline_row_is_missing(caplog: pytest.LogCaptureFixture) -> None:
    """A policy that genuinely cannot be resolved must be loud, never a silent
    skip — the silent-skip shape is how the original defect hid."""
    row = _pending_row(execution_attempt_count=1, retry_policy=None, pipeline_row_id=None)
    row.pipeline_row_id = None

    import logging

    with caplog.at_level(logging.WARNING, logger=ch.__name__):
        dispatch_mock, _budget_fail_mock, _summary, _terminalized = await _run_one_row(row)

    assert "retry_budget_unresolvable" in caplog.text
    dispatch_mock.assert_awaited_once()


class _CapturingSession:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.statements: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> SimpleNamespace:
        self.statements.append((str(stmt), params or {}))
        return SimpleNamespace(rowcount=self.rowcount)


async def test_budget_exhausted_writer_is_guarded_coded_and_observable() -> None:
    """The writer's parameters and observability legs.

    The row PREDICATE itself (``pending`` + the counter) is proven against real
    Postgres in ``tests/integration/saq/test_dispatcher_reconcile.py`` — a
    substring assertion here would not test it.
    """
    run_id = uuid.uuid4()
    session = _CapturingSession(rowcount=1)
    ingest = AsyncMock()
    triage = AsyncMock(return_value=True)
    with (
        patch.object(ch, "_ingest_saq_error", ingest),
        patch.object(ch, "persist_terminal_triage_marker", triage),
    ):
        landed = await ch._fail_retry_budget_exhausted_run(
            session,
            run_id,
            ORG,
            ceiling=_CEILING,
            execution_attempts=_CEILING,
            claim_count=7,
            node_attempt_count=2,
            pipeline_id=uuid.uuid4(),
        )

    assert landed is True
    sql, params = session.statements[0]
    assert params["code"] == "harness.retry_budget_exhausted"
    assert params["code"] == _CODE_HARNESS_RETRY_BUDGET_EXHAUSTED
    assert params["ceiling"] == _CEILING
    assert "execution_attempt_count" in sql
    assert "claim_count = " not in sql
    assert "execution_attempt_count=" + str(_CEILING) in params["detail"]
    assert "claim_count=7" in params["detail"]
    ingest.assert_awaited_once()
    triage.assert_awaited_once()


async def test_budget_exhausted_writer_does_nothing_when_the_row_advanced() -> None:
    ingest = AsyncMock()
    triage = AsyncMock(return_value=True)
    with (
        patch.object(ch, "_ingest_saq_error", ingest),
        patch.object(ch, "persist_terminal_triage_marker", triage),
    ):
        landed = await ch._fail_retry_budget_exhausted_run(
            _CapturingSession(rowcount=0),
            uuid.uuid4(),
            ORG,
            ceiling=_CEILING,
            execution_attempts=_CEILING,
            claim_count=1,
            node_attempt_count=1,
            pipeline_id=None,
        )
    assert landed is False
    ingest.assert_not_awaited()
    triage.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. Where the increment lives — admit spends, demote/resume do not
# ---------------------------------------------------------------------------


class _FakeExecutorSession:
    """AsyncSession double for the executor's admission / bump writes."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        return self._bind

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(stmt), params))
        return SimpleNamespace(fetchone=lambda: ("id",), rowcount=1, scalar_one_or_none=lambda: None)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def close(self) -> None:
        return None


def _bump_calls(sessions: list[_FakeExecutorSession]) -> list[tuple[str, dict[str, Any] | None]]:
    return [
        (sql, params)
        for session in sessions
        for sql, params in session.executed
        if "execution_attempt_count = execution_attempt_count + 1" in sql
    ]


def test_the_execute_claim_does_not_stamp_the_execution_counter() -> None:
    """If the claim stamped it, a capacity-demoted claim would spend budget."""
    assert "execution_attempt_count" not in str(pe.build_claim_update(_stale_seconds=450))


def test_neither_resume_claim_stamps_the_execution_counter() -> None:
    """A resume is a checkpoint continuation, not a graph execution."""
    assert "execution_attempt_count" not in str(pe.build_resume_claim_update(_stale_seconds=450))
    assert "execution_attempt_count" not in str(pe.build_resume_claim_update(_stale_seconds=450, claim_token="tok"))


async def _execute_with_capacity(
    *, active_runs: int, max_concurrent: int = 1
) -> tuple[list[_FakeExecutorSession], uuid.UUID]:
    """Drive the REAL ``execute()`` to the capacity decision, then make the
    pre-node stage die. Returns every fake session (to inspect the writes)."""
    run_id = uuid.uuid4()
    org_id = ORG
    pipeline_id = uuid.uuid4()
    sessions: list[_FakeExecutorSession] = []

    def _factory() -> _FakeExecutorSession:
        session = _FakeExecutorSession()
        sessions.append(session)
        return session

    run = SimpleNamespace(
        status="running",
        pipeline_id=pipeline_id,
        variant_config_snapshot=None,
        input_payload={},
        execution_attempt_count=1,
        cancellation_requested=False,
    )
    snapshot = SimpleNamespace(policy_gate_pins_json=None)
    executor = object.__new__(PipelineExecutor)
    executor._claim_token = "tok"
    executor._dispatch_phase_tracker = None
    executor._session_factory = _factory
    executor._check_policy_gate_pin = AsyncMock(return_value=None)

    with (
        patch.object(
            executor,
            "_load_execution_context",
            AsyncMock(return_value=(run, SimpleNamespace(), snapshot, {"nodes": [], "edges": []}, {})),
        ),
        patch.object(
            executor,
            "_capture_execution_scalars",
            MagicMock(
                return_value={
                    "pipeline_id": pipeline_id,
                    "max_concurrent": max_concurrent,
                    "pipeline_retry_policy": _POLICY,
                    "guard": None,
                    "snapshot_id": uuid.uuid4(),
                    "thread_id": "thread",
                    "is_correction_run": False,
                    "run_number": 1,
                }
            ),
        ),
        patch.object(executor, "_load_eval_defs_for_pipeline", AsyncMock(return_value=[])),
        patch.object(executor, "_build_eval_defs_by_node", MagicMock(return_value={})),
        patch.object(executor, "_read_org_sandbox_cap_full", AsyncMock(return_value=(None, False, "all"))),
        patch.object(executor, "_read_org_run_concurrency_limit", AsyncMock(return_value=None)),
        # The executor binds these names at module import, so patch THEM there
        # (patching ``modulo.db.crud.run`` would leave the bound names intact).
        patch.object(ex, "count_active_runs_for_pipeline", AsyncMock(return_value=active_runs)),
        patch.object(ex, "get_run", AsyncMock(return_value=run)),
        patch.object(executor, "_claim_run_and_audit", AsyncMock(return_value=run)),
        patch.object(executor, "_check_spend_ceiling_gate", AsyncMock(return_value=None)),
        patch.object(executor, "_init_run_environment", AsyncMock(side_effect=RuntimeError("pre-node death"))),
        pytest.raises(RuntimeError),
    ):
        await executor.execute(run_id=run_id, org_id=org_id, input_payload={}, claim_token="tok")

    return sessions, run_id


async def test_an_admitted_attempt_spends_one_execution_before_a_pre_node_death() -> None:
    """The pre-node death (hub init / compile) happens AFTER admission, and the
    increment is committed in the admission transaction — so it counts."""
    sessions, run_id = await _execute_with_capacity(active_runs=0)
    bumps = _bump_calls(sessions)
    assert len(bumps) == 1
    _sql, params = bumps[0]
    assert params is not None
    assert params["rid"] == str(run_id)
    assert params["oid"] == str(ORG)
    assert params["tok"] == "tok"


async def test_a_capacity_demoted_attempt_spends_nothing() -> None:
    """The run never began — the same request with a full slot must not count."""
    sessions, _run_id = await _execute_with_capacity(active_runs=1, max_concurrent=1)
    assert not _bump_calls(sessions)


# ---------------------------------------------------------------------------
# 7. The terminal code resolves at the read boundary
# ---------------------------------------------------------------------------


def test_retry_budget_exhausted_is_a_known_error_code() -> None:
    assert "harness.retry_budget_exhausted" in known_error_codes()
    assert _CODE_HARNESS_RETRY_BUDGET_EXHAUSTED in known_error_codes()


def test_retry_budget_exhausted_registered_with_expected_attributes() -> None:
    spec = ERROR_CODE_REGISTRY["harness.retry_budget_exhausted"]
    assert spec.error_class == "harness"
    assert spec.retryable is False
    assert spec.alert_severity == "warning"
    assert spec.guidance
    assert class_for("retry_budget_exhausted") == "harness"


def test_retry_budget_exhausted_bare_spelling_canonicalizes() -> None:
    assert LEGACY_ALIASES["retry_budget_exhausted"] == "harness.retry_budget_exhausted"
    assert map_legacy_code("retry_budget_exhausted") == "harness.retry_budget_exhausted"
    assert map_legacy_code("harness.retry_budget_exhausted") == "harness.retry_budget_exhausted"


# ---------------------------------------------------------------------------
# 8. The readiness detail renders the new terminal counter
# ---------------------------------------------------------------------------


def test_readiness_detail_renders_the_retry_budget_counter() -> None:
    from modulo.api.routes.health import _format_reconcile_detail

    detail = _format_reconcile_detail({"retry_budget_exhausted": 4})
    assert "retry_budget_exhausted=4" in detail
