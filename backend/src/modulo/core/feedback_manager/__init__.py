"""FeedbackManager — FeedbackRecord lifecycle, eval gap detection, correction run spawning.

The Feedback System (§8.20) treats every human rejection as structured signal.
This module manages the FeedbackRecord entity, status transitions, eval gap
detection via EvalEngine.standalone_evaluate(), and correction run mechanics.

Sub-modules:
    exceptions: FeedbackManager exception types
    status: Status transitions, constants, and helper functions
    queries: Database query helpers for FeedbackRecord pagination and lookups
    single_node_correction: Single-node correction mechanics (FAR-210)
"""

__all__ = [
    "CORRECTION_TERMINAL_STATUSES",
    "VALID_STATUS_TRANSITIONS",
    "ConcurrentModificationError",
    "FeedbackManager",
    "FeedbackManagerError",
    "FeedbackRecordNotFoundError",
    "FeedbackRecordRunNotFoundError",
    "InvalidTransitionError",
    "ValidationError",
    "dispatch_reject_correction",
    "paginate_feedback_records",
]

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalEngine

# Re-export from sub-modules for backward compatibility
from modulo.core.feedback_manager.exceptions import (
    ConcurrentModificationError,
    FeedbackManagerError,
    FeedbackRecordNotFoundError,
    FeedbackRecordRunNotFoundError,
    InvalidTransitionError,
    ValidationError,
)
from modulo.core.feedback_manager.queries import (
    build_org_scoped_conditions as _build_org_scoped_conditions,
)
from modulo.core.feedback_manager.queries import (
    enrich_with_pipeline_names as _enrich_with_pipeline_names,
)
from modulo.core.feedback_manager.queries import (  # noqa: F401
    get_feedback_record_for_node as _get_feedback_record_for_node,
)
from modulo.core.feedback_manager.queries import (
    get_or_create_feedback_record as _get_or_create_feedback_record,
)
from modulo.core.feedback_manager.queries import (
    paginate_feedback_records,
)
from modulo.core.feedback_manager.queries import (
    paginated_response as _paginated_response,
)
from modulo.core.feedback_manager.single_node_correction import (
    CorrectionRunContext as _CorrectionRunContext,
)
from modulo.core.feedback_manager.single_node_correction import (
    apply_correction_violated_check as _apply_correction_violated_check,
)
from modulo.core.feedback_manager.single_node_correction import (
    budget_exhausted_outcome as _budget_exhausted_outcome,
)
from modulo.core.feedback_manager.single_node_correction import (
    claim_correction_slot as _claim_correction_slot,
)
from modulo.core.feedback_manager.single_node_correction import (
    persist_correction_outcome as _persist_correction_outcome,
)
from modulo.core.feedback_manager.single_node_correction import (
    resume_correction_from_state as _resume_correction_from_state,
)
from modulo.core.feedback_manager.single_node_correction import (
    run_correction_attempts as _run_correction_attempts,
)
from modulo.core.feedback_manager.single_node_correction import (
    update_status_fenced as _update_status_fenced,
)
from modulo.core.feedback_manager.single_node_correction import (
    validate_correction_eligibility as _validate_correction_eligibility,
)
from modulo.core.feedback_manager.status import (  # noqa: F401
    _AI_HANDLER_TYPES,
    _DEFAULT_PAGE_SIZE,
    _MAX_PAGE_SIZE,
    _POST_CORRECTION_EVAL_NAME,
    _VALID_FEEDBACK_HANDLER_TYPES,
    CORRECTION_TERMINAL_STATUSES,
    VALID_STATUS_TRANSITIONS,
)
from modulo.core.feedback_manager.status import (
    correction_guardrail_from as _correction_guardrail_from,
)
from modulo.core.feedback_manager.status import (
    handler_type_label as _handler_type_label,
)
from modulo.core.node_output_split import node_return
from modulo.db.crud.run import create_run, get_run
from modulo.db.crud.run_node_outputs import read_run_blobs
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.utils.uuid import coerce_uuid

logger = logging.getLogger(__name__)


def _rls(method: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for RLS-scoped methods (no-op wrapper for now)."""
    import functools

    @functools.wraps(method)
    async def wrapper(self: "FeedbackManager", *args: Any, **kwargs: Any) -> Any:
        return await method(self, *args, **kwargs)

    return wrapper


async def dispatch_reject_correction(
    *,
    session_factory: Callable[..., Any],
    org_id: UUID,
    run_id: UUID,
    node_id: str,
    node_input: dict[str, Any],
    rejection_reason: str,
    gate_id: str,
) -> dict[str, Any] | None:
    """FAR-210 follow-up: dispatch the single-node correction on a HITL reject.

    Invoked from the HITL reject path (``node_runner._hitl_gate``) when the gate
    config declares a ``correction_target``. This is the AUTOMATED reject→
    correction edge: instead of only kicking back to the plain ``reject_target``,
    the blocked node's input is corrected through the RESTRICTED single-node
    correction path (``FeedbackManager.run_single_node_correction``).

    Best-effort and fully failure-isolated — it must NEVER crash the reject
    path. Returns the correction outcome dict on success, or None when no
    correction can be dispatched (no run, no correction-configured guardrail on
    the node, no FeedbackRecord / resolvable account, no run-scoped backend hub,
    or any resolution failure). All failures are logged and swallowed.
    """
    try:
        async with session_factory() as session, session.begin():
            return await _dispatch_reject_correction_in_session(
                session=session,
                org_id=org_id,
                run_id=run_id,
                node_id=node_id,
                node_input=node_input,
                rejection_reason=rejection_reason,
                gate_id=gate_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "feedback.reject_correction_dispatch_failed",
            extra={"org_id": str(org_id), "run_id": str(run_id), "node_id": node_id},
        )
        return None


async def _dispatch_reject_correction_in_session(
    *,
    session: AsyncSession,
    org_id: UUID,
    run_id: UUID,
    node_id: str,
    node_input: dict[str, Any],
    rejection_reason: str,
    gate_id: str,
) -> dict[str, Any] | None:
    """Run the reject→correction dispatch inside one session/transaction.

    Failure-isolated: returns None when no correction can be dispatched (no
    run, no correction-configured guardrail, no record/account, or no backend
    hub). The caller owns the transaction and error handling.
    """
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    await set_rls_org(session, org_id)
    await set_rls_execution_context(session)
    run = await get_run(session, run_id)
    if run is None:
        return None

    from modulo.core.guardrails.conformance import load_node_guardrails

    guardrails = await load_node_guardrails(session, org_id=org_id, pipeline_id=run.pipeline_id, node_id=node_id)
    guardrail, correction_config = _correction_guardrail_from(guardrails)
    if guardrail is None or correction_config is None:
        return None

    from modulo.core.guardrails.correction import CorrectionDefinition

    correction = CorrectionDefinition.from_eval_config({"correction": correction_config})

    record = await _get_or_create_feedback_record(
        session,
        org_id=org_id,
        run_id=run_id,
        node_id=node_id,
        gate_id=gate_id,
        account_id=run.account_id,
        rejection_reason=rejection_reason,
        rejected_output=node_input,
    )
    if record is None:
        return None

    from modulo.core.pipeline_engine.decorator import get_model_backend_hub

    hub = get_model_backend_hub()
    if hub is None:
        return None
    backend = await hub.get(UUID(str(correction.model_backend_id)))

    mgr = FeedbackManager(session, org_id)
    outcome = await mgr.run_single_node_correction(
        record_id=record.id,
        guardrail=guardrail,
        correction=correction,
        node_input=node_input,
        backend=backend,
        bound_guardrails=guardrails,
    )
    return cast(dict[str, Any], outcome)


class FeedbackManager:
    """Manages the feedback lifecycle: creation, status transitions, eval gap detection."""

    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        self._session = session
        self._org_id = org_id

    def _validate_feedback_inputs(
        self,
        rejection_reason: str,
        rejected_output: dict[str, Any],
        feedback_handler_type: str,
    ) -> str:
        """Validate and normalise feedback inputs; returns the stripped reason."""
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
        return stripped_reason

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
        stripped_reason = self._validate_feedback_inputs(rejection_reason, rejected_output, feedback_handler_type)

        record = FeedbackRecord(
            organisation_id=self._org_id,
            run_id=run_id,
            gate_id=gate_id,
            account_id=account_id,
            rejection_reason=stripped_reason,
            rejected_output=rejected_output,
            producing_node_id=coerce_uuid(producing_node_id),
            producing_agent_id=producing_agent_id,
            feedback_status="pending",
            feedback_handler_type=feedback_handler_type,
        )
        self._session.add(record)
        await self._session.flush()

        if feedback_handler_type in _AI_HANDLER_TYPES:
            await self.update_status(record.id, "correcting")
            await self.spawn_correction_run(record.id)

        logger.info(
            "Created FeedbackRecord %s (run=%s, handler=%s)",
            record.id,
            run_id,
            _handler_type_label(feedback_handler_type),
        )
        return record

    @_rls
    async def get_feedback_records(
        self,
        status: str | None = None,
        pipeline_id: UUID | None = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        include_total: bool = True,
    ) -> dict[str, Any]:
        conditions = _build_org_scoped_conditions(self._org_id, status, pipeline_id)
        rows, total = await paginate_feedback_records(self._session, conditions, page, page_size, include_total)
        return _paginated_response(rows, total, page, page_size)

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

    async def _get_record_or_raise(self, record_id: UUID) -> FeedbackRecord:
        """Fetch a FeedbackRecord scoped to this org, raising when it is missing."""
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
        return current

    @_rls
    async def update_status(self, record_id: UUID, new_status: str) -> FeedbackRecord:
        current = await self._get_record_or_raise(record_id)
        allowed = VALID_STATUS_TRANSITIONS.get(current.feedback_status, set())
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
        current = await self._get_record_or_raise(record_id)
        allowed = VALID_STATUS_TRANSITIONS.get(current.feedback_status, set())
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
        """Normalise an ORM ``EvalDefinition`` row to the engine's DTO shape."""
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

    def _evaluate_single_eval_def(
        self,
        record: FeedbackRecord,
        eval_def: Any,
        eval_engine: EvalEngine,
    ) -> tuple[bool, bool | None]:
        """Run one eval against the rejected output."""
        if not isinstance(eval_def, dict) and not hasattr(eval_def, "eval_type"):
            logger.warning("Malformed eval_def in eval_suite: %s", eval_def)
            return False, None
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
            return True, None
        return True, result.passed

    @_rls
    async def detect_eval_gap(
        self,
        record: FeedbackRecord,
        eval_engine: EvalEngine | None = None,
        eval_suite: list[Any] | None = None,
    ) -> bool:
        """Run the pipeline's eval suite against the rejected output."""
        if eval_engine is None:
            eval_engine = EvalEngine()
        if not eval_suite:
            logger.warning("detect_eval_gap called with empty eval_suite for FeedbackRecord %s", record.id)
            record.eval_gap = True
            return True
        processed_count = 0
        for eval_def in eval_suite:
            processed, passed = self._evaluate_single_eval_def(record, eval_def, eval_engine)
            if not processed:
                continue
            processed_count += 1
            if passed is not None and not passed:
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

    def _build_feedback_correction_block(
        self,
        record: FeedbackRecord,
        run_context_overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the engine-only ``feedback_correction`` block for the new run."""
        feedback_correction: dict[str, Any] = {
            "rejection_reason": record.rejection_reason,
            "rejected_output": record.rejected_output,
            "producing_node_id": str(record.producing_node_id),
            "is_correction_run": True,
        }
        if run_context_overrides:
            feedback_correction.update(run_context_overrides)
        return feedback_correction

    @_rls
    async def spawn_correction_run(
        self,
        record_id: UUID,
        run_context_overrides: dict[str, Any] | None = None,
    ) -> UUID:
        """Create a new correction run pre-seeded from the original feedback run."""
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

        feedback_correction = self._build_feedback_correction_block(record, run_context_overrides)
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
        """FAR-210 T2b: run the single-node correction path."""
        from modulo.core.guardrails.correction import (
            CorrectionOutcome,
            build_idempotency_key,
            redact_payload,
        )

        record = await self.get_feedback_record(record_id)
        if record is None:
            raise FeedbackRecordNotFoundError(f"FeedbackRecord {record_id} not found")

        ctx = _CorrectionRunContext(
            correction=correction,
            guardrail=guardrail,
            node_input=node_input,
            backend=backend,
            bound_guardrails=bound_guardrails,
            revalidation_config=revalidation_config,
            judge_callable=judge_callable,
        )
        _validate_correction_eligibility(record, ctx)

        redacted_input = redact_payload(node_input, correction.input_redaction_patterns)
        idem_key = build_idempotency_key(
            org_id=self._org_id,
            run_id=record.run_id,
            node_id=str(record.producing_node_id),
            correction_id=correction.id,
            redacted_input=redacted_input,
        )
        persisted_state = dict(record.correction_state or {})
        prior_states: list[dict[str, Any]] = [persisted_state] if persisted_state else []
        attempt = int(persisted_state.get("attempt") or 0)

        outcome: CorrectionOutcome | None = await _resume_correction_from_state(
            ctx=ctx,
            idem_key=idem_key,
            persisted_state=persisted_state,
            attempt=attempt,
        )

        if outcome is None:
            await _claim_correction_slot(self._session, self._org_id, ctx, record_id)
            outcome = await _run_correction_attempts(
                session=self._session,
                org_id=self._org_id,
                ctx=ctx,
                record=record,
                idem_key=idem_key,
                prior_states=prior_states,
                attempt=attempt,
            )
            if outcome is None:
                outcome = _budget_exhausted_outcome(
                    ctx=ctx,
                    attempt=attempt,
                    persisted_state=persisted_state,
                )

        outcome.state["produced_output"] = outcome.produced_output
        record.correction_state = outcome.state

        outcome = await _apply_correction_violated_check(outcome=outcome, ctx=ctx)

        await _persist_correction_outcome(
            session=self._session,
            org_id=self._org_id,
            record_id=record_id,
            correction=correction,
            outcome=outcome,
        )
        await self._session.flush()
        return {
            "verdict": outcome.verdict.value,
            "detail": outcome.detail,
            "needs_human_review": outcome.needs_human_review,
        }

    @_rls
    async def _escalate_record(
        self,
        record_id: UUID,
        reason: str,
    ) -> None:
        """Atomically escalate a FeedbackRecord, raising on concurrent modification."""
        await _update_status_fenced(
            self._session,
            org_id=self._org_id,
            record_id=record_id,
            new_status="escalated",
            values={},
            failure_message=f"Expected 'correcting', failed to escalate: {reason}.",
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
        """Evaluate the correction run's output and auto-resolve or flag for review."""
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
        # FAR-583 read-switch: the blobs reassemble from run_node_outputs (with
        # the legacy fallback) in the SAME transaction that loaded the run.
        blobs = await read_run_blobs(
            self._session,
            run_id=record.correction_run_id,
            organisation_id=correction_run.organisation_id,
        )
        raw_output = blobs.outputs
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
        telemetry = blobs.telemetry
        output = {nid: node_return(raw_output, telemetry, nid) for nid in raw_output}

        result = await self._run_post_correction_evaluate(
            engine=engine,
            output=output,
            eval_config=eval_config,
            record_id=record_id,
            correction_run_id=record.correction_run_id,
        )
        if result is None:
            return {
                "passed": False,
                "detail": "Post-correction eval raised an error",
                "score": 0.0,
                "needs_human_review": True,
            }

        needs_human_review = await self._resolve_or_escalate_post_eval(
            record=record,
            result=result,
            record_id=record_id,
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

    async def _run_post_correction_evaluate(
        self,
        *,
        engine: EvalEngine,
        output: dict[str, Any],
        eval_config: dict[str, Any] | None,
        record_id: UUID,
        correction_run_id: UUID,
    ) -> Any | None:
        """Run the post-correction eval, escalating the record on engine failure."""
        try:
            return engine.standalone_evaluate(
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
                correction_run_id,
            )
            await self._escalate_record(
                record_id,
                f"Post-correction eval raised an error for correction run {correction_run_id}",
            )
            return None

    async def _resolve_or_escalate_post_eval(
        self,
        *,
        record: FeedbackRecord,
        result: Any,
        record_id: UUID,
    ) -> bool:
        """Auto-resolve (or escalate) the record based on the post-correction eval outcome."""
        if not result.passed:
            await self._escalate_record(
                record_id,
                f"Correction eval failed for correction run {record.correction_run_id}",
            )
            return False
        needs_human_review = record.feedback_handler_type == "ai_correction_with_human_review"
        await _update_status_fenced(
            self._session,
            org_id=self._org_id,
            record_id=record_id,
            new_status="resolved",
            values={"needs_human_review": needs_human_review},
            failure_message="Expected 'correcting', retry the post-correction eval.",
        )
        return needs_human_review

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
        conditions = _build_org_scoped_conditions(self._org_id, status, pipeline_id)
        if handler_type:
            conditions.append(FeedbackRecord.feedback_handler_type == handler_type)
        if date_from:
            conditions.append(FeedbackRecord.created_at >= date_from)
        if date_to:
            conditions.append(FeedbackRecord.created_at <= date_to)

        rows, total = await paginate_feedback_records(self._session, conditions, page, page_size, include_total)
        pipeline_map = await _enrich_with_pipeline_names(self._session, rows)

        return _paginated_response(rows, total, page, page_size, extra={"pipeline_map": pipeline_map})

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
        rows, total = await paginate_feedback_records(self._session, conditions, page, page_size, include_total)
        return _paginated_response(rows, total, page, page_size)
