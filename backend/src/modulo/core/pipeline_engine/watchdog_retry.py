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
   ``_redispatch_after_policy``. The budget counter is
   ``max(node_attempt_count, claim_count - 1)``: node attempts alone cannot
   bound a PRE-node hang (hub init / graph compile happen BEFORE the attempt
   increment), but each watchdog retry consumes a SAQ claim, so the
   claim-derived term grows one per retry cycle and bounds every hang site.

There is exactly ONE mechanism: the watchdogs call the hook this module
builds, which reuses the live executor instance's own helpers; the wrapper
then raises the SAME ``RunRetryPolicyError`` the in-execute path raises. No
parallel reset / re-enqueue path exists.

Resume runs are EXCLUDED (hook enabled for the ``execute_run`` SAQ job
function ONLY): ``claim_resume_run_async`` cannot re-claim a run reset to
``pending`` (it claims awaiting_human/claimed/hitl_parked/stale-running
only), so a watchdog retry there would strand the run until the durable
sweeps recover it. Resume runs keep today's unconditional terminal fail.
Runtime SAQ job functions are FULLY-QUALIFIED (``modulo.core.saq_worker.
resume_run``, see ``dispatch.py``), so the check matches with the same
``endswith`` pattern ``error_tracking.saq_hooks`` uses and fails CLOSED:
``job=None`` (``resume_run``'s default) or an unknown function shape
disables the hook.

FAR-2053 — the DELIVERY VETO (opt-in per call site, ``veto_if_delivered``):
a re-dispatch re-executes the graph from the start, so a run whose marker rows
already record a completed delivery (``classify._any_marker_delivery_done`` —
the SAME FAR-228 ``delivery_done`` sentinel the node-level gate reads) must not
be re-dispatched: the side effect already happened and re-running can produce
it twice. The stall/zombie watchdog passes the flag; the node-deadline
watchdog does not (its behaviour is unchanged). The veto reads through the
SAME repo reader the executor's gate uses (``read_run_markers``), so this path
can never disagree with it about what the run recorded — there is no second
definition of "delivered" and no new bookkeeping.

Fail-closed by design: any hook failure (DB error, missing executor, lost
claim token) returns ``False`` and the watchdog terminal-fails the run
exactly as before — a retry decision must never prevent a run from reaching
a terminal state. An UNREADABLE delivery record is treated as "delivered"
(the veto engages): an unreadable fact must never license a re-dispatch.

Known residual race (bounded): between the hook's ``exec_task.done()``
re-check and the fenced pending-reset the executor task could complete
concurrently. The fenced reset is a conditional
``WHERE claim_token=:tok AND status='running'`` UPDATE — a completed or
otherwise no-longer-running run never matches (rowcount 0 → the hook stands
down and the watchdog's token/status-guarded ``fail_run_terminal`` is a
no-op), so there is never a double-fail and never a retry of a finished
run. A run reset to ``pending`` whose job then dies without re-raising is
recovered by ``dispatcher_reconcile``'s pending re-enqueue leg (the durable
backstop, FAR-1649 qa iteration 3): it re-dispatches the
``pending + dispatcher='saq' + dispatched_at < reenqueue_window`` row once
its SAQ job is genuinely gone (the per-row ``q.job()`` gate skips the row
while a retry job still exists). That recovery is safe precisely because
EVERY fenced-reset call site — including this module's — is gated on
``_graph_is_idempotent``, so the row it re-claims carries a graph already
vetted safe to re-run (FAR-295). ``stale_run_recovery_sweep`` does NOT
cover this row shape — its branches are scoped to legacy dispatchers
(``dispatcher != 'saq'``), to a capacity ``error_code`` the reset NULLs, or
to ``status='running'`` — so it is not a backstop for a fenced-reset row
and must not be named as one.
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
    _attempt_consumed_node_budget,
    _attempt_elapsed_seconds,
    _can_retry_after_policy,
    _graph_has_script_mode,
    _graph_is_idempotent,
    _record_retry_redispatch,
    _retry_after_policy,
    _retry_backoff_seconds,
)

_log = logging.getLogger(__name__)

# The hook takes ``(final_status, error_code)`` plus TWO optional keyword-only
# flags, both opt-in per call site:
#
# * ``zero_node`` (FAR-1088) — only the in-process ZOMBIE watchdog passes it;
#   its kill fires pre-first-progress, so the run executed ZERO nodes by
#   construction and the zero-node failure carve-out is safe there.
# * ``veto_if_delivered`` (FAR-2053) — the delivery veto: refuse to re-dispatch
#   a run whose markers already record a delivery. The ZOMBIE watchdog passes
#   it; the FAR-369 node-deadline watchdog (and the in-execute retry path) do
#   not, so their behaviour is unchanged.
#
# ``Callable[..., ...]`` because the kwargs are optional and call sites without
# them must keep working.
WatchdogRetryHook = Callable[..., Awaitable[bool]]

# Runtime SAQ job function names are FULLY-QUALIFIED ("modulo.core.saq_worker.execute_run"
# / "...resume_run" — see dispatch.py SAQ_*_RUN_FUNCTION and the registered
# worker functions in core/saq_worker.py). Match with the same ``endswith``
# pattern error_tracking.saq_hooks._classify uses — a bare ``!= "resume_run"``
# comparison never matches a fully-qualified name, which would build the hook
# for resume jobs (where the re-claim is impossible) instead of excluding them.
_EXECUTE_JOB_FUNCTION_SUFFIX = "execute_run"


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
    """Watchdog kills re-dispatch only on the execute path (not ``resume_run``).

    Fails CLOSED: ``job=None`` (``resume_run``'s default when it forwards the
    kwarg it never received) or an unknown function shape disables the hook —
    only the (fully-qualified) execute job function enables it. The resume
    path must keep today's unconditional terminal fail, and an unrecognised
    job shape must never be assumed retryable.
    """
    function_name = getattr(job, "function", None)
    return isinstance(function_name, str) and function_name.endswith(_EXECUTE_JOB_FUNCTION_SUFFIX)


def create_watchdog_retry_hook(
    aeng: AsyncEngine,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    executor: Any,
    exec_task: asyncio.Task[Any] | None = None,
) -> tuple[WatchdogRetryHook, WatchdogRetryOutcome]:
    """Build the watchdog retry hook + its outcome box for one run.

    The hook signature is ``(final_status, error_code, *, zero_node=False,
    veto_if_delivered=False) -> bool``: ``True`` when the run was re-dispatched
    (the watchdog must NOT terminal-fail), ``False`` when it must terminal-fail
    exactly as before. ``zero_node`` opts the caller into the FAR-1088 zero-node
    failure carve-out; ``veto_if_delivered`` opts it into the FAR-2053 delivery
    veto. Both default off, so every existing caller's behaviour is unchanged.
    """
    box = WatchdogRetryOutcome()

    async def _hook(
        final_status: str,
        error_code: str,
        *,
        zero_node: bool = False,
        veto_if_delivered: bool = False,
    ) -> bool:
        return await watchdog_retry_after_policy(
            aeng=aeng,
            run_id=run_id,
            org_id=org_id,
            executor=executor,
            exec_task=exec_task,
            final_status=final_status,
            error_code=error_code,
            zero_node=zero_node,
            veto_if_delivered=veto_if_delivered,
            outcome=box,
        )

    return _hook, box


async def _load_retry_context_or_none(
    aeng: AsyncEngine,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[dict[str, Any], str, dict[str, Any] | None, int] | None:
    """Load the retry-decision inputs, degrading a failure to ``None``.

    ``CancelledError`` propagates (the watchdog's own cancellation must never be
    swallowed into a "do not retry" decision); every other load failure logs and
    fail-closes to ``None`` — the kill then terminal-fails exactly as before.
    """
    try:
        return await _load_watchdog_retry_context(aeng, run_id=run_id, org_id=org_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline.watchdog_retry_context_load_failed run=%s", run_id, exc_info=True)
        return None


def _resolve_retry_budget(
    policy: dict[str, Any],
    final_status: str,
    error_code: str,
    *,
    zero_node: bool,
    run_id: uuid.UUID,
) -> int | None:
    """The shared matcher, plus the FAR-1088 zero-node failure carve-out.

    The raw watchdog codes resolve through the shared ``map_legacy_code`` alias
    table, so watchdog kills match the same events an in-execute outcome of the
    same shape would match (and the never-retryable exclusions apply
    identically). ``zero_node=True`` asks the SAME matcher once more for a
    failure-covered budget: an empty code means only the failure arm can match,
    and a non-covered / malformed / zero-budget policy still yields ``None``.
    """
    retry_budget = _retry_after_policy(policy, final_status, error_code)
    if retry_budget is None and zero_node:
        retry_budget = _retry_after_policy(policy, "failed", "")
        if retry_budget is not None:
            _log.info(
                "pipeline.watchdog_retry_zero_node_carve_out run=%s kill_status=%s kill_code=%s budget=%s",
                run_id,
                final_status,
                error_code,
                retry_budget,
            )
    return retry_budget


async def _attempt_count_if_retryable(
    executor: Any,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    trigger_type: str,
    graph_json: dict[str, Any] | None,
    claim_token: Any,
    claim_count: int,
    retry_budget: int,
    final_status: str,
    error_code: str,
) -> int | None:
    """Apply the shared gates + bookkeeping; return the effective attempt count.

    Returns ``None`` when the watchdog must terminal-fail exactly as before:
    FAR-210 correction run, FAR-295 non-idempotent graph, exhausted budget,
    superseded claim, or (FAR-296) a failed script-mode stale-lease probe.

    The budget also bounds PRE-node hangs (hub init / graph compile — the exact
    hang sites the zombie watchdog exists for): ``node_attempt_count`` only
    increments in ``executor._prepare_and_stream``, AFTER those sites, so a run
    hung there reads 0 on every kill. Each watchdog retry consumes a claim (the
    re-raised ``RunRetryPolicyError`` makes SAQ retry the job, whose re-claim
    grows ``claim_count``), so ``claim_count - 1`` — the number of PRIOR
    re-claims — grows one per retry cycle and bounds hangs that never reached a
    node. For a run that reached a node, ``claim_count`` is 1 and the effective
    count IS ``node_attempt_count``, so the in-execute path is unchanged.
    """
    is_correction_run = (trigger_type or "") == "correction"
    graph_idempotent = _graph_is_idempotent(graph_json)
    if is_correction_run or not graph_idempotent:
        return None
    node_attempt_count, current_claim_token, attempt_started_at = await executor._read_retry_attempt_state(
        org_id, run_id
    )
    attempt_count: int = max(node_attempt_count, claim_count - 1)
    superseded = claim_token is not None and current_claim_token is not None and current_claim_token != claim_token
    script_retry_probe_ok = True
    if _graph_has_script_mode(graph_json):
        script_retry_probe_ok = await executor._probe_script_lease(run_id=run_id, org_id=org_id)
    # FAR-2025: refuse the re-dispatch when the killed attempt already consumed the
    # graph's whole node-budget allowance (a watchdog deadline kill always has). An
    # over-budget task cannot be rescued by spending the same budgets again, so the
    # retry is a pure occupancy multiplier. ``_node_timeouts`` is empty before the
    # first node starts, so a PRE-node hang (the zombie watchdog's sites) is not
    # affected — it consumed no node budget and stays retryable.
    node_timeouts = getattr(executor, "_node_timeouts", None)
    timeouts = node_timeouts.values() if isinstance(node_timeouts, dict) else ()
    attempt_consumed_budget = _attempt_consumed_node_budget(
        _attempt_elapsed_seconds(attempt_started_at),
        timeouts,
    )
    if attempt_consumed_budget:
        _log.warning(
            "pipeline.watchdog_retry_budget_exhausted run=%s status=%s error_code=%s attempt=%s budget=%s",
            run_id,
            final_status,
            error_code,
            attempt_count,
            retry_budget,
        )
    if not _can_retry_after_policy(
        attempt_count,
        retry_budget,
        superseded,
        script_retry_probe_ok,
        attempt_consumed_budget=attempt_consumed_budget,
    ):
        return None
    return attempt_count


def _resolve_schedule_state(
    policy: dict[str, Any],
    attempt_count: int,
    run_id: uuid.UUID,
) -> tuple[str, float]:
    """Resolve the FAR-525 run-level backoff schedule and the effective sleep.

    Resolved BEFORE the fenced pending-reset so a resolver defect can never
    strand a run that was already reset (total fail-open to the hardcoded
    default schedule), mirroring ``_redispatch_after_policy``.
    """
    schedule_present, schedule_delay, schedule_multiplier, schedule_reason = rc.resolve_backoff_schedule(policy)
    if schedule_reason is not None:
        _log.warning(
            "pipeline.watchdog_retry_schedule_fail_open",
            extra={"run_id": str(run_id), "reason": schedule_reason},
        )
    if not schedule_present:
        schedule_state = "absent"
    elif schedule_reason:
        schedule_state = "failopen"
    else:
        schedule_state = "valid"
    effective_sleep = _retry_backoff_seconds(attempt_count, base=schedule_delay, multiplier=schedule_multiplier)
    return schedule_state, effective_sleep


async def _read_run_markers(
    aeng: AsyncEngine,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Read the run's reassembled marker rows (RLS-scoped, one session).

    Reads through the SAME repo reader the executor's FAR-228 gate
    (``_load_transient_state`` → ``read_run_markers``) and the run-detail
    delivery read (``api.routes.runs._run_gate_fired``) use, so this path can
    never disagree with them about what the run recorded.
    """
    from modulo.db.crud.run_node_outputs import read_run_markers
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        return await read_run_markers(session, run_id=run_id, organisation_id=org_id)


async def _run_already_delivered(
    aeng: AsyncEngine,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> bool:
    """True when the run's marker rows already record a completed delivery.

    FAR-2053: the ONE definition of "delivered" used here is the existing
    ``classify._any_marker_delivery_done`` (the FAR-228 ``delivery_done``
    sentinel) applied to the run's marker rows — no new bookkeeping, no second
    vocabulary.

    FAIL CLOSED: a probe read failure returns ``True`` (the veto engages) and
    never raises into the watchdog path — an unreadable delivery record must
    not license a re-dispatch that could repeat a side effect. The cost of
    being wrong is asymmetric: a withheld retry leaves a terminal-failed run
    that an operator can inspect, while a wrongly-permitted retry re-sends an
    email or re-opens a PR. ``CancelledError`` is re-raised — watchdog/job
    shutdown is not ours to swallow.
    """
    try:
        from modulo.core.pipeline_engine.classify import _any_marker_delivery_done

        markers = await _read_run_markers(aeng, run_id=run_id, org_id=org_id)
        return _any_marker_delivery_done(markers)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline.watchdog_retry_delivery_probe_failed run=%s", run_id, exc_info=True)
        return True


async def watchdog_retry_after_policy(
    *,
    aeng: AsyncEngine,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    executor: Any,
    final_status: str,
    error_code: str,
    exec_task: asyncio.Task[Any] | None = None,
    zero_node: bool = False,
    veto_if_delivered: bool = False,
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

    FAR-1088 zero-node failure carve-out — ``zero_node=True`` ONLY (the
    in-process zombie watchdog passes it; its kill fires pre-first-progress,
    so the run executed ZERO nodes and nothing can double-execute): when the
    normal matcher finds no coverage for the kill (e.g. the policy is
    ``{on: ["failure"]}``, which does not cover a ``stalled``/
    ``executor_stalled`` outcome), consult the SAME shared matcher once more
    for a failure-covered budget — ``_retry_after_policy(policy, "failed",
    "")`` matches ONLY the failure arm for an empty code (stall/timeout/eval
    arms cannot fire) and fail-closes on absent/empty ``on``, a malformed
    policy, or a 0 budget, so the carve-out can never pull in the FAR-649
    all-events default. This mirrors the dispatcher_reconcile cron path's
    zero-node carve-out (``_should_redispatch_nodeless``) so the two paths
    re-dispatch a failure-covered zero-node death on the same budget. With
    ``zero_node=False`` (default — node-deadline watchdog, in-execute path)
    behaviour is byte-for-byte unchanged: nodes may have executed there, so a
    failure carve-out is not safe without an explicit zero-node criterion.

    FAR-2053 delivery veto — ``veto_if_delivered=True`` ONLY (the stall/zombie
    watchdog passes it): once the retry policy would re-dispatch, the run's
    marker rows are read and, if ANY of them records ``delivery_done``, the
    re-dispatch is REFUSED and the watchdog terminal-fails instead. A
    re-dispatch re-executes the graph from the start, so re-running a run that
    already delivered can repeat its side effect; the FAR-228 node-level gate
    only bounds that for single-sandbox-node graphs on the transient-failure
    path. The probe fails CLOSED — an unreadable delivery record vetoes. The
    node-deadline watchdog does not pass the flag, so its behaviour is
    unchanged.
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

    context = await _load_retry_context_or_none(aeng, run_id=run_id, org_id=org_id)
    if context is None:
        return False
    policy, trigger_type, graph_json, claim_count = context

    # THE shared decision (matcher + FAR-1088 zero-node carve-out).
    retry_budget = _resolve_retry_budget(
        policy,
        final_status,
        error_code,
        zero_node=zero_node,
        run_id=run_id,
    )
    if retry_budget is None:
        return False
    # FAR-2053 delivery veto. Consulted only now — i.e. only when the policy
    # WOULD re-dispatch — so the extra read is never paid on the common
    # "no coverage" path and the WARNING below always means a re-dispatch was
    # actually blocked. ORDERING: before the attempt/budget gates and before
    # the fenced pending-reset, because the fact it reads (a completed side
    # effect) outranks every budget consideration: no remaining budget makes a
    # re-dispatch of already-delivered work safe.
    if veto_if_delivered and await _run_already_delivered(aeng, run_id=run_id, org_id=org_id):
        _log.warning(
            "pipeline.watchdog_retry_suppressed_delivered run=%s status=%s error_code=%s",
            run_id,
            final_status,
            error_code,
        )
        return False
    # Same gates + bookkeeping as the in-execute path (FAR-210 correction runs,
    # FAR-295 non-idempotent graphs, attempt budget, superseded claim, FAR-296
    # script-mode stale-lease probe).
    attempt_count = await _attempt_count_if_retryable(
        executor,
        run_id=run_id,
        org_id=org_id,
        trigger_type=trigger_type,
        graph_json=graph_json,
        claim_token=claim_token,
        claim_count=claim_count,
        retry_budget=retry_budget,
        final_status=final_status,
        error_code=error_code,
    )
    if attempt_count is None:
        return False

    schedule_state, effective_sleep = _resolve_schedule_state(policy, attempt_count, run_id)
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
        attempt_count,
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
) -> tuple[dict[str, Any], str, dict[str, Any] | None, int] | None:
    """Load the retry-decision inputs for one run (RLS-scoped, one session).

    Returns ``(retry_policy, trigger_type, graph_json, claim_count)`` or
    ``None`` when the run or its pipeline is missing. Mirrors
    ``_capture_execution_scalars``: the policy defaults to ``{}`` (no retry)
    when absent/malformed, and ``claim_count`` defaults to 0 when absent or
    not an int (the pre-node hang budget bound reads it from this row).
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
        pipeline = await get_pipeline(session, run.pipeline_id, organisation_id=org_id)
        if pipeline is None:
            return None
        snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
        snapshot = snapshot_result.scalar_one_or_none()
        graph_json = getattr(snapshot, "graph_json", None)

    raw_policy = getattr(pipeline, "retry_policy", None)
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    trigger_type = str(getattr(run, "trigger_type", "") or "")
    raw_claim_count = getattr(run, "claim_count", None)
    claim_count = raw_claim_count if isinstance(raw_claim_count, int) else 0
    return policy, trigger_type, graph_json if isinstance(graph_json, dict) else None, claim_count
