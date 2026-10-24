"""Eval/guardrail mute policy evaluation (FAR-2028).

This module provides the mute policy evaluation logic that determines when
an eval or guardrail definition should be auto-muted based on its acceptance rate.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval.finding_resolver import (
    evaluate_mute_policy,
)
from modulo.db.models.eval import Eval

# Default window for mute policy evaluation (30 days)
DEFAULT_MUTE_WINDOW_DAYS = 30


async def evaluate_mute_policy_for_definition(
    session: AsyncSession,
    eval_id: str,
    org_id: str,
    window_days: int = DEFAULT_MUTE_WINDOW_DAYS,
) -> dict[str, Any]:
    """Evaluate mute policy for a definition.

    This is a thin wrapper around the finding_resolver's evaluate_mute_policy
    that provides a default window.

    Args:
        session: Database session with RLS org context already set.
        eval_id: UUID of the Eval definition.
        org_id: Organisation ID (for RLS scoping).
        window_days: Time window for the acceptance rate calculation.

    Returns:
        Dict with mute policy evaluation results.
    """
    return await evaluate_mute_policy(session, eval_id, org_id, window_days)


async def get_muted_definitions(
    session: AsyncSession,
    org_id: str,
) -> list[Eval]:
    """Get all muted eval/guardrail definitions for an organisation.

    Args:
        session: Database session with RLS org context already set.
        org_id: Organisation ID.

    Returns:
        List of muted Eval definitions.
    """
    result = await session.execute(
        select(Eval).where(
            Eval.organisation_id == org_id,
            Eval.is_muted,
            Eval.deleted_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def get_definitions_near_mute_threshold(
    session: AsyncSession,
    org_id: str,
    margin: float = 0.1,
    window_days: int = DEFAULT_MUTE_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Get definitions that are close to being muted.

    Useful for alerting/dashboarding to show definitions that are approaching
    the mute threshold.

    Args:
        session: Database session with RLS org context already set.
        org_id: Organisation ID.
        margin: How close to the threshold (as a fraction) to consider "near".
                e.g., 0.1 means within 10% of the threshold.
        window_days: Time window for the acceptance rate calculation.

    Returns:
        List of dicts with definition info and mute policy status.
    """
    # Get all eval definitions with mute policy configured
    result = await session.execute(
        select(Eval).where(
            Eval.organisation_id == org_id,
            Eval.mute_threshold.is_not(None),
            Eval.mute_min_outcomes.is_not(None),
            Eval.deleted_at.is_(None),
        )
    )
    eval_defs = list(result.scalars().all())

    near_threshold = []
    for eval_def in eval_defs:
        mute_eval = await evaluate_mute_policy(session, str(eval_def.id), org_id, window_days)
        # Check if acceptance rate is within margin of threshold
        if mute_eval["total_outcomes"] > 0:
            threshold = mute_eval["threshold"] or 0
            acceptance_rate = mute_eval["acceptance_rate"]
            # Near threshold if acceptance rate is within margin * threshold of the threshold
            if abs(acceptance_rate - threshold) <= threshold * margin:
                near_threshold.append(
                    {
                        "eval_id": str(eval_def.id),
                        "name": eval_def.name,
                        "eval_type": eval_def.eval_type,
                        "acceptance_rate": acceptance_rate,
                        "threshold": threshold,
                        "total_outcomes": mute_eval["total_outcomes"],
                        "min_outcomes": mute_eval["min_outcomes"],
                        "is_muted": eval_def.is_muted,
                        "distance_from_threshold": abs(acceptance_rate - threshold),
                    }
                )

    # Sort by distance from threshold (closest first)
    near_threshold.sort(key=lambda x: x["distance_from_threshold"])
    return near_threshold


async def record_mute_audit_event(
    session: AsyncSession,
    eval_id: str,
    org_id: str,
    action: str,  # "muted" or "unmuted"
    acceptance_rate: float | None = None,
    threshold: float | None = None,
    total_outcomes: int | None = None,
    actor_id: str | None = None,
) -> None:
    """Record an audit event for mute/unmute actions.

    Args:
        session: Database session with RLS org context already set.
        eval_id: UUID of the Eval definition.
        org_id: Organisation ID.
        action: "muted" or "unmuted".
        acceptance_rate: The acceptance rate at the time of muting.
        threshold: The mute threshold configured.
        total_outcomes: The number of outcomes considered.
        actor_id: Account ID of the actor (system or user).
    """
    import uuid

    from modulo.core.audit_logger import append_audit_event

    await append_audit_event(
        session,
        org_id=uuid.UUID(org_id),
        event_type=f"eval_definition_{action}",
        actor_user_id=uuid.UUID(actor_id) if actor_id else None,
        resource_type="eval_definition",
        resource_id=uuid.UUID(eval_id),
        payload_json={
            "acceptance_rate": acceptance_rate,
            "threshold": threshold,
            "total_outcomes": total_outcomes,
            "action": action,
        },
    )


# Re-export for test patching and external use
__all__ = [
    "DEFAULT_MUTE_WINDOW_DAYS",
    "evaluate_mute_policy",
    "evaluate_mute_policy_for_definition",
    "get_definitions_near_mute_threshold",
    "get_muted_definitions",
    "record_mute_audit_event",
]
