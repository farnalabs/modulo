"""Determination API — read-only SDLC assessment and pipeline draft generation."""

import asyncio
import logging
from typing import ClassVar

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub import ConnectorDecryptError, ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.determination.draft import generate_draft
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample, run_scan
from modulo.settings import Settings, get_settings

_CODE_DETERMINATION_RUN_DETERMINATION = "determination.run_determination"
_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT = "determination.create_determination_draft"


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
    hitl_gate: bool = False


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
    principal: TenantPrincipal = require_permission("determination.scan"),
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

        async with ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=settings.fernet_key)) as hub:
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
    except SQLAlchemyError:
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
)
@handle_db_errors(_CODE_DETERMINATION_CREATE_DETERMINATION_DRAFT)
async def create_determination_draft(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("determination.scan"),
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

        async with ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=settings.fernet_key)) as hub:
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
                    hitl_gate=e.hitl_gate,
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
    except SQLAlchemyError:
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
