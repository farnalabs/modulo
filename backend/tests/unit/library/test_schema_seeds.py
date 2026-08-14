"""Tests for library schema seed definitions."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from modulo.core.seed_data.library_schemas import SCHEMAS

from .helpers import (
    VALID_FORMATS,
    VALID_PROPERTY_TYPES,
    build_valid_document,
    enum_bearing_schemas,
    iter_all_properties,
)

REQUIRED_PROPERTIES: dict[str, list[str]] = {
    "meeting-notes": ["title", "date"],
    "adr": ["title", "status", "decision"],
    "api-spec": ["endpoint", "method"],
    "design-doc": ["title", "author", "design"],
    "changelog-entry": ["version", "type", "description"],
    "release-note": ["version", "date", "title"],
    "test-plan": ["title", "feature"],
    "test-case": ["id", "title", "steps", "expected-results"],
    "db-change": ["migration-id", "description", "sql", "rollback-sql"],
    "sprint-plan": ["sprint-name", "start-date", "end-date"],
    "roadmap-item": ["quarter", "title", "priority"],
    "quality-report": ["title", "date", "period"],
    "deployment": ["version", "environment", "status"],
    "incident-report": ["id", "title", "severity", "date"],
    "env-config": ["environment", "service"],
    "okr": ["quarter", "objective", "key-results"],
    "post-mortem": ["title", "incident-ref"],
    "code-review-comment": ["file", "comment", "author"],
    "bug-report": ["title", "description", "steps-to-reproduce", "expected-behavior", "actual-behavior"],
    "issue-ticket": ["id", "title", "type"],
    "pull-request": ["id", "title", "author", "source-branch", "target-branch"],
    "user-story": ["id", "title", "description", "acceptance-criteria"],
}


def test_exactly_22_schemas() -> None:
    assert len(SCHEMAS) == 22


def test_all_schema_names_are_unique() -> None:
    names = [s["name"] for s in SCHEMAS]
    assert len(names) == len(set(names))


def test_all_schema_names_matched() -> None:
    schema_names = {s["name"] for s in SCHEMAS}
    expected = set(REQUIRED_PROPERTIES)
    assert schema_names == expected


def test_each_schema_has_description() -> None:
    for entry in SCHEMAS:
        assert entry.get("description"), f"Schema '{entry['name']}' is missing a description"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_definition_is_valid_json_schema(entry: dict[str, Any]) -> None:
    definition = entry["definition"]
    assert definition["type"] == "object"
    assert "properties" in definition
    assert isinstance(definition["properties"], dict)
    assert definition["properties"]
    Draft202012Validator.check_schema(definition)


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_definition_has_title(entry: dict[str, Any]) -> None:
    assert entry["definition"].get("title"), f"'{entry['name']}' missing title"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_definition_has_description(entry: dict[str, Any]) -> None:
    assert entry["definition"].get("description"), f"'{entry['name']}' missing description"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_each_property_has_type(entry: dict[str, Any]) -> None:
    for prop_name, prop_schema in iter_all_properties(entry["definition"]):
        assert prop_schema.get("type") in VALID_PROPERTY_TYPES, (
            f"'{entry['name']}.{prop_name}' has invalid type {prop_schema.get('type')!r}"
        )


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_each_property_has_description(entry: dict[str, Any]) -> None:
    for prop_name, prop_schema in entry["definition"]["properties"].items():
        assert "description" in prop_schema, f"'{entry['name']}.{prop_name}' missing description"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_formats_are_known(entry: dict[str, Any]) -> None:
    for prop_name, prop_schema in iter_all_properties(entry["definition"]):
        fmt = prop_schema.get("format")
        if fmt is not None:
            assert fmt in VALID_FORMATS, f"'{entry['name']}.{prop_name}' uses unknown format '{fmt}'"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_required_fields_match_properties(entry: dict[str, Any]) -> None:
    props = entry["definition"]["properties"]
    for field in entry["definition"].get("required", []):
        assert field in props, f"'{entry['name']}' required '{field}' not in properties"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_required_fields_from_spec(entry: dict[str, Any]) -> None:
    expected = set(REQUIRED_PROPERTIES[entry["name"]])
    actual = set(entry["definition"].get("required", []))
    assert actual == expected


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_json_roundtrip(entry: dict[str, Any]) -> None:
    assert json.loads(json.dumps(entry["definition"])) == entry["definition"]


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_enum_values_are_valid(entry: dict[str, Any]) -> None:
    expected_by_schema: dict[str, dict[str, list[str]]] = {
        "adr": {"status": ["proposed", "accepted", "deprecated", "superseded"]},
        "api-spec": {"method": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
        "changelog-entry": {"type": ["added", "changed", "deprecated", "removed", "fixed", "security"]},
        "test-case": {"priority": ["high", "medium", "low"]},
        "db-change": {"risk": ["low", "medium", "high"]},
        "roadmap-item": {
            "priority": ["p0", "p1", "p2", "p3"],
            "status": ["planned", "in-progress", "completed", "cancelled"],
        },
        "deployment": {
            "environment": ["staging", "production"],
            "status": ["pending", "in-progress", "completed", "rolled-back"],
        },
        "incident-report": {"status": ["investigating", "identified", "monitoring", "resolved"]},
        "okr": {"status": ["on-track", "at-risk", "behind", "not-started"]},
        "issue-ticket": {"type": ["bug", "feature", "enhancement", "chore"]},
        "pull-request": {"status": ["open", "draft", "merged", "closed"]},
    }
    expected = expected_by_schema.get(entry["name"], {})
    actual = {
        prop: prop_schema["enum"]
        for prop, prop_schema in entry["definition"]["properties"].items()
        if "enum" in prop_schema
    }
    assert actual == expected, f"'{entry['name']}' enum properties differ from the canonical spec"


def test_enum_coverage_is_exhaustive() -> None:
    """Every schema with an enum-bearing property is pinned by the canonical spec."""
    expected = {
        "adr",
        "api-spec",
        "changelog-entry",
        "test-case",
        "db-change",
        "roadmap-item",
        "deployment",
        "incident-report",
        "okr",
        "issue-ticket",
        "pull-request",
    }
    assert set(enum_bearing_schemas()) == expected


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_accepts_valid_document(entry: dict[str, Any]) -> None:
    definition = entry["definition"]
    validator = Draft202012Validator(definition)
    document = build_valid_document(definition)
    errors = list(validator.iter_errors(document))
    assert not errors, f"'{entry['name']}' rejected a valid document: {errors}"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_rejects_missing_required_field(entry: dict[str, Any]) -> None:
    definition = entry["definition"]
    required = definition.get("required", [])
    if not required:
        pytest.skip(f"'{entry['name']}' has no required fields")
    validator = Draft202012Validator(definition)
    document = build_valid_document(definition)
    document.pop(required[0], None)
    assert not validator.is_valid(document), f"'{entry['name']}' accepted a document missing required '{required[0]}'"
