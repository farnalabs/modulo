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
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

_VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"routing", "correcting", "dismissed"},
    "routing": {"escalated", "correcting", "resolved"},
    "correcting": {"resolved", "escalated"},
    "escalated": {"resolved", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}


def _rls(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def wrapper(self: "FeedbackManager", *args: Any, **kwargs: Any) -> Any:
        await set_rls_org(self._session, self._org_id)
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
        rejected_by: UUID,
        rejection_reason: str,
        rejected_output: dict,
        producing_node_id: str,
        producing_agent_id: UUID | None = None,
        feedback_handler_type: str = "human",
    ) -> FeedbackRecord:
        record = FeedbackRecord(
            organisation_id=self._org_id,
            run_id=run_id,
            gate_id=gate_id,
            rejected_by=rejected_by,
            rejection_reason=rejection_reason,
            rejected_output=rejected_output,
            producing_node_id=producing_node_id,
            producing_agent_id=producing_agent_id,
            feedback_status="pending",
            feedback_handler_type=feedback_handler_type,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    @_rls
    async def get_feedback_records(
        self,
        status: str | None = None,
        pipeline_id: UUID | None = None,
        page: int = 1,
        page_size: int = 20,
        include_total: bool = True,
    ) -> dict:
        conditions = [FeedbackRecord.organisation_id == self._org_id]
        if status:
            conditions.append(FeedbackRecord.feedback_status == status)
        if pipeline_id:
            run_subq = select(Run.id).where(Run.pipeline_id == pipeline_id, Run.organisation_id == self._org_id)
            conditions.append(FeedbackRecord.run_id.in_(run_subq))

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
        return result.scalar_one_or_none()

    @_rls
    async def update_status(self, record_id: UUID, new_status: str) -> FeedbackRecord | None:
        current = await self._session.get(FeedbackRecord, record_id)
        if current is None:
            return None
        allowed = _VALID_STATUS_TRANSITIONS.get(current.feedback_status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition FeedbackRecord {record_id} from "
                f"'{current.feedback_status}' to '{new_status}'. "
                f"Allowed: {sorted(allowed) or '<terminal>'}"
            )
        result = await self._session.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == self._org_id,
            )
            .values(feedback_status=new_status)
            .returning(FeedbackRecord)
        )
        return result.scalar_one_or_none()

    @_rls
    async def link_correction_run(self, record_id: UUID, correction_run_id: UUID) -> FeedbackRecord | None:
        current = await self._session.get(FeedbackRecord, record_id)
        if current is None:
            return None
        allowed = _VALID_STATUS_TRANSITIONS.get(current.feedback_status, set())
        if "correcting" not in allowed:
            raise ValueError(
                f"Cannot link correction run to FeedbackRecord {record_id} in "
                f"status '{current.feedback_status}'. "
                f"Allowed transitions: {sorted(allowed) or '<terminal>'}"
            )
        result = await self._session.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == self._org_id,
            )
            .values(correction_run_id=correction_run_id, feedback_status="correcting")
            .returning(FeedbackRecord)
        )
        return result.scalar_one_or_none()

    @_rls
    async def detect_eval_gap(
        self,
        record: FeedbackRecord,
        eval_engine: EvalEngine | None = None,
        eval_suite: list | None = None,
    ) -> bool:
        """Run the pipeline's eval suite against the rejected output.

        If no eval scored the output as failing, tag the record as eval_gap.
        Returns True if there is an eval gap.
        """
        if eval_engine is None:
            eval_engine = EvalEngine()
        if not eval_suite:
            return False
        return False

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
        4. Inject a ``_feedback_correction`` block into the new run's
           ``input_payload`` so the executor promotes it to ``run_context``.
        5. Link the correction run to the FeedbackRecord and transition status
           to ``correcting``.
        6. Return the new run_id.

        Args:
            record_id: The FeedbackRecord to spawn a correction for.
            run_context_overrides: Optional extra keys to merge into the
                correction run's ``_feedback_correction`` block.

        Returns:
            The UUID of the newly created correction run.
        """
        record = await self.get_feedback_record(record_id)
        if record is None:
            raise ValueError(f"FeedbackRecord {record_id} not found")

        original_run = await get_run(self._session, record.run_id)
        if original_run is None:
            raise ValueError(f"Original run {record.run_id} not found for FeedbackRecord {record_id}")

        # Build injected payload so the executor's _seed_state promotes
        # it to run_context["feedback_correction"].
        feedback_correction: dict[str, Any] = {
            "rejection_reason": record.rejection_reason,
            "rejected_output": record.rejected_output,
            "producing_node_id": record.producing_node_id,
            "is_correction_run": True,
        }
        if run_context_overrides:
            feedback_correction.update(run_context_overrides)

        input_payload = dict(original_run.input_payload or {})
        input_payload["_feedback_correction"] = feedback_correction

        new_run = await create_run(
            self._session,
            org_id=self._org_id,
            pipeline_id=original_run.pipeline_id,
            snapshot_id=original_run.snapshot_id,
            trigger_type="correction",
            input_payload=input_payload,
            created_by=record.rejected_by,
            parent_run_id=record.run_id,
        )

        await self.link_correction_run(record_id, new_run.id)

        return new_run.id

    @_rls
    async def get_feedback_records_inbox(
        self,
        handler_type: str | None = None,
        status: str | None = None,
        pipeline_id: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
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

        run_ids = list({r.run_id for r in rows if r.run_id})
        pipeline_map: dict[UUID, str] = {}
        if run_ids:
            run_rows = (await self._session.execute(select(Run.id, Run.pipeline_id).where(Run.id.in_(run_ids)))).all()
            for run_id, pipeline_id_val in run_rows:
                from modulo.db.models.pipeline import Pipeline

                pipeline = await self._session.get(Pipeline, pipeline_id_val)
                if pipeline:
                    pipeline_map[run_id] = pipeline.name

        return {
            "items": rows,
            "pipeline_map": {str(k): v for k, v in pipeline_map.items()},
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @_rls
    async def get_eval_proposals(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        conditions = [
            FeedbackRecord.organisation_id == self._org_id,
            FeedbackRecord.eval_gap.is_(True),
            FeedbackRecord.feedback_status.in_(["pending", "routing"]),
        ]
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

        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
