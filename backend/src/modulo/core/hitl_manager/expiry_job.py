"""Background job that polls for expired HITL claims and resets them.

Runs as an asyncio task alongside the FastAPI server.  Polls every ``POLL_INTERVAL``
(60 seconds by default).  On each tick it:

1. Queries all claims whose ``expires_at < NOW()`` and resets them to unclaimed.
2. Updates the run status back to ``awaiting_human`` (so it shows up in pending lists).
3. Logs a ``hitl.claim_expired`` audit event for each expired claim.
4. Dispatches a ``claim_expired`` notification event (if a ``Notifier`` was provided).

The job is started during the application lifespan and cancelled on shutdown.

PR B-2 (plan F1): the expiry work itself is extracted to the shared
:func:`expire_stale_claims` guarded by a per-org advisory lock so the SAQ system
worker's ``claim_expiry`` cron (SOLE writer / notifier in shadow) and the
in-process loop never double-write audit events. ``ClaimExpiryJob`` keeps the
in-process notification dispatch (no-op in shadow — main.py constructs it
without a notifier).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.audit_logger import append_audit_event
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

POLL_INTERVAL: float = 60.0  # seconds

# Advisory lock id for the claim-expiry sweep — shared by the in-process loop
# and the SAQ cron so only one writer runs at a time (plan F1 "SOLE writer").
# Arbitrary stable int8 value within pg_advisory_xact_lock's range.
_EXPIRY_LOCK_KEY = 721_336_517


async def expire_stale_claims(
    factory: async_sessionmaker[AsyncSession],
    notifier: Any | None = None,
) -> list[dict[str, Any]]:
    """Run one claim-expiry pass across all orgs.

    Shared by :class:`ClaimExpiryJob` (in-process) and the SAQ ``claim_expiry``
    system cron. Each org's work runs in its own transaction guarded by
    ``pg_try_advisory_xact_lock`` so a concurrent writer (in-process loop vs SAQ
    cron) finds no stale rows and skips — preventing double audit/notification.
    Returns the list of expired gate identifiers.
    """
    all_expired: list[dict[str, Any]] = []

    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        org_ids: list[uuid.UUID] = list(result.scalars())

    now = datetime.now(UTC)
    for org_id in org_ids:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)

            # Advisory lock — only one expiry writer per org at a time.
            try:
                lock_result = await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": _EXPIRY_LOCK_KEY},
                )
                if not lock_result.scalar_one():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("hitl.expiry_job.lock_unavailable org=%s", org_id)

            # 1. SELECT stale claims before resetting so we capture claimed_by
            #    and claim id for audit events.
            stale = await session.execute(
                select(
                    HitlClaim.id,
                    HitlClaim.run_id,
                    HitlClaim.gate_id,
                    HitlClaim.account_id,
                ).where(
                    HitlClaim.organisation_id == org_id,
                    HitlClaim.expires_at < now,
                    HitlClaim.account_id.is_not(None),
                    HitlClaim.decision.is_(None),
                )
            )
            stale_rows = stale.all()
            if not stale_rows:
                continue

            # 2. Build the list of claim IDs to reset
            claim_ids = [r.id for r in stale_rows]
            expired = [
                {
                    "claim_id": r.id,
                    "run_id": r.run_id,
                    "gate_id": r.gate_id,
                    "claimed_by": r.account_id,
                    "organisation_id": org_id,
                }
                for r in stale_rows
            ]
            all_expired.extend(expired)

            # 3. Reset the stale claims — re-validate conditions to prevent a
            #    race with a concurrent claim (TOCTOU from the SELECT above).
            await session.execute(
                update(HitlClaim)
                .where(
                    HitlClaim.id.in_(claim_ids),
                    HitlClaim.account_id.is_not(None),
                    HitlClaim.expires_at < now,
                    HitlClaim.decision.is_(None),
                )
                .values(
                    account_id=None,
                    claimed_at=None,
                    claim_token=None,
                    expires_at=now,
                )
            )

            # 4. Batch-reset affected runs back to awaiting_human
            run_ids = list({entry["run_id"] for entry in expired})
            await session.execute(
                update(Run).where(Run.id.in_(run_ids), Run.status == "claimed").values(status="awaiting_human")
            )

            # 5. Log audit events for each expired claim. Use savepoints so a
            #    single failed audit log does not abort the org's transaction.
            for entry in expired:
                try:
                    async with session.begin_nested():
                        await append_audit_event(
                            session,
                            org_id=org_id,
                            event_type="hitl.claim_expired",
                            resource_type="hitl_claim",
                            resource_id=entry["claim_id"],
                            payload_json={
                                "pipeline_run_id": str(entry["run_id"]),
                                "node_id": entry["gate_id"],
                                "claimed_by": str(entry["claimed_by"]) if entry["claimed_by"] else None,
                            },
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("Failed to record claim_expired audit event for claim %s", entry["claim_id"])

        # 6. Dispatch notifications outside the transaction (SAQ cron only — the
        #    in-process ClaimExpiryJob is constructed without a notifier).
        if notifier is not None:
            for entry in expired:
                try:
                    await notifier.dispatch_event(
                        org_id=org_id,
                        event_type="claim_expired",
                        payload={
                            "run_id": str(entry["run_id"]),
                            "gate_id": entry["gate_id"],
                            "claimed_by": str(entry["claimed_by"]) if entry["claimed_by"] else None,
                        },
                        run_id=str(entry["run_id"]),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception(
                        "hitl.expiry_job.notification_failed",
                        extra={"gate_id": entry["gate_id"], "run_id": str(entry["run_id"])},
                    )

    return all_expired


class ClaimExpiryJob:
    """Background coroutine that expires stale HITL claims."""

    def __init__(self, db_engine: AsyncEngine, notifier: Any | None = None) -> None:
        self._engine = db_engine
        # autobegin=False matches the DI default (dependencies.py): a session
        # with autobegin=True auto-starts an implicit transaction on first
        # execute, so a subsequent `async with session.begin():` raises
        # InvalidRequestError ("A transaction is already begun").
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._notifier = notifier

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._task is not None:
            return
        # start() is async so a loop is always running here.
        self._task = asyncio.create_task(self._run())  # nosemgrep: create-task-without-guard
        _log.info("hitl.expiry_job.started", extra={"poll_interval_s": POLL_INTERVAL})

    async def stop(self) -> None:
        """Signal the polling loop to stop and wait for it."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        _log.info("hitl.expiry_job.stopped")

    async def _run(self) -> None:
        """Main polling loop."""
        while not self._stop_event.is_set():
            try:
                expired = await self._expire_once()
                if expired:
                    _log.info("hitl.expiry_job.expired", extra={"count": len(expired)})
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("hitl.expiry_job.tick_failed")
            await asyncio.sleep(POLL_INTERVAL)

    async def _expire_once(self) -> list[dict[str, Any]]:
        """Run one expiry pass.  Returns list of expired gate identifiers."""
        return await expire_stale_claims(self._session_factory, notifier=self._notifier)
