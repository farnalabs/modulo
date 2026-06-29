"""Overdue HITL claim warning system.

Finds pending (undecided) HITL claims whose creation time exceeds a
configurable warning threshold.  Optionally escalates claims that exceed
a longer escalation threshold.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.hitl_claim import HitlClaim

_log = logging.getLogger(__name__)

DEFAULT_WARNING_HOURS = 4
DEFAULT_ESCALATION_HOURS = 24


async def get_overdue_claims(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    warning_hours: int = DEFAULT_WARNING_HOURS,
) -> list[dict]:
    """Find undecided HITL claims that exceed the warning age threshold.

    Returns a list of dicts with claim id, pipeline_run_id, node_id,
    created_at, age_hours, and status (``"warning"`` or ``"escalated"``).
    """
    now = datetime.now(UTC)
    warning_cutoff = now - timedelta(hours=warning_hours)
    escalation_cutoff = now - timedelta(hours=DEFAULT_ESCALATION_HOURS)

    result = await db_session.execute(
        select(HitlClaim).where(
            HitlClaim.organisation_id == org_id,
            HitlClaim.decision.is_(None),
            HitlClaim.created_at < warning_cutoff,
        )
    )
    claims = result.scalars().all()

    return [
        {
            "id": str(claim.id),
            "pipeline_run_id": str(claim.run_id),
            "node_id": claim.gate_id,
            "created_at": claim.created_at.isoformat(),
            "age_hours": round((now - claim.created_at).total_seconds() / 3600, 1),
            "status": "escalated" if claim.created_at < escalation_cutoff else "warning",
        }
        for claim in claims
    ]
