"""Graceful worker-shutdown run handoff (FAR-1627).

Every ``fly deploy`` to the single prod worker SIGTERMs the SAQ runs worker
while agent runs (tens of minutes long) are mid-execution. Before this module,
nothing noticed at shutdown: the executor died, the run row stayed ``running``
with a frozen heartbeat, and detection waited out the heartbeat TTL
(``SLOT_RECONCILE_STALE_SECONDS`` = 30 min) — at which point the sweep
force-released the row with a code that misattributed the loss (the FAR-1603
RCA: 10 of 19 releases over 3 days killed in-flight runs, surfaced 30-35 min
later as ``harness.dispatch_failed`` / ``never_dispatched``).

The handoff: the shutting-down worker, AFTER SAQ has drained and cancelled its
in-flight job tasks (the SAQ ``shutdown`` lifecycle hook — see
``saq_worker._shutdown_handoff_hook``), promptly writes the loss for every run
it still owns: release the claim (reset the row to ``pending``), stamp the
truthful ``harness.worker_lost`` code + a detail naming the shutdown, and clear
``dispatched_at`` / ``dispatcher`` / ``heartbeat_at`` so the normal
``dispatcher_reconcile`` re-dispatch legs admit the row on their next 60s
tick. The new worker re-dispatches within ~1 minute of becoming ready instead
of the run sitting orphaned for 30-35 minutes.

Ordering is deliberate: the hook runs only AFTER SAQ has cancelled the job
tasks (``Worker.stop()`` waits ``shutdown_grace_period_s`` then cancels with
``cancellation_hard_deadline_s``), so the predecessor executor is definitively
dead by handback time — a re-dispatch can never double-execute against a live
predecessor. That is what distinguishes this from the heartbeat-stale sweeps,
which cannot tell a dead executor from a stalled one and therefore keep the
FAR-1649 node-started re-dispatch belt. Handing a node-started run back here
re-queues it for re-execution (the run's interrupted node re-runs; E2B sandbox
work already done is lost unless re-attached) — the ticket's sanctioned
outcome ("re-dispatched within the ~1-minute window if loss is unavoidable").
Per-pipeline control of that requeue (default ``fail``) is FAR-1626's safety
net, not this module's; the handback itself must happen promptly regardless.

Never raises into the shutdown path (except ``CancelledError``): a failed
handback is logged loudly and the run falls back to the pre-existing
heartbeat-TTL sweeps — the handoff is a promptness improvement over a
30-minute backstop, never a correctness dependency.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.db.sqlstates import is_row_lock_timeout

_log = logging.getLogger(__name__)

#: Canonical (registered) error code the handback stamps. Dotted registry
#: spelling — passes ``map_legacy_code`` through unchanged, never resolves via
#: the legacy ``worker_lost`` -> ``harness.dispatch_failed`` alias (which would
#: resurrect exactly the misattribution FAR-1627 removes).
WORKER_LOST_ERROR_CODE = "harness.worker_lost"

#: Synthetic error detail stamped on every handed-back run: names the shutdown
#: and the handback so the operator reads the truth, not a dispatch-path
#: generic.
WORKER_LOST_DETAIL = (
    "Worker executing this run was shut down (deploy or restart); the claim was "
    "released and the run handed back for re-dispatch (FAR-1627)."
)

# RLS set_config helper — same contract as the periodic sweeps (the per-org
# statement runs inside set_config('app.organisation_id', ...)).
_SQL_SET_ORG_ID = "SELECT set_config('app.organisation_id', :val, true)"

# The handback UPDATE. Fenced exactly like every other claim-fenced writer:
#   * ``status = 'running'`` — only a run still marked running is handed back
#     (a run whose fate already landed — complete / failed / cancelled — is
#     untouched, rowcount 0).
#   * ``claim_token = :tok`` — the registry entry's token; a successor that
#     re-claimed the run between registration and handback rotated the token,
#     so this row is no longer ours to release (fence-out, never clobber).
#   * ``cancellation_requested = false`` — cancel wins; the cancel path owns
#     the row's status.
# Reset mirrors the heartbeat-stale slot sweep's retry arm (FAR-779/812):
# ``pending`` + cleared ``dispatched_at`` / ``dispatcher`` / ``heartbeat_at``
# so the ``dispatcher_reconcile`` pending legs (capacity-deferred:
# ``pending`` + ``dispatched_at IS NULL``) admit the row on the next 60s tick.
# ``error_code``/``error_detail`` are STAMPED (not cleared) — the reset state
# must carry the truthful worker-loss code (the re-claim's
# ``clear_error_code`` clears it when the run is picked back up).
_HANDBACK_RESET_SQL = text(
    "UPDATE runs SET status='pending', error_code=:code, error_detail=:detail, "  # nosec B608
    "completed_at=NULL, dispatched_at=NULL, dispatcher=NULL, heartbeat_at=NULL "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND status='running' "
    "AND claim_token=:tok "
    "AND cancellation_requested=false "
    "RETURNING id"
)


@dataclass(frozen=True)
class InFlightRun:
    """One run this worker process claimed and is still executing."""

    run_id: uuid.UUID
    org_id: uuid.UUID
    claim_token: str


class InFlightRunRegistry:
    """Process-local map of runs currently executing in THIS worker process.

    Keyed by ``run_id`` (a SAQ retry re-invokes ``execute_run`` and re-claims
    with a FRESH token — the re-claim replaces the entry, so the latest claim
    always wins). Registration happens right after a successful claim;
    deregistration happens ONLY on paths where the job function returns
    normally (the run's fate — complete / failed / awaiting_human / not
    claimed — was decided and written). ``asyncio.CancelledError`` (SAQ's
    shutdown cancellation) deliberately does NOT deregister: an abandoned run
    is exactly what the handback must see.
    """

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, InFlightRun] = {}
        self._lock = threading.Lock()

    def register(self, *, run_id: uuid.UUID, org_id: uuid.UUID, claim_token: str) -> None:
        with self._lock:
            self._runs[run_id] = InFlightRun(run_id=run_id, org_id=org_id, claim_token=claim_token)

    def discard(self, run_id: uuid.UUID) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def snapshot(self) -> list[InFlightRun]:
        with self._lock:
            return list(self._runs.values())

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)


_PROCESS_REGISTRY = InFlightRunRegistry()


def get_inflight_registry() -> InFlightRunRegistry:
    """The process-wide in-flight registry (one per worker process)."""
    return _PROCESS_REGISTRY


@asynccontextmanager
async def track_inflight_run(
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    claim_token: str,
    registry: InFlightRunRegistry | None = None,
) -> AsyncIterator[None]:
    """Scope one claimed run's ownership for the shutdown handoff (FAR-1627).

    Registers the run on entry and deregisters it on EVERY exit EXCEPT
    ``asyncio.CancelledError`` — SAQ's shutdown cancellation abandons the
    executor mid-run, and an abandoned run is exactly what
    :func:`handback_inflight_runs` must see. Any other exception means the
    job function failed and SAQ's ``after_process`` hook (or a terminal writer
    in the unwinding stack) owns the run's fate, so the entry is dropped.
    """
    reg = registry if registry is not None else get_inflight_registry()
    reg.register(run_id=run_id, org_id=org_id, claim_token=claim_token)
    try:
        yield
    except asyncio.CancelledError:
        # Keep the registry entry: the shutdown handoff releases this run's
        # claim once the cancellation completes (SAQ stop() runs the shutdown
        # hook after the cancellation hard deadline).
        raise
    except BaseException:
        reg.discard(run_id)
        raise
    else:
        reg.discard(run_id)


async def handback_inflight_runs(
    async_engine: AsyncEngine,
    *,
    registry: InFlightRunRegistry | None = None,
) -> dict[str, int]:
    """Release every still-registered in-flight run for re-dispatch (FAR-1627).

    Per org (RLS-scoped, row-lock bounded — FAR-1592/1584 pattern shared with
    the periodic sweeps): one guarded UPDATE per run
    (:data:`_HANDBACK_RESET_SQL`). A 55P03 row-lock-timeout expiry is NOT a
    failure for the org — its transaction rolled back whole, so none of its
    runs moved; they stay ``running`` and fall back to the heartbeat-TTL
    sweeps (WARNING + skip, never a lost recovery). Every other per-org
    failure is logged loudly and the remaining orgs still run.

    The registry is drained at the end (success or failure): the process is
    shutting down, and a stale entry must never be handed back twice.

    Never raises (except ``CancelledError``) — this runs inside the SAQ
    ``shutdown`` lifecycle hook, where an exception would surface as a worker
    crash instead of a logged, backstopped degradation.

    Returns ``{"handed_back": int, "skipped": int}`` — ``handed_back`` counts
    rows the UPDATE moved to ``pending``; ``skipped`` counts registry entries
    whose fenced UPDATE matched nothing (fate already landed, token rotated,
    or cancellation requested).
    """
    reg = registry if registry is not None else get_inflight_registry()
    entries = reg.snapshot()
    if not entries:
        return {"handed_back": 0, "skipped": 0}

    by_org: dict[uuid.UUID, list[InFlightRun]] = {}
    for entry in entries:
        by_org.setdefault(entry.org_id, []).append(entry)

    handed_back = 0
    skipped = 0
    try:
        for org_id, org_entries in by_org.items():
            try:
                async with async_engine.connect() as conn, conn.begin():
                    from modulo.db.crud.row_lock import set_mutation_row_lock_timeout

                    await set_mutation_row_lock_timeout(conn)
                    await conn.execute(text(_SQL_SET_ORG_ID), {"val": str(org_id)})
                    for entry in org_entries:
                        result = await conn.execute(
                            _HANDBACK_RESET_SQL,
                            {
                                "rid": str(entry.run_id),
                                "oid": str(org_id),
                                "tok": entry.claim_token,
                                "code": WORKER_LOST_ERROR_CODE,
                                "detail": WORKER_LOST_DETAIL,
                            },
                        )
                        if result.fetchone() is not None:
                            handed_back += 1
                            _log.warning(
                                "worker_handoff.released run=%s org=%s "
                                "(worker shutdown mid-run; reset to pending with %s for re-dispatch)",
                                entry.run_id,
                                org_id,
                                WORKER_LOST_ERROR_CODE,
                            )
                        else:
                            skipped += 1
                            _log.info(
                                "worker_handoff.skipped run=%s org=%s "
                                "(fenced out: fate already landed, claim rotated, or cancellation requested)",
                                entry.run_id,
                                org_id,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_row_lock_timeout(exc):
                    # FAR-1592 shape: the org transaction rolled back whole —
                    # NONE of its runs moved; they stay ``running`` and the
                    # heartbeat-TTL sweeps remain the backstop. WARNING, skip,
                    # continue with the remaining orgs.
                    _log.warning(
                        "worker_handoff.org_lock_timeout org=%s entries=%d "
                        "(SQLSTATE 55P03 from the bounded mutation_row_lock_timeout_ms wait) — "
                        "org transaction rolled back with NO runs released; they fall back to the "
                        "heartbeat-TTL sweeps",
                        org_id,
                        len(org_entries),
                        exc_info=True,
                    )
                    skipped += len(org_entries)
                    continue
                _log.exception(
                    "worker_handoff.org_handback_failed org=%s entries=%d (falls back to the heartbeat-TTL sweeps)",
                    org_id,
                    len(org_entries),
                )
                skipped += len(org_entries)
    finally:
        reg.clear()

    if handed_back or skipped:
        _log.warning(
            "worker_handoff.sweep_complete handed_back=%d skipped=%d orgs=%d",
            handed_back,
            skipped,
            len(by_org),
        )
    return {"handed_back": handed_back, "skipped": skipped}
