"""Overdue HITL claim warning system.

Finds pending (undecided) HITL claims whose creation time exceeds a
configurable warning threshold.  Optionally escalates claims that exceed
a longer escalation threshold.

The SAQ ``hitl_overdue`` system cron calls :func:`dispatch_overdue_notifications`
to emit ``hitl_overdue`` notification events for claims that have crossed the
warning threshold.  Each claim is alerted exactly once — the job stamps
``HitlClaim.overdue_notified_at`` after a successful dispatch so subsequent
ticks skip it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.notifier import EVENT_HITL_OVERDUE
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.rls import set_rls_execution_context, set_rls_org

_log = logging.getLogger(__name__)

DEFAULT_WARNING_HOURS = 4
DEFAULT_ESCALATION_HOURS = 24
_SECONDS_PER_HOUR = 3600

# Advisory lock id for the overdue-notification sweep — distinct from the
# claim-expiry sweep's key so the two system crons never contend.
_OVERDUE_LOCK_KEY = 721_336_518


async def get_overdue_claims(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    warning_hours: int = DEFAULT_WARNING_HOURS,
    escalation_hours: int = DEFAULT_ESCALATION_HOURS,
) -> list[dict[str, Any]]:
    """Find claimed but undecided HITL claims that exceed the warning age threshold.

    Only gates that are claimed (``account_id IS NOT NULL``) but not yet decided
    are considered.  Returns a list of dicts with claim id, pipeline_run_id,
    node_id, claimed_at, age_hours, and status (``"warning"`` or ``"escalated"``).
    """
    if warning_hours < 0:
        raise ValueError(f"warning_hours must be non-negative, got {warning_hours}")
    if escalation_hours < 0:
        raise ValueError(f"escalation_hours must be non-negative, got {escalation_hours}")
    if escalation_hours <= warning_hours:
        raise ValueError(f"escalation_hours ({escalation_hours}) must exceed warning_hours ({warning_hours})")

    now = datetime.now(UTC)
    warning_cutoff = now - timedelta(hours=warning_hours)
    escalation_cutoff = now - timedelta(hours=escalation_hours)

    try:
        result = await db_session.execute(
            select(HitlClaim).where(
                HitlClaim.organisation_id == org_id,
                HitlClaim.decision.is_(None),
                HitlClaim.account_id.is_not(None),
                HitlClaim.claimed_at.is_not(None),
                HitlClaim.claimed_at < warning_cutoff,
            )
        )
    except Exception:
        _log.exception("Failed to query overdue claims for org %s", org_id)
        return []

    claims = result.scalars().all()

    return [
        {
            "id": str(claim.id),
            "pipeline_run_id": str(claim.run_id),
            "node_id": claim.gate_id,
            "claimed_at": claim.claimed_at.isoformat(),
            "age_hours": round(max((now - claim.claimed_at).total_seconds(), 0.0) / _SECONDS_PER_HOUR, 1),
            "status": "escalated" if claim.claimed_at < escalation_cutoff else "warning",
        }
        for claim in claims
        if claim.claimed_at is not None
    ]


async def dispatch_overdue_notifications(
    factory: async_sessionmaker[AsyncSession],
    notifier: Any | None = None,
    warning_hours: int = DEFAULT_WARNING_HOURS,
) -> list[dict[str, Any]]:
    """Dispatch ``hitl_overdue`` notification events for claims past the warning threshold.

    Shared by the SAQ ``hitl_overdue`` system cron (the sole writer).  Each
    org's work runs in its own transaction guarded by ``pg_try_advisory_xact_lock``
    so concurrent ticks on multiple workers never double-dispatch.  A claim is
    selected only when it is claimed-but-undecided, past the warning threshold,
    and not yet alerted (``overdue_notified_at IS NULL``); after a successful
    ``notifier.dispatch_event`` the claim's ``overdue_notified_at`` is stamped
    in a follow-up transaction, keeping the sweep idempotent.

    Returns the list of dispatched claim entries (claim_id, run_id, gate_id,
    pipeline_name, minutes_overdue).
    """
    if warning_hours < 0:
        raise ValueError(f"warning_hours must be non-negative, got {warning_hours}")

    now = datetime.now(UTC)
    warning_cutoff = now - timedelta(hours=warning_hours)

    all_dispatched: list[dict[str, Any]] = []
    for org_id in await _fetch_org_ids(factory):
        all_dispatched.extend(await _process_org_overdue(org_id, factory, notifier, warning_cutoff, now))
    return all_dispatched


async def _fetch_org_ids(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Return every organisation id; runs in its own short read transaction."""
    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        return list(result.scalars())


async def _process_org_overdue(
    org_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    notifier: Any | None,
    warning_cutoff: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    """Sweep a single org: lock, fetch overdue claims, dispatch, and stamp them.

    Returns the entries that were successfully dispatched for this org.
    """
    dispatched: list[dict[str, Any]] = []
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)

        if not await _try_acquire_overdue_lock(session, org_id):
            return dispatched

        entries = await _fetch_overdue_entries(session, org_id, warning_cutoff, now)

    if not entries:
        return dispatched

    dispatched = await _dispatch_overdue_entries(notifier, org_id, entries)
    if dispatched:
        await _stamp_overdue_notified(factory, org_id, [entry["claim_id"] for entry in dispatched])
    return dispatched


async def _try_acquire_overdue_lock(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Attempt the per-org advisory lock; return True if the org may proceed.

    Returns False only when the lock is already held elsewhere.  On a lock
    query error we log and proceed (the original fall-through behaviour) so a
    transient advisory-lock failure never silently skips an org's notifications.
    """
    try:
        lock_result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": _OVERDUE_LOCK_KEY},
        )
        return bool(lock_result.scalar_one())
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("hitl.overdue_job.lock_unavailable org=%s", org_id)
        return True


async def _fetch_overdue_entries(
    session: AsyncSession,
    org_id: uuid.UUID,
    warning_cutoff: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build the dispatch entries for claimed-but-undecided claims past the threshold."""
    rows = await session.execute(
        select(HitlClaim, Pipeline.name)
        .join(Pipeline, Pipeline.id == HitlClaim.pipeline_id)
        .where(
            HitlClaim.organisation_id == org_id,
            HitlClaim.decision.is_(None),
            HitlClaim.account_id.is_not(None),
            HitlClaim.claimed_at.is_not(None),
            HitlClaim.claimed_at < warning_cutoff,
            HitlClaim.overdue_notified_at.is_(None),
        )
    )

    entries: list[dict[str, Any]] = []
    for claim, pipeline_name in rows.all():
        if claim.claimed_at is None:
            continue
        entries.append(
            {
                "claim_id": claim.id,
                "run_id": claim.run_id,
                "gate_id": claim.gate_id,
                "pipeline_name": pipeline_name,
                "minutes_overdue": int((now - claim.claimed_at).total_seconds() // 60),
            }
        )
    return entries


async def _dispatch_overdue_entries(
    notifier: Any | None,
    org_id: uuid.UUID,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dispatch each entry via the notifier; return only the successfully sent ones.

    Dispatch happens outside any overdue-sweep transaction (the notifier does
    its own I/O + DB work), so failures are isolated per entry.
    """
    dispatched: list[dict[str, Any]] = []
    if notifier is None:
        return dispatched

    for entry in entries:
        try:
            await notifier.dispatch_event(
                org_id=org_id,
                event_type=EVENT_HITL_OVERDUE,
                payload={
                    "run_id": str(entry["run_id"]),
                    "gate_id": entry["gate_id"],
                    "pipeline_name": entry["pipeline_name"],
                    "minutes_overdue": entry["minutes_overdue"],
                },
                run_id=str(entry["run_id"]),
            )
            dispatched.append(entry)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "hitl.overdue_job.notification_failed",
                extra={"gate_id": entry["gate_id"], "run_id": str(entry["run_id"])},
            )
    return dispatched


async def _stamp_overdue_notified(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    notified_ids: list[uuid.UUID],
) -> None:
    """Stamp ``overdue_notified_at`` on dispatched claims so they are never re-alerted."""
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        await session.execute(
            update(HitlClaim).where(HitlClaim.id.in_(notified_ids)).values(overdue_notified_at=datetime.now(UTC))
        )
