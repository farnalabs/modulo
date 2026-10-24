"""Eval/guardrail finding resolution logic (FAR-2028).

Provides the core business logic for resolving eval and guardrail findings
(accepted / dismissed / superseded), computing acceptance rates, and evaluating
mute policies.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult

# Resolution vocabulary — must match the CHECK constraint on eval_results.resolution
RESOLUTION_VALUES = ("accepted", "dismissed", "superseded", "pending")


class FindingResolutionError(ValueError):
    """Raised when a finding resolution operation is invalid."""


async def resolve_finding(
    session: AsyncSession,
    finding_id: str,
    resolution: str,
    reason: str | None,
    resolved_by: str,
    org_id: str,
) -> EvalResult:
    """Resolve an eval/guardrail finding.

    Args:
        session: Database session with RLS org context already set.
        finding_id: UUID of the EvalResult to resolve.
        resolution: One of 'accepted', 'dismissed', 'superseded', 'pending'.
        reason: Optional human-readable reason for the resolution.
        resolved_by: Account ID of the user resolving the finding.
        org_id: Organisation ID (for RLS scoping).

    Returns:
        The updated EvalResult row.

    Raises:
        FindingResolutionError: If the finding doesn't exist or resolution is invalid.
    """
    if resolution not in RESOLUTION_VALUES:
        raise FindingResolutionError(f"Invalid resolution: {resolution}. Must be one of {RESOLUTION_VALUES}")

    result = await session.execute(
        select(EvalResult).where(
            EvalResult.id == finding_id,
            EvalResult.organisation_id == org_id,
        )
    )
    finding = result.scalar_one_or_none()
    if finding is None:
        raise FindingResolutionError("Finding not found")

    # Update the finding
    finding.resolution = resolution
    finding.resolution_reason = reason
    finding.resolved_at = datetime.now(UTC)
    finding.resolved_by = uuid.UUID(resolved_by) if resolved_by else None

    await session.flush()
    return finding


async def compute_acceptance_rate(
    session: AsyncSession,
    eval_id: str,
    org_id: str,
    window_days: int | None = None,
) -> tuple[float, int]:
    """Compute the acceptance rate for an eval/guardrail definition.

    Acceptance rate = count(resolution='accepted') / count(resolution IN ('accepted', 'dismissed', 'superseded'))
    Only findings with a non-pending resolution are counted in the denominator.
    Pending findings are excluded from the calculation.

    Args:
        session: Database session with RLS org context already set.
        eval_id: UUID of the Eval definition.
        org_id: Organisation ID (for RLS scoping).
        window_days: Optional time window in days (e.g., 30 for last 30 days).
                     If None, computes over all time.

    Returns:
        Tuple of (acceptance_rate, total_resolved_count).
        acceptance_rate is a float between 0.0 and 1.0.
        total_resolved_count is the number of findings with non-pending resolution.
    """
    from datetime import timedelta

    # Build the base query for resolved findings
    conditions = [
        EvalResult.eval_id == eval_id,
        EvalResult.organisation_id == org_id,
        EvalResult.resolution.in_(("accepted", "dismissed", "superseded")),
    ]

    if window_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=window_days)
        conditions.append(EvalResult.evaluated_at >= cutoff)

    # Count total resolved
    total_q = select(func.count(EvalResult.id)).where(*conditions)
    total = (await session.execute(total_q)).scalar() or 0

    if total == 0:
        return 0.0, 0

    # Count accepted
    accepted_conditions = [*conditions, EvalResult.resolution == "accepted"]
    accepted_q = select(func.count(EvalResult.id)).where(*accepted_conditions)
    accepted = (await session.execute(accepted_q)).scalar() or 0

    return accepted / total, total


async def evaluate_mute_policy(
    session: AsyncSession,
    eval_id: str,
    org_id: str,
    window_days: int | None = None,
) -> dict[str, Any]:
    """Evaluate whether an eval/guardrail definition should be muted.

    A definition is muted when:
    - It has mute_threshold and mute_min_outcomes configured
    - It has at least mute_min_outcomes resolved findings (in the window)
    - Its acceptance rate is below mute_threshold

    Args:
        session: Database session with RLS org context already set.
        eval_id: UUID of the Eval definition.
        org_id: Organisation ID (for RLS scoping).
        window_days: Optional time window for the acceptance rate calculation.

    Returns:
        Dict with keys:
        - should_mute: bool
        - acceptance_rate: float
        - total_outcomes: int
        - threshold: float | None
        - min_outcomes: int | None
        - reason: str (explanation of the decision)
    """
    # Load the eval definition to get mute policy config
    eval_row = await session.execute(
        select(Eval).where(
            Eval.id == eval_id,
            Eval.organisation_id == org_id,
        )
    )
    eval_def = eval_row.scalar_one_or_none()
    if eval_def is None:
        return {
            "should_mute": False,
            "acceptance_rate": 0.0,
            "total_outcomes": 0,
            "threshold": None,
            "min_outcomes": None,
            "reason": "Eval definition not found",
        }

    # If no mute policy configured, never mute
    if eval_def.mute_threshold is None or eval_def.mute_min_outcomes is None:
        return {
            "should_mute": False,
            "acceptance_rate": 0.0,
            "total_outcomes": 0,
            "threshold": eval_def.mute_threshold,
            "min_outcomes": eval_def.mute_min_outcomes,
            "reason": "Mute policy not configured",
        }

    # Compute acceptance rate
    acceptance_rate, total_outcomes = await compute_acceptance_rate(session, eval_id, org_id, window_days)

    # Check if we have enough outcomes
    if total_outcomes < eval_def.mute_min_outcomes:
        return {
            "should_mute": False,
            "acceptance_rate": acceptance_rate,
            "total_outcomes": total_outcomes,
            "threshold": eval_def.mute_threshold,
            "min_outcomes": eval_def.mute_min_outcomes,
            "reason": f"Insufficient outcomes ({total_outcomes} < {eval_def.mute_min_outcomes})",
        }

    # Check if acceptance rate is below threshold
    should_mute = acceptance_rate < eval_def.mute_threshold
    reason = (
        f"Acceptance rate {acceptance_rate:.2%} below threshold {eval_def.mute_threshold:.2%} "
        f"with {total_outcomes} outcomes"
        if should_mute
        else f"Acceptance rate {acceptance_rate:.2%} meets threshold {eval_def.mute_threshold:.2%}"
    )

    return {
        "should_mute": should_mute,
        "acceptance_rate": acceptance_rate,
        "total_outcomes": total_outcomes,
        "threshold": eval_def.mute_threshold,
        "min_outcomes": eval_def.mute_min_outcomes,
        "reason": reason,
    }


async def apply_mute_policy(
    session: AsyncSession,
    eval_id: str,
    org_id: str,
    muted_by: str | None = None,
    window_days: int | None = None,
    automatic: bool = False,
) -> Eval | None:
    """Evaluate and apply mute policy for an eval/guardrail definition.

    If the policy indicates the definition should be muted and it's not already muted,
    updates the definition to muted state.

    Args:
        session: Database session with RLS org context already set.
        eval_id: UUID of the Eval definition.
        org_id: Organisation ID (for RLS scoping).
        muted_by: Account ID of the user/system applying the mute (optional).
        window_days: Optional time window for the acceptance rate calculation.

    Returns:
        The updated Eval row if muted, None if no change.
    """
    eval_result = await session.execute(
        select(Eval)
        .where(
            Eval.id == eval_id,
            Eval.organisation_id == org_id,
        )
        .with_for_update()
    )
    eval_def = eval_result.scalar_one_or_none()
    if eval_def is None:
        return None

    # The row lock serializes concurrent threshold evaluations for this
    # definition. The acceptance-rate read and state transition therefore share
    # one transaction and cannot both observe an unmuted definition.
    if eval_def.is_muted:
        return None

    mute_eval = await evaluate_mute_policy(session, eval_id, org_id, window_days)
    if mute_eval["should_mute"]:
        eval_def.is_muted = True
        eval_def.muted_at = datetime.now(UTC)
        eval_def.muted_by = uuid.UUID(muted_by) if muted_by else None
        eval_def.acceptance_rate = mute_eval["acceptance_rate"]
        await session.flush()
        if automatic:
            from modulo.core.eval.mute_policy import record_mute_audit_event

            await record_mute_audit_event(
                session,
                eval_id,
                org_id,
                "muted",
                acceptance_rate=mute_eval["acceptance_rate"],
                threshold=mute_eval["threshold"],
                total_outcomes=mute_eval["total_outcomes"],
                actor_source="system",
                automatic=True,
            )
        return eval_def

    # Update acceptance rate even if not muting
    eval_def.acceptance_rate = mute_eval["acceptance_rate"]
    await session.flush()
    return None


async def unmute_definition(
    session: AsyncSession,
    eval_id: str,
    org_id: str,
) -> Eval | None:
    """Manually unmute an eval/guardrail definition.

    Args:
        session: Database session with RLS org context already set.
        eval_id: UUID of the Eval definition.
        org_id: Organisation ID (for RLS scoping).

    Returns:
        The updated Eval row if it was muted, None if not found or not muted.
    """
    eval_result = await session.execute(
        select(Eval).where(
            Eval.id == eval_id,
            Eval.organisation_id == org_id,
        )
    )
    eval_def = eval_result.scalar_one_or_none()
    if eval_def is None or not eval_def.is_muted:
        return None

    eval_def.is_muted = False
    eval_def.muted_at = None
    eval_def.muted_by = None
    # Recompute acceptance rate on unmute
    acceptance_rate, _ = await compute_acceptance_rate(session, eval_id, org_id)
    eval_def.acceptance_rate = acceptance_rate
    await session.flush()
    return eval_def
