"""HITL (Human-In-The-Loop) API routes.

All HITL operations are scoped to the authenticated user's organisation.
Claim, approve, and reject require the run to be in ``awaiting_human`` status.

Claim-token-based approve/reject require the token returned from a successful
claim.  ``human_only`` gates additionally reject MCP-initiated approve requests
(checked by the ViewModel layer — this route does not distinguish clients).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
)
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.run import get_run, update_run_status
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.rls import set_rls_org

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
async def claim_gate(
    run_id: uuid.UUID,
    gate_id: str,
    body: ClaimRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ClaimResponse:
    """Atomically claim a HITL gate. Returns a claim_token for approve/reject."""
    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            gate = await mgr.claim(
                session,
                run_id=run_id,
                gate_id=gate_id,
                org_id=principal.organisation_id,
                claimant_id=principal.account_id,
                expiry_minutes=body.expiry_minutes,
            )
        except GateNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except AlreadyClaimedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        # Update run status to "claimed".
        await update_run_status(session, run_id, "claimed")

    assert gate.claim_token is not None
    assert gate.expires_at is not None
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
async def approve_gate(
    run_id: uuid.UUID,
    gate_id: str,
    body: ApproveRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Approve an interrupted HITL gate and resume the run."""
    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            await mgr.approve(
                session,
                run_id=run_id,
                gate_id=gate_id,
                org_id=principal.organisation_id,
                claim_token=body.claim_token,
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

    resume_data: dict[str, Any] = {"action": "approved"}
    if body.notes:
        resume_data["notes"] = body.notes

    executor = PipelineExecutor(engine)
    await executor.resume(
        run_id=run_id,
        org_id=principal.organisation_id,
        resume_data=resume_data,
    )

    return {"status": "approved", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Approve with modification
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/approve-with-modification",
    status_code=status.HTTP_200_OK,
)
async def approve_gate_with_modification(
    run_id: uuid.UUID,
    gate_id: str,
    body: ApproveWithModificationRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Approve a HITL gate with a modified output payload.

    The human reviewer's modified output replaces the agent's original output
    for downstream nodes.  A ``hitl.output_modified`` audit event is logged
    documenting the change.
    """
    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            await mgr.approve_with_modification(
                session,
                run_id=run_id,
                gate_id=gate_id,
                org_id=principal.organisation_id,
                claim_token=body.claim_token,
                modified_output=body.modified_output,
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

    resume_data: dict[str, Any] = {
        "action": "approved",
        "modified_output": body.modified_output,
    }
    if body.notes:
        resume_data["notes"] = body.notes

    executor = PipelineExecutor(engine)
    await executor.resume(
        run_id=run_id,
        org_id=principal.organisation_id,
        resume_data=resume_data,
    )

    return {"status": "approved_with_modification", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/reject",
    status_code=status.HTTP_200_OK,
)
async def reject_gate(
    run_id: uuid.UUID,
    gate_id: str,
    body: RejectRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Reject an interrupted HITL gate and route to reject_target or fail."""
    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            await mgr.reject(
                session,
                run_id=run_id,
                gate_id=gate_id,
                org_id=principal.organisation_id,
                claim_token=body.claim_token,
            )
        except GateNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GateAlreadyDecidedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ClaimTokenInvalidError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ClaimTokenExpiredError as exc:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc

    # Resume the graph with rejection data so the gate router picks the
    # reject_target branch.
    resume_data: dict[str, Any] = {"action": "rejected", "reason": body.reason}
    executor = PipelineExecutor(engine)
    await executor.resume(
        run_id=run_id,
        org_id=principal.organisation_id,
        resume_data=resume_data,
    )

    return {"status": "rejected", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Deliver Manual — human supplies output directly at a HITL gate
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/deliver-manual",
    status_code=status.HTTP_200_OK,
)
async def deliver_manual_output(
    run_id: uuid.UUID,
    gate_id: str,
    body: DeliverManualRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Deliver manually-supplied output at a HITL gate and resume the run.

    The reviewer provides the output directly instead of routing to a
    correction run or back to the agent. The output is validated and the
    run continues past the gate with the manually-supplied value.
    """
    if not body.output:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="output must be a non-empty object",
        )

    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            await mgr.deliver_manual(
                session,
                run_id=run_id,
                gate_id=gate_id,
                org_id=principal.organisation_id,
                claim_token=body.claim_token,
                output=body.output,
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

    resume_data: dict[str, Any] = {"action": "deliver_manual", "output": body.output}
    executor = PipelineExecutor(engine)
    await executor.resume(
        run_id=run_id,
        org_id=principal.organisation_id,
        resume_data=resume_data,
    )

    return {"status": "delivered_manual", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Manual node output
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/manual/{gate_id}/submit",
    status_code=status.HTTP_200_OK,
)
async def submit_manual_output(
    run_id: uuid.UUID,
    gate_id: str,
    body: ManualOutputRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Submit output for a manual-input node and resume the run."""
    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            await mgr.approve(
                session,
                run_id=run_id,
                gate_id=gate_id,
                org_id=principal.organisation_id,
                claim_token=body.claim_token,
                actor_id=principal.account_id,
            )
        except GateNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GateAlreadyDecidedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (ClaimTokenInvalidError, ClaimTokenExpiredError) as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    resume_data: dict[str, Any] = {"action": "manual_output", "output": body.output}
    executor = PipelineExecutor(engine)
    await executor.resume(
        run_id=run_id,
        org_id=principal.organisation_id,
        resume_data=resume_data,
    )

    return {"status": "submitted", "run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get(
    "/runs/{run_id}/hitl/pending",
    response_model=PendingGatesResponse,
)
async def list_run_pending_gates(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PendingGatesResponse:
    """List all pending (undecided) HITL gates for a specific run."""
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

    return PendingGatesResponse(gates=[_gate_to_response(g) for g in gates])


@router.get(
    "/hitl/pending",
    response_model=PendingGatesResponse,
)
async def list_org_pending_gates(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PendingGatesResponse:
    """List all pending HITL gates across the organisation."""
    mgr = HITLManager()
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        gates = await mgr.list_pending(session, principal.organisation_id)

    return PendingGatesResponse(gates=[_gate_to_response(g) for g in gates])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gate_to_response(g: HitlClaim) -> GateResponse:
    return GateResponse(
        run_id=g.run_id,
        gate_id=g.gate_id,
        pipeline_id=g.pipeline_id,
        claimed_by=g.claimed_by,
        claimed_at=g.claimed_at.isoformat() if g.claimed_at else None,
        expires_at=g.expires_at.isoformat() if g.expires_at else None,
        decision=g.decision,
        decision_at=g.decision_at.isoformat() if g.decision_at else None,
    )
