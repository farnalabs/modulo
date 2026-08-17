"""FeedbackManager — FeedbackRecord lifecycle, eval gap detection, correction run spawning.

The Feedback System (§8.20) treats every human rejection as structured signal.
This module manages the FeedbackRecord entity, status transitions, eval gap
detection via EvalEngine.standalone_evaluate(), and correction run mechanics.
"""

import asyncio
import functools
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalEngine
from modulo.core.node_output_split import node_return
from modulo.db.crud.run import create_run, get_run
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run

logger = logging.getLogger(__name__)

_VALID_FEEDBACK_HANDLER_TYPES = frozenset(
    {
        "human",
        "ai_correction",
        "ai_correction_with_human_review",
    }
)
_AI_HANDLER_TYPES = frozenset(
    {
        "ai_correction",
        "ai_correction_with_human_review",
    }
)
_POST_CORRECTION_EVAL_NAME = "post_correction_eval"
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


class FeedbackManagerError(Exception):
    """Base exception for FeedbackManager errors."""


class FeedbackRecordNotFoundError(FeedbackManagerError):
    """Raised when a FeedbackRecord is not found."""


class FeedbackRecordRunNotFoundError(FeedbackManagerError):
    """Raised when the original run referenced by a FeedbackRecord is not found.

    Distinct from :class:`FeedbackRecordNotFoundError`: the record exists, but
    the run it points at is gone. API routes map this to 404 while leaving the
    base :class:`FeedbackManagerError` catch free for genuinely unexpected
    subclasses (e.g. :class:`ValidationError`) rather than also collapsing them
    to 404.
    """


class InvalidTransitionError(FeedbackManagerError):
    """Raised when a feedback status transition is not allowed."""


class ConcurrentModificationError(FeedbackManagerError):
    """Raised when concurrent modification prevents a status transition."""


class ValidationError(FeedbackManagerError):
    """Raised when input validation fails."""


_VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"routing", "correcting", "resolved", "dismissed"},
    "routing": {"escalated", "correcting", "resolved", "dismissed"},
    "correcting": {"correcting", "resolved", "escalated", "dismissed"},
    "escalated": {"resolved", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}

# Statuses on which a single-node correction outcome may NEVER be written: the
# human has already decided (``escalated`` -> HITL review, ``resolved``,
# ``dismissed``). Re-entering one via the correction path would silently reverse
# that decision, so dispatch/resume gates on non-terminal status and
# ``_persist_correction_outcome`` fences its writes on the ``correcting``
# pre-state (review FAR-210 finding 3).
_CORRECTION_TERMINAL_STATUSES = frozenset({"resolved", "escalated", "dismissed"})


def _rls(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def wrapper(self: "FeedbackManager", *args: Any, **kwargs: Any) -> Any:
        return await method(self, *args, **kwargs)

    return wrapper


def _prior_states_for_retry(prior_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return prior states with ``input_fingerprint`` stripped for retry attempts.

    Within the single-node correction's retry loop the corrected INPUT is
    unchanged across attempts, so a prior state's own ``input_fingerprint``
    would match on every retry and spuriously converge the correction before
    the fresh LM attempt runs. Output fingerprints are preserved so a repeated
    produced output (genuine oscillation) still converges. The INPUT violation
    metric is likewise stripped (the same input carries the same metric on
    every retry — a repeated input metric is not oscillation); the OUTPUT
    violation metric is preserved so a strictly-worse OR repeated output
    violation still converges.
    """
    stripped: list[dict[str, Any]] = []
    for state in prior_states:
        entry = dict(state)
        entry.pop("input_fingerprint", None)
        entry.pop("input_violation_metric", None)
        stripped.append(entry)
    return stripped


class FeedbackManager:
    """Manages the feedback lifecycle: creation, status transitions, eval gap detection."""

    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        self._session = session
        self._org_id = org_id

    @_rls
    async def create_feedback_record(
        self,
        run_id: UUID,
        gate_id: str,
        account_id: UUID,
        rejection_reason: str,
        rejected_output: dict[str, Any],
        producing_node_id: str,
        producing_agent_id: UUID | None = None,
        feedback_handler_type: str = "human",
    ) -> FeedbackRecord:
        stripped_reason = rejection_reason.strip() if rejection_reason else ""
        if not stripped_reason:
            raise ValidationError("rejection_reason must not be empty")
        if len(stripped_reason) > 5000:
            raise ValidationError("rejection_reason must not exceed 5000 characters")

        output_json = json.dumps(rejected_output, default=str)
        if len(output_json) > 100_000:
            raise ValidationError("rejected_output must not exceed 100KB when serialized")

        if feedback_handler_type not in _VALID_FEEDBACK_HANDLER_TYPES:
            raise ValidationError(
                f"unknown feedback_handler_type '{feedback_handler_type}'. "
                f"Valid: {sorted(_VALID_FEEDBACK_HANDLER_TYPES)}"
            )
        record = FeedbackRecord(
            organisation_id=self._org_id,
            run_id=run_id,
            gate_id=gate_id,
            account_id=account_id,
            rejection_reason=stripped_reason,
            rejected_output=rejected_output,
            producing_node_id=producing_node_id,
            producing_agent_id=producing_agent_id,
            feedback_status="pending",
            feedback_handler_type=feedback_handler_type,
        )
        self._session.add(record)
        await self._session.flush()

        # Auto-trigger correction run for AI correction handlers (§8.20)
        if feedback_handler_type in _AI_HANDLER_TYPES:
            await self.update_status(record.id, "correcting")
            await self.spawn_correction_run(record.id)

        logger.info(
            "Created FeedbackRecord %s (run=%s, handler=%s)",
            record.id,
            run_id,
            feedback_handler_type,
        )
        return record

    def _validate_pagination(self, page: int, page_size: int) -> None:
        if page < 1:
            raise ValidationError(f"page must be >= 1, got {page}")
        if page_size < 1:
            raise ValidationError(f"page_size must be >= 1, got {page_size}")
        if page_size > _MAX_PAGE_SIZE:
            raise ValidationError(f"page_size must be <= {_MAX_PAGE_SIZE}, got {page_size}")

    async def _paginate(
        self,
        conditions: list[Any],
        page: int,
        page_size: int,
        include_total: bool = True,
    ) -> tuple[list[FeedbackRecord], int]:
        self._validate_pagination(page, page_size)
        if not conditions:
            logger.warning("_paginate called with empty conditions — no tenant filter applied")
        total = 0
        if include_total:
            total_q = select(func.count()).select_from(select(FeedbackRecord).where(*conditions).subquery())
            total = (await self._session.execute(total_q)).scalar() or 0
        offset = (page - 1) * page_size
        q = (
            select(FeedbackRecord)
            .where(*conditions)
            .order_by(FeedbackRecord.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = (await self._session.execute(q)).scalars().all()
        return list(rows), total

    @_rls
    async def get_feedback_records(
        self,
        status: str | None = None,
        pipeline_id: UUID | None = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        include_total: bool = True,
    ) -> dict[str, Any]:
        conditions = [FeedbackRecord.organisation_id == self._org_id]
        if status:
            conditions.append(FeedbackRecord.feedback_status == status)
        if pipeline_id:
            run_subq = select(Run.id).where(Run.pipeline_id == pipeline_id, Run.organisation_id == self._org_id)
            conditions.append(FeedbackRecord.run_id.in_(run_subq))

        rows, total = await self._paginate(conditions, page, page_size, include_total)

        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @_rls
    async def get_feedback_record(self, record_id: UUID) -> FeedbackRecord | None:
        result = await self._session.execute(
            select(FeedbackRecord).where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == self._org_id,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            logger.warning("FeedbackRecord %s not found for org %s", record_id, self._org_id)
        return record

    @_rls
    async def update_status(self, record_id: UUID, new_status: str) -> FeedbackRecord:
        current = (
            await self._session.execute(
                select(FeedbackRecord).where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                )
            )
        ).scalar_one_or_none()
        if current is None:
            raise FeedbackRecordNotFoundError(f"FeedbackRecord {record_id} not found")
        allowed = _VALID_STATUS_TRANSITIONS.get(current.feedback_status, set())
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition FeedbackRecord {record_id} from "
                f"'{current.feedback_status}' to '{new_status}'. "
                f"Allowed: {sorted(allowed) or '<terminal>'}"
            )
        result = await self._session.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == self._org_id,
                FeedbackRecord.feedback_status == current.feedback_status,
            )
            .values(feedback_status=new_status)
            .returning(FeedbackRecord)
        )
        updated = result.scalar_one_or_none()
        if updated is None:
            raise ConcurrentModificationError(
                f"FeedbackRecord {record_id} status changed concurrently. "
                f"Expected '{current.feedback_status}', retry the transition."
            )
        logger.info("FeedbackRecord %s status: %s → %s", record_id, current.feedback_status, new_status)
        return updated

    @_rls
    async def link_correction_run(self, record_id: UUID, correction_run_id: UUID) -> FeedbackRecord:
        current = (
            await self._session.execute(
                select(FeedbackRecord).where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                )
            )
        ).scalar_one_or_none()
        if current is None:
            raise FeedbackRecordNotFoundError(f"FeedbackRecord {record_id} not found")
        allowed = _VALID_STATUS_TRANSITIONS.get(current.feedback_status, set())
        if "correcting" not in allowed:
            raise InvalidTransitionError(
                f"Cannot link correction run to FeedbackRecord {record_id} in "
                f"status '{current.feedback_status}'. "
                f"Allowed transitions: {sorted(allowed) or '<terminal>'}"
            )
        if current.correction_run_id is not None:
            raise ConcurrentModificationError(
                f"FeedbackRecord {record_id} already has a correction run linked: {current.correction_run_id}"
            )
        result = await self._session.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == self._org_id,
                FeedbackRecord.feedback_status == current.feedback_status,
                FeedbackRecord.correction_run_id.is_(None),
            )
            .values(correction_run_id=correction_run_id, feedback_status="correcting")
            .returning(FeedbackRecord)
        )
        updated = result.scalar_one_or_none()
        if updated is None:
            raise ConcurrentModificationError(
                f"FeedbackRecord {record_id} status changed concurrently. "
                f"Expected '{current.feedback_status}', retry the link."
            )
        logger.info("Linked correction run %s to FeedbackRecord %s", correction_run_id, record_id)
        return updated

    @staticmethod
    def _normalise_eval_def(eval_def: Any) -> Any:
        """Normalise an ORM ``EvalDefinition`` row to the engine's DTO shape.

        ``EvalEngine.evaluate`` reads ``eval_def.config``, but the ORM model
        exposes ``config_json`` and no ``config`` property. Without this
        conversion every ORM eval_def raises AttributeError inside ``evaluate()``,
        which the generic handler swallows and reports as ``eval_gap=True`` for
        every record (FAR-233 review MAJOR-1). Raw config dicts and already-DTO
        definitions pass through unchanged.
        """
        if isinstance(eval_def, dict) or hasattr(eval_def, "config"):
            return eval_def
        if hasattr(eval_def, "config_json"):
            from modulo.core.eval_engine import EvalDefinition as EvalDefinitionDTO

            return EvalDefinitionDTO(
                id=eval_def.id,
                org_id=eval_def.organisation_id,
                pipeline_id=eval_def.pipeline_id,
                node_id=str(eval_def.node_id) if eval_def.node_id else None,
                name=eval_def.name,
                eval_type=eval_def.eval_type,
                config=eval_def.config_json,
                failure_behaviour=eval_def.failure_behaviour,
                pass_threshold=float(eval_def.pass_threshold) if eval_def.pass_threshold is not None else None,
                suite_id=eval_def.suite_id,
            )
        return eval_def

    @_rls
    async def detect_eval_gap(
        self,
        record: FeedbackRecord,
        eval_engine: EvalEngine | None = None,
        eval_suite: list[Any] | None = None,
    ) -> bool:
        """Run the pipeline's eval suite against the rejected output.

        If no eval scored the output as failing, tag the record with eval_gap = True.
        Returns True if there is an eval gap (no eval caught the failure).
        """
        if eval_engine is None:
            eval_engine = EvalEngine()
        if not eval_suite:
            logger.warning("detect_eval_gap called with empty eval_suite for FeedbackRecord %s", record.id)
            record.eval_gap = True
            return True
        processed_count = 0
        for eval_def in eval_suite:
            # A valid eval_def is either a raw config dict or an EvalDefinition
            # (ORM or DTO) carrying an ``eval_type`` — anything else is malformed.
            if not isinstance(eval_def, dict) and not hasattr(eval_def, "eval_type"):
                logger.warning("Malformed eval_def in eval_suite: %s", eval_def)
                continue
            processed_count += 1
            try:
                result = eval_engine.evaluate(record.rejected_output, self._normalise_eval_def(eval_def))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "EvalEngine.evaluate failed for FeedbackRecord %s on eval_def %s",
                    record.id,
                    eval_def,
                )
                continue
            if not result.passed:
                return False
        if processed_count == 0:
            logger.warning(
                "detect_eval_gap: all %d eval_defs in eval_suite were malformed for FeedbackRecord %s",
                len(eval_suite),
                record.id,
            )
        record.eval_gap = True
        await self._session.flush()
        logger.info("Eval gap detected for FeedbackRecord %s", record.id)
        return True

    @_rls
    async def spawn_correction_run(
        self,
        record_id: UUID,
        run_context_overrides: dict[str, Any] | None = None,
    ) -> UUID:
        """Create a new correction run pre-seeded from the original feedback run.

        1. Fetch the FeedbackRecord by ID.
        2. Fetch the original run (the one that produced the rejected output).
        3. Create a new run with ``parent_run_id`` set to the original run_id,
           copying the original's pipeline_id, snapshot_id, and input_payload.
        4. Pass a feedback_correction block via create_run's explicit
           ``feedback_correction`` kwarg (reserved-key safe: create_run strips
           any user-supplied ``_feedback_correction`` first, then injects this
           engine-only value post-strip) so the executor promotes it to
           ``run_context``.
        5. Link the correction run to the FeedbackRecord and transition status
           to ``correcting``.
        6. Return the new run_id.

        Args:
            record_id: The FeedbackRecord to spawn a correction for.
            run_context_overrides: Optional extra keys to merge into the
                correction run's feedback_correction block.

        Returns:
            The UUID of the newly created correction run.

        """
        record = await self.get_feedback_record(record_id)
        if record is None:
            raise FeedbackRecordNotFoundError(f"FeedbackRecord {record_id} not found")

        if record.correction_run_id is not None:
            raise ConcurrentModificationError(
                f"FeedbackRecord {record_id} already has a correction run: {record.correction_run_id}"
            )

        original_run = await get_run(self._session, record.run_id)
        if original_run is None:
            raise FeedbackRecordRunNotFoundError(
                f"Original run {record.run_id} not found for FeedbackRecord {record_id}"
            )

        feedback_correction: dict[str, Any] = {
            "rejection_reason": record.rejection_reason,
            "rejected_output": record.rejected_output,
            "producing_node_id": record.producing_node_id,
            "is_correction_run": True,
        }
        if run_context_overrides:
            feedback_correction.update(run_context_overrides)

        input_payload = dict(original_run.input_payload or {})

        new_run = await create_run(
            self._session,
            org_id=self._org_id,
            pipeline_id=original_run.pipeline_id,
            snapshot_id=original_run.snapshot_id,
            trigger_type="correction",
            input_payload=input_payload,
            account_id=record.account_id,
            parent_run_id=record.run_id,
            feedback_correction=feedback_correction,
        )

        await self.link_correction_run(record_id, new_run.id)

        logger.info(
            "Spawned correction run %s for FeedbackRecord %s (original run %s)",
            new_run.id,
            record_id,
            record.run_id,
        )
        return new_run.id

    @_rls
    async def run_single_node_correction(
        self,
        *,
        record_id: UUID,
        guardrail: Any,
        correction: Any,
        node_input: dict[str, Any],
        backend: Any,
        bound_guardrails: list[Any] | None = None,
        revalidation_config: dict[str, Any] | None = None,
        judge_callable: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """FAR-210 T2b: run the single-node correction path (NOT spawn_correction_run).

        This is the genuinely-new bounded single-node correction. It never
        re-runs the pipeline (unlike :meth:`spawn_correction_run`) — it runs
        the RESTRICTED correction backend over the pre-redacted violating node
        input, re-validates the produced output with a DIFFERENT-FAMILY
        detector, records the idempotency key + prior fingerprints on the
        FeedbackRecord, and escalates to HITL on any persistent violation.

        Retry budget (MAJOR-6): a STILL_VIOLATING outcome issues a FRESH LM
        attempt while attempts remain below ``max_attempts`` — never a
        re-validation of the recorded output. Only budget exhaustion (or a
        terminal verdict: converged / correction_violated / lm_error)
        escalates. An interrupted correction re-dispatched with the same
        idempotency key resumes by RE-VALIDATING the recorded output (never
        re-running the LM), and falls through to a fresh attempt when the
        recorded output is still violating and attempts remain.

        Claim-time concurrency cap is enforced here (mirrors the sandbox-cap
        pattern — a dispatch-time count would TOCTOU). The record currently
        being corrected is excluded from the cap count so the first correction
        never blocks itself. Dispatch/resume is gated on a NON-TERMINAL status:
        a record the human has already decided on (``resolved``, ``escalated``,
        ``dismissed``) raises ``InvalidTransitionError`` before any LM work,
        and ``_persist_correction_outcome`` fences its status writes on
        ``correcting`` so a concurrent decision is never reversed. Returns the
        correction outcome dict.
        """
        from modulo.core.audit_logger import append_audit_event
        from modulo.core.guardrails.correction import (
            EVENT_CORRECTION_ATTEMPTED,
            EVENT_CORRECTION_CAP_BLOCKED,
            CorrectionCapExceededError,
            CorrectionOutcome,
            CorrectionVerdict,
            build_idempotency_key,
            claim_correction_slot,
            redact_payload,
            resume_interrupted_correction,
        )
        from modulo.core.guardrails.correction import (
            dispatch_single_node_correction as _run_correction,
        )

        record = await self.get_feedback_record(record_id)
        if record is None:
            raise FeedbackRecordNotFoundError(f"FeedbackRecord {record_id} not found")

        if record.feedback_status in _CORRECTION_TERMINAL_STATUSES:
            # Finding 3 (review FAR-210): never run a correction on a record a
            # human has already decided on. A terminal record (``resolved`` /
            # ``escalated`` / ``dismissed``) re-entered via dispatch or resume
            # would silently reverse that decision — fail fast instead of
            # re-writing the status.
            raise InvalidTransitionError(
                f"FeedbackRecord {record_id} is in terminal status "
                f"'{record.feedback_status}'; cannot run a single-node correction "
                f"on a record a human has already decided on"
            )

        correction.validate_guardrail_binding(guardrail)
        # FAR-210: the correction backend is RESTRICTED — it must not claim any
        # vault/guardrail-config capability. Defensive: read the backend's
        # declared capability surface (empty = restricted; a privileged backend
        # would declare vault/guardrail_config access).
        backend_capabilities = getattr(backend, "capabilities", ())
        if not isinstance(backend_capabilities, (list, tuple, set)):
            backend_capabilities = ()
        correction.validate_restricted_backend(list(backend_capabilities))

        redacted_input = redact_payload(node_input, correction.input_redaction_patterns)
        idem_key = build_idempotency_key(
            org_id=self._org_id,
            run_id=record.run_id,
            node_id=record.producing_node_id,
            correction_id=correction.id,
            redacted_input=redacted_input,
        )
        persisted_state = dict(record.correction_state or {})
        prior_states: list[dict[str, Any]] = [persisted_state] if persisted_state else []
        attempt = int(persisted_state.get("attempt") or 0)

        outcome: CorrectionOutcome | None = None
        if persisted_state.get("idempotency_key") == idem_key:
            # Idempotent re-dispatch: RE-VALIDATE the recorded produced output
            # (never re-run the LM for a resume). A still-violating recorded
            # output with attempts remaining falls through to a fresh attempt.
            outcome = await resume_interrupted_correction(
                correction=correction,
                guardrail=guardrail,
                backend=backend,
                state=persisted_state,
                revalidation_config=revalidation_config,
                judge_callable=judge_callable,
            )
            if outcome.verdict == CorrectionVerdict.STILL_VIOLATING and attempt < correction.max_attempts:
                outcome = None

        if outcome is None:
            # Claim the org-wide concurrent-correction slot ONCE for this
            # correction (the whole retry sequence holds one slot; the current
            # record is excluded from the count).
            admitted = await claim_correction_slot(
                self._session,
                org_id=self._org_id,
                correction=correction,
                exclude_record_id=record_id,
            )
            if not admitted:
                await append_audit_event(
                    self._session,
                    org_id=self._org_id,
                    event_type=EVENT_CORRECTION_CAP_BLOCKED,
                    resource_type="feedback",
                    resource_id=record_id,
                    payload_json={"correction_id": correction.id, "reason": "org concurrent-correction cap reached"},
                )
                raise CorrectionCapExceededError(
                    f"Correction {correction.id!r} blocked at claim time: org concurrent-correction cap reached"
                )
            while attempt < correction.max_attempts:
                attempt += 1
                await append_audit_event(
                    self._session,
                    org_id=self._org_id,
                    event_type=EVENT_CORRECTION_ATTEMPTED,
                    resource_type="feedback",
                    resource_id=record_id,
                    payload_json={
                        "correction_id": correction.id,
                        "guardrail_id": correction.guardrail_id,
                        "node_id": record.producing_node_id,
                        "attempt": attempt,
                    },
                )
                # A retry re-runs the LM on the SAME redacted input, so its own
                # prior input fingerprint must not spuriously converge it; only
                # a repeated produced OUTPUT is oscillation.
                retry_prior = _prior_states_for_retry(prior_states) if attempt > 1 else prior_states
                outcome = await _run_correction(
                    correction=correction,
                    guardrail=guardrail,
                    node_input=node_input,
                    backend=backend,
                    prior_states=retry_prior,
                    idempotency_key=idem_key,
                    attempt=attempt,
                    revalidation_config=revalidation_config,
                    judge_callable=judge_callable,
                    bound_guardrails=bound_guardrails,
                )
                outcome.state["produced_output"] = outcome.produced_output
                record.correction_state = outcome.state
                prior_states.append(dict(outcome.state))
                if outcome.verdict != CorrectionVerdict.STILL_VIOLATING:
                    break
            if outcome is None:
                # The recorded state already consumed the whole budget and the
                # idempotency key did not match (no resume) — terminal exhaustion.
                outcome = CorrectionOutcome(
                    verdict=CorrectionVerdict.BUDGET_EXHAUSTED,
                    detail=(
                        f"correction budget exhausted (recorded attempt {attempt} of "
                        f"{correction.max_attempts}): no fresh attempt available"
                    ),
                    needs_human_review=True,
                    state=dict(persisted_state),
                )

        outcome.state["produced_output"] = outcome.produced_output
        record.correction_state = outcome.state

        outcome = await self._apply_correction_violated_check(
            outcome=outcome,
            guardrail=guardrail,
            correction=correction,
            bound_guardrails=bound_guardrails or [],
        )

        await self._persist_correction_outcome(
            record_id=record_id,
            record=record,
            guardrail=guardrail,
            correction=correction,
            outcome=outcome,
        )
        await self._session.flush()
        return {
            "verdict": outcome.verdict.value,
            "detail": outcome.detail,
            "needs_human_review": outcome.needs_human_review,
        }

    async def _apply_correction_violated_check(
        self,
        *,
        outcome: Any,
        guardrail: Any,
        correction: Any,
        bound_guardrails: list[Any],
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
            guardrails=bound_guardrails,
            exclude_name=guardrail.name,
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

    async def _persist_correction_outcome(
        self,
        *,
        record_id: UUID,
        record: Any,
        guardrail: Any,
        correction: Any,
        outcome: Any,
    ) -> None:
        """Persist a single-node correction outcome on the FeedbackRecord.

        RESOLVED transitions the record to ``resolved``; every other verdict
        (still-violating, converged, budget-exhausted, lm-error, interrupted,
        correction-violated) escalates to HITL (``escalated``) with a
        machine-readable reason. The corrected output is continuing-suspicious
        and redacted before persistence (the engine already redacted it).

        Both status writes are FENCED on the record still being ``correcting``
        (the correction path's expected pre-state, matching
        ``run_post_correction_eval`` and ``_escalate_record``): an UPDATE whose
        predicate matches no row means the status changed concurrently (e.g. a
        human escalated the record while the correction ran) and raises
        ``ConcurrentModificationError`` instead of silently reversing the
        decision (review FAR-210 finding 3).
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
            updated = (
                await self._session.execute(
                    update(FeedbackRecord)
                    .where(
                        FeedbackRecord.id == record_id,
                        FeedbackRecord.organisation_id == self._org_id,
                        FeedbackRecord.feedback_status == "correcting",
                    )
                    .values(feedback_status="resolved", needs_human_review=outcome.needs_human_review)
                    .returning(FeedbackRecord)
                )
            ).scalar_one_or_none()
            if updated is None:
                raise ConcurrentModificationError(
                    f"FeedbackRecord {record_id} status changed concurrently. "
                    f"Expected 'correcting', failed to persist a RESOLVED correction outcome."
                )
            await append_audit_event(
                self._session,
                org_id=self._org_id,
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
        updated = (
            await self._session.execute(
                update(FeedbackRecord)
                .where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                    FeedbackRecord.feedback_status == "correcting",
                )
                .values(feedback_status="escalated", needs_human_review=True)
                .returning(FeedbackRecord)
            )
        ).scalar_one_or_none()
        if updated is None:
            raise ConcurrentModificationError(
                f"FeedbackRecord {record_id} status changed concurrently. "
                f"Expected 'correcting', failed to persist an escalated correction outcome."
            )
        violation_event = (
            EVENT_CORRECTION_VIOLATED
            if verdict == CorrectionVerdict.CORRECTION_VIOLATED
            else EVENT_CORRECTION_ESCALATED
        )
        await append_audit_event(
            self._session,
            org_id=self._org_id,
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

    @_rls
    async def _escalate_record(
        self,
        record_id: UUID,
        reason: str,
    ) -> None:
        """Atomically escalate a FeedbackRecord, raising on concurrent modification."""
        result = await self._session.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == self._org_id,
                FeedbackRecord.feedback_status == "correcting",
            )
            .values(feedback_status="escalated")
            .returning(FeedbackRecord)
        )
        updated = result.scalar_one_or_none()
        if updated is None:
            raise ConcurrentModificationError(
                f"FeedbackRecord {record_id} status changed concurrently. "
                f"Expected 'correcting', failed to escalate: {reason}."
            )
        logger.warning(
            "Escalated FeedbackRecord %s: %s",
            record_id,
            reason,
        )

    @_rls
    async def run_post_correction_eval(
        self,
        record_id: UUID,
        eval_engine: EvalEngine | None = None,
        eval_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate the correction run's output and auto-resolve or flag for review.

        Called after a correction run completes.  Checks the corrected output
        via EvalEngine.standalone_evaluate() and:

          * ai_correction:              auto-resolves on pass
          * ai_correction_with_human_review: resolves but marks needs_human_review=True

        Args:
            record_id: The FeedbackRecord linked to the completed correction run.
            eval_engine: Optional EvalEngine instance (created fresh if omitted).
            eval_config: Optional config dict forwarded to standalone_evaluate().

        Returns:
            Dict with keys: passed, detail, score, needs_human_review.

        Raises:
            FeedbackRecordNotFoundError: If the record is missing.
            InvalidTransitionError: If the record is not in ``correcting`` state.
            FeedbackRecordNotFoundError: If the correction run is missing or not complete.

        """
        record = await self.get_feedback_record(record_id)
        if record is None:
            raise FeedbackRecordNotFoundError(f"FeedbackRecord {record_id} not found")
        if record.feedback_status != "correcting":
            raise InvalidTransitionError(
                f"FeedbackRecord {record_id} has status '{record.feedback_status}', expected 'correcting'"
            )
        if record.correction_run_id is None:
            raise InvalidTransitionError(f"FeedbackRecord {record_id} has no correction run linked")

        correction_run = await get_run(self._session, record.correction_run_id)
        if correction_run is None:
            raise FeedbackRecordNotFoundError(f"Correction run {record.correction_run_id} not found")
        if correction_run.status != "complete":
            raise InvalidTransitionError(
                f"Correction run {record.correction_run_id} has status '{correction_run.status}', expected 'complete'"
            )

        engine = eval_engine or EvalEngine()
        raw_output = correction_run.outputs_json
        if not raw_output:
            await self._escalate_record(
                record_id,
                f"Correction run {record.correction_run_id} produced no output",
            )
            return {
                "passed": False,
                "detail": "Correction run produced no output",
                "score": 0.0,
                "needs_human_review": True,
            }
        telemetry = correction_run.node_telemetry_json
        output = {nid: node_return(raw_output, telemetry, nid) for nid in raw_output}

        try:
            result = engine.standalone_evaluate(
                output,
                name=_POST_CORRECTION_EVAL_NAME,
                config=eval_config or {},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "standalone_evaluate failed for FeedbackRecord %s correction run %s",
                record_id,
                record.correction_run_id,
            )
            await self._escalate_record(
                record_id,
                f"Post-correction eval raised an error for correction run {record.correction_run_id}",
            )
            return {
                "passed": False,
                "detail": "Post-correction eval raised an error",
                "score": 0.0,
                "needs_human_review": True,
            }

        needs_human_review = False
        if result.passed:
            needs_human_review = record.feedback_handler_type == "ai_correction_with_human_review"
            result_update = await self._session.execute(
                update(FeedbackRecord)
                .where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                    FeedbackRecord.feedback_status == "correcting",
                )
                .values(
                    feedback_status="resolved",
                    needs_human_review=needs_human_review,
                )
                .returning(FeedbackRecord)
            )
            updated = result_update.scalar_one_or_none()
            if updated is None:
                raise ConcurrentModificationError(
                    f"FeedbackRecord {record_id} status changed concurrently. "
                    f"Expected 'correcting', retry the post-correction eval."
                )
        else:
            await self._escalate_record(
                record_id,
                f"Correction eval failed for correction run {record.correction_run_id}",
            )
        logger.info(
            "Post-correction eval for FeedbackRecord %s: passed=%s, needs_human_review=%s",
            record_id,
            result.passed,
            needs_human_review,
        )

        await self._session.flush()
        return {
            "passed": result.passed,
            "detail": result.detail,
            "score": result.score,
            "needs_human_review": needs_human_review,
        }

    @_rls
    async def _enrich_with_pipeline_names(self, rows: list[FeedbackRecord]) -> dict[str, str]:
        run_ids = list({r.run_id for r in rows if r.run_id})
        if not run_ids:
            return {}
        run_rows = (
            await self._session.execute(
                select(Run.id, Pipeline.name)
                .select_from(Run)
                .join(Pipeline, Run.pipeline_id == Pipeline.id)
                .where(Run.id.in_(run_ids))
            )
        ).all()
        return {str(run_id): pipeline_name for run_id, pipeline_name in run_rows}

    @_rls
    async def get_feedback_records_inbox(
        self,
        handler_type: str | None = None,
        status: str | None = None,
        pipeline_id: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        include_total: bool = True,
    ) -> dict[str, Any]:
        conditions = [FeedbackRecord.organisation_id == self._org_id]
        if handler_type:
            conditions.append(FeedbackRecord.feedback_handler_type == handler_type)
        if status:
            conditions.append(FeedbackRecord.feedback_status == status)
        if pipeline_id:
            run_subq = select(Run.id).where(Run.pipeline_id == pipeline_id, Run.organisation_id == self._org_id)
            conditions.append(FeedbackRecord.run_id.in_(run_subq))
        if date_from:
            conditions.append(FeedbackRecord.created_at >= date_from)
        if date_to:
            conditions.append(FeedbackRecord.created_at <= date_to)

        rows, total = await self._paginate(conditions, page, page_size, include_total)
        pipeline_map = await self._enrich_with_pipeline_names(rows)

        return {
            "items": rows,
            "pipeline_map": pipeline_map,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @_rls
    async def get_eval_proposals(
        self,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        include_total: bool = True,
    ) -> dict[str, Any]:
        conditions = [
            FeedbackRecord.organisation_id == self._org_id,
            FeedbackRecord.eval_gap.is_(True),
            FeedbackRecord.feedback_status.in_(["pending", "routing"]),
        ]
        rows, total = await self._paginate(conditions, page, page_size, include_total)

        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
