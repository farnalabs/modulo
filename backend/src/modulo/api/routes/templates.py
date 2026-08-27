"""Pipeline template REST API — browse templates and instantiate pipelines."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.pipeline import create_pipeline
from modulo.db.crud.template import (
    _agent_count_from_content,
    _preview_data_from_content,
    get_template,
    list_templates,
)
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_TEMPLATES_LIST_TEMPLATES_ENDPOINT = "templates.list_templates_endpoint"
_CODE_TEMPLATES_CREATE_PIPELINE_TEMPLATE = "templates.create_pipeline_from_template_endpoint"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["templates"])


class TemplateResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: str | None
    tags: list[str]
    agent_count: int
    preview_data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_primitive(cls, prim: LibraryPrimitive) -> "TemplateResponse":
        content = prim.content_json or {}
        return cls(
            id=prim.id,
            name=prim.name,
            description=prim.description,
            category=prim.category or content.get("category"),
            tags=prim.tags or [],
            agent_count=_agent_count_from_content(content),
            preview_data=_preview_data_from_content(content),
            created_at=prim.created_at,
            updated_at=prim.updated_at,
        )


class TemplateListResponse(BaseModel):
    items: list[TemplateResponse]
    total: int
    page: int
    page_size: int


class FromTemplateResponse(BaseModel):
    pipeline_id: uuid.UUID
    pipeline_name: str
    agent_count: int
    edge_count: int


@router.get(
    "/templates",
    responses={
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
    },
)
@handle_db_errors(_CODE_TEMPLATES_LIST_TEMPLATES_ENDPOINT)
async def list_templates_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: str | None = None,
    search: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> TemplateListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_templates(
                session,
                page=page,
                page_size=page_size,
                category=category,
                search=search,
            )
        return TemplateListResponse(
            items=[TemplateResponse.from_primitive(p) for p in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
        )
    except HTTPException:
        raise
    except IntegrityError as exc:
        logger.exception(_CODE_TEMPLATES_LIST_TEMPLATES_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_TEMPLATES_LIST_TEMPLATES_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from exc
    except Exception as e:
        logger.exception(_CODE_TEMPLATES_LIST_TEMPLATES_ENDPOINT)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from e


@router.post(
    "/pipelines/from-template/{template_id}",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors(_CODE_TEMPLATES_CREATE_PIPELINE_TEMPLATE)
async def create_pipeline_from_template_endpoint(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> FromTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            template = await get_template(session, template_id)
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Template not found",
            )

        content = template.content_json or {}
        agent_configs: list[dict[str, Any]] = content.get("agents", [])
        graph_nodes: list[dict[str, Any]] = content.get("graph_nodes", [])
        edges: list[dict[str, Any]] = content.get("edges", [])

        agent_ids: dict[int, uuid.UUID] = {}
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            for idx, _agent_cfg in enumerate(agent_configs):
                agent_id = uuid.uuid4()
                agent_ids[idx] = agent_id

            pipeline = await create_pipeline(
                session,
                org_id=principal.organisation_id,
                name=f"{template.name} (from template)",
                account_id=principal.account_id,
                description=template.description or f"Created from template: {template.name}",
            )

            resolved_nodes: list[dict[str, Any]] = []
            for node in graph_nodes:
                agent_idx = node.get("agent_index", -1)
                resolved_id_str = str(uuid.uuid4())

                if agent_idx >= 0 and agent_idx in agent_ids:
                    resolved_nodes.append(
                        {
                            "id": resolved_id_str,
                            "node_type": node.get("node_type", "agent"),
                            "agent_id": str(agent_ids[agent_idx]),
                            "label": node.get("label", agent_configs[agent_idx].get("name", "")),
                            "position": node.get("position", {"x": 100, "y": 100}),
                        }
                    )
                else:
                    resolved_nodes.append(
                        {
                            "id": resolved_id_str,
                            "node_type": node.get("node_type", "manual"),
                            "label": node.get("label", "Manual Step"),
                            "position": node.get("position", {"x": 100, "y": 100}),
                        }
                    )

            pipeline.graph_nodes_json = resolved_nodes

            from modulo.db.models.pipeline_edge import PipelineEdge

            persisted_edges: list[PipelineEdge] = []

            source_map = {n.get("id", str(i)): resolved_nodes[i]["id"] for i, n in enumerate(graph_nodes)}

            for edge in edges:
                source_id = source_map.get(edge.get("source_node_id", ""))
                target_id = source_map.get(edge.get("target_node_id", ""))
                if source_id is None or target_id is None:
                    continue
                pe = PipelineEdge(
                    id=uuid.uuid4(),
                    organisation_id=principal.organisation_id,
                    pipeline_id=pipeline.id,
                    source_node_id=uuid.UUID(source_id),
                    target_node_id=uuid.UUID(target_id),
                    edge_type=edge.get("edge_type", "normal"),
                    condition_expression=edge.get("condition_expression"),
                    hitl_gate_config=edge.get("hitl_gate_config"),
                    source_port=edge.get("source_port", "out"),
                    target_port=edge.get("target_port", "in"),
                )
                session.add(pe)
                persisted_edges.append(pe)

            await session.flush()

        return FromTemplateResponse(
            pipeline_id=pipeline.id,
            pipeline_name=pipeline.name,
            agent_count=len(agent_configs),
            edge_count=len(persisted_edges),
        )
    except HTTPException:
        raise
    except IntegrityError as exc:
        logger.exception(_CODE_TEMPLATES_CREATE_PIPELINE_TEMPLATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from exc
    except ProgrammingError as exc:
        logger.exception(_CODE_TEMPLATES_CREATE_PIPELINE_TEMPLATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from exc
    except Exception as e:
        logger.exception(_CODE_TEMPLATES_CREATE_PIPELINE_TEMPLATE)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from e
