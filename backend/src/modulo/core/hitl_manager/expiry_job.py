"""Background job that polls for expired HITL claims and resets them.

Runs as an asyncio task alongside the FastAPI server.  Polls every ``POLL_INTERVAL``
(60 seconds by default).  On each tick it:

1. Queries all claims whose ``expires_at < NOW()`` and resets them to unclaimed.
2. Updates the run status back to ``awaiting_human`` (so it shows up in pending lists).
3. Logs a ``hitl.claim_expired`` audit event for each expired claim.
4. Dispatches a ``claim_expired`` notification event (if a ``Notifier`` was provided).

The job is started during the application lifespan and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.audit_logger import append_audit_event
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

POLL_INTERVAL: float = 60.0  # seconds


class ClaimExpiryJob:
    """Background coroutine that expires stale HITL claims."""

    def __init__(self, db_engine: AsyncEngine, notifier: Any | None = None) -> None:
        self._engine = db_engine
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._notifier = notifier

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        _log.info("hitl.expiry_job.started", extra={"poll_interval_s": POLL_INTERVAL})

    async def stop(self) -> None:
        """Signal the polling loop to stop and wait for it."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
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
                break
            except Exception:
                _log.exception("hitl.expiry_job.tick_failed")
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _expire_once(self) -> list[dict[str, Any]]:
        """Run one expiry pass.  Returns list of expired gate identifiers."""
        all_expired: list[dict[str, Any]] = []

        # Collect all org IDs first so we can scope each expiry pass per org
        # (RLS requires app.organisation_id to be set per transaction).
        async with self._session_factory() as session:
            result = await session.execute(select(Organisation.id))
            org_ids: list[uuid.UUID] = list(result.scalars())

        now = datetime.now(UTC)
        for org_id in org_ids:
            async with self._session_factory() as session:
                async with session.begin():
                    await set_rls_org(session, org_id)

                    # 1. SELECT stale claims before resetting so we capture
                    #    claimed_by and claim id for audit events.
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
                    claim_ids = [r[0] for r in stale_rows]
                    expired = [
                        {
                            "claim_id": r[0],
                            "run_id": r[1],
                            "gate_id": r[2],
                            "claimed_by": r[3],
                            "organisation_id": org_id,
                        }
                        for r in stale_rows
                    ]
                    all_expired.extend(expired)

                    # 3. Reset the stale claims
                    await session.execute(
                        update(HitlClaim)
                        .where(HitlClaim.id.in_(claim_ids))
                        .values(
                            account_id=None,
                            claimed_at=None,
                            claim_token=None,
                            expires_at=None,
                        )
                    )

                    # 4. Batch-reset affected runs back to awaiting_human
                    run_ids = list({entry["run_id"] for entry in expired})
                    await session.execute(
                        update(Run)
                        .where(Run.id.in_(run_ids), Run.status == "claimed")
                        .values(status="awaiting_human")
                    )

                    # 5. Log audit events for each expired claim
                    for entry in expired:
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

            # 6. Dispatch notifications outside the transaction
            if self._notifier is not None:
                for entry in expired:
                    try:
                        await self._notifier.dispatch_event(
                            org_id=org_id,
                            event_type="claim_expired",
                            payload={
                                "run_id": str(entry["run_id"]),
                                "gate_id": entry["gate_id"],
                                "claimed_by": str(entry["claimed_by"]) if entry["claimed_by"] else None,
                            },
                            run_id=entry["run_id"],
                        )
                    except Exception:
                        _log.exception(
                            "hitl.expiry_job.notification_failed",
                            extra={"gate_id": entry["gate_id"], "run_id": str(entry["run_id"])},
                        )

        return all_expired
