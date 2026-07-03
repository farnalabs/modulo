"""Feedback system API endpoints.

URLs:
    POST   /api/v1/runs/{run_id}/feedback               — create a feedback record
    GET    /api/v1/feedback                              — list feedback records
    GET    /api/v1/feedback/{record_id}                  — get a feedback record
    PATCH  /api/v1/feedback/{record_id}/status           — update feedback status
    POST   /api/v1/feedback/{record_id}/detect-gap       — run eval gap detection
    GET    /api/v1/feedback/inbox                        — feedback inbox with filters
    GET    /api/v1/feedback/inbox/{record_id}             — single inbox item detail
    POST   /api/v1/feedback/inbox/{record_id}/review     — review + optional correction run
    GET    /api/v1/feedback/proposals                    — eval proposals queue
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feedback_manager import FeedbackManager
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1", tags=["feedback"])


class CreateFeedbackRequest(BaseModel):
    gate_id: str
    rejection_reason: str
    rejected_output: dict[str, Any] = {}
    producing_node_id: str
    producing_agent_id: uuid.UUID | None = None
    feedback_handler_type: str = "human"


class UpdateStatusRequest(BaseModel):
    status: str


class ReviewFeedbackRequest(BaseModel):
    action: str = "mark_reviewed"  # mark_reviewed | dismiss | create_correction_run
    annotation: str | None = None


def _serialise_record(r: Any, pipeline_name: str | None = None, producing_node_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "run_id": str(r.run_id) if r.run_id else None,
        "gate_id": r.gate_id,
        "rejected_by": str(r.account_id) if r.account_id else None,
        "rejection_reason": r.rejection_reason,
        "rejected_output": getattr(r, "rejected_output", {}),
        "producing_node_id": r.producing_node_id,
        "producing_node_name": producing_node_name,
        "producing_agent_id": str(r.producing_agent_id) if r.producing_agent_id else None,
        "feedback_status": r.feedback_status,
        "feedback_handler_type": r.feedback_handler_type,
        "correction_run_id": str(r.correction_run_id) if r.correction_run_id else None,
        "eval_gap": r.eval_gap,
        "needs_human_review": getattr(r, "needs_human_review", False),
        "annotation": getattr(r, "annotation", None),
        "pipeline_name": pipeline_name,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/runs/{run_id}/feedback", status_code=status.HTTP_201_CREATED)
async def create_feedback(
    run_id: uuid.UUID,
    body: CreateFeedbackRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run_result = await session.execute(
                select(Run).where(Run.id == run_id, Run.organisation_id == principal.organisation_id)
            )
            run = run_result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.create_feedback_record(
                run_id=run_id,
                gate_id=body.gate_id,
                account_id=principal.account_id,
                rejection_reason=body.rejection_reason,
                rejected_output=body.rejected_output,
                producing_node_id=body.producing_node_id,
                producing_agent_id=body.producing_agent_id,
                feedback_handler_type=body.feedback_handler_type,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    return {
        "id": str(record.id),
        "run_id": str(record.run_id),
        "gate_id": record.gate_id,
        "rejected_by": str(record.account_id),
        "rejection_reason": record.rejection_reason,
        "feedback_status": record.feedback_status,
        "feedback_handler_type": record.feedback_handler_type,
        "eval_gap": record.eval_gap,
        "correction_run_id": str(record.correction_run_id) if record.correction_run_id else None,
    }


@router.get("/feedback", status_code=status.HTTP_200_OK)
async def list_feedback(
    status_filter: str | None = Query(None, alias="status"),
    pipeline_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            result = await mgr.get_feedback_records(
                status=status_filter,
                pipeline_id=pipeline_id,
                page=page,
                page_size=page_size,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    return {
        "items": [_serialise_record(r) for r in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/feedback/inbox", status_code=status.HTTP_200_OK)
async def list_feedback_inbox(
    handler_type: str | None = Query(None, alias="type"),
    status_filter: str | None = Query(None, alias="status"),
    pipeline_id: uuid.UUID | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    date_from_dt: datetime | None = None
    date_to_dt: datetime | None = None
    if date_from:
        date_from_dt = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
    if date_to:
        date_to_dt = datetime.fromisoformat(date_to).replace(tzinfo=UTC)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            result = await mgr.get_feedback_records_inbox(
                handler_type=handler_type,
                status=status_filter,
                pipeline_id=pipeline_id,
                date_from=date_from_dt,
                date_to=date_to_dt,
                page=page,
                page_size=page_size,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    pipeline_map = result.get("pipeline_map", {})

    return {
        "items": [_serialise_record(r, pipeline_name=pipeline_map.get(str(r.run_id))) for r in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/feedback/proposals", status_code=status.HTTP_200_OK)
async def list_eval_proposals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            result = await mgr.get_eval_proposals(page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    items = result["items"]
    node_name_map: dict[str, str] = {}
    run_ids = [r.run_id for r in items if r.run_id]
    if run_ids:
        run_rows = await session.execute(
            select(Run.id, Run.snapshot_id).where(Run.id.in_(run_ids))
        )
        rows = await run_rows.all()
        snapshot_ids = [r.snapshot_id for r in rows if r.snapshot_id]
        if snapshot_ids:
            snap_rows = await session.execute(
                select(PipelineSnapshot.id, PipelineSnapshot.graph_json).where(PipelineSnapshot.id.in_(snapshot_ids))
            )
            snap_rows_result = await snap_rows.all()
            for snap_id, graph_json in snap_rows_result:
                if graph_json:
                    for node in graph_json.get("nodes", []):
                        node_name_map[str(node.get("id"))] = node.get("name") or node.get("label", "")

    return {
        "items": [_serialise_record(r, producing_node_name=node_name_map.get(r.producing_node_id)) for r in items],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/feedback/{record_id}", status_code=status.HTTP_200_OK)
async def get_feedback(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback record not found")

    return _serialise_record(record)


@router.patch("/feedback/{record_id}/status", status_code=status.HTTP_200_OK)
async def update_feedback_status(
    record_id: uuid.UUID,
    body: UpdateStatusRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    valid_statuses = {"pending", "routing", "correcting", "resolved", "escalated"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.update_status(record_id, body.status)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback record not found")

    return {
        "id": str(record.id),
        "feedback_status": record.feedback_status,
    }


@router.post("/feedback/{record_id}/detect-gap", status_code=status.HTTP_200_OK)
async def detect_eval_gap(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback record not found")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)

            eval_suite: list[EvalDefinition] = []
            if record.run_id:
                run = (
                    await session.execute(select(Run).where(Run.id == record.run_id))
                ).scalar_one_or_none()
                if run is not None:
                    eval_defs = (
                        await session.execute(
                            select(EvalDefinition).where(EvalDefinition.pipeline_id == run.pipeline_id)
                        )
                    ).scalars().all()
                    eval_suite = list(eval_defs)

            is_gap = await mgr.detect_eval_gap(record, eval_suite=eval_suite)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    return {
        "id": str(record.id),
        "eval_gap": is_gap,
    }


@router.get("/feedback/inbox/{record_id}", status_code=status.HTTP_200_OK)
async def get_inbox_item(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback record not found")

    pipeline_name: str | None = None
    if record.run_id:
        run_row = (await session.execute(select(Run).where(Run.id == record.run_id))).scalar_one_or_none()
        if run_row:
            from modulo.db.models.pipeline import Pipeline

            pipeline = await session.get(Pipeline, run_row.pipeline_id)
            if pipeline:
                pipeline_name = pipeline.name

    return _serialise_record(record, pipeline_name=pipeline_name)


@router.post("/feedback/inbox/{record_id}/review", status_code=status.HTTP_200_OK)
async def review_feedback(
    record_id: uuid.UUID,
    body: ReviewFeedbackRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    valid_actions = {"mark_reviewed", "dismiss", "create_correction_run"}
    if body.action not in valid_actions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid action. Must be one of: {', '.join(sorted(valid_actions))}",
        )

    correction_run_id: str | None = None

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)

            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback record not found")

            if body.action == "mark_reviewed":
                record = await mgr.update_status(record_id, "resolved")
                if record is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Cannot transition feedback to resolved",
                    )

            elif body.action == "dismiss":
                record = await mgr.update_status(record_id, "dismissed")
                if record is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Cannot dismiss feedback",
                    )

            elif body.action == "create_correction_run":
                if not record.run_id:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Feedback has no associated run — cannot create correction run",
                    )

                try:
                    new_run_id = await mgr.spawn_correction_run(record_id)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=str(exc),
                    ) from exc

                correction_run_id = str(new_run_id)

            if body.annotation is not None:
                from sqlalchemy import update as sa_update
                from modulo.db.models.feedback_record import FeedbackRecord

                await session.execute(
                    sa_update(FeedbackRecord)
                    .where(FeedbackRecord.id == record_id)
                    .values(annotation=body.annotation)
                )

    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        )

    return {
        "id": str(record.id),
        "feedback_status": record.feedback_status,
        "correction_run_id": correction_run_id,
    }
