"""Tests for library schema seed definitions."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from modulo.core.seed_data.library_schemas import SCHEMAS

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
    assert len(definition["properties"]) > 0
    Draft202012Validator.check_schema(definition)


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_definition_has_title(entry: dict[str, Any]) -> None:
    assert isinstance(entry["definition"].get("title"), str)


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_definition_has_description(entry: dict[str, Any]) -> None:
    assert isinstance(entry["definition"].get("description"), str)


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_each_property_has_type(entry: dict[str, Any]) -> None:
    valid = {"string", "number", "integer", "boolean", "object", "array"}
    for prop_name, prop_schema in entry["definition"]["properties"].items():
        assert prop_schema.get("type") in valid, f"'{entry['name']}.{prop_name}' bad type"


@pytest.mark.parametrize("entry", SCHEMAS, ids=lambda e: e["name"])
def test_each_property_has_description(entry: dict[str, Any]) -> None:
    for prop_name, prop_schema in entry["definition"]["properties"].items():
        assert "description" in prop_schema, f"'{entry['name']}.{prop_name}' missing description"


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
    for prop, expected in expected_by_schema.get(entry["name"], {}).items():
        actual = entry["definition"]["properties"][prop].get("enum")
        assert actual == expected


def test_serialize_deserialize_all() -> None:
    restored = json.loads(json.dumps([s["definition"] for s in SCHEMAS]))
    assert len(restored) == 22
    for orig, rest in zip([s["definition"] for s in SCHEMAS], restored):
        assert orig == rest


def test_all_definitions_self_validating() -> None:
    for entry in SCHEMAS:
        try:
            Draft202012Validator.check_schema(entry["definition"])
        except ValidationError as exc:
            pytest.fail(f"Schema '{entry['name']}' is invalid: {exc.message}")
