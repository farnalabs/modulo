"""Database query helpers for FeedbackManager."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run

logger = logging.getLogger(__name__)


async def get_feedback_record_for_node(
    session: AsyncSession,
    org_id: UUID,
    run_id: UUID,
    node_id: str,
) -> FeedbackRecord | None:
    """Return the FeedbackRecord for (run, node), or None when absent."""
    result = await session.execute(
        select(FeedbackRecord).where(
            FeedbackRecord.organisation_id == org_id,
            FeedbackRecord.run_id == run_id,
            FeedbackRecord.producing_node_id == node_id,
        )
    )
    return result.scalars().first()


async def get_or_create_feedback_record(
    session: AsyncSession,
    *,
    org_id: UUID,
    run_id: UUID,
    node_id: str,
    gate_id: str,
    account_id: UUID | None,
    rejection_reason: str,
    rejected_output: dict[str, Any],
) -> FeedbackRecord | None:
    """Return the existing FeedbackRecord for (run, node) or create one.

    Returns None when no record exists and no account is available to own a
    new one (nothing to anchor the record to).
    """
    from modulo.utils.uuid import coerce_uuid

    record = await get_feedback_record_for_node(session, org_id, run_id, node_id)
    if record is not None:
        return record
    if account_id is None:
        return None
    record = FeedbackRecord(
        organisation_id=org_id,
        run_id=run_id,
        gate_id=gate_id,
        account_id=account_id,
        rejection_reason=rejection_reason,
        rejected_output=rejected_output,
        producing_node_id=coerce_uuid(node_id),
        feedback_status="correcting",
        feedback_handler_type="human",
    )
    session.add(record)
    await session.flush()
    return record


async def enrich_with_pipeline_names(
    session: AsyncSession,
    rows: list[FeedbackRecord],
) -> dict[str, str]:
    """Map run_ids to pipeline names for the given feedback records.

    Tenant scoping comes from the RLS tenant-filter listener on the session
    (``db.rls._inject_tenant_filter``), so no explicit org predicate is needed
    here — the caller's org context is already bound to the session.
    """
    run_ids = list({r.run_id for r in rows if r.run_id})
    if not run_ids:
        return {}
    run_rows = (
        await session.execute(
            select(Run.id, Pipeline.name)
            .select_from(Run)
            .join(Pipeline, Run.pipeline_id == Pipeline.id)
            .where(Run.id.in_(run_ids))
        )
    ).all()
    return {str(run_id): pipeline_name for run_id, pipeline_name in run_rows}


async def paginate_feedback_records(
    session: AsyncSession,
    conditions: list[Any],
    page: int,
    page_size: int,
    include_total: bool = True,
) -> tuple[list[FeedbackRecord], int]:
    """Paginate FeedbackRecord queries with tenant-scoped conditions."""
    from modulo.core.feedback_manager.exceptions import ValidationError
    from modulo.core.feedback_manager.status import _MAX_PAGE_SIZE

    if page < 1:
        raise ValidationError(f"page must be >= 1, got {page}")
    if page_size < 1:
        raise ValidationError(f"page_size must be >= 1, got {page_size}")
    if page_size > _MAX_PAGE_SIZE:
        raise ValidationError(f"page_size must be <= {_MAX_PAGE_SIZE}, got {page_size}")

    if not conditions:
        logger.warning("paginate_feedback_records called with empty conditions — no tenant filter applied")

    total = 0
    if include_total:
        total_q = select(func.count()).select_from(select(FeedbackRecord).where(*conditions).subquery())
        total = (await session.execute(total_q)).scalar() or 0

    offset = (page - 1) * page_size
    q = (
        select(FeedbackRecord)
        .where(*conditions)
        .order_by(FeedbackRecord.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = (await session.execute(q)).scalars().all()
    return list(rows), total


def build_org_scoped_conditions(
    org_id: UUID,
    status: str | None = None,
    pipeline_id: UUID | None = None,
) -> list[Any]:
    """Return tenant-scoped WHERE conditions shared by the feedback list queries."""
    conditions: list[Any] = [FeedbackRecord.organisation_id == org_id]
    if status:
        conditions.append(FeedbackRecord.feedback_status == status)
    if pipeline_id:
        run_subq = select(Run.id).where(Run.pipeline_id == pipeline_id, Run.organisation_id == org_id)
        conditions.append(FeedbackRecord.run_id.in_(run_subq))
    return conditions


def paginated_response(
    rows: list[FeedbackRecord],
    total: int,
    page: int,
    page_size: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard paginated response dict, optionally with extra keys."""
    response: dict[str, Any] = {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if extra:
        response.update(extra)
    return response
