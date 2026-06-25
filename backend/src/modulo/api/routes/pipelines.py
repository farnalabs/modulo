"""Pipeline CRUD REST API.

Alpha: Graph replacement uses row-level locking (SELECT ... FOR UPDATE) in
replace_pipeline_graph. No advisory lock is deployed; the row lock on the
pipeline row serialises concurrent graph writes within a serialisable transaction.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.graph_validator import GraphValidator
from modulo.core.run_context.autonomy import (
    autonomy_change_payload,
)
from modulo.db.crud.pipeline import (
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
from modulo.db.models.schema import Schema
from modulo.db.rls import set_rls_org, set_rls_user_context

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
    default_autonomy_level: str | None
    snapshot_count: int = 0
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PipelineListResponse(BaseModel):
    items: list[PipelineResponse]
    total: int
    page: int
    page_size: int


class GraphPosition(BaseModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class ConnectorBinding(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    instance_id: uuid.UUID


class PipelineGraphNode(BaseModel):
    id: uuid.UUID
    node_type: Literal["agent", "manual"] = "agent"
    agent_id: uuid.UUID | None
    position: GraphPosition
    connector_binding: ConnectorBinding | None = None
    output_schema_id: uuid.UUID | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = None
    autonomy_recommendation: str | None = None

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
        elif self.agent_id is None:
            raise ValueError("Agent nodes require an agent")
        return self


class HitlGateConfig(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(max_length=2000)
    reject_target: uuid.UUID | None = None
    claim_expiry_minutes: int = Field(gt=0, le=1440)
    human_only: bool
    required_team_id: uuid.UUID | None = None


class PipelineGraphEdge(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str = Field(pattern="^(normal|reject)$")
    hitl_gate_config: HitlGateConfig | None = None

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
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        result = await list_pipelines(session, page=page, page_size=page_size)
    return PipelineListResponse(
        items=[PipelineResponse.model_validate(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_pipeline_endpoint(
    body: PipelineCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        pipeline = await create_pipeline(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            created_by=principal.user_id,
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
        await set_rls_user_context(session, principal.user_id, principal.org_role)
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
        await set_rls_user_context(session, principal.user_id, principal.org_role)
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
        await set_rls_user_context(session, principal.user_id, principal.org_role)
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
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        if "default_autonomy_level" in updates:
            previous = await get_pipeline(session, pipeline_id)
            prev_level = previous.default_autonomy_level if previous else None
            if prev_level != updates["default_autonomy_level"]:
                await append_audit_event(
                    session,
                    org_id=principal.organisation_id,
                    event_type="pipeline.autonomy_level_changed",
                    actor_user_id=principal.user_id,
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
        await set_rls_user_context(session, principal.user_id, principal.org_role)
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        cloned = await clone_pipeline(
            session,
            org_id=principal.organisation_id,
            pipeline_id=pipeline_id,
            created_by=principal.user_id,
            new_name=body.name,
        )
    if cloned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return PipelineResponse.model_validate(cloned)


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
    created_by: uuid.UUID | None

    model_config = {"from_attributes": True}


class SnapshotDetailResponse(SnapshotResponse):
    graph_json: dict | None = None
    connector_bindings_json: list[dict] | None = None
    schema_pins_json: list[dict] | None = None
    prompt_pins_json: list[dict] | None = None
    model_backend_pins_json: list[dict] | None = None
    default_autonomy_level: str | None = None
    run_context_defaults: dict | None = None


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
    snapshot_a: dict
    snapshot_b: dict
    nodes_added: list[dict]
    nodes_removed: list[dict]
    nodes_modified: list[dict]
    edges_added: list[dict]
    edges_removed: list[dict]
    edges_modified: list[dict]


def _snapshot_to_response(s: Any) -> SnapshotResponse:
    return SnapshotResponse(
        id=s.id,
        pipeline_id=s.pipeline_id,
        snapshot_version=s.snapshot_version,
        tag=s.tag,
        notes=s.notes,
        created_at=s.created_at,
        created_by=s.created_by,
    )


def _snapshot_to_detail_response(s: Any) -> SnapshotDetailResponse:
    return SnapshotDetailResponse(
        id=s.id,
        pipeline_id=s.pipeline_id,
        snapshot_version=s.snapshot_version,
        tag=s.tag,
        notes=s.notes,
        created_at=s.created_at,
        created_by=s.created_by,
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        snapshots, total = await list_snapshots(session, pipeline_id, page=page, page_size=page_size)
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        snapshot = await get_snapshot_detail(session, snapshot_id)
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        snapshot = await tag_snapshot(session, snapshot_id, tag=body.tag, notes=body.notes)
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        new_snapshot = await rollback_to_snapshot(session, pipeline_id, snapshot_id, created_by=principal.user_id)
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        deleted = await delete_snapshot(session, snapshot_id)
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
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        result = await diff_snapshots(session, body.snapshot_a_id, body.snapshot_b_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both snapshots not found",
        )
    return SnapshotDiffResponse(**result)
