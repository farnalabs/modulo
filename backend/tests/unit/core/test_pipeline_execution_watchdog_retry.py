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
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import watchdog_retry as wr
from modulo.core.pipeline_engine.executor import RunRetryPolicyError
from modulo.core.pipeline_engine.watchdog_retry import WatchdogRetryOutcome
from modulo.core.pipeline_execution import (
    _fail_overdue_node,
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


# ---------------------------------------------------------------------------
# FAR-705 — per-run capacity-retry budget on the stale-run sweep
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


async def _run_sweep_capture_capacity_stmt(budget: int) -> tuple[str, dict[str, object]]:
    """Run one stale sweep with the given capacity budget; return the compiled
    capacity_timeout UPDATE statement + its bound params."""
    statements: list[str] = []
    params: list[dict[str, object]] = []
    engine = _sweep_engine(statements, params)
    with patch(
        "modulo.core.pipeline_execution.get_settings",
        lambda: MagicMock(saq_capacity_retry_budget=budget),
    ):
        never, capacity, lost = await _sweep_org_stale_runs(
            engine.connect(),
            org_id=uuid.uuid4(),
            nd_window=300,
            wl_window=600,
            stranded_rows=[],
            terminalised_run_ids=[],
        )
    assert never == 0
    assert capacity == 0
    assert lost == 0
    capacity_stmts = [(s, p) for s, p in zip(statements, params, strict=True) if "capacity_timeout" in s]
    assert len(capacity_stmts) == 1
    sql, bound = capacity_stmts[0]
    return _compile_sql(sql), bound


@pytest.mark.parametrize("budget", [0, 3, 7])
async def test_sweep_capacity_timeout_guards_on_capacity_retry_budget(budget: int):
    """FAR-705: the capacity_timeout terminalisation gates CLAIMED runs on the
    per-run capacity-retry budget — the loop-cap that keeps the retryable
    capacity.* class from resurrecting a run forever — while NEVER-CLAIMED
    rows (claim_count = 0) terminalize at the TTL via their own disjunct and
    the claim-heartbeat staleness backstop covers a stale last claim."""
    sql, bound = await _run_sweep_capture_capacity_stmt(budget)
    assert (
        "AND (claim_count = 0 "
        "OR claim_count > :capacity_retry_budget "
        "OR heartbeat_at < now() - (:ttl * interval '1 minute'))" in sql
    )
    assert bound["capacity_retry_budget"] == budget


@pytest.mark.parametrize("budget", [0, 3, 20])
async def test_sweep_capacity_backstop_restores_termination_guarantee(budget: int):
    """FAR-705 fix: the budget gate alone can never terminalize a budget >=
    SAQ_RUN_CLAIM_CAP config (claims are refused at claim_count >= the cap,
    so the comparison is unsatisfiable for budget=20), and a heartbeat-age
    test can never terminalize a NEVER-CLAIMED row — the dominant capacity
    path is dispatch-time deferral WITHOUT a claim, so claim_count stays 0,
    and the stranded-refresh UPDATE restamps the row's heartbeat while it is
    inside the TTL window, so past the crossing the refreshed heartbeat would
    age a further full TTL (terminalising at ~2xTTL with the budget having no
    effect). The ``claim_count = 0`` disjunct fires at the TTL crossing for
    never-claimed rows, and the heartbeat-staleness backstop restores the
    guarantee for CLAIMED rows: a row whose last claim heartbeat is itself
    older than the TTL terminal-fails regardless of claim_count. The backstop
    age is the TTL itself (NOT a larger multiple)."""
    sql, bound = await _run_sweep_capture_capacity_stmt(budget)
    assert "claim_count = 0" in sql
    assert "heartbeat_at IS NULL" not in sql
    assert "OR heartbeat_at < now() - (:ttl * interval '1 minute'))" in sql
    assert "hard_cap_ttl" not in bound


async def test_sweep_capacity_within_budget_row_stays_pending():
    """FAR-705 fix: a within-budget CLAIMED row with a claim heartbeat inside
    the TTL window matches NONE of the terminalisation disjuncts — the
    claim_count = 0 disjunct requires no claim at all, the budget disjunct is
    strict (a claim_count == budget row does not fire it), and the age
    disjunct requires the last claim heartbeat to be itself older than the
    TTL — so it keeps the pending/re-dispatch behaviour."""
    sql, bound = await _run_sweep_capture_capacity_stmt(3)
    assert "claim_count = 0" in sql
    assert "claim_count > :capacity_retry_budget" in sql
    assert "claim_count >= :capacity_retry_budget" not in sql
    assert "heartbeat_at IS NULL" not in sql
    assert "heartbeat_at < now() - (:ttl * interval '1 minute'))" in sql
    assert bound["capacity_retry_budget"] == 3


def test_capacity_retry_budget_setting_default_and_override(monkeypatch: pytest.MonkeyPatch):
    """The new setting defaults to 3 (the FAR-705 budget) and honours its env
    override."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 32)
    monkeypatch.setenv("FERNET_KEY", "a" * 32)
    monkeypatch.delenv("SAQ_CAPACITY_RETRY_BUDGET", raising=False)
    from modulo.settings import Settings

    assert Settings().saq_capacity_retry_budget == 3
    monkeypatch.setenv("SAQ_CAPACITY_RETRY_BUDGET", "7")
    assert Settings().saq_capacity_retry_budget == 7
