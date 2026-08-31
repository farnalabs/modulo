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

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.eval_engine import EvalDefinition as EvalDefinitionDTO
from modulo.core.feedback_manager import (
    ConcurrentModificationError,
    FeedbackManager,
    FeedbackRecordNotFoundError,
    FeedbackRecordRunNotFoundError,
    InvalidTransitionError,
)
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_FEEDBACK_CREATE_FEEDBACK = "feedback.create_feedback"
_CODE_FEEDBACK_AUDIT_APPEND_FAILED = "feedback.audit_append_failed"
_CODE_FEEDBACK_PUBLISH_EVAL_PROPOSAL = "feedback.publish_eval_proposal"
_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE = "A resource conflict occurred. Please try again."
_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE = "Feedback system is not available. Run database migrations to enable this feature."
_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "Database error occurred. Please try again later."
_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE = "An unexpected error occurred. Please try again later."
_CODE_FEEDBACK_LIST = "feedback.list"
_CODE_FEEDBACK_LIST_FEEDBACK = "feedback.list_feedback"
_CODE_FEEDBACK_LIST_FEEDBACK_INBOX = "feedback.list_feedback_inbox"
_CODE_FEEDBACK_LIST_EVAL_PROPOSALS = "feedback.list_eval_proposals"
_CODE_FEEDBACK_GET_FEEDBACK = "feedback.get_feedback"
_MSG_FEEDBACK_RECORD_NOT_FOUND = "Feedback record not found"
_CODE_FEEDBACK_UPDATE_FEEDBACK_STATUS = "feedback.update_feedback_status"
_CODE_FEEDBACK_DETECT_EVAL_GAP = "feedback.detect_eval_gap"
_CODE_FEEDBACK_GET_INBOX_ITEM = "feedback.get_inbox_item"
_CODE_FEEDBACK_REVIEW_FEEDBACK = "feedback.review_feedback"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["feedback"])


class CreateFeedbackRequest(BaseModel):
    gate_id: str
    rejection_reason: str
    rejected_output: ClassVar[dict[str, Any]] = {}
    producing_node_id: str
    producing_agent_id: uuid.UUID | None = None
    feedback_handler_type: str = "human"


class UpdateStatusRequest(BaseModel):
    status: str


class ReviewFeedbackRequest(BaseModel):
    action: str  # mark_reviewed | dismiss | create_correction_run
    annotation: str | None = None


class PublishEvalProposalRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    eval_type: str = Field(pattern=r"^(llm_judge|regex|json_schema|custom_function)$")
    config: dict[str, Any] = Field(default_factory=dict)
    node_id: uuid.UUID | None = None


def _eval_def_to_dto(row: EvalDefinition, org_id: uuid.UUID) -> EvalDefinitionDTO:
    """Convert an ORM ``EvalDefinition`` row to the eval-engine DTO shape.

    The engine reads ``eval_def.config`` (``EvalEngine.evaluate``), but the ORM
    model exposes ``config_json``. This mirrors the executor's
    ``_build_eval_defs_by_node`` pattern so the standalone eval path feeds the
    engine the same DTO shape the live run path uses — without it, every eval
    raises AttributeError inside ``evaluate()`` and gap detection reports
    ``eval_gap=True`` for everything (FAR-233 review MAJOR-1).
    """
    return EvalDefinitionDTO(
        id=row.id,
        org_id=org_id,
        pipeline_id=row.pipeline_id,
        node_id=str(row.node_id) if row.node_id else None,
        name=row.name,
        eval_type=row.eval_type,
        config=row.config_json,
        failure_behaviour=row.failure_behaviour,
        pass_threshold=float(row.pass_threshold) if row.pass_threshold is not None else None,
        suite_id=row.suite_id,
    )


def _serialise_record(
    r: Any, pipeline_name: str | None = None, producing_node_name: str | None = None
) -> dict[str, Any]:
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
@handle_db_errors(_CODE_FEEDBACK_CREATE_FEEDBACK)
async def create_feedback(
    run_id: uuid.UUID,
    req: CreateFeedbackRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("feedback.create"),
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
                gate_id=req.gate_id,
                account_id=principal.account_id,
                rejection_reason=req.rejection_reason,
                rejected_output=req.rejected_output,
                producing_node_id=req.producing_node_id,
                producing_agent_id=req.producing_agent_id,
                feedback_handler_type=req.feedback_handler_type,
            )
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_CREATE_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_CREATE_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_CREATE_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error creating feedback for run %s", run_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from exc

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="feedback_record",
        event_type="feedback.created",
        resource_id=record.id,
        payload={
            "run_id": str(record.run_id) if record.run_id else None,
            "gate_id": record.gate_id,
            "feedback_handler_type": record.feedback_handler_type,
        },
        log_key=_CODE_FEEDBACK_AUDIT_APPEND_FAILED,
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
@handle_db_errors(_CODE_FEEDBACK_LIST_FEEDBACK)
async def list_feedback(
    status_filter: str | None = Query(None, alias="status"),
    pipeline_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_FEEDBACK_LIST),
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
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error listing feedback")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from exc

    return {
        "items": [_serialise_record(r) for r in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/feedback/inbox", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_LIST_FEEDBACK_INBOX)
async def list_feedback_inbox(
    handler_type: str | None = Query(None, alias="type"),
    status_filter: str | None = Query(None, alias="status"),
    pipeline_id: uuid.UUID | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_FEEDBACK_LIST),
) -> dict[str, Any]:
    date_from_dt = _parse_optional_iso(date_from, "date_from")
    date_to_dt = _parse_optional_iso(date_to, "date_to")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
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
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_FEEDBACK_INBOX)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_FEEDBACK_INBOX)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_FEEDBACK_INBOX)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing feedback inbox")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    pipeline_map = result.get("pipeline_map", {})

    return {
        "items": [_serialise_record(r, pipeline_name=pipeline_map.get(str(r.run_id))) for r in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


def _parse_optional_iso(value: str | None, field: str) -> datetime | None:
    """Parse an optional ISO-8601 query value, mapping bad input to a 422."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid {field} format: '{value}'. Use ISO 8601 format (e.g. 2024-01-01T00:00:00).",
        ) from None


async def _build_node_name_map(session: AsyncSession, items: list[Any]) -> dict[str, str]:
    """Build a graph node id -> display-name map for a set of feedback records."""
    node_name_map: dict[str, str] = {}
    run_ids = [r.run_id for r in items if r.run_id]
    if not run_ids:
        return node_name_map
    run_rows = await session.execute(select(Run.id, Run.snapshot_id).where(Run.id.in_(run_ids)))
    snapshot_ids = [r.snapshot_id for r in run_rows.all() if r.snapshot_id]
    if not snapshot_ids:
        return node_name_map
    snap_rows = await session.execute(
        select(PipelineSnapshot.id, PipelineSnapshot.graph_json).where(PipelineSnapshot.id.in_(snapshot_ids))
    )
    for _, graph_json in snap_rows.all():
        if graph_json:
            for node in graph_json.get("nodes", []):
                node_name_map[str(node.get("id"))] = node.get("name") or node.get("label", "")
    return node_name_map


@router.get("/feedback/proposals", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_LIST_EVAL_PROPOSALS)
async def list_eval_proposals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_FEEDBACK_LIST),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            result = await mgr.get_eval_proposals(page=page, page_size=page_size)
            items = result["items"]
            node_name_map = await _build_node_name_map(session, items)
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_EVAL_PROPOSALS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_EVAL_PROPOSALS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_LIST_EVAL_PROPOSALS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing eval proposals")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    return {
        "items": [_serialise_record(r, producing_node_name=node_name_map.get(str(r.producing_node_id))) for r in items],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


async def _resolve_producing_node_uuid(
    session: AsyncSession,
    record: FeedbackRecord,
    run: Run,
) -> uuid.UUID | None:
    """Resolve a FeedbackRecord's ``producing_node_id`` to a graph node UUID.

    The published eval must be scoped to a pipeline node (``EvalDefinition.node_id``)
    or it is never executed by the run-time eval loader (executor filters
    ``node_id.isnot(None)``). Resolution order: explicit request ``node_id`` is
    handled by the caller; here we parse ``producing_node_id`` as a UUID, then
    match it (by id or name/label) against the run snapshot's graph nodes.
    """
    try:
        return uuid.UUID(str(record.producing_node_id))
    except (ValueError, TypeError):
        pass
    if run.snapshot_id is None:
        return None
    snap = (
        await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
    ).scalar_one_or_none()
    if snap is None or not snap.graph_json:
        return None
    target = str(record.producing_node_id)
    for node in snap.graph_json.get("nodes", []):
        nid = node.get("id")
        if str(nid) == target or str(node.get("name")) == target or str(node.get("label")) == target:
            try:
                return uuid.UUID(str(nid))
            except (ValueError, TypeError):
                return None
    return None


async def _resolve_publish_context(
    mgr: FeedbackManager,
    session: AsyncSession,
    record_id: uuid.UUID,
    node_id: uuid.UUID | None,
) -> tuple[Run, uuid.UUID]:
    """Validate that ``record_id`` can be published and resolve its target node."""
    record = await mgr.get_feedback_record(record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback record not found")
    if record.eval_gap is not True:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only eval-gap feedback records can be published as eval proposals",
        )
    if record.feedback_status not in ("pending", "routing"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Proposal in status '{record.feedback_status}' cannot be published",
        )
    if record.run_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Feedback record has no associated run — cannot resolve the pipeline",
        )
    run = (await session.execute(select(Run).where(Run.id == record.run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if node_id is None:
        node_id = await _resolve_producing_node_uuid(session, record, run)
    if node_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Could not resolve producing_node_id to a pipeline node. "
                "Supply an explicit node_id so the published eval is scoped to a run-time node."
            ),
        )
    return run, node_id


@router.post("/feedback/proposals/{record_id}/publish", status_code=status.HTTP_201_CREATED)
@handle_db_errors(_CODE_FEEDBACK_PUBLISH_EVAL_PROPOSAL)
async def publish_eval_proposal(
    record_id: uuid.UUID,
    req: PublishEvalProposalRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("feedback.review"),
) -> dict[str, Any]:
    """Publish an eval-gap proposal as a live eval definition (PRD §8.20 ¶Eval suite growth #3).

    A human reviews/edits the proposed eval (name, eval_type, config) and
    publishes it. Publishing creates an ``EvalDefinition`` scoped to the
    feedback record's pipeline and producing node — because run-time eval
    execution and gap detection load definitions by ``pipeline_id`` at run
    time, the published eval is immediately active for future runs of that
    pipeline — then resolves the proposal record (status -> ``resolved``).
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mgr = FeedbackManager(session, principal.organisation_id)
            run, node_id = await _resolve_publish_context(mgr, session, record_id, req.node_id)

            eval_def = EvalDefinition(
                organisation_id=principal.organisation_id,
                pipeline_id=run.pipeline_id,
                node_id=node_id,
                name=req.name,
                eval_type=req.eval_type,
                config_json=req.config,
                failure_behaviour="warn",
                account_id=principal.account_id,
            )
            session.add(eval_def)
            await session.flush()

            await mgr.update_status(record_id, "resolved")
    except IntegrityError:
        logger.exception(_CODE_FEEDBACK_PUBLISH_EVAL_PROPOSAL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource conflict occurred. Please try again.",
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_FEEDBACK_PUBLISH_EVAL_PROPOSAL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feedback system is not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_FEEDBACK_PUBLISH_EVAL_PROPOSAL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again later.",
        ) from None
    except (InvalidTransitionError, ConcurrentModificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except FeedbackRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error publishing eval proposal %s", record_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again later.",
        ) from None

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="feedback_record",
        event_type="feedback.proposal_published",
        resource_id=record_id,
        payload={
            "eval_definition_id": str(eval_def.id),
            "pipeline_id": str(eval_def.pipeline_id),
            "node_id": str(eval_def.node_id) if eval_def.node_id else None,
            "eval_type": eval_def.eval_type,
            "name": eval_def.name,
        },
        log_key=_CODE_FEEDBACK_AUDIT_APPEND_FAILED,
    )

    return {
        "id": str(eval_def.id),
        "record_id": str(record_id),
        "pipeline_id": str(eval_def.pipeline_id),
        "node_id": str(eval_def.node_id) if eval_def.node_id else None,
        "name": eval_def.name,
        "eval_type": eval_def.eval_type,
        "config": eval_def.config_json,
        "feedback_status": "resolved",
    }


@router.get("/feedback/{record_id}", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_GET_FEEDBACK)
async def get_feedback(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_FEEDBACK_LIST),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_GET_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_GET_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_GET_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error getting feedback record %s", record_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)

    return _serialise_record(record)


@router.patch("/feedback/{record_id}/status", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_UPDATE_FEEDBACK_STATUS)
async def update_feedback_status(
    record_id: uuid.UUID,
    req: UpdateStatusRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("feedback.update"),
) -> dict[str, Any]:
    valid_statuses = {"pending", "routing", "correcting", "resolved", "escalated", "dismissed"}
    if req.status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    record, old_status = await _update_feedback_status_transaction(session, principal, record_id, req.status)

    await _append_feedback_status_audit(session, principal, record, record_id, old_status)

    return {
        "id": str(record.id),
        "feedback_status": record.feedback_status,
    }


async def _update_feedback_status_transaction(
    session: AsyncSession, principal: TenantPrincipal, record_id: uuid.UUID, new_status: str
) -> tuple[FeedbackRecord, str]:
    """Update the feedback status within a transaction, raising 404 if missing."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)
            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)
            old_status = record.feedback_status
            record = await mgr.update_status(record_id, new_status)
            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_UPDATE_FEEDBACK_STATUS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_UPDATE_FEEDBACK_STATUS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_UPDATE_FEEDBACK_STATUS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error updating feedback status for record %s", record_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return record, old_status


async def _append_feedback_status_audit(
    session: AsyncSession,
    principal: TenantPrincipal,
    record: FeedbackRecord,
    record_id: uuid.UUID,
    old_status: str,
) -> None:
    """Append a feedback status-changed audit event."""
    await append_audit_event_isolated(
        session,
        principal,
        resource_type="feedback_record",
        event_type="feedback.status_changed",
        resource_id=record_id,
        payload={
            "old_status": old_status,
            "new_status": record.feedback_status,
            "action": "update_status",
            "run_id": str(record.run_id) if record.run_id else None,
            "gate_id": record.gate_id,
        },
        log_key=_CODE_FEEDBACK_AUDIT_APPEND_FAILED,
    )


async def _load_eval_suite(session: AsyncSession, record: Any, org_id: uuid.UUID) -> list[EvalDefinitionDTO]:
    """Load the eval definitions for the pipeline associated with a run."""
    if not record.run_id:
        return []
    run = (await session.execute(select(Run).where(Run.id == record.run_id))).scalar_one_or_none()
    if run is None:
        return []
    eval_rows = (
        (await session.execute(select(EvalDefinition).where(EvalDefinition.pipeline_id == run.pipeline_id)))
        .scalars()
        .all()
    )
    return [_eval_def_to_dto(row, org_id) for row in eval_rows]


@router.post("/feedback/{record_id}/detect-gap", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_DETECT_EVAL_GAP)
async def detect_eval_gap(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("feedback.update"),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)

            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)

            eval_suite = await _load_eval_suite(session, record, principal.organisation_id)

            is_gap = await mgr.detect_eval_gap(record, eval_suite=eval_suite)
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_DETECT_EVAL_GAP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_DETECT_EVAL_GAP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_DETECT_EVAL_GAP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error detecting eval gap for record %s", record_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    return {
        "id": str(record.id),
        "eval_gap": is_gap,
    }


async def _resolve_pipeline_name(session: AsyncSession, record: Any) -> str | None:
    """Resolve the display name of the pipeline a feedback record's run belongs to."""
    if record is None or not record.run_id:
        return None
    run_row = (await session.execute(select(Run).where(Run.id == record.run_id))).scalar_one_or_none()
    if run_row is None:
        return None
    pipeline = await session.get(Pipeline, run_row.pipeline_id)
    return pipeline.name if pipeline is not None else None


@router.get("/feedback/inbox/{record_id}", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_GET_INBOX_ITEM)
async def get_inbox_item(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_FEEDBACK_LIST),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mgr = FeedbackManager(session, principal.organisation_id)
            record = await mgr.get_feedback_record(record_id)
            pipeline_name = await _resolve_pipeline_name(session, record)
    except IntegrityError as exc:
        logger.exception(_CODE_FEEDBACK_GET_INBOX_ITEM)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_FEEDBACK_GET_INBOX_ITEM)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_FEEDBACK_GET_INBOX_ITEM)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error getting inbox item %s", record_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)

    return _serialise_record(record, pipeline_name=pipeline_name)


async def _spawn_correction_run(
    mgr: FeedbackManager,
    record: FeedbackRecord,
    record_id: uuid.UUID,
) -> tuple[str, str]:
    """Spawn a correction run, returning ``(run_id, transitioned_to)``."""
    if not record.run_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Feedback has no associated run — cannot create correction run",
        )

    try:
        new_run_id = await mgr.spawn_correction_run(record_id)
    except FeedbackRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FeedbackRecordRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidTransitionError, ConcurrentModificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return str(new_run_id), "correcting"


async def _apply_review_action(
    mgr: FeedbackManager,
    session: AsyncSession,
    principal: TenantPrincipal,
    record_id: uuid.UUID,
    req: ReviewFeedbackRequest,
) -> tuple[FeedbackRecord, str, str | None, str | None]:
    """Apply a review action, returning (record, old_status, transition, correction_run_id)."""
    transitioned_to: str | None = None
    correction_run_id: str | None = None
    record = await mgr.get_feedback_record(record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)
    old_status = record.feedback_status

    if req.action == "mark_reviewed":
        record = await mgr.update_status(record_id, "resolved")
        transitioned_to = "resolved"
    elif req.action == "dismiss":
        record = await mgr.update_status(record_id, "dismissed")
        transitioned_to = "dismissed"
    elif req.action == "create_correction_run":
        correction_run_id, transitioned_to = await _spawn_correction_run(mgr, record, record_id)

    if req.annotation is not None:
        await session.execute(
            sa_update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.organisation_id == principal.organisation_id,
            )
            .values(annotation=req.annotation)
        )
    return record, old_status, transitioned_to, correction_run_id


@router.post("/feedback/inbox/{record_id}/review", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_FEEDBACK_REVIEW_FEEDBACK)
async def review_feedback(
    record_id: uuid.UUID,
    req: ReviewFeedbackRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("feedback.review"),
) -> dict[str, Any]:
    valid_actions = {"mark_reviewed", "dismiss", "create_correction_run"}
    if req.action not in valid_actions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid action. Must be one of: {', '.join(sorted(valid_actions))}",
        )

    old_status: str | None = None
    transitioned_to: str | None = None
    correction_run_id: str | None = None
    record: FeedbackRecord | None = None

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            mgr = FeedbackManager(session, principal.organisation_id)
            record, old_status, transitioned_to, correction_run_id = await _apply_review_action(
                mgr, session, principal, record_id, req
            )

    except IntegrityError:
        logger.exception(_CODE_FEEDBACK_REVIEW_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_RESOURCE_CONFLICT_OCCURRED_PLEASE,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_FEEDBACK_REVIEW_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEEDBACK_SYSTEM_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_FEEDBACK_REVIEW_FEEDBACK)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except (InvalidTransitionError, ConcurrentModificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except FeedbackRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FeedbackRecordRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error reviewing feedback %s", record_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_FEEDBACK_RECORD_NOT_FOUND)

    if transitioned_to is not None:
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="feedback_record",
            event_type="feedback.status_changed",
            resource_id=record_id,
            payload={
                "old_status": old_status,
                "new_status": transitioned_to,
                "action": req.action,
                "run_id": str(record.run_id) if record.run_id else None,
                "gate_id": record.gate_id,
                "correction_run_id": correction_run_id,
            },
            log_key=_CODE_FEEDBACK_AUDIT_APPEND_FAILED,
        )

    return {
        "id": str(record.id),
        "feedback_status": record.feedback_status,
        "correction_run_id": correction_run_id,
    }
