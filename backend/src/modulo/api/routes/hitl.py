"""HITL (Human-In-The-Loop) API routes.

All HITL operations are scoped to the authenticated user's organisation.
Claim, approve, and reject require the run to be in ``awaiting_human`` status.

Claim-token-based approve/reject require the token returned from a successful
claim.  ``human_only`` gates additionally reject decisions made with a
non-browser credential: the resume routes (approve / approve-with-modification
/ deliver-manual / submit-manual) resolve the gate's actual edge config and
raise 403 for API-key principals (FAR-610 — the routes previously performed no
``human_only`` check at all). When the config cannot be resolved but the gate
fired (a claim row exists), API-key principals are denied too — fail closed,
since the policy cannot be verified. ``reject_gate`` is deliberately exempt:
rejection is the safe direction. The MCP surface (``mcp_server.py``) denies
``human_only`` approve/deliver-manual outright — MCP clients authenticate with
API keys and are never browser sessions.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, nullslast, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.api.constants import MSG_DB_ERROR_PLEASE_TRY, MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR_NO_PERIOD
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
    RunNotAwaitingError,
)
from modulo.core.notifier import Notifier
from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    SandboxCapacityExceededError,
    org_sandbox_capacity_free,
)
from modulo.db.crud.hitl_gate_config import (
    edge_source_or_target,
    hitl_gate_exists_but_unresolved,
    human_only_denial,
    make_gate_id,
    normalize_gate_description,
    resolve_gate_descriptions,
    resolve_hitl_gate_config,
    snapshot_gate_config_map,
)
from modulo.db.crud.run import get_run, transition_run
from modulo.db.models.account import Account
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.pipeline_snapshot import PipelineSnapshot as SnapModel
from modulo.db.models.run import HITL_ACTIONABLE_RUN_STATUSES, Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

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
    #: FAR-613: the gate config's human description — WHY this gate exists.
    #: Resolved from the snapshot gate config (edge-level or FAR-402
    #: node-level). None for legacy gates → the UI renders the muted
    #: no-description fallback.
    description: str | None = None
    #: FAR-613: the fire-time briefing bundle persisted on the claim row
    #: (condition, trigger, source node, bounded artifacts, reason,
    #: pipeline_name). None for legacy gates.
    context: dict[str, Any] | None = None
    #: FAR-691: the claimant's human-readable display name (batched accounts
    #: lookup). None when the account row is missing — the frontend falls
    #: back to the raw account UUID.
    claimed_by_name: str | None = None
    #: FAR-691: whether the claimant is the caller, stamped server-side from
    #: the principal (the server knows the caller; no client auth state).
    #: Drives the frontend's stale-token drop for foreign-claimed gates.
    claimed_by_me: bool = False


class PendingGatesResponse(BaseModel):
    gates: list[GateResponse]


class GateListResponse(BaseModel):
    """Paginated org gate listing (FAR-692) — the repo's standard list envelope."""

    items: list[GateResponse]
    total: int
    page: int
    page_size: int


#: FAR-692: the ``status`` query param on GET /api/v1/hitl/gates. A Literal type
#: (not a bare str) so FastAPI's request validation rejects unknown values with
#: 422 for free — no hand-rolled validation in the handler.
GateStatusFilter = Literal["undecided", "pending", "claimed", "approved", "rejected", "all"]

#: page_size is clamped (not 422'd) at this ceiling — mirrors the runs-list
#: convention of a bounded page size without failing the whole request.
_GATE_PAGE_SIZE_MAX = 100

#: The ``status`` values on GET /api/v1/hitl/gates that view PENDING WORK. The
#: data-rot fence (FAR-612/FAR-604) applies only to these: like
#: ``HITLManager.list_pending`` they join ``runs`` and keep only gates whose run
#: is still actionable. ``approved``/``rejected`` (decided history) and ``all``
#: (audit view) are deliberately unfenced — see ``list_org_gates``.
_PENDING_WORK_GATE_STATUSES = frozenset({"undecided", "pending", "claimed"})


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


def _client_type(principal: TenantPrincipal) -> str:
    """The caller's credential kind for HITL audit enrichment (FAR-611).

    JWTs carry no client-type claim (no amr / token-type marker), so the
    principal's ``via_api_key`` credential-kind marker (FAR-610) is the only
    reliable signal: ``"api_key"`` for mk_ principals, ``"browser"`` for JWT
    logins. MCP callers pass ``"mcp"`` directly in ``mcp_server.py``.
    """
    return "api_key" if principal.via_api_key else "browser"


async def _enforce_human_only_gate(
    session: AsyncSession,
    principal: TenantPrincipal,
    run_id: uuid.UUID,
    gate_id: str,
) -> None:
    """Raise ``403`` when a non-browser (API-key) principal decides a ``human_only`` gate.

    FAR-610: the decision routes previously performed no ``human_only`` check.
    The gate's config is resolved from the run's snapshot graph (falling back
    to the live edges / live HITL-node config) via the shared resolver
    (:func:`modulo.db.crud.hitl_gate_config.resolve_hitl_gate_config`) — the
    gate id maps to exactly one edge by topology, never by position. Runs
    BEFORE the capacity check and the manager call so a denial has no side
    effects (fail fast, gate left undecided).

    Fail closed (FAR-610 review): when the config is UNRESOLVABLE but the
    gate actually fired (a claim row exists — see
    :func:`modulo.db.crud.hitl_gate_config.hitl_gate_exists_but_unresolved`),
    the human_only policy cannot be verified, so API-key principals are
    denied rather than silently allowed. Browser JWTs pass either way (the
    UI is their enforcement surface), and manual-node ids short-circuit
    inside the helper, so ``submit_manual_output`` is unaffected.

    Applied to the resume routes (approve / approve-with-modification /
    deliver-manual / submit-manual). ``reject_gate`` is deliberately exempt:
    rejection is the safe direction.

    Credential semantics (FAR-610 finding): REST resolves principals only from
    browser-login JWTs today — org API keys (``mk_``) are accepted solely by
    the MCP server and the few ``require_permission_any_credential`` routes.
    JWTs carry no client-type claim (no amr / token-type / client_id marker),
    so the principal's ``via_api_key`` credential-kind marker is the only
    reliable signal available. Browser JWTs pass this check; if API keys are
    ever wired into these routes (operator keys are reserved for
    HITL-approval wiring), enforcement is already in place. MCP approvals —
    the observed attack path — are denied outright for ``human_only`` gates in
    ``mcp_server._check_human_only_gate``.

    Hot path (FAR-610 review): the first line short-circuits browser JWTs —
    they are always allowed, so the resolver's 1-3 queries never run on the
    common UI approve flow. The deny policy itself lives in the shared pure
    verdict :func:`modulo.db.crud.hitl_gate_config.human_only_denial`; the
    fail-closed claim lookup runs only when the config is unresolvable.
    """
    if not principal.via_api_key:
        return
    config = await resolve_hitl_gate_config(
        session,
        run_id=run_id,
        gate_id=gate_id,
        org_id=principal.organisation_id,
    )
    gate_fired = False
    if config is None:
        gate_fired = await hitl_gate_exists_but_unresolved(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=principal.organisation_id,
        )
    verdict = human_only_denial(config, non_browser_credential=True, gate_fired=gate_fired)
    if verdict is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=verdict)


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
    """Atomically claim a HITL gate. Returns a claim_token for approve/reject.

    The post-claim run-status flip to ``claimed`` is fenced to runs still in
    ``awaiting_human`` (``transition_run`` with ``allowed_from``): if the run
    goes terminal between the claim's status pre-check and the flip, the fenced
    miss no-ops — the terminal status is preserved, the gate drops out of the
    pending list on the next refresh, and the claim token simply expires
    unused.
    """
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
                    client_type=_client_type(principal),
                )
            except GateNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except AlreadyClaimedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except RunNotAwaitingError as exc:
                # FAR-612: a terminal/still-executing run must never be flipped
                # to "claimed" by a stale gate claim -- 409 with the run's
                # actual status so the operator sees why.
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except NotTeamMemberError as exc:
                logger.warning("hitl.claim_gate.team_access_denied: %s", exc)
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

            # FAR-612: flip the run to "claimed" through the fenced transition
            # authority, guarded on the source status. A run that is not
            # awaiting a human decision is never clobbered by a claim: a run
            # that went terminal between claim()'s status pre-check and this
            # write no-ops (terminal status preserved — the gate drops out of
            # the pending list on the next refresh and the claim token simply
            # expires unused), and a ``hitl_parked`` run stays parked while its
            # gate is claimed-but-undecided (FAR-604: a claim is not a
            # decision; the un-park happens at decision time in
            # ``HITLManager._decide``). The guard lives INSIDE the conditional
            # UPDATE (``allowed_from``), so the park sweep's concurrent commit
            # is fenced out the same way ``not_status`` fenced it.
            await transition_run(
                session,
                run_id,
                principal.organisation_id,
                target_status="claimed",
                allowed_from=frozenset({"awaiting_human"}),
            )
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
            detail=MSG_DB_ERROR_PLEASE_TRY,
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


async def _run_hitl_manager(
    session: AsyncSession,
    principal: TenantPrincipal,
    run_id: uuid.UUID,
    gate_id: str,
    *,
    enforce_human_only: bool,
    require_sandbox: bool,
    mgr_method: str,
    **call_kwargs: Any,
) -> Any:
    """Open a tenant-scoped transaction and invoke a HITLManager decision method.

    Shared by the approve / approve-with-modification / reject / deliver-manual /
    submit-manual route handlers so the ``async with session.begin()`` +
    domain-exception-mapping boilerplate is not copy-pasted into every route.
    ``org_id`` and ``actor_id`` come from the principal; callers pass only the
    method-specific kwargs (``claim_token``, ``decision_payload``, ``output`` …).
    """
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            if enforce_human_only:
                await _enforce_human_only_gate(session, principal, run_id, gate_id)
            if require_sandbox:
                await _require_org_sandbox_capacity(session, run_id, principal.organisation_id)
            try:
                return await getattr(mgr, mgr_method)(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    org_id=principal.organisation_id,
                    actor_id=principal.account_id,
                    **call_kwargs,
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
        logger.exception("hitl._run_hitl_manager")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl._run_hitl_manager")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl._run_hitl_manager.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e


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
    # FAR-541: every resume decision is STAMPED with the gate it resolves so a
    # per-gate consumer (``_hitl_gate_resume_result``) can reject a foreign
    # decision left in state by an earlier gate (decisions are per-RUN but
    # consumers are per-gate; ``_hitl_decision`` is never cleared). HITLManager._decide
    # would stamp the persisted payload anyway; this explicit stamp feeds the
    # DIRECT executor.resume injection below, which bypasses _decide.
    resume_data: dict[str, Any] = {"action": "approved", "gate_id": gate_id}
    if req.notes:
        resume_data["notes"] = req.notes

    await _run_hitl_manager(
        session,
        principal,
        run_id,
        gate_id,
        enforce_human_only=True,
        require_sandbox=True,
        mgr_method="approve",
        claim_token=req.claim_token,
        decision_payload=resume_data,
        client_type=_client_type(principal),
    )

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
    # FAR-541: the payload is stamped with the gate it resolves (see approve_gate).
    # The real writer contract: action "approved" + "modified_output" (there is
    # no "approved_with_modification" action). _decide would stamp the persisted
    # payload anyway; this explicit stamp feeds the DIRECT executor.resume
    # injection below, which bypasses _decide.
    resume_data: dict[str, Any] = {
        "action": "approved",
        "gate_id": gate_id,
        "modified_output": req.modified_output,
    }
    if req.notes:
        resume_data["notes"] = req.notes
    await _run_hitl_manager(
        session,
        principal,
        run_id,
        gate_id,
        enforce_human_only=True,
        require_sandbox=True,
        mgr_method="approve_with_modification",
        claim_token=req.claim_token,
        modified_output=req.modified_output,
        decision_payload=resume_data,
        client_type=_client_type(principal),
    )

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
    # FAR-541: the payload is stamped with the gate it resolves (see approve_gate).
    # No enforce_human_only / require_sandbox guards here (unlike the resume
    # actions): rejecting routes the run to its reject_target or terminates it,
    # so it must not be blocked because the org is at sandbox capacity.
    resume_data: dict[str, Any] = {"action": "rejected", "gate_id": gate_id, "reason": req.reason}
    await _run_hitl_manager(
        session,
        principal,
        run_id,
        gate_id,
        enforce_human_only=False,
        require_sandbox=False,
        mgr_method="reject",
        claim_token=req.claim_token,
        decision_payload=resume_data,
        client_type=_client_type(principal),
    )

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

    # FAR-541: the payload is stamped with the gate it resolves (see approve_gate).
    # _decide would stamp the persisted payload anyway; this explicit stamp
    # feeds the DIRECT executor.resume injection below, which bypasses _decide.
    resume_data: dict[str, Any] = {"action": "deliver_manual", "gate_id": gate_id, "output": req.output}
    await _run_hitl_manager(
        session,
        principal,
        run_id,
        gate_id,
        enforce_human_only=True,
        require_sandbox=True,
        mgr_method="deliver_manual",
        claim_token=req.claim_token,
        output=req.output,
        decision_payload=resume_data,
        client_type=_client_type(principal),
    )

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
    # FAR-541: stamped with the NODE id being delivered to — the manual node's
    # consumer (``_manual_node``) resumes only on a decision stamped for it.
    # _decide would stamp the persisted payload anyway; this explicit stamp
    # feeds the DIRECT executor.resume injection below, which bypasses _decide.
    resume_data: dict[str, Any] = {"action": "manual_output", "gate_id": gate_id, "output": req.output}
    await _run_hitl_manager(
        session,
        principal,
        run_id,
        gate_id,
        enforce_human_only=True,
        require_sandbox=True,
        mgr_method="approve",
        claim_token=req.claim_token,
        decision_payload=resume_data,
        client_type=_client_type(principal),
    )

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
            gate_description_map: dict[str, str | None] = {}
            if run is not None and run.snapshot_id:
                snap_result = await session.execute(
                    select(PipelineSnapshot).where(
                        PipelineSnapshot.id == run.snapshot_id,
                        PipelineSnapshot.organisation_id == principal.organisation_id,
                    )
                )
                snapshot = snap_result.scalar_one_or_none()
                if snapshot is not None and isinstance(snapshot.graph_json, dict):
                    gate_label_map = _build_gate_label_map(snapshot.graph_json)
                    gate_description_map = _build_gate_description_map(snapshot.graph_json)

            # FAR-691: batched claimant display names + the caller-owns-claim
            # stamp, resolved inside the same transaction/RLS context.
            claimant_names = await _load_claimant_name_map(session, gates)
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
            detail=MSG_DB_ERROR_PLEASE_TRY,
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
        gates=[
            _gate_to_response(
                g,
                pipeline_name=pipeline_name,
                label=gate_label_map.get(g.gate_id),
                description=gate_description_map.get(g.gate_id),
                claimed_by_name=claimant_names.get(g.account_id) if g.account_id is not None else None,
                claimed_by_me=g.account_id == principal.account_id,
            )
            for g in gates
        ]
    )


@router.get(
    "/hitl/pending",
)
@handle_db_errors("hitl.list_org_pending_gates")
async def list_org_pending_gates(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("hitl.list"),
) -> PendingGatesResponse:
    """List pending HITL gates across the organisation.

    Gates on terminal runs are excluded (they are data rot, not pending work):
    the manager joins ``runs`` and keeps only undecided gates whose run is in
    ``awaiting_human``, ``claimed``, or ``hitl_parked`` status (FAR-612,
    FAR-604).
    """
    mgr = HITLManager()
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            # include_claimed (FAR-686): claimed-but-undecided gates stay on
            # the review page so the reviewer can still act on them.
            gates = await mgr.list_pending(session, principal.organisation_id, include_claimed=True)

            pipeline_ids = list({g.pipeline_id for g in gates})
            pipeline_map: dict[uuid.UUID, str] = {}
            if pipeline_ids:
                pipeline_rows = await session.execute(
                    select(Pipeline.id, Pipeline.name).where(Pipeline.id.in_(pipeline_ids))
                )
                pipeline_map = {row[0]: row[1] for row in pipeline_rows.all()}

            # FAR-613: resolve each gate's description from its run's snapshot
            # graph via the shared batched resolver (two IN queries over the
            # few pending gates' runs + snapshots — never per-gate walks), so
            # the org review page gets the briefing without an N+1. Context
            # comes from the claim row itself (_gate_to_response).
            description_by_gate = await resolve_gate_descriptions(
                session, gates=gates, org_id=principal.organisation_id
            )
            # FAR-686: also resolve each gate's human label at org level so the
            # shared gate card shows a readable name (frontend falls back to
            # shortId when a label is missing).
            gate_label_map = await _load_gate_label_map(session, gates)
            # FAR-691: batched claimant display names + the caller-owns-claim
            # stamp, resolved inside the same transaction/RLS context.
            claimant_names = await _load_claimant_name_map(session, gates)
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
            detail=MSG_DB_ERROR_PLEASE_TRY,
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
    # expensive. Description IS resolved (FAR-613) and labels (FAR-686) are
    # both resolved in one batched pass each, keyed by (run_id, gate_id)
    # because gate ids are only unique per run — two runs can reuse the same
    # gate id with different labels/descriptions. Frontend falls back to
    # shortId when a label is missing.
    return PendingGatesResponse(
        gates=[
            _gate_to_response(
                g,
                pipeline_name=pipeline_map.get(g.pipeline_id),
                description=description_by_gate.get((g.run_id, g.gate_id)),
                label=gate_label_map.get((g.run_id, g.gate_id)),
                claimed_by_name=claimant_names.get(g.account_id) if g.account_id is not None else None,
                claimed_by_me=g.account_id == principal.account_id,
            )
            for g in gates
        ]
    )


@router.get(
    "/hitl/gates",
)
@handle_db_errors("hitl.list_org_gates")
async def list_org_gates(
    status_filter: GateStatusFilter = Query(default="undecided", alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("hitl.list"),
) -> GateListResponse:
    """Paginated org-wide gate listing including DECIDED gates (FAR-692).

    The review page's status filter was a no-op for approved/rejected because
    ``GET /api/v1/hitl/pending`` only ever returns undecided gates. This
    endpoint lists gates in EVERY state:

    - ``undecided`` (DEFAULT): ``decision IS NULL`` — pending AND claimed.
    - ``pending``: undecided and unclaimed.
    - ``claimed``: undecided and claimed.
    - ``approved`` / ``rejected``: the decided history.
    - ``all``: everything.

    The data-rot fence (FAR-612/FAR-604) applies ONLY to the pending-work
    statuses — ``undecided``/``pending``/``claimed`` join ``runs`` and keep
    only gates whose run is still in ``HITL_ACTIONABLE_RUN_STATUSES``
    (``awaiting_human``/``claimed``/``hitl_parked``), exactly like
    ``HITLManager.list_pending``: an undecided gate on any other run status
    is orphaned data rot (e.g. rows left by the since-fixed auto-approve
    bug), not pending work. History views (``approved``/``rejected``) and
    the ``all`` audit view are deliberately UNFENCED: a decided gate's run
    has legitimately moved past ``awaiting_human``, and the audit view must
    surface data-rot rows.

    ``/api/v1/hitl/pending`` is deliberately UNCHANGED (API stability — other
    consumers depend on its undecided-only shape). The response envelope
    mirrors the repo's standard list convention (items/total/page/page_size,
    as the runs list uses) with the existing ``GateResponse`` items.
    """
    decision_filters = _gate_decision_filters(status_filter)

    # Pending-work fence (FAR-612/FAR-604): the undecided family must match
    # HITLManager.list_pending — joined to runs and restricted to runs still
    # in an actionable status, because an undecided gate on a terminal/
    # complete run is orphaned data rot, not pending work. History views
    # (approved/rejected) and the `all` audit view are deliberately unfenced.
    fenced_to_actionable_runs = status_filter in _PENDING_WORK_GATE_STATUSES
    if fenced_to_actionable_runs:
        decision_filters.append(Run.status.in_(HITL_ACTIONABLE_RUN_STATUSES))

    # page_size clamps at the ceiling (never 422s) — an oversized client hint
    # still gets a usable page, matching the "don't fail the request" intent.
    effective_page_size = min(page_size, _GATE_PAGE_SIZE_MAX)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            total, gates = await _fetch_gate_page(
                session,
                decision_filters=decision_filters,
                fenced_to_actionable_runs=fenced_to_actionable_runs,
                page=page,
                effective_page_size=effective_page_size,
            )
            pipeline_map, description_by_gate, gate_label_map, claimant_names = await _load_gate_page_enrichment(
                session, gates=gates, organisation_id=principal.organisation_id
            )
    except ProgrammingError as exc:
        logger.exception("hitl.list_org_gates")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("hitl.list_org_gates")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hitl.list_org_gates.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return GateListResponse(
        items=[
            _gate_to_response(
                g,
                pipeline_name=pipeline_map.get(g.pipeline_id),
                description=description_by_gate.get((g.run_id, g.gate_id)),
                label=gate_label_map.get((g.run_id, g.gate_id)),
                claimed_by_name=claimant_names.get(g.account_id) if g.account_id is not None else None,
                claimed_by_me=g.account_id == principal.account_id,
            )
            for g in gates
        ],
        total=total,
        page=page,
        page_size=effective_page_size,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gate_decision_filters(status_filter: GateStatusFilter) -> list[Any]:
    """Decision-column WHERE filters for each ``/hitl/gates`` status filter (FAR-692).

    ``undecided`` keeps claimed + unclaimed; ``pending`` narrows to unclaimed;
    ``claimed`` to claimed; ``approved``/``rejected`` surface the decided
    history; ``all`` applies no decision filter at all.
    """
    if status_filter == "all":
        return []
    if status_filter == "undecided":
        return [HitlClaim.decision.is_(None)]
    if status_filter == "pending":
        return [HitlClaim.decision.is_(None), HitlClaim.account_id.is_(None)]
    if status_filter == "claimed":
        return [HitlClaim.decision.is_(None), HitlClaim.account_id.is_not(None)]
    if status_filter == "approved":
        return [HitlClaim.decision == "approved"]
    # "rejected" — Literal narrows everything else away
    return [HitlClaim.decision == "rejected"]


async def _fetch_gate_page(
    session: AsyncSession,
    *,
    decision_filters: list[Any],
    fenced_to_actionable_runs: bool,
    page: int,
    effective_page_size: int,
) -> tuple[int, list[HitlClaim]]:
    """Run the count + page queries for ``/hitl/gates`` in the caller's transaction.

    Count and page derive from the SAME composed filters; the runs join is
    part of both so the count matches the fenced page. The page query is
    skipped entirely when the count is zero.
    """
    count_stmt = select(func.count()).select_from(HitlClaim)
    if fenced_to_actionable_runs:
        count_stmt = count_stmt.join(Run, HitlClaim.run_id == Run.id)
    count_stmt = count_stmt.where(*decision_filters)
    total = (await session.execute(count_stmt)).scalar_one()

    gates: list[HitlClaim] = []
    if total:
        gates_stmt = select(HitlClaim)
        if fenced_to_actionable_runs:
            gates_stmt = gates_stmt.join(Run, HitlClaim.run_id == Run.id)
        gates_stmt = (
            gates_stmt.where(*decision_filters)
            .order_by(
                nullslast(HitlClaim.decision_at.desc()),
                nullslast(HitlClaim.claimed_at.desc()),
                HitlClaim.id.desc(),
            )
            .offset((page - 1) * effective_page_size)
            .limit(effective_page_size)
        )
        gates = list((await session.execute(gates_stmt)).scalars())
    return total, gates


async def _load_gate_page_enrichment(
    session: AsyncSession,
    *,
    gates: list[HitlClaim],
    organisation_id: uuid.UUID,
) -> tuple[
    dict[uuid.UUID, str],
    dict[tuple[uuid.UUID, str], str | None],
    dict[tuple[uuid.UUID, str], str],
    dict[uuid.UUID, str],
]:
    """Batched briefing enrichment for the ``/hitl/gates`` page (FAR-692).

    Decided gates carry the same enrichment as pending ones: pipeline names,
    descriptions (FAR-613), human labels (FAR-686 — resolved for decided
    gates too, keyed by (run_id, gate_id)) and claimant display names +
    the caller-owns-claim stamp (FAR-691), all in batched passes inside the
    caller's transaction/RLS context.
    """
    pipeline_ids = list({g.pipeline_id for g in gates})
    pipeline_map: dict[uuid.UUID, str] = {}
    if pipeline_ids:
        pipeline_rows = await session.execute(select(Pipeline.id, Pipeline.name).where(Pipeline.id.in_(pipeline_ids)))
        pipeline_map = {row[0]: row[1] for row in pipeline_rows.all()}

    description_by_gate = await resolve_gate_descriptions(session, gates=gates, org_id=organisation_id)
    gate_label_map = await _load_gate_label_map(session, gates)
    claimant_names = await _load_claimant_name_map(session, gates)
    return pipeline_map, description_by_gate, gate_label_map, claimant_names


async def _load_gate_label_map(session: AsyncSession, gates: list[HitlClaim]) -> dict[tuple[uuid.UUID, str], str]:
    """Batched ``(run_id, gate_id) -> human label`` resolution for the org endpoint.

    Keyed by ``(run_id, gate_id)`` — gate ids are unique per run only, so
    keying by bare gate_id would collide across runs sharing an id.

    Resolves each pending gate's run -> snapshot -> ``hitl_gate_config.label``
    with two set-based queries (runs, then snapshots). Graceful degradation:
    a missing run/snapshot or a non-dict ``graph_json`` simply leaves that
    gate without a label (frontend falls back to shortId) — one bad snapshot
    never breaks the whole list. All lookups happen inside the caller's
    transaction.
    """
    if not gates:
        return {}

    run_ids = list({g.run_id for g in gates})
    run_rows = (await session.execute(select(Run.id, Run.snapshot_id).where(Run.id.in_(run_ids)))).all()
    run_to_snapshot: dict[uuid.UUID, uuid.UUID] = {row[0]: row[1] for row in run_rows if row[1] is not None}
    if not run_to_snapshot:
        return {}

    snapshot_ids = list(set(run_to_snapshot.values()))
    snap_rows = (
        await session.execute(select(SnapModel.id, SnapModel.graph_json).where(SnapModel.id.in_(snapshot_ids)))
    ).all()
    labels_by_snapshot: dict[uuid.UUID, dict[str, str]] = {}
    for snap_id, graph_json in snap_rows:
        if not isinstance(graph_json, dict):
            continue
        try:
            labels_by_snapshot[snap_id] = _build_gate_label_map(graph_json)
        except Exception:
            # One corrupted snapshot must not break the whole pending list.
            logger.exception("hitl.list_org_pending_gates.label_map_failed")

    gate_label_map: dict[tuple[uuid.UUID, str], str] = {}
    for g in gates:
        snap_id = run_to_snapshot.get(g.run_id)
        if snap_id is None:
            continue
        label = labels_by_snapshot.get(snap_id, {}).get(g.gate_id)
        if label:
            gate_label_map[(g.run_id, g.gate_id)] = label
    return gate_label_map


def _build_gate_label_map(graph_json: dict[str, Any]) -> dict[str, str]:
    """Map gate_id -> human label from snapshot edges carrying hitl_gate_config.

    Gate id format is ``hitl_gate_<source>_<target>`` (see
    ``graph_cache._make_gate_id``). Edges without a ``label`` in their
    ``hitl_gate_config`` are omitted so the frontend falls back to shortId.
    Node-key resolution uses the shared ``edge_source_or_target`` (canonical +
    persisted key styles) — the same helper the FAR-610 gate-config resolver
    uses, so labels and enforcement always agree on the gate id derivation.
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
        source = edge_source_or_target(edge, "source")
        target = edge_source_or_target(edge, "target")
        if source and target:
            gate_label_map[make_gate_id(source, target)] = str(label)
    return gate_label_map


def _gate_description_from_graph(graph_json: dict[str, Any] | None, gate_id: str) -> str | None:
    """The gate's human description from a snapshot graph, or None (FAR-613).

    Shared normalisation lives in ``hitl_gate_config.normalize_gate_description``
    so both pending endpoints and the MCP gate resource render the same muted
    no-description fallback for the same gates.
    """
    if not isinstance(graph_json, dict):
        return None
    config = snapshot_gate_config_map(graph_json).get(gate_id)
    return normalize_gate_description(config)


def _build_gate_description_map(graph_json: dict[str, Any]) -> dict[str, str | None]:
    """Map gate_id -> the gate config's human description (FAR-613).

    Sibling of :func:`_build_gate_label_map` — same snapshot walk (via the
    shared ``snapshot_gate_config_map``, which covers BOTH gate shapes:
    edge-level configs and FAR-402 node-level ``hitl_config``), keyed by the
    same derived gate ids, so labels and descriptions always agree on the
    gate id derivation. Gates whose config carries no usable description map
    to ``None`` (the frontend renders the muted no-description fallback).
    """
    return {
        gate_id: _gate_description_from_graph(graph_json, gate_id) for gate_id in snapshot_gate_config_map(graph_json)
    }


def _gate_to_response(
    g: HitlClaim,
    pipeline_name: str | None = None,
    label: str | None = None,
    description: str | None = None,
    claimed_by_name: str | None = None,
    claimed_by_me: bool = False,
) -> GateResponse:
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
        description=description,
        context=g.context_json if isinstance(g.context_json, dict) else None,
        claimed_by_name=claimed_by_name,
        claimed_by_me=claimed_by_me,
    )


async def _load_claimant_name_map(session: AsyncSession, gates: list[HitlClaim]) -> dict[uuid.UUID, str]:
    """Batched ``account_id -> display name`` resolution for the pending endpoints.

    FAR-691: the review page renders "Claimed by <name>" instead of a raw
    account UUID. Collects the claimant account ids from the pending gates
    and resolves them with ONE select on the accounts table — the same
    batched pattern as :func:`_load_gate_label_map`. The best human-readable
    field wins (``display_name``, falling back to ``email`` when the display
    name is empty); a missing account row simply leaves that gate without a
    name (the frontend falls back to the raw UUID). All lookups happen
    inside the caller's transaction/RLS context.
    """
    account_ids = list({g.account_id for g in gates if g.account_id is not None})
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            select(Account.id, Account.display_name, Account.email).where(Account.id.in_(account_ids))
        )
    ).all()
    return {row[0]: row[1] or row[2] for row in rows}
