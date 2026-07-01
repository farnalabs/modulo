"""CompositeTemplate CRUD REST API."""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.composite_template import (
    create_composite_template,
    delete_composite_template,
    get_composite_template,
    list_composite_templates,
    update_composite_template,
)
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/composite-templates", tags=["composite-templates"])


class SelectOption(BaseModel):
    label: str
    value: str


class TargetInjection(BaseModel):
    mode: str = "prompt_replace"
    node_id: str
    injection_point: str = "prompt_template"


class ParameterPort(BaseModel):
    id: str
    name: str
    label: str
    description: str | None = None
    type: Literal["string", "number", "boolean", "select", "model_backend_ref", "schema_ref"]
    required: bool = False
    default_value: Any = None
    multiline: bool = False
    options: list[SelectOption] | None = None
    target_injection: TargetInjection


class CompositeTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    sub_pipeline_graph_json: dict[str, Any]
    parameter_ports_json: list[ParameterPort] = Field(default_factory=list)
    input_schema_id: uuid.UUID | None = None
    output_schema_id: uuid.UUID | None = None
    version: str = "1.0.0"


class CompositeTemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    sub_pipeline_graph_json: dict[str, Any] | None = None
    parameter_ports_json: list[ParameterPort] | None = None
    input_schema_id: uuid.UUID | None = None
    output_schema_id: uuid.UUID | None = None
    version: str | None = None


class CompositeTemplateResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    sub_pipeline_graph_json: dict[str, Any]
    parameter_ports_json: list[dict[str, Any]]
    input_schema_id: uuid.UUID | None
    output_schema_id: uuid.UUID | None
    version: str
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompositeTemplateListResponse(BaseModel):
    items: list[CompositeTemplateResponse]
    total: int
    page: int
    page_size: int


@router.get("", response_model=CompositeTemplateListResponse)
async def list_composite_templates_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> CompositeTemplateListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_composite_templates(
                session, org_id=principal.organisation_id, page=page, page_size=page_size,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return CompositeTemplateListResponse(
        items=[CompositeTemplateResponse.model_validate(t) for t in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=CompositeTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_composite_template_endpoint(
    body: CompositeTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> CompositeTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await create_composite_template(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                name=body.name,
                description=body.description,
                sub_pipeline_graph_json=body.sub_pipeline_graph_json,
                parameter_ports_json=[p.model_dump() for p in body.parameter_ports_json],
                input_schema_id=body.input_schema_id,
                output_schema_id=body.output_schema_id,
                version=body.version,
            )
        return CompositeTemplateResponse.model_validate(template)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.get("/{template_id}", response_model=CompositeTemplateResponse)
async def get_composite_template_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> CompositeTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await get_composite_template(session, template_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")
    return CompositeTemplateResponse.model_validate(template)


@router.patch("/{template_id}", response_model=CompositeTemplateResponse)
async def update_composite_template_endpoint(
    template_id: uuid.UUID,
    body: CompositeTemplateUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> CompositeTemplateResponse:
    updates: dict[str, Any] = {}
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "parameter_ports_json" and v is not None:
            updates[k] = [p.model_dump() if isinstance(p, BaseModel) else p for p in v]
        else:
            updates[k] = v
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await update_composite_template(session, template_id, updates)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")
    return CompositeTemplateResponse.model_validate(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_composite_template_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await delete_composite_template(session, template_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")


# ---------------------------------------------------------------------------
# Editor: open composite sub-pipeline graph for editing
# ---------------------------------------------------------------------------


class EditorGraphResponse(BaseModel):
    """Mirrors the pipeline graph shape but within a composite scope."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class EditorGraphUpdate(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/{template_id}/editor", response_model=EditorGraphResponse)
async def get_composite_editor_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> EditorGraphResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await get_composite_template(session, template_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")
    graph = template.sub_pipeline_graph_json
    return EditorGraphResponse(
        nodes=graph.get("nodes", []),
        edges=graph.get("edges", []),
    )


@router.put("/{template_id}/editor", response_model=EditorGraphResponse)
async def save_composite_editor_endpoint(
    template_id: uuid.UUID,
    body: EditorGraphUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> EditorGraphResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await get_composite_template(session, template_id)
            if template is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")
            graph = dict(template.sub_pipeline_graph_json) if template.sub_pipeline_graph_json else {}
            graph["nodes"] = body.nodes
            graph["edges"] = body.edges
            template = await update_composite_template(session, template_id, {
                "sub_pipeline_graph_json": graph,
            })
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")
    return EditorGraphResponse(
        nodes=template.sub_pipeline_graph_json.get("nodes", []),
        edges=template.sub_pipeline_graph_json.get("edges", []),
    )


# ---------------------------------------------------------------------------
# Publish: mark composite as published with a version
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Detect: scan sub-pipeline prompts for {{parameter.*}} placeholders
# ---------------------------------------------------------------------------


class DetectParamsRequest(BaseModel):
    node_ids: list[str] = Field(default_factory=list)


class DetectParamsResponse(BaseModel):
    ports: list[ParameterPort] = Field(default_factory=list)


@router.post("/detect-params", response_model=DetectParamsResponse)
async def detect_params_endpoint(
    body: DetectParamsRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> DetectParamsResponse:
    """Scan sub-pipeline agent prompts for ``{{parameter.*}}`` placeholders.

    TODO: Implement actual prompt scanning. Currently returns an empty list.
    The frontend handles empty results gracefully via its best-effort contract.
    """
    return DetectParamsResponse(ports=[])


class PublishRequest(BaseModel):
    version: str | None = Field(
        default=None,
        min_length=1,
        pattern=r"^\d+\.\d+\.\d+$",
        description="Override version string, defaults to '1.0.0'",
    )


class PublishResponse(BaseModel):
    id: uuid.UUID
    version: str
    published: bool


@router.post("/{template_id}/publish", response_model=PublishResponse)
async def publish_composite_endpoint(
    template_id: uuid.UUID,
    body: PublishRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PublishResponse:
    version = body.version or "1.0.0"
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await update_composite_template(session, template_id, {"version": version})
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite template not found")
    return PublishResponse(id=template.id, version=template.version, published=True)
