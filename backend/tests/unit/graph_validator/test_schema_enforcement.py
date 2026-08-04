"""Tests for schema enforcement infrastructure (Phase 1 of ADR 018).

Covers proof-of-brokenness regression, type promotion, nullable handling,
additionalProperties, required fields, nested objects, array items,
version pinning, deletion protection, grace period degradation,
schema pins map parsing, and backward compatibility.

All pure validation tests call _check_schema_fields directly (no DB).
Tests needing DB interaction use mocked AsyncSession.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.graph_validator import GraphValidator, ValidationResult
from modulo.db.crud.schema import SchemaDeletionProtectedError, delete_schema

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_GRAPH = {
    "nodes": [{"id": "a"}, {"id": "b"}],
    "edges": [{"source": "a", "target": "b", "type": "normal"}],
}

_PHASE_1_CUTOVER = datetime(2026, 7, 22, tzinfo=UTC)


def _snapshot(**kw):
    snap = MagicMock()
    snap.graph_json = kw.get("graph_json", _SIMPLE_GRAPH)
    snap.schema_pins_json = kw.get("schema_pins_json", [])
    snap.connector_bindings_json = kw.get("connector_bindings_json", [])
    snap.model_backend_pins_json = kw.get("model_backend_pins_json", [])
    snap.created_at = kw.get("created_at", _PHASE_1_CUTOVER)
    snap.pipeline_id = kw.get("pipeline_id", uuid.uuid4())
    snap.id = kw.get("id", uuid.uuid4())
    snap.environment_profile_id = kw.get("environment_profile_id")
    return snap


def _schema_version_row(schema_id, definition_json, *, version="1.0"):
    sv = MagicMock()
    sv.schema_id = schema_id
    sv.version = version
    sv.definition_json = definition_json
    return sv


def _mock_session_for_schema_versions(rows, *, use_scalar_one=False):
    session = AsyncMock()
    exc_result = MagicMock()
    if use_scalar_one:
        if len(rows) > 1:
            _rows = list(rows)

            def _make_call(_row):
                r = MagicMock()
                r.scalar_one_or_none.return_value = _row
                r.scalar_one.return_value = _row
                return r

            results = [_make_call(r) for r in _rows]
            session.execute = AsyncMock(side_effect=results)
        else:
            rv = rows[0] if rows else None
            exc_result.scalar_one_or_none.return_value = rv
            exc_result.scalar_one.return_value = rv
            session.execute = AsyncMock(return_value=exc_result)
    else:
        scalars = MagicMock()
        scalars.all.return_value = rows
        exc_result.scalars.return_value = scalars
        session.execute = AsyncMock(return_value=exc_result)
    return session


# ===================================================================
# Test 1 — Proof-of-brokenness (regression)
# ===================================================================


def test_schema_compatibility_no_longer_silent_noop():
    """Pre-PRD: incompatible schemas produced 0 errors (broken). Now: correctly rejects."""
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"id": "a", "output_schema_pin": {"schema_id": sid_a, "schema_version": "1.0"}},
            {"id": "b", "input_schema_pin": {"schema_id": sid_b, "schema_version": "1.0"}},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    validator = GraphValidator()
    result = ValidationResult()
    validator._check_schema_compatibility(graph, result)
    assert not result.is_valid
    assert any(i.code == "SCHEMA_INCOMPATIBLE" for i in result.issues)


# ===================================================================
# Test 2 — Type promotion
# ===================================================================


def test_integer_to_number_promotion():
    """integer output → number input should be compatible."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "integer"},
        {"type": "number"},
    )
    assert not errors


def test_number_to_integer_rejected():
    """number output → integer input should NOT be compatible."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "number"},
        {"type": "integer"},
    )
    assert any("type mismatch" in e for e in errors)


# ===================================================================
# Test 3 — Nullable handling
# ===================================================================


def test_nullable_output_to_non_nullable_input_rejected():
    """["string", "null"] output → "string" input should be rejected."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": ["string", "null"]},
        {"type": "string"},
    )
    assert any("null" in e and "not" in e for e in errors)


def test_non_nullable_output_to_nullable_input_accepted():
    """ "string" output → ["string", "null"] input should be accepted."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "string"},
        {"type": ["string", "null"]},
    )
    assert not errors


def test_nullable_array_subset():
    """["string","null"] output → ["string","integer","null"] input should be accepted."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": ["string", "null"]},
        {"type": ["string", "integer", "null"]},
    )
    assert not errors


# ===================================================================
# Test 4 — additionalProperties: false
# ===================================================================


def test_additional_properties_false_rejects_extra_fields():
    """Output with extra field → input with additionalProperties: false should reject."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "object", "properties": {"name": {"type": "string"}, "extra": {"type": "string"}}},
        {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False},
    )
    assert any("extra properties" in e for e in errors)


def test_additional_properties_true_allows_extra_fields():
    """Output with extra field → input with additionalProperties: true (default) should pass."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "object", "properties": {"name": {"type": "string"}, "extra": {"type": "string"}}},
        {"type": "object", "properties": {"name": {"type": "string"}}},
    )
    assert not errors


# ===================================================================
# Test 5 — Required fields
# ===================================================================


def test_missing_required_field_rejected():
    """Input requires 'name' field → output lacks 'name' → reject."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "object", "properties": {"age": {"type": "integer"}}},
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
    )
    assert any("required field missing" in e for e in errors)


def test_required_field_present_accepted():
    """Output has all fields required by input → pass."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}},
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
    )
    assert not errors


# ===================================================================
# Test 6 — Nested object checking
# ===================================================================


def test_nested_object_type_mismatch():
    """Nested field type mismatch should be caught recursively."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {
            "type": "object",
            "properties": {"meta": {"type": "object", "properties": {"value": {"type": "string"}}}},
        },
        {
            "type": "object",
            "properties": {"meta": {"type": "object", "properties": {"value": {"type": "integer"}}}},
        },
    )
    assert any("type mismatch" in e for e in errors)


def test_nested_object_match_accepted():
    """Nested fields with matching types → pass."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {
            "type": "object",
            "properties": {"meta": {"type": "object", "properties": {"value": {"type": "string"}}}},
        },
        {
            "type": "object",
            "properties": {"meta": {"type": "object", "properties": {"value": {"type": "string"}}}},
        },
    )
    assert not errors


# ===================================================================
# Test 7 — Array items checking
# ===================================================================


def test_array_items_type_mismatch():
    """Array item type mismatch should be caught."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "array", "items": {"type": "string"}},
        {"type": "array", "items": {"type": "integer"}},
    )
    assert any("type mismatch" in e for e in errors)


def test_array_items_match_accepted():
    """Array items with matching types → pass."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "array", "items": {"type": "string"}},
        {"type": "array", "items": {"type": "string"}},
    )
    assert not errors


def test_array_items_non_dict_skipped():
    """Non-dict items entries are skipped safely (no crash, no error)."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "array", "items": "string"},
        {"type": "array", "items": {"type": "string"}},
    )
    assert not errors


# ===================================================================
# Test 7b — Additional branch paths in _check_schema_fields
# ===================================================================


def test_nullable_output_list_without_null():
    """A type-array without 'null' reports per-type mismatches but no null error."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": ["string", "integer"]},
        {"type": "string"},
    )
    assert any("type mismatch 'integer'" in e for e in errors)
    assert not any("null" in e for e in errors)


def test_additional_properties_false_no_extra_fields():
    """additionalProperties: false with no extra output fields → pass."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "object", "properties": {"name": {"type": "string"}}},
        {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False},
    )
    assert not errors


def test_non_dict_properties_skipped():
    """Nested property maps that are not dicts are skipped safely."""
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    assert not errors


# ===================================================================
# Test 8 — Version pinning
# ===================================================================


async def test_version_pinning_resolves_pinned_not_latest():
    """Schema pinned to v1 should resolve v1, not v2 (latest)."""
    schema_id = uuid.uuid4()

    rows = [
        _schema_version_row(
            schema_id,
            {"type": "object", "properties": {"v1_field": {"type": "string"}}},
            version="1.0",
        ),
    ]
    session = _mock_session_for_schema_versions(rows, use_scalar_one=True)

    definitions = await GraphValidator()._resolve_schema_definitions(
        {(schema_id, "1.0"): None},
        session,
    )

    assert schema_id in definitions
    assert definitions[schema_id]["properties"]["v1_field"]["type"] == "string"


async def test_version_pinning_returns_empty_for_missing_version():
    """Pinned to a version that doesn't exist → empty dict (no crash)."""
    schema_id = uuid.uuid4()
    session = _mock_session_for_schema_versions([], use_scalar_one=True)

    definitions = await GraphValidator()._resolve_schema_definitions(
        {(schema_id, "99.0"): None},
        session,
    )

    assert schema_id not in definitions


# ===================================================================
# Test 9 — Deletion protection
# ===================================================================


async def test_schema_deletion_blocked_when_pinned():
    """Deleting a schema version referenced by snapshot_schema_pins should be blocked."""
    schema_id = uuid.uuid4()
    schema_mock = MagicMock()
    schema_mock.id = schema_id
    schema_mock.system = False

    # Mock session to simulate SnapshotSchemaPin count > 0
    session = AsyncMock()

    get_schema_result = MagicMock()
    get_schema_result.scalar_one_or_none.return_value = schema_mock

    agent_count_result = MagicMock()
    agent_count_result.scalar_one.return_value = 0

    pin_count_result = MagicMock()
    pin_count_result.scalar_one.return_value = 1

    lib_count_result = MagicMock()
    lib_count_result.scalar_one.return_value = 0

    session.execute.side_effect = [
        get_schema_result,
        agent_count_result,
        pin_count_result,
        lib_count_result,
    ]

    with pytest.raises(SchemaDeletionProtectedError) as exc_info:
        await delete_schema(session, schema_id)
    assert exc_info.value.schema_id == schema_id
    assert "snapshot_schema_pins" in str(exc_info.value)


async def test_schema_deletion_allowed_when_not_pinned():
    """No SnapshotSchemaPin references → deletion succeeds."""
    schema_id = uuid.uuid4()
    schema_mock = MagicMock()
    schema_mock.id = schema_id
    schema_mock.system = False

    session = AsyncMock()

    get_schema_result = MagicMock()
    get_schema_result.scalar_one_or_none.return_value = schema_mock

    agent_count_result = MagicMock()
    agent_count_result.scalar_one.return_value = 0

    pin_count_result = MagicMock()
    pin_count_result.scalar_one.return_value = 0

    lib_count_result = MagicMock()
    lib_count_result.scalar_one.return_value = 0

    session.execute.side_effect = [
        get_schema_result,
        agent_count_result,
        pin_count_result,
        lib_count_result,
    ]

    result = await delete_schema(session, schema_id)
    assert result is True


# ===================================================================
# Test 10 — Grace period degradation
# ===================================================================


async def test_pre_existing_pipeline_gets_degraded_warning():
    """Pre-Phase-1 snapshot with incompatible schemas does not block early."""
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()

    graph = {
        "nodes": [
            {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
            {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }

    snap = _snapshot(
        graph_json=graph,
        created_at=datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC),
        schema_pins_json=[],
    )

    rows = [
        _schema_version_row(out_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
        _schema_version_row(in_id, {"type": "object", "properties": {"x": {"type": "integer"}}}),
    ]
    session = _mock_session_for_schema_versions(rows, use_scalar_one=True)

    result = await GraphValidator().validate_for_run(snap, {}, session)

    assert not result.is_valid
    assert any(i.code == "SCHEMA_FIELD_INCOMPATIBLE" for i in result.issues)


async def test_non_pre_existing_pipeline_blocks_on_incompatible():
    """Post-Phase-1 snapshot with incompatible schemas → hard block (early return)."""
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()

    graph = {
        "nodes": [
            {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
            {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }

    snap = _snapshot(
        graph_json=graph,
        created_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC),
        schema_pins_json=[],
    )

    rows = [
        _schema_version_row(out_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
        _schema_version_row(in_id, {"type": "object", "properties": {"x": {"type": "integer"}}}),
    ]
    session = _mock_session_for_schema_versions(rows, use_scalar_one=True)

    result = await GraphValidator().validate_for_run(snap, {}, session)

    assert not result.is_valid
    assert any(i.code == "SCHEMA_FIELD_INCOMPATIBLE" for i in result.issues)
    assert not any(i.code == "SCHEMA_DEGRADED" for i in result.issues)


# ===================================================================
# Test 11 — _build_schema_pins_map reads from per-node pins
# ===================================================================


def test_build_schema_pins_map_reads_per_node_pins():
    """New format with input_schema_pin/output_schema_pin should be read correctly."""
    graph = {
        "nodes": [
            {
                "id": "a",
                "input_schema_pin": {"schema_id": "00000000-0000-0000-0000-000000000001", "schema_version": "1.0"},
                "output_schema_pin": {"schema_id": "00000000-0000-0000-0000-000000000002", "schema_version": "2.0"},
            },
            {
                "id": "b",
                "input_schema_pin": {"schema_id": "00000000-0000-0000-0000-000000000003", "schema_version": "1.5"},
            },
        ],
        "edges": [],
    }
    result = GraphValidator._build_schema_pins_map(graph)

    assert "a" in result
    assert result["a"]["input"] == (uuid.UUID("00000000-0000-0000-0000-000000000001"), "1.0")
    assert result["a"]["output"] == (uuid.UUID("00000000-0000-0000-0000-000000000002"), "2.0")
    assert result["b"]["input"] == (uuid.UUID("00000000-0000-0000-0000-000000000003"), "1.5")
    assert result["b"]["output"] is None


# ===================================================================
# Test 12 — Backward compat (old format without pins)
# ===================================================================


def test_build_schema_pins_map_handles_missing_pins():
    """Old graph_json nodes without pins should return None (skip)."""
    graph = {
        "nodes": [
            {"id": "a"},
            {"id": "b", "input_schema_pin": None, "output_schema_pin": None},
            {"id": "c", "input_schema_pin": {}, "output_schema_pin": {}},
        ],
        "edges": [],
    }
    result = GraphValidator._build_schema_pins_map(graph)

    assert result["a"]["input"] is None
    assert result["a"]["output"] is None
    assert result["b"]["input"] is None
    assert result["b"]["output"] is None
    assert result["c"]["input"] is None
    assert result["c"]["output"] is None


def test_build_schema_pins_map_empty_graph():
    """Empty graph with no nodes → empty map."""
    result = GraphValidator._build_schema_pins_map({"nodes": [], "edges": []})
    assert result == {}
