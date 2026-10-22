"""Unit tests for the shared watchdog-kill retry mechanism (FAR-690 / FAR-693)
and the per-run capacity-retry budget on the stale-run sweep (FAR-705).

FAR-690: the FAR-369 absolute node-deadline watchdog used to terminal-fail the
run DIRECTLY, bypassing the run-level retry decision. FAR-693: the executor-
level zombie watchdog had the same bypass for stalls. Both now consult the
pipeline's ``retry_policy`` through the ONE shared mechanism
(``pipeline_engine.watchdog_retry``) and re-dispatch via the SAME fenced
pending-reset + ``RunRetryPolicyError`` re-raise the in-execute retry path
uses. These tests drive the REAL hook → decision → executor-helper chain with
only the DB/session seams mocked (prove-the-fix: every test fails without the
wiring, because the watchdog would terminal-fail unconditionally).

FAR-1463 adds the observability half: a firing is recorded durably on
``runs.node_deadline_watchdog_fired_count`` BEFORE the retry consult, so it is
counted on BOTH outcomes — the re-dispatch (which otherwise leaves no
analytics fingerprint, because the fenced pending-reset nulls ``error_code``)
and the terminal fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import watchdog_retry as wr
from modulo.core.pipeline_engine.executor import RunRetryPolicyError
from modulo.core.pipeline_engine.watchdog_retry import WatchdogRetryOutcome
from modulo.core.pipeline_execution import (
    _fail_overdue_node,
    _record_derived_deadline_cost,
    _salvage_cancelled_run_cost,
    _sweep_org_stale_runs,
    _watchdog_retry_enabled_for_job,
    run_executor_with_watchdog,
    zombie_watchdog,
)


def _make_executor_mock(
    *, claim_token: str = "tok-watch-1", attempt_count: int = 1, probe_ok: bool = True, reset_rowcount: int = 1
) -> MagicMock:
    """Executor double exposing exactly the seams the shared hook reuses."""
    executor = MagicMock()
    executor._claim_token = claim_token
    executor._read_retry_attempt_state = AsyncMock(return_value=(attempt_count, claim_token))
    executor._probe_script_lease = AsyncMock(return_value=probe_ok)
    executor._fenced_pending_reset = AsyncMock(return_value=reset_rowcount)
    return executor


def _watchdog_retry_session_factory() -> MagicMock:
    """Fake session factory for ``watchdog_retry._load_watchdog_retry_context``.

    The run/pipeline rows come from patched crud functions; the session only
    serves the snapshot select.
    """
    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [{"id": "node-a"}], "edges": []}
    result = MagicMock()
    result.scalar_one_or_none.return_value = snapshot

    @asynccontextmanager
    async def _session_ctx() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    return MagicMock(side_effect=lambda: _session_ctx())


def _db_seams(
    retry_policy: dict[str, Any] | None,
    *,
    trigger_type: str = "manual",
    graph_json: dict[str, Any] | None = None,
    claim_count: int = 1,
):
    """Patch the DB seams the shared hook's context loader resolves lazily.

    ``claim_count`` is the run row's total claim count — the pre-node hang
    budget bound (FAR-693 fix) reads it from the already-loaded row. Default
    1 = a single claim, so the effective attempt count IS
    ``node_attempt_count`` and the in-execute behaviour is unchanged.
    """
    run = MagicMock()
    run.trigger_type = trigger_type
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.claim_count = claim_count
    pipeline = MagicMock()
    pipeline.retry_policy = retry_policy if retry_policy is not None else {}

    @asynccontextmanager
    async def _session_ctx() -> AsyncIterator[MagicMock]:
        snapshot = MagicMock()
        snapshot.graph_json = graph_json if graph_json is not None else {"nodes": [{"id": "node-a"}], "edges": []}
        result = MagicMock()
        result.scalar_one_or_none.return_value = snapshot
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    factory = MagicMock(side_effect=lambda: _session_ctx())
    return (
        patch("modulo.db.crud.run.get_run", AsyncMock(return_value=run)),
        patch("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=pipeline)),
        patch("modulo.db.rls.set_rls_org", AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", AsyncMock()),
        patch("modulo.core.pipeline_engine.watchdog_retry.async_sessionmaker", return_value=factory),
        # FAR-2053: the stall veto reads the run's delivery markers before the
        # retry decision. These tests exercise the retry/terminal path with a
        # mock engine, so the marker reader is stubbed to "no markers" — the
        # same no-veto outcome a genuinely marker-less run produces.
        patch("modulo.core.pipeline_execution._read_delivery_markers", AsyncMock(return_value=None)),
    )


async def _consult_hook(
    executor: MagicMock,
    *,
    retry_policy: dict[str, Any] | None,
    final_status: str,
    error_code: str,
    attempt_count: int = 1,
    trigger_type: str = "manual",
    graph_json: dict[str, Any] | None = None,
    reset_rowcount: int = 1,
    claim_count: int = 1,
    zero_node: bool = False,
) -> tuple[bool, WatchdogRetryOutcome, AsyncMock]:
    """Run the REAL shared hook once with only DB seams mocked."""
    executor._read_retry_attempt_state = AsyncMock(return_value=(attempt_count, executor._claim_token))
    executor._fenced_pending_reset = AsyncMock(return_value=reset_rowcount)
    exec_task = asyncio.create_task(asyncio.sleep(999))
    box = WatchdogRetryOutcome()
    seams = _db_seams(retry_policy, trigger_type=trigger_type, graph_json=graph_json, claim_count=claim_count)
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        sleep_mock = stack.enter_context(
            patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock())
        )
        dispatched = await wr.watchdog_retry_after_policy(
            aeng=MagicMock(),
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            executor=executor,
            final_status=final_status,
            error_code=error_code,
            exec_task=exec_task,
            zero_node=zero_node,
            outcome=box,
        )
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    return dispatched, box, sleep_mock


# ---------------------------------------------------------------------------
# FAR-690 — deadline-watchdog kills honour the run-level retry policy
# ---------------------------------------------------------------------------


async def test_deadline_kill_redispatches_under_timeout_policy():
    """(a) A deadline-watchdog kill under {on: [timeout], max_retries: 1}
    re-dispatches the run instead of terminal-failing it, with the budget
    bookkeeping decremented via the attempt state."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock()
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    # NOT terminal-failed — the re-dispatch owns the run now.
    fail.assert_not_awaited()
    assert box.requested is True
    assert box.final_status == "failed"
    assert box.retry_budget == 1
    # The shared re-dispatch mechanism ran: fenced pending-reset + backoff.
    executor._fenced_pending_reset.assert_awaited_once()
    assert stall.is_set()


async def test_deadline_kill_terminal_fails_when_budget_exhausted():
    """(b) A second kill with the retry budget exhausted terminal-fails
    exactly as before."""
    executor = _make_executor_mock(attempt_count=2)
    fail = AsyncMock(return_value=True)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


async def test_deadline_kill_terminal_fails_without_timeout_coverage():
    """(c) A watchdog kill under a policy that does not cover the deadline
    outcome terminal-fails (unchanged behaviour)."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock(return_value=True)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["stall"], "max_retries": 2})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-1993 — a completed deadline-kill books DERIVED sandbox cost
# ---------------------------------------------------------------------------


async def test_deadline_kill_books_derived_cost_after_successful_terminal_write():
    """A terminal write that LANDED is followed by the derived-cost booking,
    credited with ``_timeout / 3600.0`` hours.

    Prove-the-fix: without the call site the compensation is never awaited, so
    the run keeps recording $0 for the node's whole deadline.
    """
    fail = AsyncMock(return_value=True)
    derived = AsyncMock()
    record = AsyncMock()
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", record))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_derived_deadline_cost", derived))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    derived.assert_awaited_once()
    assert derived.await_args.args[1] == "run-1"
    assert derived.await_args.args[2] == "org-1"
    assert derived.await_args.kwargs["derived_sandbox_wall_hours"] == 300 / 3600.0
    # Round-3 M1: the killed node travels so finalize can scope the sandbox
    # proof to THIS node (a run-scoped marker for another node must not license
    # crediting this node's deadline).
    assert derived.await_args.kwargs["killed_node_id"] == "n1"
    assert "n1" in derived.await_args.kwargs["error_detail"]
    assert "300s" in derived.await_args.kwargs["error_detail"]


async def test_deadline_kill_books_no_derived_cost_when_redispatching():
    """The re-dispatch branch must NOT book a derived cost: the run continues
    and will be finalised normally by its successor execution."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock(return_value=True)
    derived = AsyncMock()
    record = AsyncMock()
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, _box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", record))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_derived_deadline_cost", derived))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_not_awaited()
    derived.assert_not_awaited()


async def test_deadline_kill_books_no_derived_cost_when_terminal_write_rejected():
    """A fenced terminal write that did NOT land (already terminal / superseded
    / cancelled) leaves nothing to compensate — no derived cost is booked."""
    fail = AsyncMock(return_value=False)
    derived = AsyncMock()
    record = AsyncMock()
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", record))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_derived_deadline_cost", derived))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    derived.assert_not_awaited()


async def test_record_derived_deadline_cost_rereises_cancelled_error() -> None:
    """Worker shutdown is not ours to swallow.

    ``_record_derived_deadline_cost`` is fail-open for ordinary failures, but a
    ``CancelledError`` from the compensating write must propagate so the SAQ
    job is actually cancelled rather than silently abandoned mid-write.
    """
    with (
        patch(
            "modulo.core.pipeline_execution.async_sessionmaker",
            side_effect=asyncio.CancelledError(),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _record_derived_deadline_cost(
            MagicMock(),
            "run-1",
            "org-1",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=300 / 3600.0,
            killed_node_id="n1",
        )


# ---------------------------------------------------------------------------
# FAR-1914 round-2 C — the compensating cost writers bound the row lock FIRST
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _acm(obj: Any):
    yield obj


def _begin_bound_session() -> AsyncMock:
    """Session double whose ``begin()`` is an async context manager."""
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _session_factory(session: AsyncMock) -> MagicMock:
    return MagicMock(side_effect=lambda: _acm(session))


async def test_record_derived_deadline_cost_bounds_the_row_lock_before_finalize() -> None:
    """Round-2 C (prove-the-fix): bound the hot ``runs`` row lock first.

    The finalizing write takes ``update_run_status``'s ``SELECT ... FOR
    UPDATE``. Without the entry-point ``set_mutation_row_lock_timeout`` the
    wait is unbounded on a contended row. Order is asserted so a bound placed
    AFTER the lock cannot pass.
    """
    order: list[str] = []
    session = _begin_bound_session()

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _finalize(*_a: Any, **_k: Any) -> None:
        order.append("finalize")

    with (
        patch("modulo.core.pipeline_execution.async_sessionmaker", return_value=_session_factory(session)),
        patch("modulo.core.pipeline_execution.set_rls_org", new=AsyncMock()),
        patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.core.cost_controller.finalize.finalize_failed_run", new=_finalize),
    ):
        await _record_derived_deadline_cost(
            MagicMock(),
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=300 / 3600.0,
            killed_node_id="n1",
        )

    assert order == ["bound", "finalize"]


async def test_salvage_cancelled_run_cost_bounds_the_row_lock_before_salvage() -> None:
    """Round-2 C: the post-settle salvage bounds the row lock first too."""
    order: list[str] = []
    session = _begin_bound_session()

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _salvage(*_a: Any, **_k: Any) -> None:
        order.append("salvage")

    with (
        patch("modulo.core.pipeline_execution.async_sessionmaker", return_value=_session_factory(session)),
        patch("modulo.core.pipeline_execution.set_rls_org", new=AsyncMock()),
        patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.core.cost_controller.finalize.salvage_cancelled_run_cost", new=_salvage),
    ):
        await _salvage_cancelled_run_cost(MagicMock(), str(uuid.uuid4()), str(uuid.uuid4()))

    assert order == ["bound", "salvage"]


# ---------------------------------------------------------------------------
# FAR-1463 — a watchdog FIRING is recorded whether it re-dispatches or
# terminal-fails (the re-dispatch path otherwise leaves no fingerprint)
# ---------------------------------------------------------------------------


def _firing_order_recorder(order: list[str]) -> AsyncMock:
    """Recorder double that appends ``"record"`` so call ORDER vs the outcome
    (reset / terminal fail) is assertable — durable-first is the contract."""
    return AsyncMock(side_effect=lambda *_a, **_k: order.append("record"))


async def test_deadline_kill_records_firing_before_redispatch():
    """A firing is durably recorded ONCE, BEFORE the re-dispatch — the branch
    that otherwise leaves ZERO analytics fingerprints, because the fenced
    pending-reset nulls ``error_code`` (FAR-1423's "0 terminalisations
    misread as 0 firings").

    Prove-the-fix: without the ``_record_node_deadline_watchdog_firing`` call
    in ``_fail_overdue_node`` the recorder is never awaited and ``order`` never
    contains ``"record"`` — both assertions fail.
    """
    executor = _make_executor_mock(attempt_count=1)
    order: list[str] = []

    def _reset(**_kwargs: Any) -> int:
        order.append("reset")
        return 1

    executor._fenced_pending_reset = AsyncMock(side_effect=_reset)
    fail = AsyncMock()
    record = _firing_order_recorder(order)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", record))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    record.assert_awaited_once()
    # Recorded FIRST, so the count is durable before the run is demoted.
    assert order == ["record", "reset"]
    fail.assert_not_awaited()
    assert box.requested is True


async def test_deadline_kill_records_firing_before_terminal_fail():
    """The SAME recording happens when the kill terminal-fails (no ``timeout``
    coverage here): recorded once, first, and the run then fails with
    ``node_deadline_exceeded`` exactly as before.

    Together the two tests pin the FAR-1463 contract: a firing is recorded
    whether it re-dispatches or terminal-fails.
    """
    executor = _make_executor_mock(attempt_count=1)
    order: list[str] = []

    def _fail(*_a: Any, **_k: Any) -> bool:
        order.append("fail")
        return True

    fail = AsyncMock(side_effect=_fail)
    record = _firing_order_recorder(order)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["stall"], "max_retries": 2})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", record))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    record.assert_awaited_once()
    assert order == ["record", "fail"]
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


async def test_standdown_deadline_kill_records_no_firing():
    """A kill that STANDS DOWN (the run finished / the executor finished) is
    not a firing: no recording, so a finished run is never counted."""
    executor = _make_executor_mock(attempt_count=1)

    async def _already_done() -> None:
        return None

    exec_task = asyncio.create_task(_already_done())
    await exec_task
    fail = AsyncMock(return_value=True)
    record = _firing_order_recorder([])
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", record))
        # run_done_event set — the watchdog stands down before consulting the hook.
        done.set()
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    record.assert_not_awaited()
    fail.assert_not_awaited()
    assert box.requested is False


async def test_deadline_kill_never_redispatches_finished_run():
    """(d) A kill racing run completion never re-dispatches (and the
    stand-down path never double-fails): a done exec task makes the hook stand
    down before any reset, and a finished run makes the watchdog stand down
    entirely."""
    executor = _make_executor_mock(attempt_count=1)

    async def _already_done() -> None:
        return None

    exec_task = asyncio.create_task(_already_done())
    await exec_task
    fail = AsyncMock(return_value=True)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        # run_done_event set — the watchdog stands down before consulting the hook.
        done.set()
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    fail.assert_not_awaited()
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


async def test_deadline_kill_hook_failure_fails_closed_to_terminal():
    """A hook failure (e.g. DB error while loading the policy) never prevents
    the terminal fail — the retry decision must not strand a run."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock(return_value=True)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    with (
        patch("modulo.db.crud.run.get_run", AsyncMock(side_effect=RuntimeError("db down"))),
        patch("modulo.core.pipeline_execution.fail_run_terminal", fail),
    ):
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
    assert box.requested is False


async def test_deadline_kill_lost_fence_stands_down_without_redispatch():
    """A fence loss (rowcount 0 — a successor owns the run, or it just went
    terminal) must NOT re-dispatch; the watchdog's guarded terminal write
    becomes a no-op in the real DB."""
    executor = _make_executor_mock(attempt_count=1, reset_rowcount=0)
    dispatched, box, _sleep = await _consult_hook(
        executor,
        retry_policy={"on": ["timeout"], "max_retries": 1},
        final_status="failed",
        error_code="node_deadline_exceeded",
        reset_rowcount=0,
    )
    assert dispatched is False
    assert box.requested is False


async def test_deadline_kill_never_resets_non_idempotent_graph():
    """FAR-1649 qa iteration 3: the fenced pending-reset is NEVER performed
    for a non-idempotent graph, so a node-started run on such a graph can
    never become the ``pending`` row the restored reconcile re-enqueue leg
    re-dispatches — that leg is safe precisely because its only producer is
    idempotency-gated here (``not graph_idempotent`` stand-down), which is
    the guarantee "never re-EXECUTE a node-started NON-idempotent run".

    The timeout policy DOES cover the kill and the budget IS available — the
    graph is the ONLY thing that stands the hook down."""
    executor = _make_executor_mock(attempt_count=1)
    dispatched, box, sleep_mock = await _consult_hook(
        executor,
        retry_policy={"on": ["timeout"], "max_retries": 3},
        final_status="failed",
        error_code="node_deadline_exceeded",
        graph_json={"nodes": [{"id": "n1", "idempotent": False}], "edges": []},
    )
    assert dispatched is False
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()
    sleep_mock.assert_not_awaited()


async def test_hook_stands_down_when_exec_task_already_done():
    """The hook's own done() re-check (belt-and-braces against the cancel→
    consult race) stands down BEFORE any DB consult when the executor task
    finished — with the DB seams healthy, so the check itself is what causes
    the stand-down."""
    executor = _make_executor_mock(attempt_count=1)

    async def _already_done() -> None:
        return None

    exec_task = asyncio.create_task(_already_done())
    await exec_task
    box = WatchdogRetryOutcome()
    seams = _db_seams({"on": ["timeout"], "max_retries": 2})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        dispatched = await wr.watchdog_retry_after_policy(
            aeng=MagicMock(),
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            executor=executor,
            final_status="failed",
            error_code="node_deadline_exceeded",
            exec_task=exec_task,
            outcome=box,
        )
    assert dispatched is False
    assert box.requested is False
    executor._read_retry_attempt_state.assert_not_awaited()
    executor._fenced_pending_reset.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-693 — zombie-watchdog stalls honour the run-level retry policy
# ---------------------------------------------------------------------------


async def test_zombie_stall_redispatches_under_stall_policy():
    """{on: [stall], max_retries: 1} → the setup-grace stall re-dispatches the
    run (fenced pending-reset + budget) instead of terminal-failing it."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock()
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["stall"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await zombie_watchdog(
            MagicMock(),
            "run-1",
            "org-1",
            asyncio.Event(),
            exec_task=exec_task,
            stall_requested=stall,
            grace_seconds=0.01,
            retry_hook=hook,
        )
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_not_awaited()
    assert box.requested is True
    assert box.final_status == "stalled"
    assert box.retry_budget == 1
    executor._fenced_pending_reset.assert_awaited_once()
    assert stall.is_set()


async def test_zombie_stall_terminal_fails_when_budget_exhausted():
    executor = _make_executor_mock(attempt_count=2)
    fail = AsyncMock(return_value=True)
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["stall"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await zombie_watchdog(
            MagicMock(),
            "run-1",
            "org-1",
            asyncio.Event(),
            exec_task=exec_task,
            stall_requested=stall,
            grace_seconds=0.01,
            retry_hook=hook,
        )
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "executor_stalled"
    assert box.requested is False


async def test_zombie_stall_terminal_fails_without_stall_coverage():
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock(return_value=True)
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["timeout"], "max_retries": 2})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await zombie_watchdog(
            MagicMock(),
            "run-1",
            "org-1",
            asyncio.Event(),
            exec_task=exec_task,
            stall_requested=stall,
            grace_seconds=0.01,
            retry_hook=hook,
        )
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "executor_stalled"
    assert box.requested is False


async def test_zombie_stall_never_retries_correction_run():
    """FAR-210 gate mirrored: correction runs are never re-dispatched by the
    retry policy — the watchdog terminal-fails them."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock(return_value=True)
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["stall"], "max_retries": 2}, trigger_type="correction")
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await zombie_watchdog(
            MagicMock(),
            "run-1",
            "org-1",
            asyncio.Event(),
            exec_task=exec_task,
            stall_requested=stall,
            grace_seconds=0.01,
            retry_hook=hook,
        )
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert box.requested is False


# ---------------------------------------------------------------------------
# FAR-1088 — the zero-node failure carve-out, gated on an explicit flag
# ---------------------------------------------------------------------------


async def test_zero_node_flag_enables_failure_carve_out():
    """``zero_node=True`` (the zombie watchdog's kill is pre-first-progress —
    zero nodes by construction) lets a ``failure``-covered policy re-dispatch
    the kill, under THAT policy's budget — the same decision the
    dispatcher_reconcile cron path's carve-out makes."""
    executor = _make_executor_mock(attempt_count=1)
    dispatched, box, _sleep = await _consult_hook(
        executor,
        retry_policy={"on": ["failure"], "max_retries": 2},
        final_status="stalled",
        error_code="executor_stalled",
        zero_node=True,
    )
    assert dispatched is True
    assert box.requested is True
    assert box.retry_budget == 2
    executor._fenced_pending_reset.assert_awaited_once()


async def test_without_zero_node_flag_failure_policy_does_not_cover_stall_kill():
    """Default (``zero_node=False``): byte-for-byte old behaviour — a
    failure-covered policy does NOT cover a stalled/executor_stalled kill, so
    the watchdog terminal-fails. This is the gate that keeps the FAR-369
    node-deadline watchdog (nodes may have executed) unchanged."""
    executor = _make_executor_mock(attempt_count=1)
    dispatched, box, _sleep = await _consult_hook(
        executor,
        retry_policy={"on": ["failure"], "max_retries": 2},
        final_status="stalled",
        error_code="executor_stalled",
    )
    assert dispatched is False
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


async def test_zombie_stall_redispatches_under_failure_policy_via_zero_node_carve_out():
    """End-to-end at the zombie-watchdog site: a failure-covered policy
    re-dispatches the setup-grace stall (the hook is consulted with
    ``zero_node=True``) instead of terminal-failing it."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock()
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["failure"], "max_retries": 1})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await zombie_watchdog(
            MagicMock(),
            "run-1",
            "org-1",
            asyncio.Event(),
            exec_task=exec_task,
            stall_requested=stall,
            grace_seconds=0.01,
            retry_hook=hook,
        )
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_not_awaited()
    assert box.requested is True
    assert box.final_status == "stalled"
    assert box.retry_budget == 1
    executor._fenced_pending_reset.assert_awaited_once()
    assert stall.is_set()


async def test_deadline_kill_gets_no_failure_carve_out():
    """The FAR-369 node-deadline kill under the SAME failure-covered policy
    still terminal-fails: ``_fail_overdue_node`` never passes ``zero_node``
    (nodes may have executed on that path). Proves the carve-out is gated on
    the explicit flag, not silently applied to every watchdog kill."""
    executor = _make_executor_mock(attempt_count=1)
    fail = AsyncMock(return_value=True)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    exec_task = asyncio.create_task(asyncio.sleep(999))
    stall = asyncio.Event()
    done = asyncio.Event()
    hook, box = wr.create_watchdog_retry_hook(MagicMock(), uuid.uuid4(), uuid.uuid4(), executor, exec_task=exec_task)
    seams = _db_seams({"on": ["failure"], "max_retries": 2})
    with contextlib.ExitStack() as stack:
        for seam in seams:
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall, retry_hook=hook)
    exec_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await exec_task
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
    assert box.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


# ---------------------------------------------------------------------------
# The wrapper seam — RunRetryPolicyError re-raise (the SAQ re-enqueue)
# ---------------------------------------------------------------------------


def _watchdog_wrapper_seams(retry_policy: dict[str, Any] | None):
    seams = list(_db_seams(retry_policy))
    seams.append(patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()))
    return seams


async def test_watchdog_redispatch_reraises_run_retry_policy_error():
    """End-to-end wrapper seam: a stalled execute-path run whose policy covers
    the stall is re-dispatched — the wrapper re-raises RunRetryPolicyError (a
    NodeCancelledError subclass) so SAQ retries the job, and the run is NOT
    terminal-failed."""
    executor = _make_executor_mock(attempt_count=1)
    hang = asyncio.Event()

    async def _hang() -> None:
        # Never-set event: hangs without asyncio.sleep (the shared hook's
        # backoff sleep is patched at the asyncio boundary in the seams).
        await hang.wait()

    fail = AsyncMock()
    with contextlib.ExitStack() as stack:
        for seam in _watchdog_wrapper_seams({"on": ["stall"], "max_retries": 1}):
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution.heartbeat_loop", AsyncMock()))
        stack.enter_context(
            patch(
                "modulo.core.pipeline_execution.get_settings",
                lambda: MagicMock(saq_setup_grace_seconds=0.01, saq_node_default_timeout_seconds=1200),
            )
        )
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await run_executor_with_watchdog(
                MagicMock(),
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=MagicMock(function="execute_run"),
                execute_fn=_hang,
            )
    assert exc_info.value.status == "stalled"
    assert exc_info.value.max_retries == 1
    fail.assert_not_awaited()
    executor._fenced_pending_reset.assert_awaited_once()


async def test_watchdog_no_coverage_still_terminal_fails_and_returns_failed():
    """Regression guard: with no policy coverage the wrapper returns the
    honest ``failed`` outcome and the watchdog terminal-fails exactly as
    before."""
    executor = _make_executor_mock(attempt_count=1)
    hang = asyncio.Event()

    async def _hang() -> None:
        await hang.wait()

    fail = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        for seam in _watchdog_wrapper_seams({}):
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution.heartbeat_loop", AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution._read_run_status", AsyncMock(return_value="failed")))
        stack.enter_context(
            patch(
                "modulo.core.pipeline_execution.get_settings",
                lambda: MagicMock(saq_setup_grace_seconds=0.01, saq_node_default_timeout_seconds=1200),
            )
        )
        result = await run_executor_with_watchdog(
            MagicMock(),
            run_id=str(uuid.uuid4()),
            org_id=str(uuid.uuid4()),
            executor=executor,
            job=MagicMock(function="execute_run"),
            execute_fn=_hang,
        )
    assert result == {"status": "failed"}
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "executor_stalled"
    executor._fenced_pending_reset.assert_not_awaited()


async def test_resume_job_disables_watchdog_retry():
    """Resume jobs keep today's unconditional terminal fail: their claim
    cannot re-claim a pending run, so the hook is never built. Runtime SAQ
    job functions are FULLY-QUALIFIED ("modulo.core.saq_worker.resume_run",
    dispatch.py) — this pins the endswith match against the real shape (the
    old bare ``!= "resume_run"`` comparison never matched it and built the
    hook for resume jobs)."""
    executor = _make_executor_mock(attempt_count=1)
    hang = asyncio.Event()

    async def _hang() -> None:
        await hang.wait()

    fail = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        for seam in _watchdog_wrapper_seams({"on": ["stall"], "max_retries": 1}):
            stack.enter_context(seam)
        create_hook = stack.enter_context(
            patch("modulo.core.pipeline_engine.watchdog_retry.create_watchdog_retry_hook")
        )
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution.heartbeat_loop", AsyncMock()))
        stack.enter_context(patch("modulo.core.pipeline_execution._read_run_status", AsyncMock(return_value="failed")))
        stack.enter_context(
            patch(
                "modulo.core.pipeline_execution.get_settings",
                lambda: MagicMock(saq_setup_grace_seconds=0.01, saq_node_default_timeout_seconds=1200),
            )
        )
        result = await run_executor_with_watchdog(
            MagicMock(),
            run_id=str(uuid.uuid4()),
            org_id=str(uuid.uuid4()),
            executor=executor,
            job=MagicMock(function="modulo.core.saq_worker.resume_run"),
            execute_fn=_hang,
        )
    assert result == {"status": "failed"}
    create_hook.assert_not_called()
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "executor_stalled"


def test_watchdog_retry_enabled_for_job():
    """The guard matches the FULLY-QUALIFIED runtime job-function strings
    (dispatch.py) via the saq_hooks endswith pattern, keeps the bare
    spellings working, and fails CLOSED: job=None (resume_run's default) or
    an unknown function disables the hook. The None expectation is an
    INTENTIONAL pin reversal — the old ``!= "resume_run"`` comparison
    enabled the hook when the job was absent, which would have let a resume
    watchdog kill re-dispatch into an unreclaimable pending run."""
    assert _watchdog_retry_enabled_for_job(MagicMock(function="execute_run")) is True
    assert _watchdog_retry_enabled_for_job(MagicMock(function="modulo.core.saq_worker.execute_run")) is True
    assert _watchdog_retry_enabled_for_job(MagicMock(function="modulo.core.saq_worker.resume_run")) is False
    assert _watchdog_retry_enabled_for_job(MagicMock(function="resume_run")) is False
    assert _watchdog_retry_enabled_for_job(None) is False
    assert _watchdog_retry_enabled_for_job(MagicMock()) is False


async def test_script_mode_graph_requires_lease_probe_before_redispatch():
    """FAR-296 gate mirrored: a script-mode graph consults the stale-lease
    probe before any re-dispatch; a failed probe fails closed to the terminal
    fail."""
    executor = _make_executor_mock(attempt_count=1, probe_ok=False)
    graph_json = {"nodes": [{"id": "n1", "node_type": "sandbox_agent", "mode": "script"}], "edges": []}
    dispatched, box, _sleep = await _consult_hook(
        executor,
        retry_policy={"on": ["timeout"], "max_retries": 2},
        final_status="failed",
        error_code="node_deadline_exceeded",
        graph_json=graph_json,
    )
    executor._probe_script_lease.assert_awaited_once()
    assert dispatched is False
    assert box.requested is False


async def test_pre_node_hang_budget_bounded_by_claim_count():
    """FAR-693 fix: a PRE-node hang (hub init / graph compile — the exact hang
    sites the zombie watchdog exists for, which happen BEFORE the attempt
    increment in _prepare_and_stream) exhausts the budget too.

    node_attempt_count stays 0 on every kill, but each watchdog retry consumes
    a SAQ claim (the re-raised RunRetryPolicyError retries the job, whose
    re-claim grows claim_count), so the effective attempt count
    max(node_attempt_count, claim_count - 1) grows per retry cycle and the
    run terminal-fails once it exceeds max_retries instead of churning pending
    under the SAQ claim cap. Sequence pinned for max_retries=1: claim 1
    (effective 0) re-dispatches, claim 2 (effective 1) re-dispatches — the
    bounded slack of the prescribed counter — and claim 3 (effective 2)
    terminal-fails."""
    executor = _make_executor_mock(attempt_count=0)
    dispatched1, box1, _sleep1 = await _consult_hook(
        executor,
        retry_policy={"on": ["stall"], "max_retries": 1},
        final_status="stalled",
        error_code="executor_stalled",
        attempt_count=0,
        claim_count=1,
    )
    assert dispatched1 is True
    assert box1.requested is True

    dispatched2, box2, _sleep2 = await _consult_hook(
        executor,
        retry_policy={"on": ["stall"], "max_retries": 1},
        final_status="stalled",
        error_code="executor_stalled",
        attempt_count=0,
        claim_count=2,
    )
    assert dispatched2 is True
    assert box2.requested is True

    dispatched3, box3, _sleep3 = await _consult_hook(
        executor,
        retry_policy={"on": ["stall"], "max_retries": 1},
        final_status="stalled",
        error_code="executor_stalled",
        attempt_count=0,
        claim_count=3,
    )
    assert dispatched3 is False
    assert box3.requested is False
    executor._fenced_pending_reset.assert_not_awaited()


async def test_in_execute_budget_unchanged_by_claim_count():
    """The claim-derived budget term must stay surgical: a run that reached a
    node (claim_count 1 — a single claim) keeps the in-execute behaviour —
    the effective attempt count IS node_attempt_count."""
    executor = _make_executor_mock(attempt_count=2)
    dispatched, box, _sleep = await _consult_hook(
        executor,
        retry_policy={"on": ["timeout"], "max_retries": 1},
        final_status="failed",
        error_code="node_deadline_exceeded",
        attempt_count=2,
        claim_count=1,
    )
    assert dispatched is False
    assert box.requested is False


async def test_context_load_sets_rls_org_and_execution_context():
    """Tenancy pin (qa fix 5): the retry-context loader MUST set the RLS org
    (to the run's org) AND the execution context on its session before
    reading — deleting either call would silently widen the read (or read
    nothing under RLS) and no other test would notice."""
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    set_rls_org_mock = AsyncMock()
    set_rls_ctx_mock = AsyncMock()
    run = MagicMock()
    run.trigger_type = "manual"
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.claim_count = 2
    pipeline = MagicMock()
    pipeline.retry_policy = {"on": ["timeout"], "max_retries": 1}
    with (
        patch("modulo.db.crud.run.get_run", AsyncMock(return_value=run)),
        patch("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=pipeline)),
        patch("modulo.db.rls.set_rls_org", set_rls_org_mock),
        patch("modulo.db.rls.set_rls_execution_context", set_rls_ctx_mock),
        patch(
            "modulo.core.pipeline_engine.watchdog_retry.async_sessionmaker",
            return_value=_watchdog_retry_session_factory(),
        ),
    ):
        context = await wr._load_watchdog_retry_context(MagicMock(), run_id=run_id, org_id=org_id)
    assert context is not None
    set_rls_org_mock.assert_awaited_once_with(ANY, org_id)
    set_rls_ctx_mock.assert_awaited_once_with(ANY)
    # The claim count is read from the same row (the pre-node hang budget
    # bound consumes it) and surfaces in the loaded context.
    assert context[3] == 2


async def test_retry_context_loader_reraises_cancellation() -> None:
    """``CancelledError`` must propagate, never be swallowed into a fail-open
    ``None`` — the watchdog's own cancellation would otherwise be converted
    into a silent "do not retry" decision."""
    with (
        patch.object(
            wr,
            "_load_watchdog_retry_context",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await wr._load_retry_context_or_none(MagicMock(), run_id=uuid.uuid4(), org_id=uuid.uuid4())


async def test_retry_context_loader_fails_open_to_none_on_error(caplog: pytest.LogCaptureFixture) -> None:
    """Every NON-cancellation load failure logs and degrades to ``None`` so the
    watchdog still terminal-fails the run."""
    with (
        patch.object(
            wr,
            "_load_watchdog_retry_context",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = await wr._load_retry_context_or_none(MagicMock(), run_id=uuid.uuid4(), org_id=uuid.uuid4())
    assert result is None
    assert any(r.msg == "pipeline.watchdog_retry_context_load_failed run=%s" for r in caplog.records)


def test_resolve_schedule_state_failopen_logs_warning_and_reports(caplog: pytest.LogCaptureFixture) -> None:
    """A structurally-invalid schedule (present, but rejected) resolves to the
    hardcoded default AND emits the fail-open warning carrying the reason."""
    policy = {"backoff_schedule": {"delay_seconds": 0}}
    with caplog.at_level(logging.WARNING):
        state, effective_sleep = wr._resolve_schedule_state(policy, 1, uuid.uuid4())
    assert state == "failopen"
    assert effective_sleep > 0
    assert any(r.msg == "pipeline.watchdog_retry_schedule_fail_open" for r in caplog.records)


def test_resolve_schedule_state_valid_uses_the_run_schedule() -> None:
    """A valid run-level schedule resolves to ``valid`` (no warning)."""
    policy = {"backoff_schedule": {"delay_seconds": 30, "multiplier": 1.0}}
    state, effective_sleep = wr._resolve_schedule_state(policy, 1, uuid.uuid4())
    assert state == "valid"
    assert effective_sleep > 0


# ---------------------------------------------------------------------------
# FAR-1883 — per-pipeline queue timeout on the stale-run sweep
#
# (This section replaced the FAR-705 capacity-retry-budget coverage: the
# instance-wide 120-minute ``capacity_timeout`` TTL and its budget gate were
# removed, so the sweep's capacity branch is now the per-pipeline
# ``queue.expired`` supersession.)
# ---------------------------------------------------------------------------


def _sweep_engine(statements: list[str], params: list[dict[str, object]]):
    """Async conn double recording statements/params for the sweep branches."""

    class _AsyncResult:
        def __init__(self) -> None:
            self.rowcount = 0
            self._rows: list[Any] = []

        def all(self) -> list[Any]:
            return self._rows

    class _AsyncConn:
        # FAR-1601: faithful ``AsyncConnection`` shape — the row-lock bound
        # reads ``conn.dialect`` (a real AsyncConnection has NO ``get_bind``).
        dialect = SimpleNamespace(name="sqlite")

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        def begin(self) -> Self:
            return self

        async def execute(self, stmt: object, bind: dict[str, object] | None = None) -> _AsyncResult:
            statements.append(str(stmt))
            params.append(bind or {})
            return _AsyncResult()

    class _AsyncEngine:
        def connect(self) -> _AsyncConn:
            return _AsyncConn()

    return _AsyncEngine()


def _compile_sql(stmt: object) -> str:
    return str(stmt)


async def _run_sweep_capture_queue_expired_stmt() -> tuple[str, dict[str, object]]:
    """Run one stale sweep; return the compiled queue-expiry UPDATE + bind."""
    statements: list[str] = []
    params: list[dict[str, object]] = []
    engine = _sweep_engine(statements, params)
    never, expired, lost = await _sweep_org_stale_runs(
        engine.connect(),
        org_id=uuid.uuid4(),
        nd_window=300,
        wl_window=600,
        stranded_rows=[],
        terminalised_run_ids=[],
        queue_expired_run_ids=[],
    )
    assert never == 0
    assert expired == 0
    assert lost == 0
    expired_stmts = [(s, p) for s, p in zip(statements, params, strict=True) if "error_code = 'queue.expired'" in s]
    assert len(expired_stmts) == 1
    sql, bound = expired_stmts[0]
    return _compile_sql(sql), bound


async def test_sweep_queue_expiry_is_per_pipeline_and_capacity_only():
    """FAR-1883: the queue timeout is a PER-PIPELINE column, not an
    instance-wide TTL, and applies ONLY to capacity-queued rows (pending + a
    capacity marker). It is anchored on created_at ("time queued") - never a
    heartbeat or dispatch timestamp - so a pending->running->pending demote
    bounce cannot reset the clock."""
    sql, bound = await _run_sweep_capture_queue_expired_stmt()
    assert "FROM pipelines p" in sql
    assert "runs.pipeline_id = p.id" in sql
    assert "p.queue_timeout_seconds IS NOT NULL" in sql
    assert "runs.created_at < now() - (p.queue_timeout_seconds * interval '1 second')" in sql
    assert "runs.status = 'pending'" in sql
    assert "runs.error_code IN ('org_capacity_limited', 'pipeline_capacity')" in sql
    # One TTL per state class: the expiry must not touch never-dispatched or
    # enqueue-failed rows.
    assert "never_dispatched" not in sql
    assert "enqueue_failed" not in sql
    assert "heartbeat_at" not in sql
    assert "dispatched_at" not in sql
    assert set(bound) == {"oid", "detail"}


async def test_sweep_queue_expiry_is_a_supersession_not_a_failure():
    """FAR-1883: expiry ENDS the run as a supersession - status ``rejected``
    with the distinct ``queue.expired`` code - never ``failed``, so it cannot
    enter failure counts/thresholds or page anyone, and it does not reuse
    ``capacity_timeout``'s retired terminal-FAIL meaning."""
    sql, bound = await _run_sweep_capture_queue_expired_stmt()
    assert "SET status = 'rejected'" in sql
    assert "error_code = 'queue.expired'" in sql
    assert "status = 'failed'" not in sql
    assert "capacity_timeout" not in sql
    assert bound["detail"] == "Waited in the capacity queue past the pipeline's queue_timeout."


async def test_sweep_never_dispatched_branch_keeps_its_own_window():
    """FAR-1883: one TTL per state class - the never-dispatched branch still
    bounds on its own ``nd_window`` (300s default) and must not read the new
    per-pipeline ``queue_timeout_seconds``."""
    statements: list[str] = []
    params: list[dict[str, object]] = []
    engine = _sweep_engine(statements, params)
    await _sweep_org_stale_runs(
        engine.connect(),
        org_id=uuid.uuid4(),
        nd_window=300,
        wl_window=600,
        stranded_rows=[],
        terminalised_run_ids=[],
        queue_expired_run_ids=[],
    )
    never = [(s, p) for s, p in zip(statements, params, strict=True) if "error_code = 'never_dispatched'" in s]
    assert len(never) == 1
    never_sql, never_bound = never[0]
    assert ":nd_window" in never_sql
    assert "queue_timeout_seconds" not in never_sql
    assert never_bound["nd_window"] == 300


def test_capacity_retry_budget_setting_default_and_override(monkeypatch: pytest.MonkeyPatch):
    """FAR-1883: the FAR-705 budget knob is RETIRED (nothing reads it now that
    the capacity_timeout TTL is gone), but the field is retained so an
    existing deployment's env value is accepted rather than rejected; its
    default and env override still parse."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 32)
    monkeypatch.setenv("FERNET_KEY", "a" * 32)
    monkeypatch.delenv("SAQ_CAPACITY_RETRY_BUDGET", raising=False)
    from modulo.settings import Settings

    assert Settings().saq_capacity_retry_budget == 3
    monkeypatch.setenv("SAQ_CAPACITY_RETRY_BUDGET", "7")
    assert Settings().saq_capacity_retry_budget == 7


# ---------------------------------------------------------------------------
# FAR-1914 B2 — the deadline kill waits for the executor's cancel cleanup
# ---------------------------------------------------------------------------


async def test_deadline_kill_waits_for_cancel_cleanup_before_terminalising():
    """The cancelled executor's cleanup must run BEFORE the terminal write.

    The cleanup is what persists the delivery/triage marker (including the
    ``pr_url`` a run opened seconds before it was killed). Without the bounded
    settle wait the terminal write races it — the c4222006 shape, recorded as a
    bare failure even though the PR was created.
    """
    order: list[str] = []

    async def _exec_body() -> None:
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            order.append("cleanup")
            raise

    async def _fail(*args: Any, **kwargs: Any) -> bool:
        order.append("terminal")
        return True

    exec_task = asyncio.create_task(_exec_body())
    # Let the executor task actually START before the watchdog cancels it. A
    # never-scheduled task is cancelled before its body runs, so its
    # cancellation cleanup would never execute — that is a test artefact, not
    # the production shape (the node has been in flight for its full deadline).
    await asyncio.sleep(0)
    deadlines = {"n1": (time.monotonic() - 1.0, 300)}
    done = asyncio.Event()
    stall = asyncio.Event()
    with (
        patch("modulo.core.pipeline_execution.fail_run_terminal", _fail),
        patch("modulo.core.pipeline_execution._record_node_deadline_watchdog_firing", new=AsyncMock()),
        patch("modulo.core.pipeline_execution._record_derived_deadline_cost", new=AsyncMock()),
    ):
        await _fail_overdue_node(MagicMock(), "run-1", "org-1", deadlines, exec_task, done, stall)
    if not exec_task.done():
        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task
    assert order == ["cleanup", "terminal"]


# ---------------------------------------------------------------------------
# FAR-1914 C2 — the salvage runs AFTER the executor task settles
# ---------------------------------------------------------------------------


async def _wrapper_outcome(
    *,
    run_id: str,
    org_id: str,
    read_status: str,
    salvage: AsyncMock,
    fail: AsyncMock,
):
    """Drive the watchdog wrapper to its outcome with the run row re-read as
    ``read_status``; return the wrapper's outcome dict (the salvage seam is
    ``salvage``, the terminal-fail seam is ``fail``)."""
    executor = _make_executor_mock(attempt_count=1)
    hang = asyncio.Event()

    async def _hang() -> None:
        await hang.wait()

    with contextlib.ExitStack() as stack:
        for seam in _watchdog_wrapper_seams({}):
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution.heartbeat_loop", AsyncMock()))
        stack.enter_context(
            patch("modulo.core.pipeline_execution._read_run_status", AsyncMock(return_value=read_status))
        )
        stack.enter_context(patch("modulo.core.pipeline_execution._salvage_cancelled_run_cost", salvage))
        stack.enter_context(
            patch(
                "modulo.core.pipeline_execution.get_settings",
                lambda: MagicMock(saq_setup_grace_seconds=0.01, saq_node_default_timeout_seconds=1200),
            )
        )
        return await run_executor_with_watchdog(
            MagicMock(),
            run_id=run_id,
            org_id=org_id,
            executor=executor,
            job=MagicMock(function="execute_run"),
            execute_fn=_hang,
        )


async def test_settled_cancelled_run_is_salvaged():
    """FAR-1914 C2 prove-the-fix: when the executor task has settled and the
    run row re-reads ``cancelled``, the wrapper hands the SAME engine/run/org
    identity it resolved to ``_salvage_cancelled_run_cost`` — the shakeout
    gate observed the salvage missing entirely because the wiring did not
    exist; delete the ``_salvage_cancelled_run_cost`` call in
    ``run_executor_with_watchdog`` and this fails with ``salvage`` never
    awaited.

    Shipped outcome mapping: ``_executor_status_response`` has no ``cancelled``
    arm, so a cancelled run still reports the fail-shaped outcome dict — the
    salvage attribution is the point, not the wrapper's summary dict.
    """
    salvage = AsyncMock()
    fail = AsyncMock(return_value=True)
    run_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    result = await _wrapper_outcome(
        run_id=run_id,
        org_id=org_id,
        read_status="cancelled",
        salvage=salvage,
        fail=fail,
    )

    salvage.assert_awaited_once()
    assert salvage.await_args.args == (ANY, run_id, org_id)
    assert result == {"status": "failed"}


async def test_failed_run_status_is_not_salvaged():
    """The gate is on the re-read status: a watchdog terminal-fail lands here
    with the run already ``failed`` and its deadline cost owned by
    ``_record_derived_deadline_cost`` — no salvage may fire, and the
    terminal-fail itself still runs."""
    salvage = AsyncMock()
    fail = AsyncMock(return_value=True)
    run_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    result = await _wrapper_outcome(
        run_id=run_id,
        org_id=org_id,
        read_status="failed",
        salvage=salvage,
        fail=fail,
    )

    salvage.assert_not_awaited()
    fail.assert_awaited_once()
    assert result == {"status": "failed"}


async def test_salvage_cancelled_run_cost_rereises_cancelled_error() -> None:
    """FAR-1914 C2: worker shutdown is not ours to swallow.

    The post-settle salvage is fail-open for ordinary failures (the run is
    already terminal), but a ``CancelledError`` from the compensating write
    must propagate so the SAQ job is actually cancelled.
    """
    with (
        patch(
            "modulo.core.pipeline_execution.async_sessionmaker",
            side_effect=asyncio.CancelledError(),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _salvage_cancelled_run_cost(MagicMock(), "run-1", "org-1")


async def test_salvage_cancelled_run_cost_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    """An ordinary write failure must never turn a terminal cancelled run into
    a raised error — the run stays cancelled and the failure is logged."""
    with (
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_execution"),
        patch(
            "modulo.core.pipeline_execution.async_sessionmaker",
            side_effect=RuntimeError("db down"),
        ),
    ):
        await _salvage_cancelled_run_cost(MagicMock(), "run-1", "org-1")

    assert "pipeline_execution.cancel_cost_salvage_failed" in caplog.text


# ---------------------------------------------------------------------------
# FAR-1914 M1 — the settle bound covers the whole retention budget
# ---------------------------------------------------------------------------


def test_executor_cancel_settle_covers_the_retention_worst_case():
    """FAR-1914 M1 prove-the-fix: the deadline watchdog's pre-terminal wait must
    cover the FULL bounded cancel-retention budget.

    Worst case is four bounded 5s steps — first drain + second/final drain +
    log-tail fetch + marker persist — plus the 0.5s delivery grace = 20.5s.
    The pre-rework 12s under-covered it, so a delivered-then-killed run could
    be terminalised BEFORE its marker (and therefore its cost/delivery
    evidence) landed. 25-30s is the accepted headroom band.
    """
    from modulo.core.pipeline_engine.node_runner import (
        _CANCEL_DELIVERY_GRACE_SECONDS,
        _IDEMPOTENCY_GATE_CANCEL_PERSIST_TIMEOUT,
    )
    from modulo.core.pipeline_execution import _EXECUTOR_CANCEL_SETTLE_SECONDS

    retention_worst_case = 4 * _IDEMPOTENCY_GATE_CANCEL_PERSIST_TIMEOUT + _CANCEL_DELIVERY_GRACE_SECONDS
    assert retention_worst_case == 20.5
    assert retention_worst_case <= _EXECUTOR_CANCEL_SETTLE_SECONDS
    assert _EXECUTOR_CANCEL_SETTLE_SECONDS <= 30.0
