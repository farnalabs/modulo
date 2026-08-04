"""Shared constants and helpers for library test modules.

The canonical sets below are derived from the actual source primitives
rather than hardcoded, so they cannot drift from the library catalog the
way a hand-maintained list can.
"""

from __future__ import annotations

import re
from typing import Any, cast

from modulo.core.library.agents import definitions as _agent_defs
from modulo.core.library.complexity_reviewer import COMPLEXITY_REVIEWER
from modulo.core.library.workflows import definitions as _workflow_defs
from modulo.core.seed_data.library_schemas import SCHEMAS


def _kebab(name: str) -> str:
    """Kebab-case slug used by workflow step agent references."""
    return name.lower().replace(" ", "-")


KNOWN_AGENTS: set[str] = {
    _kebab(entry["name"])
    for entry in vars(_agent_defs).values()
    if isinstance(entry, dict) and entry.get("node_type") == "agent" and "name" in entry
} | {_kebab(COMPLEXITY_REVIEWER["name"])}

VALID_CONNECTOR_TYPES: set[str] = {
    "source_control",
    "ci_runner",
    "ci_cd",
    "incident_management",
    "issue_tracking",
    "filesystem",
    "monitoring",
    "messaging",
}

# Every workflow primitive exported by the definitions module, keyed by its
# canonical constant name so parametrized tests can identify failures.
WORKFLOWS: dict[str, dict[str, Any]] = {
    name: getattr(_workflow_defs, name)
    for name in dir(_workflow_defs)
    if name.isupper() and isinstance(getattr(_workflow_defs, name), dict)
}

REQUIRED_WORKFLOW_KEYS = {"name", "description", "version", "author", "tags", "pipeline_steps", "default_config"}

VALID_PROPERTY_TYPES = {"string", "number", "integer", "boolean", "object", "array"}

VALID_FORMATS = {"date", "date-time"}


def _string_value(prop_schema: dict[str, Any]) -> str:
    """Pick a string that satisfies *prop_schema*, honoring ``pattern``.

    Patterns are matched by a best-effort literal extraction: all literal
    characters (alphanumerics, ``_`` and ``-``) are pulled out of the regex.
    If nothing can be extracted the value falls back to ``"x"``, which the
    valid-document test will surface loudly for any pattern it cannot satisfy.
    """
    pattern = prop_schema.get("pattern")
    if not pattern:
        return "x"
    literal = re.sub(r"[^A-Za-z0-9_-]", "", pattern)
    return literal or "x"


def build_valid_document(definition: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal document that validates against *definition*.

    Every required property is filled with a value of its declared type
    (first enum value when the property constrains enums).
    """
    document: dict[str, Any] = {}
    for required in definition.get("required", []):
        prop_schema = definition["properties"].get(required)
        if prop_schema is None:
            continue
        prop_type = prop_schema.get("type")
        if prop_schema.get("enum"):
            document[required] = prop_schema["enum"][0]
        elif prop_type == "array":
            document[required] = []
        elif prop_type == "object":
            document[required] = {}
        elif prop_type in ("number", "integer"):
            document[required] = 1
        elif prop_type == "boolean":
            document[required] = True
        elif prop_schema.get("format") == "date":
            document[required] = "2000-01-01"
        elif prop_schema.get("format") == "date-time":
            document[required] = "2000-01-01T00:00:00Z"
        else:
            document[required] = _string_value(prop_schema)
    return document


def iter_all_properties(definition: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(property_name, property_schema)`` for a schema definition.

    Array properties are expanded so their ``items`` schemas are validated
    too, catching invalid item types that a top-level-only walk would miss.
    """
    properties: list[tuple[str, dict[str, Any]]] = []
    for name, prop_schema in definition.get("properties", {}).items():
        properties.append((name, prop_schema))
        items = prop_schema.get("items")
        if isinstance(items, dict):
            properties.append((f"{name}[]", items))
    return properties


def enum_bearing_schemas() -> dict[str, dict[str, list[str]]]:
    """Return ``schema_name -> {property: enum_values}`` from the source."""
    result: dict[str, dict[str, list[str]]] = {}
    for entry in SCHEMAS:
        definition = cast(dict[str, Any], entry["definition"])
        enums = {
            name: prop_schema["enum"]
            for name, prop_schema in definition.get("properties", {}).items()
            if "enum" in prop_schema
        }
        if enums:
            result[cast(str, entry["name"])] = enums
    return result
