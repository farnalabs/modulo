"""Unit tests for the shared watchdog-kill retry mechanism (FAR-690 / FAR-693)

FAR-690: the FAR-369 absolute node-deadline watchdog and FAR-693: the
executor-level zombie watchdog used to terminal-fail runs DIRECTLY, bypassing
the run-level retry decision. Both now consult the
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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import watchdog_retry as wr
from modulo.core.pipeline_engine.executor import RunRetryPolicyError
from modulo.core.pipeline_engine.watchdog_retry import WatchdogRetryOutcome
from modulo.core.pipeline_execution import (
    _fail_overdue_node,
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
    retry_policy: dict[str, Any] | None, *, trigger_type: str = "manual", graph_json: dict[str, Any] | None = None
):
    """Patch the DB seams the shared hook's context loader resolves lazily."""
    run = MagicMock()
    run.trigger_type = trigger_type
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
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
) -> tuple[bool, WatchdogRetryOutcome, AsyncMock]:
    """Run the REAL shared hook once with only DB seams mocked."""
    executor._read_retry_attempt_state = AsyncMock(return_value=(attempt_count, executor._claim_token))
    executor._fenced_pending_reset = AsyncMock(return_value=reset_rowcount)
    exec_task = asyncio.create_task(asyncio.sleep(999))
    box = WatchdogRetryOutcome()
    seams = _db_seams(retry_policy, trigger_type=trigger_type, graph_json=graph_json)
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
    cannot re-claim a pending run, so the hook is never built."""
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
            job=MagicMock(function="resume_run"),
            execute_fn=_hang,
        )
    assert result == {"status": "failed"}
    create_hook.assert_not_called()
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "executor_stalled"


def test_watchdog_retry_enabled_for_job():
    assert _watchdog_retry_enabled_for_job(MagicMock(function="execute_run")) is True
    assert _watchdog_retry_enabled_for_job(None) is True
    assert _watchdog_retry_enabled_for_job(MagicMock(function="resume_run")) is False


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
