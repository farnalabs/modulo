"""Single-node correction mechanics for the FeedbackManager.

Extracted from the monolithic FeedbackManager to improve maintainability.
Handles the FAR-210 single-node correction path, including retry budgets,
idempotency, and outcome persistence.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.feedback_manager.exceptions import (
    ConcurrentModificationError,
    FeedbackRecordNotFoundError,
    InvalidTransitionError,
)
from modulo.core.feedback_manager.status import CORRECTION_TERMINAL_STATUSES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorrectionRunContext:
    """Immutable parameters describing one single-node correction run.

    Bundles the correction definition, its guardrail, the restricted backend,
    the violating node input, and the bound guardrails / eval config so the
    retry loop and resume path do not thread a long argument list between
    helpers.
    """

    correction: Any
    guardrail: Any
    node_input: dict[str, Any]
    backend: Any
    bound_guardrails: list[Any] | None = None
    revalidation_config: dict[str, Any] | None = None
    judge_callable: Any | None = None


def validate_correction_eligibility(record: Any, ctx: CorrectionRunContext) -> None:
    """Fence a single-node correction: no terminal records, restricted backend only."""
    if record.feedback_status in CORRECTION_TERMINAL_STATUSES:
        raise InvalidTransitionError(
            f"FeedbackRecord {record.id} is in terminal status "
            f"'{record.feedback_status}'; cannot run a single-node correction "
            f"on a record a human has already decided on"
        )

    ctx.correction.validate_guardrail_binding(ctx.guardrail)
    backend_capabilities = getattr(ctx.backend, "capabilities", ())
    if not isinstance(backend_capabilities, (list, tuple, set)):
        backend_capabilities = ()
    ctx.correction.validate_restricted_backend(list(backend_capabilities))


async def resume_correction_from_state(
    *,
    ctx: CorrectionRunContext,
    idem_key: str,
    persisted_state: dict[str, Any],
    attempt: int,
) -> Any | None:
    """Re-validate a recorded outcome on an idempotent re-dispatch.

    Returns None when there is nothing to resume (the persisted state does
    not match this dispatch's idempotency key) or when a still-violating
    recorded output falls through to a fresh attempt.
    """
    from modulo.core.guardrails.correction import (
        CorrectionVerdict,
        resume_interrupted_correction,
    )

    if persisted_state.get("idempotency_key") != idem_key:
        return None
    outcome = await resume_interrupted_correction(
        correction=ctx.correction,
        _guardrail=ctx.guardrail,
        _backend=ctx.backend,
        state=persisted_state,
        revalidation_config=ctx.revalidation_config,
        judge_callable=ctx.judge_callable,
    )
    if outcome.verdict == CorrectionVerdict.STILL_VIOLATING and attempt < ctx.correction.max_attempts:
        return None
    return outcome


async def claim_correction_slot(
    session: AsyncSession,
    org_id: UUID,
    ctx: CorrectionRunContext,
    record_id: UUID,
) -> None:
    """Claim the org-wide concurrent-correction slot once, raising when the cap is reached."""
    from modulo.core.audit_logger import append_audit_event
    from modulo.core.guardrails.correction import (
        EVENT_CORRECTION_CAP_BLOCKED,
        CorrectionCapExceededError,
        claim_correction_slot as _claim_slot,
    )

    admitted = await _claim_slot(
        session,
        org_id=org_id,
        correction=ctx.correction,
        exclude_record_id=record_id,
    )
    if not admitted:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=EVENT_CORRECTION_CAP_BLOCKED,
            resource_type="feedback",
            resource_id=record_id,
            payload_json={"correction_id": ctx.correction.id, "reason": "org concurrent-correction cap reached"},
        )
        raise CorrectionCapExceededError(
            f"Correction {ctx.correction.id!r} blocked at claim time: org concurrent-correction cap reached"
        )


async def run_correction_attempts(
    *,
    session: AsyncSession,
    org_id: UUID,
    ctx: CorrectionRunContext,
    record: Any,
    idem_key: str,
    prior_states: list[dict[str, Any]],
    attempt: int,
) -> Any | None:
    """Run fresh LM attempts until convergence, budget exhaustion, or a terminal verdict.

    Returns None when the budget was already exhausted at entry (no attempt
    ran); the caller converts that into a BUDGET_EXHAUSTED outcome.
    """
    from modulo.core.audit_logger import append_audit_event
    from modulo.core.feedback_manager.status import prior_states_for_retry
    from modulo.core.guardrails.correction import (
        EVENT_CORRECTION_ATTEMPTED,
        CorrectionVerdict,
    )
    from modulo.core.guardrails.correction import (
        dispatch_single_node_correction as _run_correction,
    )

    outcome = None
    while attempt < ctx.correction.max_attempts:
        attempt += 1
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=EVENT_CORRECTION_ATTEMPTED,
            resource_type="feedback",
            resource_id=record.id,
            payload_json={
                "correction_id": ctx.correction.id,
                "guardrail_id": ctx.correction.guardrail_id,
                "node_id": str(record.producing_node_id),
                "attempt": attempt,
            },
        )
        retry_prior = prior_states_for_retry(prior_states) if attempt > 1 else prior_states
        outcome = await _run_correction(
            correction=ctx.correction,
            guardrail=ctx.guardrail,
            node_input=ctx.node_input,
            backend=ctx.backend,
            prior_states=retry_prior,
            idempotency_key=idem_key,
            attempt=attempt,
            revalidation_config=ctx.revalidation_config,
            judge_callable=ctx.judge_callable,
            bound_guardrails=ctx.bound_guardrails,
        )
        outcome.state["produced_output"] = outcome.produced_output
        record.correction_state = outcome.state
        prior_states.append(dict(outcome.state))
        if outcome.verdict != CorrectionVerdict.STILL_VIOLATING:
            break
    return outcome


def budget_exhausted_outcome(
    *,
    ctx: CorrectionRunContext,
    attempt: int,
    persisted_state: dict[str, Any],
) -> Any:
    """Build the terminal BUDGET_EXHAUSTED outcome for a fully-consumed budget."""
    from modulo.core.guardrails.correction import CorrectionOutcome, CorrectionVerdict

    return CorrectionOutcome(
        verdict=CorrectionVerdict.BUDGET_EXHAUSTED,
        detail=(
            f"correction budget exhausted (recorded attempt {attempt} of "
            f"{ctx.correction.max_attempts}): no fresh attempt available"
        ),
        needs_human_review=True,
        state=dict(persisted_state),
    )


async def apply_correction_violated_check(
    *,
    outcome: Any,
    ctx: CorrectionRunContext,
) -> Any:
    """Escalate ``correction_violated`` when the corrected output violates a bound guardrail.

    The corrected output is CONTINUING-SUSPICIOUS — a produced output that
    itself violates a (different) bound guardrail is never silently
    accepted. Only a RESOLVED outcome is checked (already-violating /
    errored outcomes are already escalated).
    """
    from modulo.core.guardrails.correction import (
        CorrectionVerdict,
        check_corrected_output_violates_guardrails,
    )

    if outcome.verdict != CorrectionVerdict.RESOLVED or outcome.produced_output is None:
        return outcome
    violator = await check_corrected_output_violates_guardrails(
        corrected_output=outcome.produced_output,
        guardrails=ctx.bound_guardrails or [],
        exclude_name=ctx.guardrail.name,
    )
    if violator is not None:
        from dataclasses import replace

        return replace(
            outcome,
            verdict=CorrectionVerdict.CORRECTION_VIOLATED,
            detail=f"correction_violated: corrected output violates bound guardrail {violator!r}",
            needs_human_review=True,
        )
    return outcome


async def update_status_fenced(
    session: AsyncSession,
    *,
    org_id: UUID,
    record_id: UUID,
    new_status: str,
    values: dict[str, Any],
    failure_message: str,
) -> Any:
    """Run a status UPDATE fenced on the record still being ``correcting``.

    Returns the updated row; raises ``ConcurrentModificationError`` when no
    row matched (the record left the ``correcting`` pre-state concurrently,
    so the write would otherwise silently reverse a human decision).
    """
    from sqlalchemy import update

    from modulo.db.models.feedback_record import FeedbackRecord

    updated = (
        await session.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == org_id,
                FeedbackRecord.feedback_status == "correcting",
            )
            .values(**values, feedback_status=new_status)
            .returning(FeedbackRecord)
        )
    ).scalar_one_or_none()
    if updated is None:
        raise ConcurrentModificationError(
            f"FeedbackRecord {record_id} status changed concurrently. {failure_message}"
        )
    return updated


async def persist_correction_outcome(
    session: AsyncSession,
    *,
    org_id: UUID,
    record_id: UUID,
    correction: Any,
    outcome: Any,
) -> None:
    """Persist a single-node correction outcome on the FeedbackRecord.

    RESOLVED transitions the record to ``resolved``; every other verdict
    (still-violating, converged, budget-exhausted, lm-error, interrupted,
    correction-violated) escalates to HITL (``escalated``) with a
    machine-readable reason.
    """
    from modulo.core.audit_logger import append_audit_event
    from modulo.core.guardrails.correction import (
        EVENT_CORRECTION_ESCALATED,
        EVENT_CORRECTION_RESOLVED,
        EVENT_CORRECTION_VIOLATED,
        CorrectionVerdict,
    )

    verdict = CorrectionVerdict(outcome.verdict)
    if verdict == CorrectionVerdict.RESOLVED:
        await update_status_fenced(
            session,
            org_id=org_id,
            record_id=record_id,
            new_status="resolved",
            values={"needs_human_review": outcome.needs_human_review},
            failure_message="Expected 'correcting', failed to persist a RESOLVED correction outcome.",
        )
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=EVENT_CORRECTION_RESOLVED,
            resource_type="feedback",
            resource_id=record_id,
            payload_json={
                "correction_id": correction.id,
                "guardrail_id": correction.guardrail_id,
                "detail": (outcome.detail or "")[:500],
            },
        )
        return
    await update_status_fenced(
        session,
        org_id=org_id,
        record_id=record_id,
        new_status="escalated",
        values={"needs_human_review": True},
        failure_message="Expected 'correcting', failed to persist an escalated correction outcome.",
    )
    violation_event = (
        EVENT_CORRECTION_VIOLATED
        if verdict == CorrectionVerdict.CORRECTION_VIOLATED
        else EVENT_CORRECTION_ESCALATED
    )
    await append_audit_event(
        session,
        org_id=org_id,
        event_type=violation_event,
        resource_type="feedback",
        resource_id=record_id,
        payload_json={
            "correction_id": correction.id,
            "guardrail_id": correction.guardrail_id,
            "verdict": verdict.value,
            "detail": (outcome.detail or "")[:500],
        },
    )
    logger.warning(
        "Single-node correction escalated FeedbackRecord %s verdict=%s",
        record_id,
        verdict.value,
    )
