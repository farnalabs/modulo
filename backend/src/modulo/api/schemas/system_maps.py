"""Discovery request/response contracts for the internal System Maps preview."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


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
