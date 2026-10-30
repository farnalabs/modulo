"""System Maps request/response schemas (FAR-2236).

Centralised Pydantic models for the System Maps REST API surface.
All validation uses vocabularies from the ORM models (db/models/system_map.py).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class SystemMapNodeCreate(BaseModel):
    node_key: str = Field(min_length=1, max_length=100)
    node_type: str = Field(pattern="|".join(VALID_NODE_TYPES))
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
    relationship_type: str = Field(pattern="|".join(VALID_REL_TYPES))
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
    binding_kind: str = Field(pattern="|".join(VALID_BINDING_KINDS))


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
    # Requests
    "SystemMapCreate",
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
