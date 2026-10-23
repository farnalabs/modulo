"""FAR-2078 slice 2 — per-attempt execution instrumentation.

The storage slice added ``run_attempts`` (model + CRUD). This slice WRITES the
rows: one OPEN row at admission, closed when the attempt ends. These tests pin
the four properties the slice exists for:

1. **Same event, same transaction.** The row is opened at the exact point the
   FAR-2076 execution budget is spent, gated on the bump landing — so attempts
   can never outnumber ``runs.execution_attempt_count``.
2. **The reset retains its reason.** ``_fenced_pending_reset`` nulls the run's
   ``error_code``; the attempt row must keep the reason and the code it ended
   with. This is proven against a REAL SQLite database, not a mock.
3. **Fail-open.** A failed attempt write (start or close) is logged and never
   fails the run, the reset, or the shutdown handback. The start write also
   runs inside a SAVEPOINT: on Postgres a bare caught statement error would
   leave the transaction aborted and fail the admission commit instead.
4. **Wiring.** Every enumerated terminal path closes the row it opened.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.errors import NodeCancelledError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import pipeline_execution as pe
from modulo.core import worker_handoff as wh
from modulo.core.pipeline_engine import PipelineExecutor
from modulo.core.pipeline_engine import executor as ex
from modulo.core.pipeline_engine import watchdog_retry as wr
from modulo.db.crud import run_attempts as attempt_crud
from modulo.db.models.run_attempt import (
    ATTEMPT_TERMINAL_REASON_COMPLETED,
    ATTEMPT_TERMINAL_REASON_DEADLINE,
    ATTEMPT_TERMINAL_REASON_GUARD_CAP,
    ATTEMPT_TERMINAL_REASON_PROVIDER_ERROR,
    ATTEMPT_TERMINAL_REASON_RESET,
    ATTEMPT_TERMINAL_REASON_STALL,
    ATTEMPT_TERMINAL_REASON_WORKER_LOST,
    RunAttempt,
)

ORG = uuid.uuid4()
RUN = uuid.uuid4()


# ---------------------------------------------------------------------------
# 1. The run-outcome → closed-vocabulary mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        ("complete", None, ATTEMPT_TERMINAL_REASON_COMPLETED),
        ("complete", "harness.idempotency_gate", ATTEMPT_TERMINAL_REASON_COMPLETED),
        ("rejected", "hitl.rejected", ATTEMPT_TERMINAL_REASON_COMPLETED),
        ("cancelled", None, ATTEMPT_TERMINAL_REASON_COMPLETED),
        ("stalled", "executor_stalled", ATTEMPT_TERMINAL_REASON_STALL),
        ("failed", "executor_stalled", ATTEMPT_TERMINAL_REASON_STALL),
        ("failed", "node_deadline_exceeded", ATTEMPT_TERMINAL_REASON_DEADLINE),
        ("failed", "node.deadline_exceeded", ATTEMPT_TERMINAL_REASON_DEADLINE),
        ("failed", "node.timeout", ATTEMPT_TERMINAL_REASON_DEADLINE),
        ("failed", "executor_heartbeat_lost", ATTEMPT_TERMINAL_REASON_WORKER_LOST),
        ("failed", "harness.worker_lost", ATTEMPT_TERMINAL_REASON_WORKER_LOST),
        ("failed", "harness.dispatch_failed", ATTEMPT_TERMINAL_REASON_WORKER_LOST),
        ("failed", "runaway", ATTEMPT_TERMINAL_REASON_GUARD_CAP),
        ("failed", "cost_ceiling_exceeded", ATTEMPT_TERMINAL_REASON_GUARD_CAP),
        ("eval_failed", "eval_blocked", ATTEMPT_TERMINAL_REASON_GUARD_CAP),
        ("budget_exceeded", None, ATTEMPT_TERMINAL_REASON_GUARD_CAP),
        ("failed", "provider.billing", ATTEMPT_TERMINAL_REASON_PROVIDER_ERROR),
        ("failed", "executor_failed", ATTEMPT_TERMINAL_REASON_PROVIDER_ERROR),
    ],
    ids=lambda value: str(value),
)
def test_terminal_reason_mapping(status: str, error_code: str | None, expected: str) -> None:
    assert pe.attempt_terminal_reason(status, error_code) == expected


@pytest.mark.parametrize("status", [None, "pending", "running", "awaiting_human", "hitl_parked", "claimed"])
def test_a_non_terminal_run_leaves_the_attempt_open(status: str | None) -> None:
    """``awaiting_human`` is the load-bearing case: a resume is a checkpoint
    continuation, NOT a new execution attempt, so the row stays open until the
    resumed stream ends."""
    assert pe.attempt_terminal_reason(status, None) is None


# ---------------------------------------------------------------------------
# 2. Admission opens the row — same transaction, same event
# ---------------------------------------------------------------------------


class _FakeBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _AdmissionSession:
    """Session double modelling every statement the admission path issues."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.savepoints: list[str] = []
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        return self._bind

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    def begin_nested(self) -> _FakeBegin:
        self.savepoints.append("nested")
        return _FakeBegin()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def get(self, entity: Any, ident: Any, **kwargs: Any) -> Any:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def close(self) -> None:
        return None


def _admitted_run() -> SimpleNamespace:
    return SimpleNamespace(
        status="running",
        execution_attempt_count=0,
        cancellation_requested=False,
        claim_token="tok",
    )


async def _admit(
    *,
    counted: bool = True,
    start_error: BaseException | None = None,
    active_runs: int = 0,
    max_concurrent: int = 1,
) -> tuple[Any, AsyncMock, _AdmissionSession]:
    """Drive the REAL ``_claim_under_capacity`` with a slot free."""
    run = _admitted_run()
    session = _AdmissionSession()
    executor = PipelineExecutor(MagicMock())
    executor._claim_token = "tok"
    executor._session_factory = MagicMock(return_value=session)
    bump = AsyncMock(return_value=counted)
    start = AsyncMock(side_effect=start_error)
    with (
        patch.object(ex, "set_rls_org", AsyncMock()),
        patch.object(ex, "set_rls_execution_context", AsyncMock()),
        patch.object(ex, "set_mutation_row_lock_timeout", AsyncMock()),
        patch.object(ex, "get_run", AsyncMock(return_value=run)),
        patch.object(ex, "count_active_runs_for_pipeline", AsyncMock(return_value=active_runs)),
        patch.object(ex, "update_run_status", AsyncMock(return_value=None)),
        patch.object(executor, "_claim_run_and_audit", AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_execution.bump_execution_attempt_count", bump),
        patch("modulo.db.crud.run_attempts.start_attempt", start),
    ):
        result = await executor._claim_under_capacity(
            run_id=RUN,
            org_id=ORG,
            pipeline_id=uuid.uuid4(),
            max_concurrent=max_concurrent,
            org_sandbox_cap=None,
            org_sandbox_host_only=False,
            org_sandbox_population="all",
            org_run_limit=None,
        )
    return result, start, session


async def test_admission_opens_the_attempt_row_with_the_spent_execution() -> None:
    result, start, session = await _admit()

    assert result.status == "running"
    start.assert_awaited_once()
    assert start.await_args is not None
    assert start.await_args.args[1] == RUN
    # The row is opened in a SAVEPOINT so a diagnostics failure cannot abort
    # the admission transaction (see the fail-open test below).
    assert session.savepoints == ["nested"]


async def test_a_demoted_claim_opens_no_attempt_row() -> None:
    """The attempt never began, so neither the budget nor the history may grow."""
    _result, start, session = await _admit(active_runs=1, max_concurrent=1)

    start.assert_not_awaited()
    assert not session.savepoints


async def test_a_fence_rejected_budget_bump_opens_no_attempt_row() -> None:
    """Attempt rows must never outnumber ``execution_attempt_count``: a bump
    the claim fence rejected spent no execution, so it starts no attempt."""
    result, start, session = await _admit(counted=False)

    assert result.status == "running"
    start.assert_not_awaited()
    assert not session.savepoints


async def test_attempt_row_write_failure_never_fails_the_admission() -> None:
    """FAIL-OPEN: diagnostics must never gate an admitted execution."""
    result, start, session = await _admit(start_error=RuntimeError("attempt table is down"))

    assert result.status == "running"
    start.assert_awaited_once()
    # The write ran inside a SAVEPOINT: on Postgres a caught statement error
    # otherwise leaves the transaction aborted, and the admission COMMIT (not
    # just the diagnostics write) would fail.
    assert session.savepoints == ["nested"]


# ---------------------------------------------------------------------------
# 3. The reset retains its own reason (REAL SQLite — the load-bearing case)
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_maker(tmp_path: Any) -> Any:
    """File-backed SQLite with the parent tables + the real ``run_attempts`` DDL."""
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'attempts.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE organisations (id VARCHAR(32) PRIMARY KEY)")
        await conn.exec_driver_sql(
            "CREATE TABLE runs ("
            "id VARCHAR(32) PRIMARY KEY, "
            "organisation_id VARCHAR(32) NOT NULL, "
            "status VARCHAR(20) NOT NULL, "
            "claim_token VARCHAR(64), "
            "cancellation_requested BOOLEAN NOT NULL DEFAULT 0, "
            "error_code VARCHAR(255), "
            "error_detail TEXT)"
        )
        await conn.run_sync(RunAttempt.__table__.create)
        await conn.exec_driver_sql("INSERT INTO organisations (id) VALUES (:org)", {"org": ORG.hex})
        await conn.exec_driver_sql(
            "INSERT INTO runs (id, organisation_id, status, claim_token, error_code) "
            "VALUES (:run, :org, 'running', 'tok', 'executor_stalled')",
            {"run": RUN.hex, "org": ORG.hex},
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


def _reset_executor(maker: async_sessionmaker[AsyncSession]) -> PipelineExecutor:
    executor = object.__new__(PipelineExecutor)
    executor._claim_token = "tok"
    executor._session_factory = maker
    return executor


async def test_a_fenced_reset_retains_the_attempts_reason_and_error_code(
    sqlite_maker: async_sessionmaker[AsyncSession],
) -> None:
    """PROVE-THE-FIX for the whole table: the reset nulls ``runs.error_code``,
    so the ONLY surviving record of why the attempt ended is its own row."""
    async with sqlite_maker() as session, session.begin():
        await attempt_crud.start_attempt(session, RUN)

    executor = _reset_executor(sqlite_maker)
    await executor._record_attempt_reset(run_id=RUN, org_id=ORG, error_code="executor_stalled")

    # The EXACT statement ``_fenced_pending_reset`` issues. It is applied here
    # rather than through the method because the method binds ``str(run_id)``
    # (dashed) while SQLite stores ``Uuid`` columns as 32-char hex — a Postgres-
    # only comparison. The property under test is that the reset does not touch
    # the attempt row, which this statement proves on a real database.
    async with sqlite_maker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET status='pending', error_code=NULL, error_detail=NULL "
                "WHERE id=:rid AND claim_token=:tok AND status='running' AND cancellation_requested = false"
            ),
            {"rid": RUN.hex, "tok": "tok"},
        )

    async with sqlite_maker() as session, session.begin():
        run_row = (
            await session.execute(text("SELECT status, error_code FROM runs WHERE id=:rid"), {"rid": RUN.hex})
        ).first()
    assert run_row is not None
    assert run_row[0] == "pending"
    assert run_row[1] is None, "the reset clears the run-level reason (the bug this table exists for)"

    async with sqlite_maker() as session:
        attempts = await attempt_crud.list_attempts(session, RUN)
    assert len(attempts) == 1
    assert attempts[0].terminal_reason == ATTEMPT_TERMINAL_REASON_RESET
    assert attempts[0].error_code == "executor_stalled"
    assert attempts[0].ended_at is not None


async def test_close_with_no_open_attempt_is_an_idempotent_no_op(
    sqlite_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A second close (or a close for a never-admitted run) must not invent a
    row — a fence loss / deferral must never close a successor's attempt."""
    async with sqlite_maker() as session, session.begin():
        await attempt_crud.start_attempt(session, RUN)
        await attempt_crud.close_attempt(session, RUN, terminal_reason="completed")

    assert await pe.record_attempt_close(sqlite_maker, RUN, ORG, terminal_reason="reset") is None


# ---------------------------------------------------------------------------
# 4. Fail-open: a close failure never propagates
# ---------------------------------------------------------------------------


async def test_attempt_close_failure_is_swallowed_and_logged(
    sqlite_maker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(attempt_crud, "close_attempt", AsyncMock(side_effect=RuntimeError("db down"))),
        caplog.at_level("WARNING", logger="modulo.core.pipeline_execution"),
    ):
        result = await pe.record_attempt_close(sqlite_maker, RUN, ORG, terminal_reason="reset")

    assert result is None
    assert any("attempt_close_failed" in record.message for record in caplog.records)


async def test_a_failing_close_never_breaks_the_reset_path(
    sqlite_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The reset already committed; instrumentation may not raise into it."""
    executor = _reset_executor(sqlite_maker)
    closer = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(attempt_crud, "close_attempt", closer):
        await executor._record_attempt_reset(run_id=RUN, org_id=ORG, error_code="executor_stalled")

    closer.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Wiring — every enumerated terminal path closes the row it opened
# ---------------------------------------------------------------------------


def _transient_executor(*, reset_rowcount: int | None) -> PipelineExecutor:
    executor = object.__new__(PipelineExecutor)
    executor._claim_token = "tok"
    executor._load_transient_state = AsyncMock(return_value=(0, "tok", None, False, "pipeline:1"))
    executor._idempotency_gate_ok = MagicMock(return_value=False)
    executor._script_lease_ok = AsyncMock(return_value=True)
    executor._fenced_pending_reset = AsyncMock(return_value=reset_rowcount)
    executor._cleanup_run_resources = AsyncMock()
    executor._record_attempt_reset = AsyncMock()
    return executor


async def _decide_transient(executor: PipelineExecutor) -> dict[str, Any]:
    with patch("modulo.settings.get_settings", return_value=SimpleNamespace(saq_run_retries=5)):
        return await executor._decide_transient_failure(
            exc=NodeCancelledError("transient sandbox failure"),
            run_id=RUN,
            org_id=ORG,
            graph_json={"nodes": [{"id": "n1"}], "edges": []},
            graph_idempotent=True,
            single_sandbox_node=False,
            model_backend_hub=None,
            connector_hub=None,
            broker=MagicMock(),
            stall_requested=None,
            pipeline_retry_policy={"on": ["failure"], "max_retries": 2},
        )


async def test_a_confirmed_transient_reset_closes_the_attempt_with_its_cause() -> None:
    executor = _transient_executor(reset_rowcount=1)

    decision = await _decide_transient(executor)

    assert decision["decision"] == "requeue"
    executor._record_attempt_reset.assert_awaited_once()
    assert executor._record_attempt_reset.await_args is not None
    assert executor._record_attempt_reset.await_args.kwargs["error_code"] == "NodeCancelledError"


@pytest.mark.parametrize("rowcount", [0, None])
async def test_a_fence_lost_or_timed_out_reset_closes_no_attempt(rowcount: int | None) -> None:
    """A fence loss belongs to a successor's row; a timeout is left to the
    backstop's terminal write (which closes with the truth)."""
    executor = _transient_executor(reset_rowcount=rowcount)

    decision = await _decide_transient(executor)

    assert decision["decision"] == "requeue"
    executor._record_attempt_reset.assert_not_awaited()


async def test_a_retry_policy_redispatch_closes_the_attempt_as_a_reset() -> None:
    executor = object.__new__(PipelineExecutor)
    executor._claim_token = "tok"
    executor._fenced_pending_reset = AsyncMock(return_value=1)
    executor._cleanup_run_resources = AsyncMock()
    executor._record_attempt_reset = AsyncMock()
    with (
        patch.object(ex, "_record_retry_redispatch", MagicMock()),
        patch("modulo.settings.get_settings", return_value=SimpleNamespace(saq_retry_delay=0)),
        patch.object(ex.asyncio, "sleep", AsyncMock()),
        pytest.raises(ex.RunRetryPolicyError),
    ):
        await executor._redispatch_after_policy(
            run_id=RUN,
            org_id=ORG,
            final_status="failed",
            error_code="executor_failed",
            execution_attempt_count=1,
            retry_budget=2,
            pipeline_retry_policy={"on": ["failure"], "max_retries": 2},
            model_backend_hub=None,
            connector_hub=None,
        )

    executor._record_attempt_reset.assert_awaited_once()
    assert executor._record_attempt_reset.await_args is not None
    assert executor._record_attempt_reset.await_args.kwargs["error_code"] == "executor_failed"


async def test_a_watchdog_redispatch_closes_the_attempt_as_a_reset() -> None:
    executor = MagicMock()
    executor._claim_token = "tok"
    executor._fenced_pending_reset = AsyncMock(return_value=1)
    closer = AsyncMock(return_value=1)
    with (
        patch.object(
            wr,
            "_load_retry_context_or_none",
            AsyncMock(return_value=({"on": ["stall"], "max_retries": 2}, "manual", {})),
        ),
        patch.object(wr, "_resolve_retry_budget", MagicMock(return_value=2)),
        patch.object(wr, "_attempt_count_if_retryable", AsyncMock(return_value=1)),
        patch.object(wr, "_resolve_schedule_state", MagicMock(return_value=("absent", 0.0))),
        patch.object(wr, "_record_retry_redispatch", MagicMock()),
        patch.object(wr.asyncio, "sleep", AsyncMock()),
        patch("modulo.core.pipeline_execution.record_attempt_close", closer),
    ):
        result = await wr.watchdog_retry_after_policy(
            aeng=MagicMock(),
            run_id=RUN,
            org_id=ORG,
            executor=executor,
            final_status="stalled",
            error_code=pe.EXECUTOR_STALLED_ERROR_CODE,
        )

    assert result is True
    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert closer.await_args.kwargs["terminal_reason"] == ATTEMPT_TERMINAL_REASON_RESET
    assert closer.await_args.kwargs["error_code"] == pe.EXECUTOR_STALLED_ERROR_CODE


async def test_a_handed_back_worker_loss_closes_the_attempt() -> None:
    entry = wh.InFlightRun(run_id=RUN, org_id=ORG, claim_token="tok")
    closer = AsyncMock(return_value=0)
    with patch("modulo.core.pipeline_execution.record_attempt_close", closer):
        await wh._close_handed_back_attempts(MagicMock(), [entry])

    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert closer.await_args.kwargs["terminal_reason"] == ATTEMPT_TERMINAL_REASON_WORKER_LOST
    assert closer.await_args.kwargs["error_code"] == wh.WORKER_LOST_ERROR_CODE


@pytest.mark.parametrize(
    ("status", "error_code", "expected_reason"),
    [
        ("complete", None, ATTEMPT_TERMINAL_REASON_COMPLETED),
        ("failed", pe.EXECUTOR_STALLED_ERROR_CODE, ATTEMPT_TERMINAL_REASON_STALL),
        ("failed", pe.NODE_DEADLINE_EXCEEDED_ERROR_CODE, ATTEMPT_TERMINAL_REASON_DEADLINE),
        ("failed", "executor_heartbeat_lost", ATTEMPT_TERMINAL_REASON_WORKER_LOST),
    ],
)
async def test_the_wrapper_closes_the_attempt_for_a_terminal_outcome(
    status: str, error_code: str | None, expected_reason: str
) -> None:
    closer = AsyncMock(return_value=0)
    with patch("modulo.core.pipeline_execution.record_attempt_close", closer):
        await pe._record_executor_outcome_attempt(MagicMock(), RUN, ORG, status=status, error_code=error_code)

    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert closer.await_args.kwargs["terminal_reason"] == expected_reason


async def test_the_wrapper_does_not_close_an_awaiting_human_attempt() -> None:
    """A resume continues the same attempt — closing here would end it early."""
    closer = AsyncMock()
    with patch("modulo.core.pipeline_execution.record_attempt_close", closer):
        await pe._record_executor_outcome_attempt(MagicMock(), RUN, ORG, status="awaiting_human", error_code=None)

    closer.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. The close path is exercised end-to-end on SQLite (start -> close)
# ---------------------------------------------------------------------------


async def test_start_then_close_round_trips_through_the_real_table(
    sqlite_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_maker() as session, session.begin():
        index = await attempt_crud.start_attempt(session, RUN, started_at=datetime.now(UTC))
    assert index == 0

    closed = await pe.record_attempt_close(sqlite_maker, RUN, ORG, terminal_reason=ATTEMPT_TERMINAL_REASON_COMPLETED)
    assert closed == 0

    async with sqlite_maker() as session:
        attempts = await attempt_crud.list_attempts(session, RUN)
    assert attempts[0].terminal_reason == ATTEMPT_TERMINAL_REASON_COMPLETED
    assert attempts[0].ended_at is not None


# ---------------------------------------------------------------------------
# 7. Wiring — the raw terminal writers close the row too
# ---------------------------------------------------------------------------


def _fake_engine(*, landed: bool) -> MagicMock:
    """Engine double for the raw terminal writers (``fail_run_terminal`` /
    ``mark_complete``): one connection whose UPDATE reports a landed row."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=SimpleNamespace(fetchone=lambda: ("id",) if landed else None))
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    conn.begin = MagicMock(return_value=begin_cm)
    connect_cm = MagicMock()
    connect_cm.__aenter__ = AsyncMock(return_value=conn)
    connect_cm.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.connect = MagicMock(return_value=connect_cm)
    return engine


async def test_fail_run_terminal_closes_the_attempt_with_the_mapped_reason() -> None:
    """The shared raw terminal writer is the close point for the watchdog
    kills, heartbeat loss, the worker-shutdown terminalise and the stale
    sweeps — deleting the call would leave every one of them open."""
    closer = AsyncMock(return_value=0)
    with (
        patch.object(pe, "_advance_journeys_from_stored_refs", AsyncMock()),
        patch.object(pe, "_record_fact_for_terminal_failed_run", AsyncMock()),
        patch.object(pe, "_bound_hot_runs_row_lock", AsyncMock()),
        patch("modulo.core.pipeline_execution.record_attempt_close", closer),
    ):
        ok = await pe.fail_run_terminal(
            _fake_engine(landed=True),
            str(RUN),
            str(ORG),
            error_code=pe.EXECUTOR_STALLED_ERROR_CODE,
            error_detail="stalled",
        )

    assert ok is True
    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert closer.await_args.kwargs["terminal_reason"] == ATTEMPT_TERMINAL_REASON_STALL
    assert closer.await_args.kwargs["error_code"] == pe.EXECUTOR_STALLED_ERROR_CODE


async def test_a_fenced_out_terminal_write_closes_nothing() -> None:
    """Rowcount 0 means a successor already owns the run — its attempt row is
    not ours to close."""
    closer = AsyncMock(return_value=0)
    with (
        patch.object(pe, "_advance_journeys_from_stored_refs", AsyncMock()),
        patch.object(pe, "_record_fact_for_terminal_failed_run", AsyncMock()),
        patch.object(pe, "_bound_hot_runs_row_lock", AsyncMock()),
        patch("modulo.core.pipeline_execution.record_attempt_close", closer),
    ):
        ok = await pe.fail_run_terminal(
            _fake_engine(landed=False),
            str(RUN),
            str(ORG),
            error_code=pe.EXECUTOR_STALLED_ERROR_CODE,
            error_detail="stalled",
        )

    assert ok is False
    closer.assert_not_awaited()


async def test_mark_complete_closes_the_attempt_as_completed() -> None:
    closer = AsyncMock(return_value=0)
    with (
        patch.object(pe, "_advance_journeys_from_stored_refs", AsyncMock()),
        patch.object(pe, "_bound_hot_runs_row_lock", AsyncMock()),
        patch("modulo.core.pipeline_execution.record_attempt_close", closer),
    ):
        await pe.mark_complete(_fake_engine(landed=True), str(RUN), str(ORG))

    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert closer.await_args.kwargs["terminal_reason"] == ATTEMPT_TERMINAL_REASON_COMPLETED


# ---------------------------------------------------------------------------
# 8. Wiring — the shutdown handback closes only what it actually handed back
# ---------------------------------------------------------------------------


def _handback_engine(*, reset_landed: bool) -> MagicMock:
    conn = MagicMock()

    async def _execute(stmt: object, params: object = None) -> Any:
        if stmt is wh._HANDBACK_RESET_SQL:
            return SimpleNamespace(fetchone=lambda: ("id",) if reset_landed else None)
        return SimpleNamespace(fetchone=lambda: None)

    conn.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    conn.begin = MagicMock(return_value=begin_cm)
    connect_cm = MagicMock()
    connect_cm.__aenter__ = AsyncMock(return_value=conn)
    connect_cm.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.connect = MagicMock(return_value=connect_cm)
    return engine


async def _handback_once(*, reset_landed: bool) -> tuple[MagicMock, dict[str, int]]:
    reg = wh.InFlightRunRegistry()
    reg.register(run_id=RUN, org_id=ORG, claim_token="tok")
    reg.mark_cancellation_observed(RUN, "tok")
    row = SimpleNamespace(
        status="running",
        dispatch_phase="first_node_dispatched",
        node_attempt_count=1,
        execution_attempt_count=1,
        node_token_usage={"n1": 10},
        started_at=datetime.now(UTC),
        dispatched_at=None,
        sandbox_dispatch_state=None,
        retry_policy={},
        outputs_absent=True,
        checkpoints_absent=True,
    )
    closer = AsyncMock(return_value=0)

    async def _load(conn: object, *, run_id: uuid.UUID, org_id: uuid.UUID) -> Any:
        return row, {"nodes": [{"id": "n1"}]}

    with (
        patch.object(wh, "_load_handback_row", _load),
        patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", AsyncMock()),
        patch.object(wh, "_close_handed_back_attempts", closer),
    ):
        result = await wh.handback_inflight_runs(_handback_engine(reset_landed=reset_landed), registry=reg)
    return closer, result


async def test_a_handed_back_run_closes_its_attempt() -> None:
    closer, result = await _handback_once(reset_landed=True)

    assert result["handed_back"] == 1
    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert [entry.run_id for entry in closer.await_args.args[1]] == [RUN]


async def test_a_fenced_out_handback_closes_no_attempt() -> None:
    closer, result = await _handback_once(reset_landed=False)

    assert result["handed_back"] == 0
    closer.assert_awaited_once()
    assert closer.await_args is not None
    assert not closer.await_args.args[1]
