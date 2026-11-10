"""Canonical set of library-primitive types. Single source of truth for the
DB CHECK constraint, the community-service validation, and the route-layer
Pydantic filters. Every site that enumerates valid types MUST import from here.
"""

from __future__ import annotations

PRIMITIVE_TYPES: tuple[str, ...] = (
    "schema",
    "agent",
    "workflow",
    "pipeline_template",
    "test_fixture",
    "composite",
    "integration",
    "lifecycle_map",
    "library_collection",
)

# Allowed pin types for library_collection manifests (ADR 032).
# Only atomic, non-collection types may be pinned.
COLLECTION_PIN_TYPES: frozenset[str] = frozenset({"schema", "agent", "workflow", "pipeline_template"})

MAX_COLLECTION_PINS: int = 25
