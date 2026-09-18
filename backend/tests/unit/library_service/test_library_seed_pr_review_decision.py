"""Tests for the PR Review Decision schema seed primitive."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from modulo.core.library_service import _MODULO_PRIMITIVES
from modulo.core.seed_data.library_schemas import SCHEMAS

_PRIMITIVE_SLUG = "pr-review-decision"
_PRIMITIVE_TYPE = "schema"
_EXPECTED_TAGS = ["schema", "pr", "review", "github", "decision"]

_VALID_VERDICT: dict = {
    "decision": "APPROVE",
    "summary": "All changes look good; no issues found.",
    "findings": [
        {
            "severity": "minor",
            "file": "src/app.py",
            "line": 42,
            "comment": "Consider extracting this into a helper.",
        }
    ],
}

_REVIEW_VERDICT: dict = {
    "decision": "REQUEST_CHANGES",
    "summary": "Found a critical bug and a style issue.",
    "findings": [
        {
            "severity": "critical",
            "file": "src/auth.py",
            "line": 15,
            "comment": "SQL injection vulnerability.",
        },
        {
            "severity": "minor",
            "comment": "Missing trailing newline.",
        },
    ],
}


def _find_seed_primitive():
    """Return the PR Review Decision seed primitive from _MODULO_PRIMITIVES."""
    for p in _MODULO_PRIMITIVES:
        if p.slug == _PRIMITIVE_SLUG and p.primitive_type == _PRIMITIVE_TYPE:
            return p
    return None


def _find_schema_entry():
    """Return the PR Review Decision entry from the library schemas SCHEMAS list."""
    for entry in SCHEMAS:
        if entry["name"] == _PRIMITIVE_SLUG:
            return entry
    return None


# ---------------------------------------------------------------------------
# Presence and conventions
# ---------------------------------------------------------------------------


def test_primitive_exists_in_seed_data():
    prim = _find_seed_primitive()
    assert prim is not None, f"Schema primitive '{_PRIMITIVE_SLUG}' not found in _MODULO_PRIMITIVES"


def test_primitive_has_correct_type():
    prim = _find_seed_primitive()
    assert prim.primitive_type == _PRIMITIVE_TYPE


def test_primitive_has_correct_tags():
    prim = _find_seed_primitive()
    assert prim.tags == _EXPECTED_TAGS


def test_primitive_has_correct_version():
    prim = _find_seed_primitive()
    assert prim.version == "1.0"


def test_primitive_has_correct_source():
    prim = _find_seed_primitive()
    assert prim.source == "modulo"


def test_primitive_description_not_empty():
    prim = _find_seed_primitive()
    assert prim.description, "Description must not be empty"


def test_schema_entry_exists_in_library_schemas():
    entry = _find_schema_entry()
    assert entry is not None, f"Schema entry '{_PRIMITIVE_SLUG}' not found in library_schemas.SCHEMAS"


def test_schema_entry_has_title():
    entry = _find_schema_entry()
    assert entry["definition"].get("title"), "Schema definition must have a title"


def test_schema_entry_has_description():
    entry = _find_schema_entry()
    assert entry["definition"].get("description"), "Schema definition must have a description"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _get_validator() -> Draft202012Validator:
    entry = _find_schema_entry()
    return Draft202012Validator(entry["definition"])


def test_valid_approve_verdict_accepted():
    validator = _get_validator()
    errors = list(validator.iter_errors(_VALID_VERDICT))
    assert not errors, f"Valid APPROVE verdict rejected: {errors}"


def test_valid_request_changes_verdict_accepted():
    validator = _get_validator()
    errors = list(validator.iter_errors(_REVIEW_VERDICT))
    assert not errors, f"Valid REQUEST_CHANGES verdict rejected: {errors}"


def test_rejects_decision_neutral():
    validator = _get_validator()
    payload = {"decision": "NEUTRAL", "summary": "Looks fine."}
    assert not validator.is_valid(payload), "Schema should reject decision='NEUTRAL'"


def test_rejects_missing_summary():
    validator = _get_validator()
    payload = {"decision": "APPROVE"}
    assert not validator.is_valid(payload), "Schema should reject payload missing 'summary'"


def test_rejects_missing_decision():
    validator = _get_validator()
    payload = {"summary": "Looks fine."}
    assert not validator.is_valid(payload), "Schema should reject payload missing 'decision'"


def test_rejects_empty_object():
    validator = _get_validator()
    assert not validator.is_valid({}), "Schema should reject empty object"


def test_rejects_additional_properties():
    validator = _get_validator()
    payload = {
        "decision": "APPROVE",
        "summary": "Looks fine.",
        "extra_field": "not allowed",
    }
    assert not validator.is_valid(payload), "Schema should reject additional properties"


def test_findings_severity_enum_enforced():
    validator = _get_validator()
    payload = {
        "decision": "REQUEST_CHANGES",
        "summary": "Issues found.",
        "findings": [{"severity": "invalid", "comment": "bad"}],
    }
    assert not validator.is_valid(payload), "Schema should reject invalid severity"


def test_findings_requires_severity_and_comment():
    validator = _get_validator()
    payload = {
        "decision": "REQUEST_CHANGES",
        "summary": "Issues found.",
        "findings": [{"severity": "major"}],
    }
    assert not validator.is_valid(payload), "Schema should reject finding missing 'comment'"


def test_empty_findings_array_valid():
    validator = _get_validator()
    payload = {"decision": "APPROVE", "summary": "Clean diff.", "findings": []}
    errors = list(validator.iter_errors(payload))
    assert not errors, f"Empty findings array rejected: {errors}"


def test_valid_json_roundtrip():
    entry = _find_schema_entry()
    definition = entry["definition"]
    serialized = json.dumps(definition)
    deserialized = json.loads(serialized)
    assert deserialized == definition
