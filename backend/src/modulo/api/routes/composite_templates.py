"""CompositeTemplate CRUD REST API."""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.composite_engine.expander import (
    _PARAM_PLACEHOLDER_RE,
    _PROMPT_FIELDS,
)
from modulo.db.crud.composite_template import (
    create_composite_template,
    get_composite_template,
    list_composite_templates,
    restore_composite_template,
    soft_delete_composite_template,
    update_composite_template,
)
from modulo.db.rls import set_rls_org

_MSG_DATABASE_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable."
_MSG_COMPOSITE_TEMPLATE_NOT_FOUND = "Composite template not found"
_PERM_PIPELINE_UPDATE = "pipeline.update"


logger = logging.getLogger(__name__)

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
    parameter_schema_id: uuid.UUID | None = None
    version: str = "1.0.0"


class CompositeTemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    sub_pipeline_graph_json: dict[str, Any] | None = None
    parameter_ports_json: list[ParameterPort] | None = None
    input_schema_id: uuid.UUID | None = None
    output_schema_id: uuid.UUID | None = None
    parameter_schema_id: uuid.UUID | None = None
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
    parameter_schema_id: uuid.UUID | None
    version: str
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class CompositeTemplateListResponse(BaseModel):
    items: list[CompositeTemplateResponse]
    total: int
    page: int
    page_size: int


@router.get("")
@handle_db_errors("composite_templates.list_composite_templates_endpoint")
async def list_composite_templates_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> CompositeTemplateListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_composite_templates(
                session,
                org_id=principal.organisation_id,
                page=page,
                page_size=page_size,
            )
    except ProgrammingError:
        logger.exception("composite_templates.list_composite_templates_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.list_composite_templates_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in list_composite_templates_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return CompositeTemplateListResponse(
        items=[CompositeTemplateResponse.model_validate(t) for t in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("composite_templates.create_composite_template_endpoint")
async def create_composite_template_endpoint(
    req: CompositeTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("pipeline.create"),
) -> CompositeTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await create_composite_template(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                name=req.name,
                description=req.description,
                sub_pipeline_graph_json=req.sub_pipeline_graph_json,
                parameter_ports_json=[p.model_dump() for p in req.parameter_ports_json],
                input_schema_id=req.input_schema_id,
                output_schema_id=req.output_schema_id,
                parameter_schema_id=req.parameter_schema_id,
                version=req.version,
            )
        return CompositeTemplateResponse.model_validate(template)
    except ProgrammingError:
        logger.exception("composite_templates.create_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.create_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in create_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.get("/{template_id}")
@handle_db_errors("composite_templates.get_composite_template_endpoint")
async def get_composite_template_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> CompositeTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await get_composite_template(session, template_id)
    except ProgrammingError:
        logger.exception("composite_templates.get_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.get_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in get_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
    return CompositeTemplateResponse.model_validate(template)


@router.patch("/{template_id}")
@handle_db_errors("composite_templates.update_composite_template_endpoint")
async def update_composite_template_endpoint(
    template_id: uuid.UUID,
    req: CompositeTemplateUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_PIPELINE_UPDATE),
) -> CompositeTemplateResponse:
    updates: dict[str, Any] = {}
    for k, v in req.model_dump(exclude_unset=True).items():
        if k == "parameter_ports_json" and v is not None:
            updates[k] = [p.model_dump() if isinstance(p, BaseModel) else p for p in v]
        else:
            updates[k] = v
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await update_composite_template(session, template_id, updates)
    except ProgrammingError:
        logger.exception("composite_templates.update_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.update_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in update_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
    return CompositeTemplateResponse.model_validate(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("composite_templates.delete_composite_template_endpoint")
async def delete_composite_template_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("pipeline.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await soft_delete_composite_template(session, template_id)
    except ProgrammingError:
        logger.exception("composite_templates.delete_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.delete_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in delete_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)


@router.post("/{template_id}/restore")
@handle_db_errors("composite_templates.restore_composite_template_endpoint")
async def restore_composite_template_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("pipeline.create"),
) -> CompositeTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await restore_composite_template(session, template_id)
    except ProgrammingError:
        logger.exception("composite_templates.restore_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.restore_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in restore_composite_template_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
    return CompositeTemplateResponse.model_validate(template)


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


@router.get("/{template_id}/editor")
@handle_db_errors("composite_templates.get_composite_editor_endpoint")
async def get_composite_editor_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> EditorGraphResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await get_composite_template(session, template_id)
    except ProgrammingError:
        logger.exception("composite_templates.get_composite_editor_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in get_composite_editor_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
    graph = template.sub_pipeline_graph_json
    return EditorGraphResponse(
        nodes=graph.get("nodes", []),
        edges=graph.get("edges", []),
    )


@router.put("/{template_id}/editor")
@handle_db_errors("composite_templates.save_composite_editor_endpoint")
async def save_composite_editor_endpoint(
    template_id: uuid.UUID,
    req: EditorGraphUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_PIPELINE_UPDATE),
) -> EditorGraphResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await get_composite_template(session, template_id)
            if template is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
            graph = dict(template.sub_pipeline_graph_json) if template.sub_pipeline_graph_json else {}
            graph["nodes"] = req.nodes
            graph["edges"] = req.edges
            template = await update_composite_template(
                session,
                template_id,
                {
                    "sub_pipeline_graph_json": graph,
                },
            )
    except ProgrammingError:
        logger.exception("composite_templates.save_composite_editor_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.save_composite_editor_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in save_composite_editor_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
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
    nodes: list[dict[str, Any]] = Field(default_factory=list)


class DetectParamsResponse(BaseModel):
    ports: list[ParameterPort] = Field(default_factory=list)


# Placeholder format and prompt-bearing fields are shared with the runtime
# expander (modulo.core.composite_engine.expander) so detection and expansion
# stay consistent.


def _detect_parameter_ports(nodes: list[dict[str, Any]]) -> list[ParameterPort]:
    """Scan *nodes* for ``{{parameter.<name>}}`` placeholders.

    Each unique placeholder yields one ``ParameterPort`` whose
    ``target_injection.node_id`` points at the first node that referenced it.
    """
    ports: list[ParameterPort] = []
    seen: set[str] = set()
    for node in nodes:
        raw_id = node.get("id")
        node_id = str(raw_id) if raw_id is not None else ""
        for field in _PROMPT_FIELDS:
            text = node.get(field)
            if not isinstance(text, str):
                continue
            for name in _PARAM_PLACEHOLDER_RE.findall(text):
                if name in seen:
                    continue
                seen.add(name)
                ports.append(
                    ParameterPort(
                        id=str(uuid.uuid4()),
                        name=name,
                        label=name.replace("_", " ").title(),
                        description=None,
                        type="string",
                        required=False,
                        default_value=None,
                        multiline=False,
                        options=None,
                        target_injection=TargetInjection(
                            mode="prompt_replace",
                            node_id=node_id,
                            injection_point="prompt_template",
                        ),
                    )
                )
    return ports


@router.post("/detect-params")
@handle_db_errors("composite_templates.detect_params_endpoint")
async def detect_params_endpoint(
    req: DetectParamsRequest,
    _principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> DetectParamsResponse:
    """Scan sub-pipeline node prompts for ``{{parameter.*}}`` placeholders.

    Best-effort detection: every unique ``{{parameter.<name>}}`` placeholder
    found on the supplied node definitions (``prompt``, ``prompt_template``,
    ``agent_prompt`` fields) yields a ``ParameterPort``. Nodes without matches
    produce no ports, and the frontend merges new ports with existing ones.
    """
    try:
        return DetectParamsResponse(ports=_detect_parameter_ports(req.nodes))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in detect_params_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


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


@router.post("/{template_id}/publish")
@handle_db_errors("composite_templates.publish_composite_endpoint")
async def publish_composite_endpoint(
    template_id: uuid.UUID,
    req: PublishRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_PIPELINE_UPDATE),
) -> PublishResponse:
    version = req.version or "1.0.0"
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            template = await update_composite_template(session, template_id, {"version": version})
    except ProgrammingError:
        logger.exception("composite_templates.publish_composite_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("composite_templates.publish_composite_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in publish_composite_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_COMPOSITE_TEMPLATE_NOT_FOUND)
    return PublishResponse(id=template.id, version=template.version, published=True)
