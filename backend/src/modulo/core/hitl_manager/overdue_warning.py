"""Overdue HITL claim warning system.

Finds pending (undecided) HITL claims whose creation time exceeds a
configurable warning threshold.  Optionally escalates claims that exceed
a longer escalation threshold.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.hitl_claim import HitlClaim

_log = logging.getLogger(__name__)

DEFAULT_WARNING_HOURS = 4
DEFAULT_ESCALATION_HOURS = 24
_SECONDS_PER_HOUR = 3600


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
