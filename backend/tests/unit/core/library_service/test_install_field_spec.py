"""Round-trip tests for collection-install field-spec → JSON Schema conversion.

Proves (FAR-826) that the installed ``definition_json`` for every shipped
schema seed preserves the full structure of the seed's ``content_json.fields``
shape — including array ``items`` (string shorthand and nested sub-field
object maps) and nested required lists — not merely that an entity exists.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from modulo.core.library_service._seed_data import _MODULO_PRIMITIVES
from modulo.core.library_service.install import (
    _definition_from_field_spec,
    _schema_definition_from_content,
)

SCHEMA_SEEDS = [p for p in _MODULO_PRIMITIVES if p.primitive_type == "schema"]
SCHEMA_SEEDS_BY_SLUG = {p.slug: p for p in SCHEMA_SEEDS}


# The exact definition_json collection install must produce for each shipped
# schema seed. These mirror the intent of the seed's ``fields`` shape — a
# structure-losing conversion (dropping ``items``, flattening the nested
# findings object) fails the equality assertion.
EXPECTED_DEFINITIONS: dict[str, dict[str, Any]] = {
    "prd-input": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "problem_statement": {"type": "string"},
            "goals": {"type": "array", "items": {"type": "string"}},
            "non_goals": {"type": "array", "items": {"type": "string"}},
            "stakeholders": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "problem_statement"],
    },
    "requirements-output": {
        "type": "object",
        "properties": {
            "functional": {"type": "array", "items": {"type": "string"}},
            "non_functional": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["functional"],
    },
    "pr-review-decision": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "comment": {"type": "string"},
                    },
                },
            },
        },
        "required": ["decision", "summary"],
    },
}


def test_every_schema_seed_has_an_expected_definition() -> None:
    """Front the round-trip matrix: every shipped schema seed must be covered."""
    assert {p.slug for p in SCHEMA_SEEDS} == set(EXPECTED_DEFINITIONS)


@pytest.mark.parametrize(
    ("slug", "expected"),
    sorted(EXPECTED_DEFINITIONS.items()),
    ids=sorted(EXPECTED_DEFINITIONS),
)
def test_schema_seed_definition_round_trip(slug: str, expected: dict[str, Any]) -> None:
    """The installed definition_json preserves the seed's full structure."""
    seed = SCHEMA_SEEDS_BY_SLUG[slug]
    definition = _schema_definition_from_content(seed.content_json or {})
    assert definition == expected


def _seed_fields(slug: str) -> list[dict[str, Any]]:
    seed = SCHEMA_SEEDS_BY_SLUG[slug]
    return cast("list[dict[str, Any]]", seed.content_json["fields"])


def test_required_list_matches_seed_required_flags() -> None:
    """Required fields in the seed surface as the definition's required list."""
    definition = _schema_definition_from_content(
        {**SCHEMA_SEEDS_BY_SLUG["requirements-output"].content_json, "fields": _seed_fields("requirements-output")}
    )
    assert definition is not None
    seed_required = [f["name"] for f in _seed_fields("requirements-output") if f.get("required", False)]
    assert definition["required"] == seed_required


def test_string_shorthand_items_map_to_scalar_item_schema() -> None:
    """``items: "string"`` becomes a constrained scalar item schema."""
    shorthand_targets: dict[str, list[str]] = {
        "prd-input": ["goals", "non_goals", "stakeholders"],
        "requirements-output": ["functional"],
    }
    for seed_slug, field_names in shorthand_targets.items():
        seed = SCHEMA_SEEDS_BY_SLUG[seed_slug]
        definition = _schema_definition_from_content(seed.content_json or {})
        assert definition is not None
        properties = definition["properties"]
        for name in field_names:
            assert properties[name] == {"type": "array", "items": {"type": "string"}}


def test_nested_subfield_items_become_object_item_schema() -> None:
    """The pr-review-decision findings structure survives installation."""
    definition = _schema_definition_from_content(SCHEMA_SEEDS_BY_SLUG["pr-review-decision"].content_json or {})
    assert definition is not None
    findings = definition["properties"]["findings"]
    assert findings["items"]["type"] == "object"
    assert findings["items"]["properties"]["severity"]["enum"] == ["critical", "major", "minor"]
    assert findings["items"]["properties"]["line"]["type"] == "integer"


def test_array_spec_without_items_stays_unconstrained() -> None:
    """An array field with no ``items`` key keeps the previous behaviour."""
    assert _definition_from_field_spec({"type": "array"}) == {"type": "array"}


def test_non_array_specs_are_unchanged() -> None:
    """Non-array specs do not gained items keys and keep enum handling."""
    assert _definition_from_field_spec({"type": "string"}) == {"type": "string"}
    assert _definition_from_field_spec({"type": "integer"}) == {"type": "integer"}
    assert _definition_from_field_spec({"type": "string", "enum": ["a", "b"]}) == {
        "type": "string",
        "enum": ["a", "b"],
    }
    assert _definition_from_field_spec("boolean") == {"type": "boolean"}


def test_dict_items_collect_nested_required_entries() -> None:
    """Sub-specs declaring ``required: true`` surface in the items' required list."""
    spec = {
        "type": "array",
        "items": {
            "severity": {"type": "string", "required": True},
            "comment": {"type": "string"},
        },
    }
    assert _definition_from_field_spec(spec) == {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"severity": {"type": "string"}, "comment": {"type": "string"}},
            "required": ["severity"],
        },
    }


def test_unrecognised_items_shapes_fall_back_gracefully() -> None:
    """A non-string/non-dict items value leaves the array unconstrained (no crash)."""
    assert _definition_from_field_spec({"type": "array", "items": 42}) == {"type": "array"}
