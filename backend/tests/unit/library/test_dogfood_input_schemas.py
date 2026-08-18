"""Dogfooding: reusable input schemas for dogfood pipelines.

Verifies the schema system end-to-end for the two shared dogfood input
schemas (``pr-review-input`` and ``ticket-input``): each is defined in the
org seed data, is a valid JSON Schema, accepts a valid payload ("latest"
version), and rejects an invalid payload — mirroring the contract exposed by
the ``validate_payload`` MCP tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from modulo.core.seed_data.library_schemas import SCHEMAS

DOGFOOD_SCHEMAS_DIR = Path(__file__).resolve().parents[4] / "devtools" / "dogfood" / "schemas"


def _definition(name: str) -> dict[str, Any]:
    for entry in SCHEMAS:
        if entry["name"] == name:
            return entry["definition"]
    raise AssertionError(f"Schema '{name}' not defined in library seed data")


VALID_SAMPLES: dict[str, dict[str, Any]] = {
    "pr-review-input": {
        "number": 42,
        "repository": "farnalabs/modulo",
        "head-ref": "feat/far-304",
        "head-sha": "abc123def456",
        "action": "synchronize",
    },
    "ticket-input": {
        "id": "FAR-304",
        "title": "Dogfood: define a reusable input schema",
        "body": "Bind a registered schema to the dogfood pipeline input.",
        "type": "feature",
        "priority": "high",
        "labels": ["dogfood"],
        "repo": "farnalabs/modulo",
    },
}

INVALID_SAMPLES: dict[str, dict[str, Any]] = {
    "pr-review-input": {"number": "not-an-int", "repository": "farnalabs/modulo"},
    "ticket-input": {"id": "FAR-304", "title": "Missing type enum"},
}


@pytest.mark.parametrize("name", ["pr-review-input", "ticket-input"])
def test_schema_is_defined(name: str) -> None:
    """The schema is defined in library seed data and is a valid JSON Schema."""
    definition = _definition(name)
    assert definition
    Draft202012Validator.check_schema(definition)


@pytest.mark.parametrize("name", ["pr-review-input", "ticket-input"])
def test_valid_sample_payload_passes_validation(name: str) -> None:
    """A valid sample payload validates against the schema's latest definition."""
    validator = Draft202012Validator(_definition(name))
    errors = list(validator.iter_errors(VALID_SAMPLES[name]))
    assert not errors, f"'{name}' rejected a valid payload: {errors}"


@pytest.mark.parametrize("name", ["pr-review-input", "ticket-input"])
def test_invalid_payload_fails_validation(name: str) -> None:
    """A payload missing required / mistyped fields is rejected."""
    validator = Draft202012Validator(_definition(name))
    assert not validator.is_valid(INVALID_SAMPLES[name])


def _reference_files() -> list[Path]:
    if not DOGFOOD_SCHEMAS_DIR.is_dir():
        pytest.skip(f"Dogfood schema references dir not found: {DOGFOOD_SCHEMAS_DIR}")
    return sorted(DOGFOOD_SCHEMAS_DIR.glob("*.schema.json"))


def test_reference_copies_match_seed_definitions() -> None:
    """Version-controlled copies under devtools/dogfood/schemas stay in sync."""
    seeds = {entry["name"]: entry for entry in SCHEMAS}
    refs = _reference_files()
    assert refs, "No dogfood schema reference copies found"

    for ref in refs:
        data = json.loads(ref.read_text())
        seed = seeds.get(data["name"])
        assert seed is not None, f"Reference copy {ref.name} has no matching seed schema"
        assert data["name"] == seed["name"]
        assert data["description"] == seed["description"]
        assert data["definition"] == seed["definition"]

    assert {json.loads(f.read_text())["name"] for f in refs} == {"pr-review-input", "ticket-input"}
