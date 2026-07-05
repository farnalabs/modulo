"""FeedbackManager — FeedbackRecord lifecycle, eval gap detection, correction run spawning.

The Feedback System (§8.20) treats every human rejection as structured signal.
This module manages the FeedbackRecord entity, status transitions, eval gap
detection via EvalEngine.standalone_evaluate(), and correction run mechanics.
"""
import functools
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalEngine
from modulo.db.crud.run import create_run, get_run
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

logger = logging.getLogger(__name__)

_VALID_FEEDBACK_HANDLER_TYPES = frozenset({
    "human",
    "ai_correction",
    "ai_correction_with_human_review",
})
_AI_HANDLER_TYPES = frozenset({
    "ai_correction",
    "ai_correction_with_human_review",
})
_FEEDBACK_CORRECTION_KEY = "_feedback_correction"
_POST_CORRECTION_EVAL_NAME = "post_correction_eval"
_DEFAULT_PAGE_SIZE = 20


class FeedbackManagerError(Exception):
    """Base exception for FeedbackManager errors."""


class FeedbackRecordNotFoundError(FeedbackManagerError):
    """Raised when a FeedbackRecord is not found."""


class InvalidTransitionError(FeedbackManagerError):
    """Raised when a feedback status transition is not allowed."""


class ConcurrentModificationError(FeedbackManagerError):
    """Raised when concurrent modification prevents a status transition."""


class ValidationError(FeedbackManagerError):
    """Raised when input validation fails."""

_VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"routing", "correcting", "dismissed"},
    "routing": {"escalated", "correcting", "resolved"},
    "correcting": {"correcting", "resolved", "escalated"},
    "escalated": {"resolved", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}


def _rls(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def wrapper(self: "FeedbackManager", *args: Any, **kwargs: Any) -> Any:
        try:
            await set_rls_org(self._session, self._org_id)
        except Exception:
            logger.exception("RLS setup failed for org %s on method %s", self._org_id, method.__name__)
            raise
        return await method(self, *args, **kwargs)

    return wrapper


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
        if not rejection_reason or not rejection_reason.strip():
            raise ValidationError("rejection_reason must not be empty")
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
            rejection_reason=rejection_reason.strip(),
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
            record.id, run_id, feedback_handler_type,
        )
        return record

    def _validate_pagination(self, page: int, page_size: int) -> None:
        if page < 1:
            raise ValidationError(f"page must be >= 1, got {page}")
        if page_size < 1:
            raise ValidationError(f"page_size must be >= 1, got {page_size}")
        if page_size > 100:
            raise ValidationError(f"page_size must be <= 100, got {page_size}")

    async def _paginate(
        self,
        conditions: list[Any],
        page: int,
        page_size: int,
        include_total: bool = True,
    ) -> tuple[list[FeedbackRecord], int]:
        self._validate_pagination(page, page_size)
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
        return rows, total

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
        for eval_def in eval_suite:
            if not isinstance(eval_def, dict) and not hasattr(eval_def, "passed"):
                logger.warning("Malformed eval_def in eval_suite: %s", eval_def)
                continue
            try:
                result = eval_engine.evaluate(record.rejected_output, eval_def)
            except Exception:
                logger.exception(
                    "EvalEngine.evaluate failed for FeedbackRecord %s on eval_def %s",
                    record.id, eval_def,
                )
                continue
            if not result.passed:
                return False
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
        4. Inject a _feedback_correction block into the new run's
           ``input_payload`` so the executor promotes it to ``run_context``.
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

        original_run = await get_run(self._session, record.run_id)
        if original_run is None:
            raise FeedbackManagerError(
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
        input_payload[_FEEDBACK_CORRECTION_KEY] = feedback_correction

        new_run = await create_run(
            self._session,
            org_id=self._org_id,
            pipeline_id=original_run.pipeline_id,
            snapshot_id=original_run.snapshot_id,
            trigger_type="correction",
            input_payload=input_payload,
            account_id=record.account_id,
            parent_run_id=record.run_id,
        )

        await self.link_correction_run(record_id, new_run.id)

        logger.info(
            "Spawned correction run %s for FeedbackRecord %s (original run %s)",
            new_run.id, record_id, record.run_id,
        )
        return new_run.id

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
                f"Correction run {record.correction_run_id} has status "
                f"'{correction_run.status}', expected 'complete'"
            )

        engine = eval_engine or EvalEngine()
        output = correction_run.outputs_json
        if not output:
            await self._session.execute(
                update(FeedbackRecord)
                .where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                    FeedbackRecord.feedback_status == "correcting",
                )
                .values(feedback_status="escalated")
            )
            logger.warning(
                "Correction run %s produced no output for FeedbackRecord %s — escalated",
                record.correction_run_id, record_id,
            )
            await self._session.flush()
            return {
                "passed": False,
                "detail": "Correction run produced no output",
                "score": 0.0,
                "needs_human_review": True,
            }
        output = dict(output)

        try:
            result = engine.standalone_evaluate(
                output,
                name=_POST_CORRECTION_EVAL_NAME,
                config=eval_config or {},
            )
        except Exception:
            logger.exception(
                "standalone_evaluate failed for FeedbackRecord %s correction run %s",
                record_id, record.correction_run_id,
            )
            await self._session.execute(
                update(FeedbackRecord)
                .where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                    FeedbackRecord.feedback_status == "correcting",
                )
                .values(feedback_status="escalated")
            )
            await self._session.flush()
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
            await self._session.execute(
                update(FeedbackRecord)
                .where(
                    FeedbackRecord.id == record_id,
                    FeedbackRecord.organisation_id == self._org_id,
                    FeedbackRecord.feedback_status == "correcting",
                )
                .values(feedback_status="escalated")
            )
            logger.warning(
                "Correction eval failed for FeedbackRecord %s — escalated for review",
                record_id,
            )
        logger.info(
            "Post-correction eval for FeedbackRecord %s: passed=%s, needs_human_review=%s",
            record_id, result.passed, needs_human_review,
        )

        await self._session.flush()
        return {
            "passed": result.passed,
            "detail": result.detail,
            "score": result.score,
            "needs_human_review": needs_human_review,
        }

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
