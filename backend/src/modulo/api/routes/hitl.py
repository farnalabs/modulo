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

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR_NO_PERIOD
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import _get_engine, get_db_session, pg_connection_string, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    DecisionPayloadError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
    NotTeamMemberError,
)
from modulo.core.notifier import Notifier
from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    SandboxCapacityExceededError,
    org_sandbox_capacity_free,
)
from modulo.db.crud.run import get_run, update_run_status
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline import Pipeline
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again."
_CODE_HITL_APPROVE = "hitl.approve"


def _build_resume_executor(engine: AsyncEngine) -> PipelineExecutor:
    """Build a resume executor wired with the ``hitl_awaiting`` notifier.

    Closes the team-hitl-gates Known Gap: a resume that re-interrupts on a
    further HITL gate must dispatch the webhook/in-app notification, not just
    the WebSocket broker event. Notifier init is failure-isolated (fail-open —
    the resume still runs if the notifier cannot be constructed).
    """
    notifier: Notifier | None = None
    try:
        notifier = Notifier(engine, get_settings().fernet_key)
    except Exception:
        logger.exception("hitl.build_resume_executor.notifier_init_failed")
    return PipelineExecutor(
        engine,
        checkpointer_conn_string=pg_connection_string(get_settings().database_url),
        notifier=notifier,
    )


logger = logging.getLogger(__name__)

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
    #: Human label from the snapshot edge's ``hitl_gate_config.label``
    #: (frontend UUID hygiene — falls back to shortId when absent).
    label: str | None = None


class PendingGatesResponse(BaseModel):
    gates: list[GateResponse]


async def _require_org_sandbox_capacity(session: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Raise ``409`` when the org sandbox cap blocks a gate resume.

    Runs BEFORE the HITL gate decision is committed so a capacity decline
    never deadlocks the run (gate decided + run stuck). The gate stays
    undecided and the human can retry once a slot frees.

    409 Conflict (rather than 202 Accepted) is returned because nothing is
    accepted or queued by this call — the request conflicts with the current
    state (org at sandbox capacity) and no state is changed.

    Applied to the resume actions (approve / approve-with-modification /
    deliver-manual / submit-manual), which continue executing the sandbox
    graph. ``reject_gate`` is deliberately exempt: a rejection routes the run
    to its ``reject_target`` or terminates it — it does not resume sandbox
    execution, so blocking it on capacity would only confuse the operator.
    """
    if not await org_sandbox_capacity_free(session, org_id, run_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox concurrency limit reached; gate left undecided. Retry when capacity frees up.",
        )


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/hitl/{gate_id}/claim",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("hitl.claim_gate")
async def claim_gate(
    run_id: uuid.UUID,
    gate_id: str,
    req: ClaimRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("hitl.claim"),
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
        logger.exception("hitl.claim_gate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.claim_gate")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.claim_gate.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
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
async def approve_gate(
    run_id: uuid.UUID,
    gate_id: str,
    req: ApproveRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = require_permission(_CODE_HITL_APPROVE),
) -> dict[str, str]:
    """Approve an interrupted HITL gate and resume the run."""
    resume_data: dict[str, Any] = {"action": "approved"}
    if req.notes:
        resume_data["notes"] = req.notes

    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _require_org_sandbox_capacity(session, run_id, principal.organisation_id)
            try:
                await mgr.approve(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    actor_id=principal.account_id,
                    decision_payload=resume_data,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
            except DecisionPayloadError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ProgrammingError as exc:
        logger.exception("hitl.approve_gate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.approve_gate")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.approve_gate.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    try:
        executor = _build_resume_executor(engine)
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except SandboxCapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
async def approve_gate_with_modification(
    run_id: uuid.UUID,
    gate_id: str,
    req: ApproveWithModificationRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = require_permission(_CODE_HITL_APPROVE),
) -> dict[str, str]:
    """Approve a HITL gate with a modified output payload.

    The human reviewer's modified output replaces the agent's original output
    for downstream nodes.  A ``hitl.output_modified`` audit event is logged
    documenting the change.
    """
    mgr = HITLManager()
    resume_data: dict[str, Any] = {
        "action": "approved",
        "modified_output": req.modified_output,
    }
    if req.notes:
        resume_data["notes"] = req.notes
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _require_org_sandbox_capacity(session, run_id, principal.organisation_id)
            try:
                await mgr.approve_with_modification(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    modified_output=req.modified_output,
                    actor_id=principal.account_id,
                    decision_payload=resume_data,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
            except DecisionPayloadError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ProgrammingError as exc:
        logger.exception("hitl.approve_gate_with_modification")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.approve_gate_with_modification")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.approve_gate_with_modification.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    try:
        executor = _build_resume_executor(engine)
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except SandboxCapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
async def reject_gate(
    run_id: uuid.UUID,
    gate_id: str,
    req: RejectRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = require_permission("hitl.reject"),
) -> dict[str, str]:
    """Reject an interrupted HITL gate and route to reject_target or fail."""
    resume_data: dict[str, Any] = {"action": "rejected", "reason": req.reason}
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            # No org-sandbox-capacity gate here (unlike the resume actions):
            # rejecting routes the run to its reject_target or terminates it —
            # it must not be blocked (or 202-"queued") because the org is at
            # sandbox capacity.
            try:
                await mgr.reject(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    actor_id=principal.account_id,
                    claim_token=req.claim_token,
                    decision_payload=resume_data,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
            except DecisionPayloadError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ProgrammingError as exc:
        logger.exception("hitl.reject_gate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.reject_gate")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.reject_gate.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    # Resume the graph with rejection data so the gate router picks the
    # reject_target branch.
    try:
        executor = _build_resume_executor(engine)
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
            check_sandbox_capacity=False,
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
async def deliver_manual_output(
    run_id: uuid.UUID,
    gate_id: str,
    req: DeliverManualRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = require_permission("hitl.deliver_manual"),
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

    resume_data: dict[str, Any] = {"action": "deliver_manual", "output": req.output}
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _require_org_sandbox_capacity(session, run_id, principal.organisation_id)
            try:
                await mgr.deliver_manual(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    output=req.output,
                    actor_id=principal.account_id,
                    decision_payload=resume_data,
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except GateAlreadyDecidedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ClaimTokenInvalidError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ClaimTokenExpiredError as exc:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
            except DecisionPayloadError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ProgrammingError as exc:
        logger.exception("hitl.deliver_manual_output")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.deliver_manual_output")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.deliver_manual_output.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    try:
        executor = _build_resume_executor(engine)
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except SandboxCapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
async def submit_manual_output(
    run_id: uuid.UUID,
    gate_id: str,
    req: ManualOutputRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = require_permission(_CODE_HITL_APPROVE),
) -> dict[str, str]:
    """Submit output for a manual-input node and resume the run."""
    resume_data: dict[str, Any] = {"action": "manual_output", "output": req.output}
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _require_org_sandbox_capacity(session, run_id, principal.organisation_id)
            try:
                await mgr.approve(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    claim_token=req.claim_token,
                    actor_id=principal.account_id,
                    decision_payload=resume_data,
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
            except DecisionPayloadError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ProgrammingError as exc:
        logger.exception("hitl.submit_manual_output")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.submit_manual_output")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.submit_manual_output.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    try:
        executor = _build_resume_executor(engine)
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except SandboxCapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
)
@handle_db_errors("hitl.list_run_pending_gates")
async def list_run_pending_gates(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("hitl.list"),
) -> PendingGatesResponse:
    """List all pending (undecided) HITL gates for a specific run."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
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

            gate_label_map: dict[str, str] = {}
            if run is not None and run.snapshot_id:
                from modulo.db.models.pipeline_snapshot import PipelineSnapshot as SnapModel

                snap_result = await session.execute(select(SnapModel).where(SnapModel.id == run.snapshot_id))
                snapshot = snap_result.scalar_one_or_none()
                if snapshot is not None and isinstance(snapshot.graph_json, dict):
                    gate_label_map = _build_gate_label_map(snapshot.graph_json)
    except ProgrammingError as exc:
        logger.exception("hitl.list_run_pending_gates")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.list_run_pending_gates")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.list_run_pending_gates.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return PendingGatesResponse(
        gates=[_gate_to_response(g, pipeline_name=pipeline_name, label=gate_label_map.get(g.gate_id)) for g in gates]
    )


@router.get(
    "/hitl/pending",
)
@handle_db_errors("hitl.list_org_pending_gates")
async def list_org_pending_gates(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("hitl.list"),
) -> PendingGatesResponse:
    """List all pending HITL gates across the organisation."""
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            gates = await mgr.list_pending(session, principal.organisation_id)

            pipeline_ids = list({g.pipeline_id for g in gates})
            pipeline_map: dict[uuid.UUID, str] = {}
            if pipeline_ids:
                pipeline_rows = await session.execute(
                    select(Pipeline.id, Pipeline.name).where(Pipeline.id.in_(pipeline_ids))
                )
                pipeline_map = {row[0]: row[1] for row in pipeline_rows.all()}
    except ProgrammingError as exc:
        logger.exception("hitl.list_org_pending_gates")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.list_org_pending_gates")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.list_org_pending_gates.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    # Org-level: gates span many runs, so per-run snapshot lookups are
    # expensive. Leave label=None here — the frontend falls back to shortId.
    # Only the run-level endpoint (used by RunDetailView) resolves the label.
    return PendingGatesResponse(
        gates=[_gate_to_response(g, pipeline_name=pipeline_map.get(g.pipeline_id)) for g in gates]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _edge_source_or_target(edge: dict[str, Any], key: str) -> str | None:
    """Resolve an edge's source/target node id (canonical + persisted keys).

    Mirrors ``graph_cache._get_edge_val`` but returns None instead of raising
    when the edge omits the field, so a malformed snapshot edge never breaks
    gate-label resolution.
    """
    value = edge.get(key) or edge.get(f"{key}_node_id")
    return str(value) if value is not None else None


def _build_gate_label_map(graph_json: dict[str, Any]) -> dict[str, str]:
    """Map gate_id -> human label from snapshot edges carrying hitl_gate_config.

    Gate id format is ``hitl_gate_<source>_<target>`` (see
    ``graph_cache._make_gate_id``). Edges without a ``label`` in their
    ``hitl_gate_config`` are omitted so the frontend falls back to shortId.
    """
    gate_label_map: dict[str, str] = {}
    for edge in graph_json.get("edges", []):
        if not isinstance(edge, dict):
            continue
        hitl_config = edge.get("hitl_gate_config")
        if not isinstance(hitl_config, dict):
            continue
        label = hitl_config.get("label")
        if not label:
            continue
        source = _edge_source_or_target(edge, "source")
        target = _edge_source_or_target(edge, "target")
        if source and target:
            gate_label_map[f"hitl_gate_{source}_{target}"] = str(label)
    return gate_label_map


def _gate_to_response(g: HitlClaim, pipeline_name: str | None = None, label: str | None = None) -> GateResponse:
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
        label=label,
    )
