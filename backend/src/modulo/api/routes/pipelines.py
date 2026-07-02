"""Pipeline CRUD REST API.

Alpha: Graph replacement uses row-level locking (SELECT ... FOR UPDATE) in
replace_pipeline_graph. No advisory lock is deployed; the row lock on the
pipeline row serialises concurrent graph writes within a serialisable transaction.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.graph_validator import GraphValidator
from modulo.core.reports.quality_report import (
    deliver_quality_report,
    generate_quality_report,
)
from modulo.core.run_context.autonomy import (
    autonomy_change_payload,
)
from modulo.db.crud.composite_template import create_composite_template
from modulo.db.crud.pipeline import (
    check_pipeline_name_available,
    clone_pipeline,
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    get_pipeline_graph,
    list_pipelines,
    replace_pipeline_graph,
    update_pipeline,
)
from modulo.db.crud.pipeline_snapshot_versioning import (
    delete_snapshot,
    diff_snapshots,
    get_snapshot_detail,
    list_snapshots,
    rollback_to_snapshot,
    tag_snapshot,
)
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.schema import Schema
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pipelines", tags=["pipelines"])

# DoS guard: reject graphs larger than these limits before any DB work.
_MAX_GRAPH_NODES = 500
_MAX_GRAPH_EDGES = 1000


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    visibility: str = Field(default="org", pattern=r"^(org|team)$")
    max_concurrent_runs: int = 5
    lock_wait_timeout_seconds: int = 300
    node_timeout_seconds: int = 300
    run_context_defaults: dict[str, Any] = Field(default_factory=dict)
    default_autonomy_level: str = "manual_approval"


class PipelineUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    visibility: str | None = Field(None, pattern=r"^(org|team)$")
    max_concurrent_runs: int | None = None
    lock_wait_timeout_seconds: int | None = None
    node_timeout_seconds: int | None = None
    run_context_defaults: dict[str, Any] | None = None
    default_autonomy_level: str | None = None


class PipelineResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    visibility: str
    max_concurrent_runs: int
    lock_wait_timeout_seconds: int
    node_timeout_seconds: int
    run_context_defaults: dict[str, Any]
    default_autonomy_level: str | None = None
    snapshot_count: int = 0
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class PipelineListResponse(BaseModel):
    items: list[PipelineResponse]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None
    has_more: bool = False


class GraphPosition(BaseModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class ConnectorBinding(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    instance_id: uuid.UUID


class PipelineGraphNode(BaseModel):
    id: uuid.UUID
    node_type: Literal["agent", "manual", "composite"] = "agent"
    agent_id: uuid.UUID | None = None
    position: GraphPosition
    connector_binding: ConnectorBinding | None = None
    output_schema_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=255)
    role: str | None = None
    autonomy_recommendation: str | None = None
    composite_ref: uuid.UUID | None = None
    composite_parameter_values: dict[str, Any] | None = None
    composite_input_mapping: dict | None = None
    composite_output_mapping: dict | None = None

    @model_validator(mode="after")
    def validate_node_type(self) -> "PipelineGraphNode":
        if self.node_type == "manual":
            if self.agent_id is not None:
                raise ValueError("Manual nodes cannot reference an agent")
            if self.connector_binding is not None:
                raise ValueError("Manual nodes cannot have connector bindings")
            if self.output_schema_id is None:
                raise ValueError("Manual nodes require an output schema")
            if self.label is None:
                raise ValueError("Manual nodes require a label")
        elif self.node_type == "composite":
            if self.composite_ref is None:
                raise ValueError("Composite nodes require a composite_ref")
            if self.agent_id is not None:
                raise ValueError("Composite nodes cannot reference an agent")
            if self.connector_binding is not None:
                raise ValueError("Composite nodes cannot have connector bindings")
        elif self.agent_id is None:
            raise ValueError("Agent nodes require an agent")
        return self


class EvalCondition(BaseModel):
    eval_name: str = Field(
        min_length=1, max_length=255,
        description="Name of the eval definition to reference.",
    )
    threshold: float = Field(
        ge=0.0, le=1.0,
        description="Score threshold for the condition.",
    )
    operator: str = Field(
        pattern="^(lt|gt|lte|gte|eq|neq)$",
        description="Comparison operator: lt (score < threshold), gt (score > threshold), "
        "lte (score <= threshold), gte (score >= threshold), eq (score == threshold), "
        "neq (score != threshold).",
    )


class HitlGateConfig(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(max_length=2000)
    reject_target: uuid.UUID | None = None
    claim_expiry_minutes: int = Field(gt=0, le=1440)
    human_only: bool
    required_team_id: uuid.UUID | None = None
    condition: str | None = Field(
        default=None,
        max_length=500,
        description="JMESPath expression evaluated against the upstream node output. "
        "If it returns true, gate activates. If false/empty/null, gate is skipped.",
    )
    eval_condition: EvalCondition | None = Field(
        default=None,
        description="Eval-reference condition: references an eval definition by name "
        "with threshold and operator. Evaluated after eval-before-interrupt runs. "
        "If the condition evaluates to true (e.g., score < threshold with operator lt), "
        "the gate fires. If false, execution continues without interrupting.",
    )


class PipelineGraphEdge(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str = Field(pattern="^(normal|reject|conditional)$")
    hitl_gate_config: HitlGateConfig | None = None
    condition_expression: str | None = Field(
        default=None,
        max_length=500,
        description="JMESPath expression for conditional edge routing. "
        "Evaluated against pipeline state; if truthy, routes to target.",
    )

    model_config = {"from_attributes": True}


class GraphValidationIssue(BaseModel):
    severity: str
    code: str
    message: str
    node_id: str | None = None


class PipelineGraphUpdate(BaseModel):
    nodes: list[PipelineGraphNode]
    edges: list[PipelineGraphEdge]

    @model_validator(mode="after")
    def reject_database_conflicts(self) -> "PipelineGraphUpdate":
        if len(self.nodes) > _MAX_GRAPH_NODES:
            raise ValueError(f"Graph exceeds maximum of {_MAX_GRAPH_NODES} nodes")
        if len(self.edges) > _MAX_GRAPH_EDGES:
            raise ValueError(f"Graph exceeds maximum of {_MAX_GRAPH_EDGES} edges")
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Graph node IDs must be unique")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("Graph edge IDs must be unique")
        paths = [(edge.source_node_id, edge.target_node_id, edge.edge_type) for edge in self.edges]
        if len(paths) != len(set(paths)):
            raise ValueError("Graph edge paths must be unique")
        return self


class PipelineGraphResponse(PipelineGraphUpdate):
    validation_issues: list[GraphValidationIssue] = Field(default_factory=list)


def _graph_response(
    nodes: list[dict[str, Any]],
    edges: list[Any],
    *,
    validation_issues: list[GraphValidationIssue] | None = None,
) -> PipelineGraphResponse:
    return PipelineGraphResponse(
        nodes=[PipelineGraphNode.model_validate(node) for node in nodes],
        edges=[PipelineGraphEdge.model_validate(edge) for edge in edges],
        validation_issues=validation_issues or [],
    )


async def _resolve_graph_references(
    session: AsyncSession,
    nodes: list[PipelineGraphNode],
    org_id: uuid.UUID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate tenant-owned graph references and derive validator pins."""
    agent_ids = {node.agent_id for node in nodes if node.agent_id is not None}
    agents = (
        list(
            (
                await session.execute(
                    select(Agent).where(
                        Agent.organisation_id == org_id,
                        Agent.id.in_(agent_ids),
                    )
                )
            ).scalars()
        )
        if agent_ids
        else []
    )
    agents_by_id = {agent.id: agent for agent in agents}
    missing_agent_ids = sorted(agent_ids - agents_by_id.keys(), key=str)
    if missing_agent_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown agent IDs for this organisation: {missing_agent_ids}",
        )

    manual_schema_ids = {
        node.output_schema_id for node in nodes if node.node_type == "manual" and node.output_schema_id is not None
    }
    existing_schema_ids = (
        set(
            (
                await session.execute(
                    select(Schema.id).where(
                        Schema.organisation_id == org_id,
                        Schema.id.in_(manual_schema_ids),
                    )
                )
            ).scalars()
        )
        if manual_schema_ids
        else set()
    )
    missing_schema_ids = sorted(manual_schema_ids - existing_schema_ids, key=str)
    if missing_schema_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown manual output schema IDs for this organisation: {missing_schema_ids}",
        )

    schema_pins: list[dict[str, Any]] = []
    model_backend_pins: list[dict[str, Any]] = []
    for node in nodes:
        if node.agent_id is not None:
            agent = agents_by_id[node.agent_id]
            schema_pins.extend(
                [
                    {
                        "node_id": str(node.id),
                        "direction": "input",
                        "schema_id": str(agent.input_schema_id),
                    },
                    {
                        "node_id": str(node.id),
                        "direction": "output",
                        "schema_id": str(agent.output_schema_id),
                    },
                ]
            )
            model_backend_pins.append(
                {
                    "node_id": str(node.id),
                    "model_backend_id": str(agent.model_backend_id),
                }
            )
        elif node.output_schema_id is not None:
            schema_pins.append(
                {
                    "node_id": str(node.id),
                    "direction": "output",
                    "schema_id": str(node.output_schema_id),
                }
            )
    return schema_pins, model_backend_pins


@router.get("", response_model=PipelineListResponse)
async def list_pipelines_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        result = await list_pipelines(session, page=page, page_size=page_size, cursor=cursor)
    return PipelineListResponse(
        items=[PipelineResponse.model_validate(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.post("", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_pipeline_endpoint(
    body: PipelineCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        pipeline = await create_pipeline(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            account_id=principal.account_id,
            description=body.description,
            visibility=body.visibility,
            max_concurrent_runs=body.max_concurrent_runs,
            lock_wait_timeout_seconds=body.lock_wait_timeout_seconds,
            node_timeout_seconds=body.node_timeout_seconds,
            run_context_defaults=body.run_context_defaults,
            default_autonomy_level=body.default_autonomy_level,
        )
    return PipelineResponse.model_validate(pipeline)


@router.get("/{pipeline_id}", response_model=PipelineResponse)
async def get_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return PipelineResponse.model_validate(pipeline)


@router.get("/{pipeline_id}/graph", response_model=PipelineGraphResponse)
async def get_pipeline_graph_endpoint(
    pipeline_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineGraphResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        graph = await get_pipeline_graph(session, pipeline_id)
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    nodes, edges = graph
    return _graph_response(nodes, edges)


@router.patch("/{pipeline_id}/graph", response_model=PipelineGraphResponse)
async def replace_pipeline_graph_endpoint(
    pipeline_id: uuid.UUID,
    body: PipelineGraphUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineGraphResponse:
    node_data = [node.model_dump(mode="json") for node in body.nodes]
    edge_data = [
        {
            "id": edge.id,
            "source_node_id": edge.source_node_id,
            "target_node_id": edge.target_node_id,
            "edge_type": edge.edge_type,
            "condition_expression": edge.condition_expression,
            "hitl_gate_config": (
                edge.hitl_gate_config.model_dump(mode="json") if edge.hitl_gate_config is not None else None
            ),
        }
        for edge in body.edges
    ]
    validator_graph = {
        "nodes": node_data,
        "edges": [
            {
                "source": str(edge.source_node_id),
                "target": str(edge.target_node_id),
                "type": edge.edge_type,
                "condition_expression": edge.condition_expression,
                "hitl_gate_config": (
                    edge.hitl_gate_config.model_dump(mode="json") if edge.hitl_gate_config is not None else None
                ),
            }
            for edge in body.edges
        ],
    }
    connector_bindings = [
        {
            "node_id": str(node.id),
            "connector_instance_id": str(node.connector_binding.instance_id),
        }
        for node in body.nodes
        if node.connector_binding is not None
    ]

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        schema_pins, model_backend_pins = await _resolve_graph_references(
            session,
            body.nodes,
            principal.organisation_id,
        )
        graph = await replace_pipeline_graph(
            session,
            pipeline_id=pipeline_id,
            org_id=principal.organisation_id,
            nodes=node_data,
            edges=edge_data,
        )
        if graph is not None:
            validation = await GraphValidator().validate_definition(
                validator_graph,
                session,
                connector_bindings=connector_bindings,
                schema_pins=schema_pins,
                model_backend_pins=model_backend_pins,
            )
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    nodes, edges = graph
    issues = [
        GraphValidationIssue(
            severity=issue.severity,
            code=issue.code,
            message=issue.message,
            node_id=issue.node_id,
        )
        for issue in validation.issues
    ]
    return _graph_response(nodes, edges, validation_issues=issues)


@router.patch("/{pipeline_id}", response_model=PipelineResponse)
async def update_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    body: PipelineUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineResponse:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        if "default_autonomy_level" in updates:
            previous = await get_pipeline(session, pipeline_id)
            prev_level = previous.default_autonomy_level if previous else None
            if prev_level != updates["default_autonomy_level"]:
                await append_audit_event(
                    session,
                    org_id=principal.organisation_id,
                    event_type="pipeline.autonomy_level_changed",
                    actor_user_id=principal.account_id,
                    resource_type="pipeline",
                    resource_id=pipeline_id,
                    payload_json=autonomy_change_payload(
                        previous=prev_level,
                        current=updates["default_autonomy_level"],
                    ),
                    request_id=getattr(principal, "request_id", None),
                )
        pipeline = await update_pipeline(session, pipeline_id, updates)
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return PipelineResponse.model_validate(pipeline)


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        deleted = await delete_pipeline(session, pipeline_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


class PipelineCloneRequest(BaseModel):
    name: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Overrides the default 'Copy of {original_name}' name",
    )


@router.post("/{pipeline_id}/clone", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def clone_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    body: PipelineCloneRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineResponse:
    _log.info("Copy request: pipeline=%s org=%s user=%s", pipeline_id, principal.organisation_id, principal.account_id)

    if principal.org_role not in ("admin", "owner", "member"):
        _log.warning(
            "Copy denied: user %s has role '%s' (requires admin/owner/member)",
            principal.account_id,
            principal.org_role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organisation members and admins can clone pipelines",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)

        # Step 1 — validate source exists
        _log.info("Step 1/4: verifying source pipeline %s exists", pipeline_id)
        source = await get_pipeline(session, pipeline_id)
        if source is None:
            _log.warning("Copy aborted: source pipeline %s not found", pipeline_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"pipeline_copy_failed: Source pipeline not found [pipeline_id: {pipeline_id}]",
            )

        # Step 2 — validate name availability
        target_name = body.name or f"Copy of {source.name}"
        _log.info("Step 2/4: checking name '%s' is available", target_name)
        if not await check_pipeline_name_available(session, principal.organisation_id, target_name):
            _log.warning("Copy aborted: name '%s' already exists in org %s", target_name, principal.organisation_id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"pipeline_copy_failed: A pipeline named '{target_name}' already exists in this organisation",
            )

        # Step 3 — execute copy
        _log.info("Step 3/4: cloning pipeline %s -> '%s'", pipeline_id, target_name)
        try:
            cloned = await clone_pipeline(
                session,
                org_id=principal.organisation_id,
                pipeline_id=pipeline_id,
                account_id=principal.account_id,
                new_name=body.name,
            )
        except Exception:
            _log.exception("Step 3/4 failed: unexpected error cloning pipeline %s", pipeline_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="pipeline_copy_failed: Unexpected error during pipeline copy. Check server logs for details.",
            ) from None

        if cloned is None:
            _log.warning("Step 3/4 failed: source pipeline %s disappeared during copy", pipeline_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"pipeline_copy_failed: Source pipeline disappeared during copy [pipeline_id: {pipeline_id}]",
            )

        # Step 4 — audit event
        _log.info("Step 4/4: recording audit event for clone %s -> %s", pipeline_id, cloned.id)

    _log.info("Copy complete: %s -> %s (%s)", pipeline_id, cloned.id, target_name)
    return PipelineResponse.model_validate(cloned)


# ---------------------------------------------------------------------------
# Save as composite
# ---------------------------------------------------------------------------


class SaveAsCompositeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    selected_node_ids: list[uuid.UUID] = Field(min_length=1)


_PARAM_PATTERN = re.compile(r"\{\{parameter\.(\w+)\}\}")


@router.post("/{pipeline_id}/save-as-composite", status_code=status.HTTP_201_CREATED)
async def save_as_composite_endpoint(
    pipeline_id: uuid.UUID,
    body: SaveAsCompositeRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)

        pipeline = await get_pipeline(session, pipeline_id)
        if pipeline is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")

        all_nodes = pipeline.graph_nodes_json
        selected_ids_str = {str(nid) for nid in body.selected_node_ids}
        sub_nodes = [n for n in all_nodes if str(n.get("id")) in selected_ids_str]
        if not sub_nodes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="No valid nodes selected",
            )

        sub_node_ids_str = {str(n.get("id")) for n in sub_nodes}

        # Auto-detect parameter placeholders: scan all agent prompts referenced by selected nodes
        agent_ids = {n.get("agent_id") for n in sub_nodes if n.get("agent_id") is not None}
        detected_ports: list[dict[str, Any]] = []
        if agent_ids:
            agents_result = await session.execute(
                select(Agent).where(Agent.id.in_(agent_ids), Agent.organisation_id == principal.organisation_id)
            )
            for agent in agents_result.scalars().all():
                matches = _PARAM_PATTERN.findall(agent.prompt_template or "")
                for param_name in matches:
                    # Avoid duplicates
                    if not any(p.get("name") == param_name for p in detected_ports):
                        detected_ports.append({
                            "id": str(uuid.uuid4()),
                            "name": param_name,
                            "label": param_name.replace("_", " ").title(),
                            "description": None,
                            "type": "string",
                            "required": False,
                            "default_value": None,
                            "options": None,
                            "target_injection": {
                                "mode": "prompt_replace",
                                "node_id": str(agent.id),
                                "injection_point": "prompt_template",
                            },
                        })

        # Extract edges that connect selected nodes
        all_edges_raw = await session.execute(
            select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id)
        )
        sub_edges = []
        for edge in all_edges_raw.scalars().all():
            if str(edge.source_node_id) in sub_node_ids_str and str(edge.target_node_id) in sub_node_ids_str:
                sub_edges.append({
                    "id": str(edge.id),
                    "source_node_id": str(edge.source_node_id),
                    "target_node_id": str(edge.target_node_id),
                    "edge_type": edge.edge_type,
                    "condition_expression": edge.condition_expression,
                    "hitl_gate_config": edge.hitl_gate_config,
                })

        # Create the composite template
        template = await create_composite_template(
            session,
            org_id=principal.organisation_id,
            account_id=principal.account_id,
            name=body.name,
            description=body.description,
            sub_pipeline_graph_json={"nodes": [dict(n) for n in sub_nodes], "edges": sub_edges},
            parameter_ports_json=detected_ports,
            version="0.1.0",
        )

    return {
        "id": str(template.id),
        "name": template.name,
        "version": template.version,
        "parameter_ports": detected_ports,
    }


# ---------------------------------------------------------------------------
# Quality Report
# ---------------------------------------------------------------------------


class QualityReportResponse(BaseModel):
    period: dict[str, str]
    summary: dict[str, Any]
    week_over_week: dict[str, Any]
    trend: list[dict[str, Any]]
    eval_breakdown: dict[str, Any]
    deliveries: list[dict[str, Any]]


@router.post(
    "/{pipeline_id}/quality-report",
    response_model=QualityReportResponse,
)
async def trigger_quality_report(
    pipeline_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> QualityReportResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            pipeline = await get_pipeline(session, pipeline_id)
            if pipeline is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")

            report = await generate_quality_report(session, principal.organisation_id)

            endpoints = (
                await session.execute(
                    select(NotificationEndpoint).where(
                        NotificationEndpoint.organisation_id == principal.organisation_id,
                    )
                )
            ).scalars()

            recipient_urls: list[str] = []
            for ep in endpoints:
                try:
                    events = json.loads(ep.events) if ep.events else []
                except (json.JSONDecodeError, TypeError):
                    events = []
                if "quality_report" in events:
                    recipient_urls.append(ep.url)

            deliveries: list[dict[str, Any]] = []
            if recipient_urls:
                deliveries = await deliver_quality_report(report, {"webhook_urls": recipient_urls})

    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc

    return QualityReportResponse(
        period=report["period"],
        summary=report["summary"],
        week_over_week=report["week_over_week"],
        trend=report["trend"],
        eval_breakdown=report["eval_breakdown"],
        deliveries=deliveries,
    )


# ---------------------------------------------------------------------------
# Snapshot Versioning
# ---------------------------------------------------------------------------


class SnapshotResponse(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_version: int
    tag: str | None
    notes: str | None
    created_at: datetime | None
    created_by: uuid.UUID | None = Field(default=None, validation_alias="account_id")

    model_config = {"from_attributes": True}


class SnapshotDetailResponse(SnapshotResponse):
    graph_json: dict[str, Any] | None = None
    connector_bindings_json: list[dict[str, Any]] | None = None
    schema_pins_json: list[dict[str, Any]] | None = None
    prompt_pins_json: list[dict[str, Any]] | None = None
    model_backend_pins_json: list[dict[str, Any]] | None = None
    default_autonomy_level: str | None = None
    run_context_defaults: dict[str, Any] | None = None


class SnapshotTagUpdate(BaseModel):
    tag: str | None = None
    notes: str | None = None


class SnapshotListResponse(BaseModel):
    items: list[SnapshotResponse]
    total: int


class SnapshotDiffQuery(BaseModel):
    snapshot_a_id: uuid.UUID
    snapshot_b_id: uuid.UUID


class SnapshotDiffResponse(BaseModel):
    snapshot_a: dict[str, Any]
    snapshot_b: dict[str, Any]
    nodes_added: list[dict[str, Any]]
    nodes_removed: list[dict[str, Any]]
    nodes_modified: list[dict[str, Any]]
    edges_added: list[dict[str, Any]]
    edges_removed: list[dict[str, Any]]
    edges_modified: list[dict[str, Any]]


def _snapshot_to_response(s: Any) -> SnapshotResponse:
    return SnapshotResponse(
        id=s.id,
        pipeline_id=s.pipeline_id,
        snapshot_version=s.snapshot_version,
        tag=s.tag,
        notes=s.notes,
        created_at=s.created_at,
        account_id=s.account_id,
    )


def _snapshot_to_detail_response(s: Any) -> SnapshotDetailResponse:
    return SnapshotDetailResponse(
        id=s.id,
        pipeline_id=s.pipeline_id,
        snapshot_version=s.snapshot_version,
        tag=s.tag,
        notes=s.notes,
        created_at=s.created_at,
        account_id=s.account_id,
        graph_json=s.graph_json,
        connector_bindings_json=s.connector_bindings_json,
        schema_pins_json=s.schema_pins_json,
        prompt_pins_json=s.prompt_pins_json,
        model_backend_pins_json=s.model_backend_pins_json,
        default_autonomy_level=s.default_autonomy_level,
        run_context_defaults=s.run_context_defaults,
    )


@router.get("/{pipeline_id}/snapshots", response_model=SnapshotListResponse)
async def list_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SnapshotListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            snapshots, total = await list_snapshots(session, pipeline_id, page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pipeline snapshot feature requires database migrations.",
        )
    return SnapshotListResponse(
        items=[_snapshot_to_response(s) for s in snapshots],
        total=total,
    )


@router.get("/{pipeline_id}/snapshots/{snapshot_id}", response_model=SnapshotDetailResponse)
async def get_snapshot_detail_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SnapshotDetailResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            snapshot = await get_snapshot_detail(session, snapshot_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pipeline snapshot feature requires database migrations.",
        )
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return _snapshot_to_detail_response(snapshot)


@router.patch("/{pipeline_id}/snapshots/{snapshot_id}", response_model=SnapshotResponse)
async def tag_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    body: SnapshotTagUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SnapshotResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            snapshot = await tag_snapshot(session, snapshot_id, tag=body.tag, notes=body.notes)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pipeline snapshot feature requires database migrations.",
        )
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return _snapshot_to_response(snapshot)


@router.post("/{pipeline_id}/snapshots/{snapshot_id}/rollback", response_model=SnapshotResponse)
async def rollback_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SnapshotResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            new_snapshot = await rollback_to_snapshot(session, pipeline_id, snapshot_id, account_id=principal.account_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pipeline snapshot feature requires database migrations.",
        )
    if new_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Snapshot or pipeline not found",
        )
    return _snapshot_to_response(new_snapshot)


@router.delete("/{pipeline_id}/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    if principal.org_role not in ("admin", "owner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete snapshots",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_snapshot(session, snapshot_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pipeline snapshot feature requires database migrations.",
        )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Snapshot not found or cannot delete the latest snapshot",
        )


@router.post("/{pipeline_id}/snapshots/diff", response_model=SnapshotDiffResponse)
async def diff_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    body: SnapshotDiffQuery,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SnapshotDiffResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await diff_snapshots(session, body.snapshot_a_id, body.snapshot_b_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pipeline snapshot feature requires database migrations.",
        )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both snapshots not found",
        )
    return SnapshotDiffResponse(**result)


# ---------------------------------------------------------------------------
# Node conversion: manual ↔ agent
# ---------------------------------------------------------------------------


class ConvertToAgentRequest(BaseModel):
    agent_id: uuid.UUID
    connector_binding: ConnectorBinding
    model_backend_id: uuid.UUID


@router.post(
    "/{pipeline_id}/nodes/{node_id}/convert-to-agent",
    response_model=PipelineGraphResponse,
)
async def convert_node_to_agent_endpoint(
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID,
    body: ConvertToAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineGraphResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            graph = await get_pipeline_graph(session, pipeline_id)
            if graph is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
            nodes, edges = graph

            target = _find_node_in_list(nodes, node_id)
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if target.get("node_type") != "manual":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Only manual nodes can be converted to agent",
                )

            agent = (
                await session.execute(
                    select(Agent).where(
                        Agent.id == body.agent_id,
                        Agent.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if agent is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

            connector = (
                await session.execute(
                    select(ConnectorInstance).where(
                        ConnectorInstance.id == body.connector_binding.instance_id,
                        ConnectorInstance.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if connector is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
            if connector.connector_type_id != body.connector_binding.type:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Connector type mismatch",
                )

            model_backend = (
                await session.execute(
                    select(ModelBackend).where(
                        ModelBackend.id == body.model_backend_id,
                        ModelBackend.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if model_backend is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")

            target["node_type"] = "agent"
            target["agent_id"] = str(body.agent_id)
            target["connector_binding"] = {
                "type": body.connector_binding.type,
                "instance_id": str(body.connector_binding.instance_id),
            }
            target.pop("output_schema_id", None)

            await append_audit_event(
                session,
                org_id=principal.organisation_id,
                actor_user_id=principal.account_id,
                event_type="pipeline.node.convert_to_agent",
                resource_type="pipeline",
                resource_id=str(pipeline_id),
                payload_json={
                    "node_id": str(node_id),
                    "agent_id": str(body.agent_id),
                },
            )

            saved = await _save_graph(session, pipeline_id, principal.organisation_id, nodes, edges)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    saved_nodes, saved_edges = saved
    return _graph_response(saved_nodes, saved_edges)


@router.post(
    "/{pipeline_id}/nodes/{node_id}/revert-to-manual",
    response_model=PipelineGraphResponse,
)
async def revert_node_to_manual_endpoint(
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID,
    snapshot_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineGraphResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            graph = await get_pipeline_graph(session, pipeline_id)
            if graph is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
            nodes, edges = graph

            target = _find_node_in_list(nodes, node_id)
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if target.get("node_type") != "agent":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Only agent nodes can be reverted to manual",
                )

            snapshot = await get_snapshot_detail(session, snapshot_id)
            if snapshot is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")

            snapshot_nodes = snapshot.graph_json.get("nodes", [])
            snapshot_node = _find_node_in_list(snapshot_nodes, node_id)
            if snapshot_node is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Snapshot does not contain this node",
                )
            if snapshot_node.get("node_type") != "manual":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Snapshot node was not a manual node",
                )

            output_schema_id = snapshot_node.get("output_schema_id")
            if output_schema_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Snapshot node has no output schema",
                )

            target["node_type"] = "manual"
            sid = str(output_schema_id) if not isinstance(output_schema_id, str) else output_schema_id
            target["output_schema_id"] = sid
            target.pop("agent_id", None)
            target.pop("connector_binding", None)
            if not target.get("label"):
                target["label"] = snapshot_node.get("label") or f"Manual {node_id}"

            await append_audit_event(
                session,
                org_id=principal.organisation_id,
                actor_user_id=principal.account_id,
                event_type="pipeline.node.revert_to_manual",
                resource_type="pipeline",
                resource_id=str(pipeline_id),
                payload_json={
                    "node_id": str(node_id),
                    "snapshot_id": str(snapshot_id),
                },
            )

            saved = await _save_graph(session, pipeline_id, principal.organisation_id, nodes, edges)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    saved_nodes, saved_edges = saved
    return _graph_response(saved_nodes, saved_edges)


def _find_node_in_list(nodes: list[dict[str, Any]], node_id: uuid.UUID) -> dict[str, Any] | None:
    """Find a node dict by ID within a list of node dicts."""
    node_id_str = str(node_id)
    for n in nodes:
        raw_id = n.get("id")
        if raw_id is None:
            continue
        if isinstance(raw_id, uuid.UUID):
            if raw_id == node_id:
                return n
        elif str(raw_id) == node_id_str:
            return n
    return None


def _edge_to_dict(e: Any) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "source_node_id": str(e.source_node_id),
        "target_node_id": str(e.target_node_id),
        "edge_type": e.edge_type,
        "condition_expression": getattr(e, "condition_expression", None),
        "hitl_gate_config": dict(e.hitl_gate_config) if isinstance(e.hitl_gate_config, dict) else e.hitl_gate_config,
    }


async def _save_graph(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[Any],
) -> tuple[list[dict[str, Any]], list[Any]] | None:
    """Persist updated nodes + edges via replace_pipeline_graph.

    Accepts edges as either ORM model instances (PipelineEdge) or plain dicts.
    """
    edge_dicts = [_edge_to_dict(e) if hasattr(e, "source_node_id") else dict(e) for e in edges]
    return await replace_pipeline_graph(
        session,
        pipeline_id=pipeline_id,
        org_id=org_id,
        nodes=nodes,
        edges=edge_dicts,
    )
