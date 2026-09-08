"""FAR-690 — the ONE shared watchdog-kill retry decision + re-dispatch.

The FAR-369 absolute node-deadline watchdog used to terminal-fail the run
DIRECTLY (``fail_run_terminal``) after cancelling ``execute()`` — the kill
never reached the run-level retry decision (``_retry_after_policy``), so a run
whose pipeline policy covered the outcome (``timeout``) was never
re-dispatched. This module wires the watchdog kill into the SAME mechanism the
in-execute retry path uses:

1. DECISION: the shared ``_retry_after_policy`` pure function — the exact
   matcher the in-execute path consults (same events, same legacy-code alias
   table, same never-retryable script-mode / hang-death exclusions).
2. GATES: the same ``_retry_policy_applies`` gates (correction runs excluded —
   FAR-210; non-idempotent graphs excluded — FAR-295) and the same
   ``_can_retry_after_policy`` bookkeeping (attempt budget vs ``max_retries``,
   superseded-claim check, FAR-296 script-mode stale-lease probe).
3. RE-DISPATCH: the same fenced pending-reset (``_fenced_pending_reset``) and
   the same SAQ re-enqueue — the wrapper (``run_executor_with_watchdog``)
   re-raises ``RunRetryPolicyError`` (a ``NodeCancelledError`` subclass) so
   SAQ retries the job and ``claim_run_async`` re-claims the pending run —
   with the same jittered capped backoff (``_retry_backoff_seconds`` + the
   run's ``backoff_schedule``) slept BEFORE the re-raise, mirroring
   ``_redispatch_after_policy``.

There is exactly ONE mechanism: the watchdogs call the hook this module
builds, which reuses the live executor instance's own helpers; the wrapper
then raises the SAME ``RunRetryPolicyError`` the in-execute path raises. No
parallel reset / re-enqueue path exists.

Resume runs are EXCLUDED (hook disabled for the ``resume_run`` SAQ job
function): ``claim_resume_run_async`` cannot re-claim a run reset to
``pending`` (it claims awaiting_human/claimed/hitl_parked/stale-running
only), so a watchdog retry there would strand the run until the durable
sweeps recover it. Resume runs keep today's unconditional terminal fail.

Fail-closed by design: any hook failure (DB error, missing executor, lost
claim token) returns ``False`` and the watchdog terminal-fails the run
exactly as before — a retry decision must never prevent a run from reaching
a terminal state.

Known residual race (bounded): between the hook's ``exec_task.done()``
re-check and the fenced pending-reset the executor task could complete
concurrently. The fenced reset is a conditional
``WHERE claim_token=:tok AND status='running'`` UPDATE — a completed or
otherwise no-longer-running run never matches (rowcount 0 → the hook stands
down and the watchdog's token/status-guarded ``fail_run_terminal`` is a
no-op), so there is never a double-fail and never a retry of a finished
run. A run reset to ``pending`` whose job then dies without re-raising is
recovered by ``dispatcher_reconcile`` / ``stale_run_recovery_sweep`` (the
durable backstop).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.pipeline_engine import retry_compensation as rc
from modulo.core.pipeline_engine.executor import (
    _can_retry_after_policy,
    _graph_has_script_mode,
    _graph_is_idempotent,
    _record_retry_redispatch,
    _retry_after_policy,
    _retry_backoff_seconds,
)

_log = logging.getLogger(__name__)

WatchdogRetryHook = Callable[[str, str], Awaitable[bool]]

# The resume SAQ job function name — the one path whose watchdog kills keep
# today's unconditional terminal fail (see module docstring).
_RESUME_JOB_FUNCTION = "resume_run"


@dataclass
class WatchdogRetryOutcome:
    """Result box shared by the watchdog hook and the wrapper.

    ``requested=True`` means the hook performed the fenced pending-reset and
    the wrapper must raise ``RunRetryPolicyError`` (so SAQ re-dispatches the
    job) instead of standing down. The wrapper awaits the watchdogs to
    completion before reading this box, so the flags are always settled.
    """

    requested: bool = False
    final_status: str = ""
    retry_budget: int = 0


def watchdog_retry_enabled_for_job(job: Any) -> bool:
    """Watchdog kills re-dispatch only on the execute path (not ``resume_run``)."""
    return getattr(job, "function", None) != _RESUME_JOB_FUNCTION


def create_watchdog_retry_hook(
    aeng: AsyncEngine,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    executor: Any,
    exec_task: asyncio.Task[Any] | None = None,
) -> tuple[WatchdogRetryHook, WatchdogRetryOutcome]:
    """Build the watchdog retry hook + its outcome box for one run.

    The hook signature is ``(final_status, error_code) -> bool``: ``True`` when
    the run was re-dispatched (the watchdog must NOT terminal-fail), ``False``
    when it must terminal-fail exactly as before.
    """
    box = WatchdogRetryOutcome()

    async def _hook(final_status: str, error_code: str) -> bool:
        return await watchdog_retry_after_policy(
            aeng=aeng,
            run_id=run_id,
            org_id=org_id,
            executor=executor,
            exec_task=exec_task,
            final_status=final_status,
            error_code=error_code,
            outcome=box,
        )

    return _hook, box


async def watchdog_retry_after_policy(
    *,
    aeng: AsyncEngine,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    executor: Any,
    final_status: str,
    error_code: str,
    exec_task: asyncio.Task[Any] | None = None,
    outcome: WatchdogRetryOutcome | None = None,
) -> bool:
    """Consult the run's retry_policy for a watchdog kill; re-dispatch if allowed.

    Returns ``True`` when the re-dispatch was performed: the fenced
    pending-reset CONFIRMED (rowcount 1), the backoff was slept, and the
    outcome box is set — the caller must NOT terminal-fail and the wrapper
    must re-raise ``RunRetryPolicyError`` so SAQ retries the job. Returns
    ``False`` when the watchdog must terminal-fail exactly as before (no
    policy coverage, exhausted budget, correction run, non-idempotent graph,
    superseded claim, stale script lease, lost fence, or any load failure —
    fail closed).
    """
    box = outcome if outcome is not None else WatchdogRetryOutcome()
    if executor is None:
        return False
    # The fenced pending-reset is guarded by the executor's captured claim
    # token — without one there is nothing to fence (the run cannot be safely
    # demoted), so stand down to the terminal fail.
    claim_token = getattr(executor, "_claim_token", None)
    if not claim_token:
        return False
    # Belt-and-braces: if the executor task finished (or finished while the
    # watchdog was cancelling it) the run reached its own outcome — never
    # re-dispatch a finished run. The fenced reset's status='running' guard
    # is the authoritative protection; this check only avoids the DB roundtrip.
    if exec_task is not None and exec_task.done():
        return False

    try:
        context = await _load_watchdog_retry_context(aeng, run_id=run_id, org_id=org_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline.watchdog_retry_context_load_failed run=%s", run_id, exc_info=True)
        return False
    if context is None:
        return False
    policy, trigger_type, graph_json = context

    # THE shared decision — the exact pure function the in-execute path uses.
    # The raw watchdog codes resolve through the shared map_legacy_code alias
    # table (node_deadline_exceeded -> node.deadline_exceeded -> "timeout";
    # executor_stalled -> agent.stall -> "stall"), so watchdog kills match the
    # same events an in-execute outcome of the same shape would match, and the
    # never-retryable exclusions (script-mode terminal codes, hang deaths)
    # apply identically.
    retry_budget = _retry_after_policy(policy, final_status, error_code)
    if retry_budget is None:
        return False
    # Same gates as _retry_policy_applies: FAR-210 correction runs and FAR-295
    # non-idempotent graphs are never re-dispatched (re-running would
    # re-execute a side effect / a correction's budget is owned elsewhere).
    is_correction_run = (trigger_type or "") == "correction"
    graph_idempotent = _graph_is_idempotent(graph_json)
    if is_correction_run or not graph_idempotent:
        return False
    # Same bookkeeping as the in-execute path: attempt budget, superseded
    # claim, FAR-296 script-mode stale-lease probe.
    node_attempt_count, current_claim_token = await executor._read_retry_attempt_state(org_id, run_id)
    superseded = claim_token is not None and current_claim_token is not None and current_claim_token != claim_token
    script_retry_probe_ok = True
    if _graph_has_script_mode(graph_json):
        script_retry_probe_ok = await executor._probe_script_lease(run_id=run_id, org_id=org_id)
    if not _can_retry_after_policy(node_attempt_count, retry_budget, superseded, script_retry_probe_ok):
        return False

    # FAR-525: resolve the run-level backoff_schedule EARLY — the pure
    # computation happens BEFORE the fenced pending-reset so a resolver
    # defect can never strand a run that was already reset (total fail-open
    # to the hardcoded default schedule), mirroring _redispatch_after_policy.
    schedule_present, schedule_delay, schedule_multiplier, schedule_reason = rc.resolve_backoff_schedule(policy)
    if schedule_reason is not None:
        _log.warning(
            "pipeline.watchdog_retry_schedule_fail_open",
            extra={"run_id": str(run_id), "reason": schedule_reason},
        )
    schedule_state = "absent" if not schedule_present else ("failopen" if schedule_reason else "valid")
    effective_sleep = _retry_backoff_seconds(node_attempt_count, base=schedule_delay, multiplier=schedule_multiplier)
    reset_rowcount = await executor._fenced_pending_reset(run_id=run_id, org_id=org_id)
    if not reset_rowcount:
        # Fence lost — a successor owns the run (or it just reached a terminal
        # state). Stand down: the watchdog's token/status-guarded
        # fail_run_terminal is a no-op either way.
        return False
    # FAR-525 observability: count only CONFIRMED resets (same seam as the
    # in-execute path — same counter, same reason vocabulary).
    _record_retry_redispatch(reason=final_status, schedule_state=schedule_state, delay_seconds=effective_sleep)
    _log.warning(
        "pipeline.watchdog_retry_redispatch run=%s status=%s error_code=%s attempt=%s budget=%s "
        "sleep_seconds=%.3f schedule_state=%s",
        run_id,
        final_status,
        error_code,
        node_attempt_count,
        retry_budget,
        effective_sleep,
        schedule_state,
    )
    # Same backoff-before-re-enqueue shape as _redispatch_after_policy: the
    # delay is slept BEFORE the SAQ re-dispatch (the wrapper's re-raise) so a
    # repeated watchdog kill never re-fires back-to-back.
    await asyncio.sleep(effective_sleep)
    box.requested = True
    box.final_status = final_status
    box.retry_budget = retry_budget
    return True


async def _load_watchdog_retry_context(
    aeng: AsyncEngine,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[dict[str, Any], str, dict[str, Any] | None] | None:
    """Load the retry-decision inputs for one run (RLS-scoped, one session).

    Returns ``(retry_policy, trigger_type, graph_json)`` or ``None`` when the
    run or its pipeline is missing. Mirrors ``_capture_execution_scalars``:
    the policy defaults to ``{}`` (no retry) when absent/malformed.
    """
    from modulo.db.crud.pipeline import get_pipeline
    from modulo.db.crud.run import get_run
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        run = await get_run(session, run_id)
        if run is None:
            return None
        pipeline = await get_pipeline(session, run.pipeline_id)
        if pipeline is None:
            return None
        snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
        snapshot = snapshot_result.scalar_one_or_none()
        graph_json = getattr(snapshot, "graph_json", None)

    raw_policy = getattr(pipeline, "retry_policy", None)
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    trigger_type = str(getattr(run, "trigger_type", "") or "")
    return policy, trigger_type, graph_json if isinstance(graph_json, dict) else None
