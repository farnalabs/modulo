"""FAR-2076 — ONE retry budget across every path that grants an execution.

A run could execute more times than its ``retry_policy`` allowed because three
mechanisms each counted a different budget:

* the run-level policy path (``worker_handoff`` / executor ``_retry_after_policy``)
  — correct, bounded by ``retry_policy.max_retries``;
* the in-execute transient retry — bounded by ``settings.saq_run_retries`` (5),
  which does not consult the policy;
* the ``dispatcher_reconcile`` pending re-enqueue backstop — counted nothing.

Observed: a pipeline with ``{"on": ["failure"], "max_retries": 2}`` (at most 3
executions) reached attempt 4.

This file pins the shared measure, the execution ceiling, and the boundary at
``1 + max_retries`` on every granting path — plus the reconcile behaviour: an
over-budget pending row is terminal-failed with a distinct code (never
re-dispatched, never silently skipped), while a within-budget row keeps the
durability recovery the backstop exists for.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import cron_helpers as ch
from modulo.core.dispatch import (
    clamp_saq_retries,
    retry_budget_exhausted,
    retry_executions_consumed,
    retry_max_executions,
)
from modulo.core.pipeline_engine import executor as ex
from modulo.core.pipeline_engine import watchdog_retry as wr
from modulo.core.worker_handoff import (
    WORKER_LOST_ERROR_CODE,
    _node_started_policy_allows,
)

ORG = uuid.uuid4()

#: The policy under test: ``max_retries`` 2 => at most 3 executions.
_POLICY = {"on": ["failure"], "max_retries": 2}
_CEILING = 3


# ---------------------------------------------------------------------------
# 1. The one shared measure + ceiling
# ---------------------------------------------------------------------------


def test_consumed_executions_uses_max_of_node_attempts_and_prior_claims() -> None:
    assert retry_executions_consumed(node_attempt_count=2, claim_count=3) == 2
    # Pre-node hang: a claim consumed an execution without a node attempt.
    assert retry_executions_consumed(node_attempt_count=0, claim_count=3) == 2
    assert retry_executions_consumed(node_attempt_count=0, claim_count=0) == 0
    # Garbage counters coerce to zero rather than raising.
    assert retry_executions_consumed(node_attempt_count=None, claim_count="2") == 0


def test_execution_ceiling_is_one_plus_max_retries() -> None:
    assert retry_max_executions(_POLICY) == _CEILING
    # An EXPLICIT zero-retry policy authorises exactly one execution.
    assert retry_max_executions({"on": ["failure"], "max_retries": 0}) == 1
    # No/absent/malformed budget => no ceiling => existing behaviour preserved.
    assert retry_max_executions(None) is None
    assert retry_max_executions({}) is None
    assert retry_max_executions({"on": ["failure"]}) is None
    assert retry_max_executions({"max_retries": "2"}) is None


def test_budget_exhausted_at_the_ceiling_not_before() -> None:
    # 2 executions consumed -> the third (and last) is still permitted.
    assert not retry_budget_exhausted(_POLICY, node_attempt_count=2, claim_count=2)
    # 3 consumed -> exhausted.
    assert retry_budget_exhausted(_POLICY, node_attempt_count=3, claim_count=3)
    # No policy -> never exhausted by this gate.
    assert not retry_budget_exhausted({}, node_attempt_count=99, claim_count=99)


# ---------------------------------------------------------------------------
# 2. The policy path (worker-loss handback) — boundary at max_retries
# ---------------------------------------------------------------------------


def _handback_row(*, node_attempt_count: int, claim_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        retry_policy=_POLICY,
        node_attempt_count=node_attempt_count,
        claim_count=claim_count,
    )


def test_handback_grants_the_third_execution_and_refuses_the_fourth() -> None:
    allowed, reason = _node_started_policy_allows(_handback_row(node_attempt_count=2, claim_count=2))
    assert allowed
    assert reason == ""

    refused, reason = _node_started_policy_allows(_handback_row(node_attempt_count=3, claim_count=3))
    assert not refused
    assert "budget exhausted" in reason


# ---------------------------------------------------------------------------
# 3. The watchdog retry decision — SAME shared bound
# ---------------------------------------------------------------------------


def _watchdog_executor(node_attempt_count: int) -> MagicMock:
    executor = MagicMock()
    executor._read_retry_attempt_state = AsyncMock(return_value=(node_attempt_count, "tok", None))
    executor._node_timeouts = {}
    return executor


async def test_watchdog_allows_exactly_ceiling_executions() -> None:
    # attempt 2 consumed -> a third execution may be granted.
    assert (
        await wr._attempt_count_if_retryable(
            _watchdog_executor(2),
            run_id=uuid.uuid4(),
            org_id=ORG,
            trigger_type="manual",
            graph_json={"nodes": [], "edges": []},
            claim_token="tok",
            claim_count=1,
            retry_budget=_POLICY["max_retries"],
            final_status="failed",
            error_code=WORKER_LOST_ERROR_CODE,
        )
        == 2
    )
    # attempt 3 consumed -> refused (would be the fourth execution).
    assert (
        await wr._attempt_count_if_retryable(
            _watchdog_executor(3),
            run_id=uuid.uuid4(),
            org_id=ORG,
            trigger_type="manual",
            graph_json={"nodes": [], "edges": []},
            claim_token="tok",
            claim_count=1,
            retry_budget=_POLICY["max_retries"],
            final_status="failed",
            error_code=WORKER_LOST_ERROR_CODE,
        )
        is None
    )
    # A PRE-node hang is bounded by prior claims: 3 prior claims = 3 consumed.
    assert (
        await wr._attempt_count_if_retryable(
            _watchdog_executor(0),
            run_id=uuid.uuid4(),
            org_id=ORG,
            trigger_type="manual",
            graph_json={"nodes": [], "edges": []},
            claim_token="tok",
            claim_count=4,
            retry_budget=_POLICY["max_retries"],
            final_status="stalled",
            error_code="executor_stalled",
        )
        is None
    )
    # ... but 2 prior claims still permits the third execution.
    assert (
        await wr._attempt_count_if_retryable(
            _watchdog_executor(0),
            run_id=uuid.uuid4(),
            org_id=ORG,
            trigger_type="manual",
            graph_json={"nodes": [], "edges": []},
            claim_token="tok",
            claim_count=3,
            retry_budget=_POLICY["max_retries"],
            final_status="stalled",
            error_code="executor_stalled",
        )
        == 2
    )


# ---------------------------------------------------------------------------
# 4. The in-execute transient retry — clamped by the policy ceiling
# ---------------------------------------------------------------------------


def test_transient_retry_allowance_is_clamped_by_the_policy() -> None:
    assert ex._transient_retry_allowance(5, _POLICY) == _CEILING
    # No policy => the configured transport allowance is unchanged.
    assert ex._transient_retry_allowance(5, {}) == 5
    assert ex._transient_retry_allowance(5, None) == 5
    # A tighter policy still wins over the configured value.
    assert ex._transient_retry_allowance(5, {"on": ["failure"], "max_retries": 1}) == 2


def test_transient_requeue_gate_stops_at_the_ceiling() -> None:
    allowance = ex._transient_retry_allowance(5, _POLICY)
    # attempt 2 consumed + allowance 3 -> a third execution is permitted.
    assert ex._can_fenced_requeue(
        node_attempt_count=2,
        retries=allowance,
        superseded=False,
        stalled=False,
        script_lease_ok=True,
        graph_idempotent=True,
    )
    # attempt 3 -> refused (the fourth execution).
    assert not ex._can_fenced_requeue(
        node_attempt_count=3,
        retries=allowance,
        superseded=False,
        stalled=False,
        script_lease_ok=True,
        graph_idempotent=True,
    )


# ---------------------------------------------------------------------------
# 5. The dispatcher_reconcile re-enqueue backstop
# ---------------------------------------------------------------------------


def _pending_row(
    *,
    node_attempt_count: int,
    claim_count: int,
    retry_policy: Any = _POLICY,
    error_code: str | None = None,
) -> SimpleNamespace:
    """A pending, node-started, fenced-reset-shaped row (the backstop's class)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        status="pending",
        dispatcher="saq",
        dispatch_phase="first_node_dispatched",
        error_code=error_code,
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


async def _run_one_row(row: SimpleNamespace) -> tuple[Any, Any]:
    """Drive ``_reconcile_one_row`` to the retry-budget gate; return the mocks."""
    dispatch_mock = AsyncMock(return_value=0)
    budget_fail_mock = AsyncMock(return_value=True)
    summary: dict[str, Any] = {}
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
            [],
            early_detect_minutes=None,
            floor_nodeless_window=None,
            stale_window=1800,
        )
    return dispatch_mock, budget_fail_mock


async def test_reconcile_terminal_fails_a_pending_row_beyond_budget() -> None:
    row = _pending_row(node_attempt_count=3, claim_count=3)

    dispatch_mock, budget_fail_mock = await _run_one_row(row)

    budget_fail_mock.assert_awaited_once()
    assert budget_fail_mock.await_args is not None
    assert budget_fail_mock.await_args.kwargs["ceiling"] == _CEILING
    dispatch_mock.assert_not_awaited()


async def test_reconcile_still_recovers_a_within_budget_pending_row() -> None:
    row = _pending_row(node_attempt_count=2, claim_count=2)

    dispatch_mock, budget_fail_mock = await _run_one_row(row)

    budget_fail_mock.assert_not_awaited()
    dispatch_mock.assert_awaited_once()
    assert dispatch_mock.await_args is not None
    assert dispatch_mock.await_args.args[4] == "execute_run"


async def test_reconcile_terminal_fails_a_handed_back_row_beyond_budget() -> None:
    """The FAR-1627 handback stamps ``harness.worker_lost`` — still bounded."""
    row = _pending_row(node_attempt_count=3, claim_count=3, error_code=WORKER_LOST_ERROR_CODE)

    dispatch_mock, budget_fail_mock = await _run_one_row(row)

    budget_fail_mock.assert_awaited_once()
    dispatch_mock.assert_not_awaited()


async def test_reconcile_does_not_budget_gate_capacity_marked_rows() -> None:
    """A capacity demotion consumes a claim but executes nothing."""
    row = _pending_row(node_attempt_count=0, claim_count=30, error_code="pipeline_capacity")

    dispatch_mock, budget_fail_mock = await _run_one_row(row)

    budget_fail_mock.assert_not_awaited()
    dispatch_mock.assert_awaited_once()


async def test_reconcile_without_policy_preserves_the_old_recovery() -> None:
    row = _pending_row(node_attempt_count=30, claim_count=30, retry_policy={})

    dispatch_mock, budget_fail_mock = await _run_one_row(row)

    budget_fail_mock.assert_not_awaited()
    dispatch_mock.assert_awaited_once()


async def test_budget_exhausted_terminal_fail_is_guarded_and_coded() -> None:
    class _CapturingSession:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount
            self.statements: list[tuple[str, dict[str, Any]]] = []

        async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> SimpleNamespace:
            self.statements.append((str(stmt), params or {}))
            return SimpleNamespace(rowcount=self.rowcount)

    run_id = uuid.uuid4()
    session = _CapturingSession(rowcount=1)
    assert await ch._fail_retry_budget_exhausted_run(session, run_id, ORG, ceiling=_CEILING)

    sql, params = session.statements[0]
    assert params["code"] == ch._RETRY_BUDGET_EXHAUSTED_ERROR_CODE
    assert params["code"] == "retry_budget_exhausted"
    assert params["ceiling"] == _CEILING
    # The UPDATE re-checks the premise on the row itself.
    assert "status='pending'" in sql
    assert "GREATEST" in sql
    assert "node_attempt_count" in sql
    assert "claim_count" in sql

    # A row that advanced between the scan and the write is NOT reported killed.
    assert not await ch._fail_retry_budget_exhausted_run(_CapturingSession(rowcount=0), run_id, ORG, ceiling=_CEILING)


# ---------------------------------------------------------------------------
# 6. The SAQ transport allowance
# ---------------------------------------------------------------------------


def test_saq_retries_are_clamped_to_the_remaining_policy_budget() -> None:
    # retries = RE-attempts, so the initial dispatch of a 3-execution ceiling
    # may carry at most 2.
    assert clamp_saq_retries(_POLICY, node_attempt_count=0, claim_count=0, configured=5) == 2
    # After one execution, only one re-attempt remains within the ceiling.
    assert clamp_saq_retries(_POLICY, node_attempt_count=1, claim_count=1, configured=5) == 1
    # At the ceiling / beyond it, no transport retry may be granted.
    assert clamp_saq_retries(_POLICY, node_attempt_count=2, claim_count=2, configured=5) == 0
    assert clamp_saq_retries(_POLICY, node_attempt_count=3, claim_count=3, configured=5) == 0
    # No policy => existing transport behaviour.
    assert clamp_saq_retries({}, node_attempt_count=0, claim_count=0, configured=5) == 5
    assert clamp_saq_retries(None, node_attempt_count=0, claim_count=0, configured=5) == 5


async def test_dispatch_passes_the_clamped_retries_to_the_queue() -> None:
    from modulo.core import dispatch

    captured: dict[str, Any] = {}

    class _Queue:
        name = "runs"

        async def job(self, key: str) -> None:
            return None

        def job_id(self, key: str) -> str:
            return "jid"

        async def enqueue(self, function: str, **kwargs: Any) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(id="jid")

    with (
        patch.object(dispatch, "RedisQueue", lambda *a, **k: _Queue()),
        patch.object(dispatch, "_get_shared_redis", AsyncMock(return_value=MagicMock())),
    ):
        job_id, deduped = await dispatch._enqueue_saq("run-id", "org-id", "runs", "execute_run", None, retries=2)

    assert job_id == "jid"
    assert deduped is False
    assert captured["retries"] == 2


async def test_saq_retries_for_run_reads_the_policy_and_counters() -> None:
    from modulo.core import dispatch

    run = SimpleNamespace(node_attempt_count=3, claim_count=3, pipeline_id=uuid.uuid4())
    pipeline = SimpleNamespace(retry_policy=_POLICY)
    result = MagicMock()
    result.scalar_one_or_none.return_value = pipeline

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    with patch("modulo.db.crud.run.get_run", AsyncMock(return_value=run)):
        retries = await dispatch._saq_retries_for_run(session, uuid.uuid4(), job_type="execute_run")

    # Ceiling 3, consumed 3 => no transport retry remains.
    assert retries == 0
