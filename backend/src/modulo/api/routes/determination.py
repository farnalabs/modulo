"""Determination API — read-only SDLC assessment and pipeline draft generation."""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import ClassVar, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.connectors.base import ConnectorType
from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event
from modulo.core.connector_hub import ConnectorDecryptError, ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.determination.comparison import (
    ProposalComparison,
    ProposalComparisonRequest,
    compare_proposals,
)
from modulo.determination.draft import generate_draft
from modulo.determination.inference import Finding, build_proposal_contract, infer
from modulo.determination.models import ProposalContract, ReviewState
from modulo.determination.review import (
    ProposalReviewError,
    ProposalReviewRequest,
    ReviewDecision,
    ReviewedProposal,
    review_proposal,
)
from modulo.determination.scanner import ScanSample, run_scan
from modulo.settings import Settings, get_settings

_CODE_DETERMINATION_RUN_DETERMINATION = "determination.run_determination"
_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT = "determination.create_determination_draft"
_CODE_DETERMINATION_REVIEW_PROPOSAL = "determination.review_proposal"

#: Rich audit event; the coarse ``audited()`` event uses ``api_access_post``.
_EVENT_DETERMINATION_PROPOSAL_REVIEWED = "determination.proposal_review_decided"
_RESOURCE_DETERMINATION_PROPOSAL = "determination_proposal"

#: Permission that gates every determination read and review endpoint.
_PERMISSION_DETERMINATION_SCAN = "determination.scan"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/determination", tags=["determination"])

_DETERMINATION_SCOPES = frozenset(
    {
        ConnectorType.GITHUB,
        ConnectorType.GITLAB,
        ConnectorType.JIRA,
    }
)


class SampleResponse(BaseModel):
    connector_id: str
    connector_type: str
    resource: str
    sample_count: int
    error: str | None = None


class FindingResponse(BaseModel):
    category: str
    finding: str
    evidence: str
    confidence: str
    uncertainty: str = ""
    related_connector: str | None = None


class DeterminationResponse(BaseModel):
    samples: list[SampleResponse]
    findings: list[FindingResponse]
    summary: str


class DraftNodeResponse(BaseModel):
    id: str
    node_type: str
    label: str
    connector_type: str | None = None
    required_capabilities: ClassVar[list[str]] = []


class DraftEdgeResponse(BaseModel):
    source: str
    target: str
    edge_type: str = "normal"
    hitl_review: bool = False


class AutomationSuggestion(BaseModel):
    stage: str
    suggestion: str
    connector_type: str | None = None


class DraftResponse(BaseModel):
    nodes: list[DraftNodeResponse]
    edges: list[DraftEdgeResponse]
    findings: list[FindingResponse]
    automation_suggestions: list[AutomationSuggestion]
    summary: str
    proposal: ProposalContract


class ProposalReviewResponse(BaseModel):
    """A recorded review decision; the proposal remains ``not_activated``."""

    proposal: ProposalContract
    decision: ReviewDecision
    review_state: ReviewState
    activation_status: Literal["not_activated"]
    actor_id: uuid.UUID
    actor_username: str
    decided_at: datetime
    reason: str | None = None
    acknowledged_assumption_ids: list[uuid.UUID]
    corrected_derived_element_ids: list[uuid.UUID]
    removed_derived_element_ids: list[uuid.UUID]


def _finding_to_response(f: Finding) -> FindingResponse:
    return FindingResponse(
        category=f.category,
        finding=f.finding,
        evidence=f.evidence,
        confidence=f.confidence,
        uncertainty=f.uncertainty,
        related_connector=str(f.related_connector) if f.related_connector else None,
    )


def _sample_to_response(s: ScanSample) -> SampleResponse:
    return SampleResponse(
        connector_id=str(s.connector_id),
        connector_type=str(s.connector_type),
        resource=s.resource,
        sample_count=s.sample_count,
        error=s.error,
    )


@router.get(
    "",
    responses={
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        502: {"description": "Bad Gateway"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_DETERMINATION_RUN_DETERMINATION)
async def run_determination(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERMISSION_DETERMINATION_SCAN),
    settings: Settings = Depends(get_settings),
) -> DeterminationResponse:
    """Scan all connected tools and produce an SDLC maturity assessment."""
    try:
        try:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                await set_rls_user_context(session, principal.account_id, principal.org_role)
                instances = await list_connector_instances(
                    session, organisation_id=principal.organisation_id, page_size=100
                )

        except ProgrammingError:
            logger.exception("routes.determination")

            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="This feature is not available. Run database migrations to enable it.",
            ) from None

        relevant: list[ConnectorInstance] = [
            ci for ci in instances.items if ci.connector_type_id in {t.value for t in _DETERMINATION_SCOPES}
        ]

        secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
        async with ConnectorHub(secrets_backend=secrets_backend) as hub:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                try:
                    await hub.initialise(relevant)
                except ConnectorDecryptError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Failed to decrypt credentials for connector {exc.connector_id}",
                    ) from exc

            samples = await run_scan(hub)

        findings = infer(samples)

        stage_findings = [f for f in findings if f.category == "overview"]
        summary = stage_findings[0].finding if stage_findings else "No SDLC stages detected"

        return DeterminationResponse(
            samples=[_sample_to_response(s) for s in samples],
            findings=[_finding_to_response(f) for f in findings],
            summary=summary,
        )
    except HTTPException:
        raise
    except IntegrityError:
        logger.exception(_CODE_DETERMINATION_RUN_DETERMINATION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_DETERMINATION_RUN_DETERMINATION)
        raise HTTPException(
            status_code=501, detail="Feature is not available. Run database migrations to enable it."
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "determination.run_determination")
        logger.exception(_CODE_DETERMINATION_RUN_DETERMINATION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable. Please try again later.",
        ) from None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error in run_determination")
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


@router.post(
    "/draft",
    responses={
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        502: {"description": "Bad Gateway"},
        503: {"description": "Service Unavailable"},
    },
    dependencies=[
        Depends(audited("determination_draft_created", "determination_draft", principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors(_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT)
async def create_determination_draft(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERMISSION_DETERMINATION_SCAN),
    settings: Settings = Depends(get_settings),
) -> DraftResponse:
    """Run determination scan and produce an editable pipeline draft.

    The draft includes inferred stages as nodes, transitions as edges,
    automation suggestions, and all supporting evidence.
    No changes are made to any connected system.
    """
    try:
        try:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                await set_rls_user_context(session, principal.account_id, principal.org_role)
                instances = await list_connector_instances(
                    session, organisation_id=principal.organisation_id, page_size=100
                )

        except ProgrammingError:
            logger.exception("routes.determination")

            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="This feature is not available. Run database migrations to enable it.",
            ) from None

        relevant: list[ConnectorInstance] = [
            ci for ci in instances.items if ci.connector_type_id in {t.value for t in _DETERMINATION_SCOPES}
        ]

        secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
        async with ConnectorHub(secrets_backend=secrets_backend) as hub:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                try:
                    await hub.initialise(relevant)
                except ConnectorDecryptError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Failed to decrypt credentials for connector {exc.connector_id}",
                    ) from exc

            samples = await run_scan(hub)

        findings = infer(samples)
        draft = generate_draft(samples, findings)
        proposal = build_proposal_contract(samples, findings)

        stage_findings = [f for f in findings if f.category == "overview"]
        summary = stage_findings[0].finding if stage_findings else "No SDLC stages detected"

        return DraftResponse(
            nodes=[
                DraftNodeResponse(
                    id=n.id,
                    node_type=n.node_type,
                    label=n.label,
                    connector_type=n.connector_type,
                    required_capabilities=n.required_capabilities,
                )
                for n in draft.nodes
            ],
            edges=[
                DraftEdgeResponse(
                    source=e.source,
                    target=e.target,
                    edge_type=e.edge_type,
                    hitl_review=e.hitl_review,
                )
                for e in draft.edges
            ],
            findings=[_finding_to_response(f) for f in draft.findings],
            automation_suggestions=[
                AutomationSuggestion(
                    stage=s["stage"],
                    suggestion=s["suggestion"],
                    connector_type=s.get("connector_type"),
                )
                for s in draft.automation_suggestions
            ],
            summary=summary,
            proposal=proposal,
        )
    except HTTPException:
        raise
    except IntegrityError:
        logger.exception(_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT)
        raise HTTPException(
            status_code=501, detail="Feature is not available. Run database migrations to enable it."
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "determination.create_determination_draft")
        logger.exception(_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable. Please try again later.",
        ) from None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error in create_determination_draft")
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


@router.post(
    "/compare",
    dependencies=[
        Depends(audited("api_access_post", _RESOURCE_DETERMINATION_PROPOSAL, principal_dep=get_current_tenant_user))
    ],
)
async def compare_determination_proposals(
    comparison_request: ProposalComparisonRequest,
    request: Request,
    _principal: TenantPrincipal = require_permission(_PERMISSION_DETERMINATION_SCAN),
) -> ProposalComparison:
    """Compare a base proposal with a target proposal.

    Each side carries its proposal contract plus the draft nodes and edges it
    was generated with. The comparison is pure and deterministic
    (``modulo.determination.comparison``): it reports added, removed and
    changed nodes and edges with stable ids, names every changed property with
    its base and target values, carries each side's provenance onto its items,
    and reports only the approval/HITL and automation categories the draft
    graph actually encodes.

    Read-only: the handler takes no database session, persists the comparison
    nowhere, activates nothing and writes to no connected tool. Both returned
    side summaries carry ``activation_status`` ``not_activated``. Because the
    request cannot change state it marks itself a no-op write, so ``audited()``
    does not add a success event for a read-only comparison; a failed attempt
    is still recorded.
    """
    comparison = compare_proposals(comparison_request.base, comparison_request.target)
    request.state.audit_no_change = True
    return comparison


@router.post(
    "/review",
    responses={
        409: {"description": "Conflict"},
        422: {"description": "Review decision rejected"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
    dependencies=[
        Depends(audited("api_access_post", _RESOURCE_DETERMINATION_PROPOSAL, principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors(_CODE_DETERMINATION_REVIEW_PROPOSAL)
async def review_determination_proposal(
    review_request: ProposalReviewRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERMISSION_DETERMINATION_SCAN),
) -> ProposalReviewResponse:
    """Record a reviewer decision on a determination proposal.

    Accepts the proposal contract (validated on the way in), a decision
    (``approve`` / ``reject`` / ``needs_review``), optional corrections to
    derived elements, and optional acknowledgements of unresolved assumptions
    by id. A reject requires a reason; an approve is refused while the proposal
    carries low or unknown confidence assumptions the caller has not
    acknowledged.

    Reviewing a proposal never activates it: no pipeline is created, no trigger
    is enabled and nothing is written to a connected tool - the returned and
    recorded ``activation_status`` stays ``not_activated``. The decision, its
    reason, the acknowledged assumption ids and the corrected
    derived-element ids are recorded as one audit event.
    """
    try:
        reviewed = review_proposal(review_request)
    except ProposalReviewError as exc:
        detail = str(exc)
        if exc.blocking_assumption_ids:
            ids = ", ".join(str(assumption_id) for assumption_id in exc.blocking_assumption_ids)
            detail = f"{detail}: {ids}"
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from None

    await _record_review_decision(session, principal, reviewed)

    return ProposalReviewResponse(
        proposal=reviewed.proposal,
        decision=reviewed.decision,
        review_state=reviewed.review_state,
        activation_status=reviewed.activation_status,
        actor_id=principal.account_id,
        actor_username=principal.username,
        decided_at=reviewed.decided_at,
        reason=reviewed.reason,
        acknowledged_assumption_ids=reviewed.acknowledged_assumption_ids,
        corrected_derived_element_ids=reviewed.corrected_derived_element_ids,
        removed_derived_element_ids=reviewed.removed_derived_element_ids,
    )


async def _record_review_decision(
    session: AsyncSession,
    principal: TenantPrincipal,
    reviewed: ReviewedProposal,
) -> None:
    """Append the review decision to the audit chain.

    The audit chain is the record of the decision (there is no review table),
    so this append is deliberately NOT failure-isolated: if the decision cannot
    be recorded the request fails rather than reporting a decision that was
    never written.
    """
    payload: dict[str, object] = {
        "proposal_id": str(reviewed.proposal.proposal_id),
        "attempt_id": str(reviewed.proposal.attempt.id),
        "contract_version": reviewed.proposal.contract_version,
        "decision": reviewed.decision,
        "review_state": reviewed.review_state,
        "activation_status": reviewed.activation_status,
        "reason": reviewed.reason,
        "acknowledged_assumption_ids": [str(i) for i in reviewed.acknowledged_assumption_ids],
        "corrected_derived_element_ids": [str(i) for i in reviewed.corrected_derived_element_ids],
        "removed_derived_element_ids": [str(i) for i in reviewed.removed_derived_element_ids],
        "decided_at": reviewed.decided_at.isoformat(),
        "actor": principal.username,
        "summary": (
            f"Determination proposal {str(reviewed.proposal.proposal_id)[:8]} "
            f"{reviewed.review_state} by {principal.username}"
        ),
    }
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        await append_audit_event(
            session,
            org_id=principal.organisation_id,
            event_type=_EVENT_DETERMINATION_PROPOSAL_REVIEWED,
            actor_user_id=principal.account_id,
            resource_type=_RESOURCE_DETERMINATION_PROPOSAL,
            resource_id=reviewed.proposal.proposal_id,
            payload_json=payload,
        )
