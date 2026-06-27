"""Determination API — read-only SDLC assessment and pipeline draft generation."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub import ConnectorDecryptError, ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.rls import set_rls_org
from modulo.determination.draft import generate_draft
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample, run_scan
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/determination", tags=["determination"])

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_DETERMINATION_SCOPES = frozenset(
    {
        ConnectorType.GITHUB,
        ConnectorType.GITLAB,
        ConnectorType.JIRA,
        ConnectorType.LINEAR,
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
    required_capabilities: list[str] = []


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


async def _load_and_scan(settings: Settings) -> tuple[list[ScanSample], list[Finding]]:
    """Load relevant connector instances and run the scan + inference."""
    from modulo.api.dependencies import get_db_session as _get_session

    async for session in _get_session():
        async with session.begin():
            await set_rls_org(session, _PLACEHOLDER_ORG_ID)
            instances = await list_connector_instances(session, page_size=100)

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
    return samples, findings


@router.get("", response_model=DeterminationResponse)
async def run_determination(
    session: AsyncSession = Depends(get_db_session),
    _: str = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> DeterminationResponse:
    """Scan all connected tools and produce an SDLC maturity assessment."""
    async with session.begin():
        await set_rls_org(session, _PLACEHOLDER_ORG_ID)
        instances = await list_connector_instances(session, page_size=100)

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


@router.post("/draft", response_model=DraftResponse)
async def create_determination_draft(
    session: AsyncSession = Depends(get_db_session),
    _: str = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> DraftResponse:
    """Run determination scan and produce an editable pipeline draft.

    The draft includes inferred stages as nodes, transitions as edges,
    automation suggestions, and all supporting evidence.
    No changes are made to any connected system.
    """
    async with session.begin():
        await set_rls_org(session, _PLACEHOLDER_ORG_ID)
        instances = await list_connector_instances(session, page_size=100)

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
