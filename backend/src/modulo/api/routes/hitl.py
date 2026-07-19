"""HITL (Human-In-The-Loop) API routes.

All HITL operations are scoped to the authenticated user's organisation.
Claim, approve, and reject require the run to be in ``awaiting_human`` status.

Claim-token-based approve/reject require the token returned from a successful
claim.  ``human_only`` gates additionally reject MCP-initiated approve requests
(checked by the ViewModel layer — this route does not distinguish clients).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import _get_engine, get_db_session, pg_connection_string
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
    NotTeamMemberError,
)
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.run import get_run, update_run_status
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline import Pipeline
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["hitl"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ClaimRequest(BaseModel):
    expiry_minutes: int = Field(default=15, ge=1, le=1440)


class ClaimResponse(BaseModel):
    run_id: uuid.UUID
    gate_id: str
    claim_token: str
    expires_at: str


class ApproveRequest(BaseModel):
    claim_token: str
    notes: str | None = None


class ApproveWithModificationRequest(BaseModel):
    claim_token: str
    modified_output: dict[str, Any]
    notes: str | None = None


class RejectRequest(BaseModel):
    claim_token: str
    reason: str = Field(..., min_length=1)


class DeliverManualRequest(BaseModel):
    claim_token: str
    output: dict[str, Any]


class ManualOutputRequest(BaseModel):
    claim_token: str
    output: dict[str, Any]


class GateResponse(BaseModel):
    run_id: uuid.UUID
    gate_id: str
    pipeline_id: uuid.UUID
    pipeline_name: str | None = None
    claimed_by: uuid.UUID | None = None
    claimed_at: str | None = None
    expires_at: str | None = None
    decision: str | None = None
    decision_at: str | None = None


class PendingGatesResponse(BaseModel):
    gates: list[GateResponse]


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/claim",
    response_model=ClaimResponse,
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.claim_gate")
@router.post(
    "/runs/{run_id}/hitl/{gate_id}/claim",
    response_model=ClaimResponse,
    status_code=status.HTTP_200_OK,
)
async def claim_gate(
    run_id: uuid.UUID,
    gate_id: str,
    req: ClaimRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ClaimResponse:
    """Atomically claim a HITL gate. Returns a claim_token for approve/reject."""
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                gate = await mgr.claim(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claimant_id=principal.account_id,
                    expiry_minutes=req.expiry_minutes,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except AlreadyClaimedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except NotTeamMemberError as exc:
                logger.warning("hitl.claim_gate.team_access_denied: %s", exc)
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

            # Update run status to "claimed".
            await update_run_status(session, run_id, "claimed")
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.claim_gate.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    if gate.claim_token is None or gate.expires_at is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="gate_missing_claim_data: Gate claim token or expiry missing after successful claim",
        )
    return ClaimResponse(
        run_id=gate.run_id,
        gate_id=gate.gate_id,
        claim_token=gate.claim_token,
        expires_at=gate.expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/approve",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.approve_gate")
@router.post(
    "/runs/{run_id}/hitl/{gate_id}/approve",
    status_code=status.HTTP_200_OK,
)
async def approve_gate(
    run_id: uuid.UUID,
    gate_id: str,
    req: ApproveRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    """Approve an interrupted HITL gate and resume the run."""
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                await mgr.approve(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    actor_id=principal.account_id,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.approve_gate.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    resume_data: dict[str, Any] = {"action": "approved"}
    if req.notes:
        resume_data["notes"] = req.notes

    try:
        executor = PipelineExecutor(
            engine,
            checkpointer_conn_string=pg_connection_string(get_settings().database_url),
        )
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except Exception as exc:
        logger.exception("hitl.approve_gate.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after approval",
        ) from exc

    return {"status": "approved", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Approve with modification
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/approve-with-modification",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.approve_gate_with_modification")
@router.post(
    "/runs/{run_id}/hitl/{gate_id}/approve-with-modification",
    status_code=status.HTTP_200_OK,
)
async def approve_gate_with_modification(
    run_id: uuid.UUID,
    gate_id: str,
    req: ApproveWithModificationRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    """Approve a HITL gate with a modified output payload.

    The human reviewer's modified output replaces the agent's original output
    for downstream nodes.  A ``hitl.output_modified`` audit event is logged
    documenting the change.
    """
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                await mgr.approve_with_modification(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    modified_output=req.modified_output,
                    actor_id=principal.account_id,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.approve_gate_with_modification.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    resume_data: dict[str, Any] = {
        "action": "approved",
        "modified_output": req.modified_output,
    }
    if req.notes:
        resume_data["notes"] = req.notes

    try:
        executor = PipelineExecutor(
            engine,
            checkpointer_conn_string=pg_connection_string(get_settings().database_url),
        )
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except Exception as exc:
        logger.exception("hitl.approve_with_modification.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after approval with modification",
        ) from exc

    return {"status": "approved_with_modification", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/reject",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.reject_gate")
@router.post(
    "/runs/{run_id}/hitl/{gate_id}/reject",
    status_code=status.HTTP_200_OK,
)
async def reject_gate(
    run_id: uuid.UUID,
    gate_id: str,
    req: RejectRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    """Reject an interrupted HITL gate and route to reject_target or fail."""
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                await mgr.reject(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    actor_id=principal.account_id,
                    claim_token=req.claim_token,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.reject_gate.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    # Resume the graph with rejection data so the gate router picks the
    # reject_target branch.
    resume_data: dict[str, Any] = {"action": "rejected", "reason": req.reason}
    try:
        executor = PipelineExecutor(
            engine,
            checkpointer_conn_string=pg_connection_string(str(engine.url)),
        )
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except Exception as exc:
        logger.exception("hitl.reject_gate.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after rejection",
        ) from exc

    return {"status": "rejected", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Deliver Manual — human supplies output directly at a HITL gate
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/deliver-manual",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.deliver_manual_output")
@router.post(
    "/runs/{run_id}/hitl/{gate_id}/deliver-manual",
    status_code=status.HTTP_200_OK,
)
async def deliver_manual_output(
    run_id: uuid.UUID,
    gate_id: str,
    req: DeliverManualRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    """Deliver manually-supplied output at a HITL gate and resume the run.

    The reviewer provides the output directly instead of routing to a
    correction run or back to the agent. The output is validated and the
    run continues past the gate with the manually-supplied value.
    """
    if not req.output:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="output must be a non-empty object",
        )

    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                await mgr.deliver_manual(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    output=req.output,
                    actor_id=principal.account_id,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.deliver_manual_output.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    resume_data: dict[str, Any] = {"action": "deliver_manual", "output": req.output}
    try:
        executor = PipelineExecutor(
            engine,
            checkpointer_conn_string=pg_connection_string(str(engine.url)),
        )
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except Exception as exc:
        logger.exception("hitl.deliver_manual_output.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after manual delivery",
        ) from exc

    return {"status": "delivered_manual", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Manual node output
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/manual/{gate_id}/submit",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.submit_manual_output")
@router.post(
    "/runs/{run_id}/manual/{gate_id}/submit",
    status_code=status.HTTP_200_OK,
)
async def submit_manual_output(
    run_id: uuid.UUID,
    gate_id: str,
    req: ManualOutputRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    """Submit output for a manual-input node and resume the run."""
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                await mgr.approve(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    actor_id=principal.account_id,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
            except NotTeamMemberError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.submit_manual_output.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    resume_data: dict[str, Any] = {"action": "manual_output", "output": req.output}
    try:
        executor = PipelineExecutor(
            engine,
            checkpointer_conn_string=pg_connection_string(str(engine.url)),
        )
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except Exception as exc:
        logger.exception("hitl.submit_manual_output.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after manual output submission",
        ) from exc

    return {"status": "submitted", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get(
    "/runs/{run_id}/hitl/pending",
    response_model=PendingGatesResponse,
)
@handle_db_errors("hitl.list_run_pending_gates")
@router.get(
    "/runs/{run_id}/hitl/pending",
    response_model=PendingGatesResponse,
)
async def list_run_pending_gates(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> PendingGatesResponse:
    """List all pending (undecided) HITL gates for a specific run."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)
            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            result = await session.execute(
                select(HitlClaim).where(
                    HitlClaim.run_id == run_id,
                    HitlClaim.organisation_id == principal.organisation_id,
                    HitlClaim.decision.is_(None),
                )
            )
            gates = list(result.scalars())

            pipeline_name: str | None = None
            if gates:
                pipeline = await session.get(Pipeline, gates[0].pipeline_id)
                pipeline_name = pipeline.name if pipeline else None
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.list_run_pending_gates.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    return PendingGatesResponse(gates=[_gate_to_response(g, pipeline_name=pipeline_name) for g in gates])


@router.get(
    "/hitl/pending",
    response_model=PendingGatesResponse,
)
@handle_db_errors("hitl.list_org_pending_gates")
@router.get(
    "/hitl/pending",
    response_model=PendingGatesResponse,
)
async def list_org_pending_gates(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> PendingGatesResponse:
    """List all pending HITL gates across the organisation."""
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            gates = await mgr.list_pending(session, principal.organisation_id)

            pipeline_ids = list({g.pipeline_id for g in gates})
            pipeline_map: dict[uuid.UUID, str] = {}
            if pipeline_ids:
                pipeline_rows = await session.execute(
                    select(Pipeline.id, Pipeline.name).where(Pipeline.id.in_(pipeline_ids))
                )
                for pid, pname in pipeline_rows.all():
                    pipeline_map[pid] = pname
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.list_org_pending_gates.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    return PendingGatesResponse(
        gates=[_gate_to_response(g, pipeline_name=pipeline_map.get(g.pipeline_id)) for g in gates]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gate_to_response(g: HitlClaim, pipeline_name: str | None = None) -> GateResponse:
    return GateResponse(
        run_id=g.run_id,
        gate_id=g.gate_id,
        pipeline_id=g.pipeline_id,
        pipeline_name=pipeline_name,
        claimed_by=g.account_id,
        claimed_at=g.claimed_at.isoformat() if g.claimed_at else None,
        expires_at=g.expires_at.isoformat() if g.expires_at else None,
        decision=g.decision,
        decision_at=g.decision_at.isoformat() if g.decision_at else None,
    )
