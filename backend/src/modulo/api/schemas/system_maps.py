"""System Maps request/response schemas (FAR-2236).

Centralised Pydantic models for the System Maps REST API surface.
All validation uses vocabularies from the ORM models (db/models/system_map.py).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, model_validator

# ─── Centralised vocabularies (mirror ORM CHECK constraints) ───

VALID_NODE_TYPES: tuple[str, ...] = (
    "pipeline",
    "agent",
    "connector",
    "improvement_loop",
    "data_store",
    "external_system",
)

VALID_REL_TYPES: tuple[str, ...] = ("flow", "data", "trigger", "loop_back")

VALID_REVISION_STATUSES: tuple[str, ...] = ("draft", "published", "archived")

VALID_BINDING_KINDS: tuple[str, ...] = ("trigger", "action_pipeline")

VALID_VISIBILITY: tuple[str, ...] = ("org", "team")

_ANCHORED_NODE_TYPE_PATTERN = f"^({'|'.join(VALID_NODE_TYPES)})$"
_ANCHORED_REL_TYPE_PATTERN = f"^({'|'.join(VALID_REL_TYPES)})$"
_ANCHORED_BINDING_KIND_PATTERN = f"^({'|'.join(VALID_BINDING_KINDS)})$"


# ─── Request bodies ───


class SystemMapCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: Literal["org", "team"] = "org"
    default_map: bool = False


class SystemMapUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: Literal["org", "team"] | None = None
    default_map: bool | None = None


class SystemMapRevisionCreate(BaseModel):
    """Create a draft revision (nodes, relationships, loop configs)."""

    nodes: list["SystemMapNodeCreate"] = Field(default_factory=list)
    relationships: list["SystemMapRelationshipCreate"] = Field(default_factory=list)
    loop_configs: list["SystemMapLoopConfigCreate"] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_graph_references(self) -> "SystemMapRevisionCreate":
        node_keys = [node.node_key for node in self.nodes]
        node_key_set = set(node_keys)
        if len(node_keys) != len(node_key_set):
            raise ValueError("node_key values must be unique within a revision")
        relationship_keys = {
            (rel.source_node_key, rel.target_node_key, rel.relationship_type) for rel in self.relationships
        }
        if len(relationship_keys) != len(self.relationships):
            raise ValueError("relationships must be unique within a revision")
        missing_relationship_nodes = sorted(
            {
                key
                for rel in self.relationships
                for key in (rel.source_node_key, rel.target_node_key)
                if key not in node_key_set
            }
        )
        if missing_relationship_nodes:
            raise ValueError(f"relationships reference missing nodes: {', '.join(missing_relationship_nodes)}")
        loop_keys = [loop.node_key for loop in self.loop_configs]
        if len(loop_keys) != len(set(loop_keys)):
            raise ValueError("loop_configs must be unique by node_key within a revision")
        missing_loop_nodes = sorted(key for key in loop_keys if key not in node_key_set)
        if missing_loop_nodes:
            raise ValueError(f"loop_configs reference missing nodes: {', '.join(missing_loop_nodes)}")
        wrong_loop_types = sorted(
            loop.node_key
            for loop in self.loop_configs
            if next(node for node in self.nodes if node.node_key == loop.node_key).node_type != "improvement_loop"
        )
        if wrong_loop_types:
            raise ValueError(f"loop_configs require improvement_loop nodes: {', '.join(wrong_loop_types)}")
        return self


class SystemMapNodeCreate(BaseModel):
    node_key: str = Field(min_length=1, max_length=100)
    node_type: str = Field(pattern=_ANCHORED_NODE_TYPE_PATTERN)
    display_name: str = Field(min_length=1, max_length=255)
    config_json: dict[str, Any] = Field(default_factory=dict)
    pipeline_id: uuid.UUID | None = None
    connector_instance_id: uuid.UUID | None = None
    owner_team_id: uuid.UUID | None = None
    eval_id: uuid.UUID | None = None
    position_x: Decimal = Field(default=Decimal(0))
    position_y: Decimal = Field(default=Decimal(0))


class SystemMapRelationshipCreate(BaseModel):
    source_node_key: str = Field(min_length=1, max_length=100)
    target_node_key: str = Field(min_length=1, max_length=100)
    relationship_type: str = Field(pattern=_ANCHORED_REL_TYPE_PATTERN)
    config_json: dict[str, Any] = Field(default_factory=dict)


class SystemMapLoopConfigCreate(BaseModel):
    node_key: str = Field(min_length=1, max_length=100)
    target_signal: str = Field(min_length=1, max_length=100)
    observation_window_seconds: int = Field(ge=1)
    action_pipeline_id: uuid.UUID
    budget_limit_usd: Decimal = Field(ge=0)
    cooldown_seconds: int = Field(ge=0)
    outcome_metric: str = Field(min_length=1, max_length=100)


class AttributionBindingCreate(BaseModel):
    pipeline_id: uuid.UUID
    trigger_id: uuid.UUID | None = None
    node_key: str = Field(min_length=1, max_length=100)
    binding_kind: str = Field(pattern=_ANCHORED_BINDING_KIND_PATTERN)

    @model_validator(mode="after")
    def validate_trigger_semantics(self) -> "AttributionBindingCreate":
        if self.binding_kind == "trigger" and self.trigger_id is None:
            raise ValueError("trigger bindings require trigger_id")
        if self.binding_kind == "action_pipeline" and self.trigger_id is not None:
            raise ValueError("action_pipeline bindings cannot include trigger_id")
        return self


# ─── Response models ───


class SystemMapResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    owner_team_id: uuid.UUID | None
    visibility: str
    default_map: bool
    published_revision_id: uuid.UUID | None
    account_id: uuid.UUID
    updated_by: uuid.UUID | None
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SystemMapListResponse(BaseModel):
    items: list[SystemMapResponse]
    total: int
    page: int
    page_size: int


class SystemMapRevisionResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    system_map_id: uuid.UUID
    revision_number: int
    status: str
    content_json: dict[str, Any]
    published_at: datetime | None
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SystemMapNodeResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    revision_id: uuid.UUID
    node_key: str
    node_type: str
    display_name: str
    config_json: dict[str, Any]
    pipeline_id: uuid.UUID | None
    connector_instance_id: uuid.UUID | None
    owner_team_id: uuid.UUID | None
    eval_id: uuid.UUID | None
    position_x: Decimal
    position_y: Decimal
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SystemMapRelationshipResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    revision_id: uuid.UUID
    source_node_key: str
    target_node_key: str
    relationship_type: str
    config_json: dict[str, Any]
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SystemMapLoopConfigResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    revision_id: uuid.UUID
    node_key: str
    target_signal: str
    observation_window_seconds: int
    action_pipeline_id: uuid.UUID
    budget_limit_usd: Decimal
    cooldown_seconds: int
    outcome_metric: str
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SystemMapRevisionDetailResponse(BaseModel):
    """Full revision with nested nodes, relationships, and loop configs."""

    revision: SystemMapRevisionResponse
    nodes: list[SystemMapNodeResponse]
    relationships: list[SystemMapRelationshipResponse]
    loop_configs: list[SystemMapLoopConfigResponse]


class SystemMapOverviewNodeHealth(BaseModel):
    """Per-node health metrics for the overview aggregate."""

    node_key: str
    node_type: str
    display_name: str
    health: Literal["healthy", "degraded", "unhealthy", "unknown"]
    queue_depth: int | None = None
    throughput_per_min: float | None = None
    capacity_pressure: float | None = None  # 0.0-1.0
    failure_rate: float | None = None  # 0.0-1.0
    telemetry_coverage: float | None = None  # 0.0-1.0
    last_run_id: uuid.UUID | None = None
    last_run_status: str | None = None
    last_run_completed_at: datetime | None = None


class SystemMapOverviewLoopConfig(BaseModel):
    """Per-loop-config status for the overview aggregate."""

    node_key: str
    target_signal: str
    observation_window_seconds: int
    action_pipeline_id: uuid.UUID
    budget_limit_usd: Decimal
    cooldown_seconds: int
    outcome_metric: str
    last_admission_at: datetime | None = None
    last_admission_status: str | None = None
    cooldown_active: bool = False
    cooldown_until: datetime | None = None
    budget_used_usd: Decimal = Decimal(0)
    budget_period_start: datetime | None = None


class SystemMapOverviewBottleneck(BaseModel):
    """Detected bottleneck in the system map."""

    node_key: str
    node_type: str
    display_name: str
    kind: Literal["starved", "overloaded", "failing", "unobserved"]
    detail: str
    severity: Literal["low", "medium", "high", "critical"]


class SystemMapOverviewResponse(BaseModel):
    """Aggregate read model for a published system map."""

    system_map_id: uuid.UUID
    revision_id: uuid.UUID
    revision_number: int
    generated_at: datetime
    nodes: list[SystemMapOverviewNodeHealth]
    loop_configs: list[SystemMapOverviewLoopConfig]
    bottlenecks: list[SystemMapOverviewBottleneck]


class AttributionBindingResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    pipeline_id: uuid.UUID
    trigger_id: uuid.UUID | None
    system_map_id: uuid.UUID
    node_key: str
    binding_kind: str
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class PublishValidationError(BaseModel):
    """Error detail for publish validation failures."""

    code: str
    message: str
    node_key: str | None = None
    relationship_id: uuid.UUID | None = None


class PublishValidationErrorHTTP(HTTPException):
    """422 raised when a system map revision publish fails validation.

    The exception carries the validation errors and the revision so the exception handler
    can render them as a 200 PublishResponse. Raising (instead of returning)
    ensures the audited() dependency records outcome="error".
    """

    def __init__(self, validation_errors: list[PublishValidationError], revision: Any) -> None:
        self.validation_errors = validation_errors
        self.revision = revision
        # Note: status_code=422 is used here so the exception type is recognisable,
        # but the registered exception handler converts it to a 200 PublishResponse
        # (wire-contract preservation: clients expect 200 with errors in the body).
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Publish validation failed", "errors": [e.model_dump() for e in validation_errors]},
        )


class PublishResponse(BaseModel):
    revision: SystemMapRevisionResponse
    validation_errors: list[PublishValidationError] = Field(default_factory=list)


# Forward references for recursive types
SystemMapRevisionCreate.model_rebuild()


__all__ = [
    "VALID_BINDING_KINDS",
    # Vocabularies
    "VALID_NODE_TYPES",
    "VALID_REL_TYPES",
    "VALID_REVISION_STATUSES",
    "VALID_VISIBILITY",
    "AttributionBindingCreate",
    "AttributionBindingResponse",
    "PublishResponse",
    "PublishValidationError",
    "PublishValidationErrorHTTP",
    # Requests
    "SystemMapCreate",
    "SystemMapDiscoveryReport",
    "SystemMapDiscoveryRequest",
    "SystemMapDiscoverySkipped",
    "SystemMapDiscoverySource",
    "SystemMapListResponse",
    "SystemMapLoopConfigCreate",
    "SystemMapLoopConfigResponse",
    "SystemMapNodeCreate",
    "SystemMapNodeResponse",
    "SystemMapOverviewBottleneck",
    "SystemMapOverviewLoopConfig",
    "SystemMapOverviewNodeHealth",
    "SystemMapOverviewResponse",
    "SystemMapRelationshipCreate",
    "SystemMapRelationshipResponse",
    # Responses
    "SystemMapResponse",
    "SystemMapRevisionCreate",
    "SystemMapRevisionDetailResponse",
    "SystemMapRevisionResponse",
    "SystemMapUpdate",
]

# ─── Discovery contracts ───


class SystemMapDiscoveryRequest(BaseModel):
    name: str = Field(default="Discovered System Map", min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class SystemMapDiscoverySource(BaseModel):
    kind: str
    identifier: str
    name: str


class SystemMapDiscoverySkipped(BaseModel):
    source: str
    reason: str


class SystemMapDiscoveryReport(BaseModel):
    sources_inspected: list[SystemMapDiscoverySource]
    node_count: int
    edge_count: int
    inferred_nodes: list[dict[str, Any]]
    inferred_edges: list[dict[str, Any]]
    skipped: list[SystemMapDiscoverySkipped]
    warnings: list[str]
    draft_map_id: uuid.UUID
    draft_revision_id: uuid.UUID
    draft_revision_number: int
    published: bool = False
