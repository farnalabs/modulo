"""Unit tests for remaining GraphValidator edge paths.

Covers small branches not exercised by the primary suites: ``_is_pre_existing``
snapshot age handling, ``INPUT_NULL_PAYLOAD``, missing node id topology error,
deferred schema keywords, nullable/enum schema-field handling, empty pin and
invalid-uuid short-circuits, and ``_validate_payload`` type edge cases.
"""

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core import graph_validator as _gv_module
from modulo.core.graph_validator import GraphValidator, ValidationResult
from modulo.core.graph_validator._types import try_parse_uuids
from modulo.core.graph_validator.category_validator import validate_node_category

_PHASE_1_CUTOVER = _gv_module._PHASE_1_CUTOVER


def _snapshot(**kw) -> MagicMock:
    snap = MagicMock()
    snap.graph_json = kw.get("graph_json", {"nodes": [], "edges": []})
    snap.schema_pins_json = kw.get("schema_pins_json", [])
    snap.connector_bindings_json = kw.get("connector_bindings_json", [])
    snap.model_backend_pins_json = kw.get("model_backend_pins_json", [])
    snap.environment_profile_id = kw.get("environment_profile_id")
    snap.created_at = kw.get("created_at")
    return snap


# ---------------------------------------------------------------------------
# _is_pre_existing
# ---------------------------------------------------------------------------


def test_pre_existing_none_created_at():
    """Snapshot without created_at is not treated as pre-existing."""
    assert not _gv_module._is_pre_existing(_snapshot(created_at=None))


def test_pre_existing_before_cutover():
    snap = _snapshot(created_at=_PHASE_1_CUTOVER - timedelta(days=1))
    assert _gv_module._is_pre_existing(snap)


def test_pre_existing_after_cutover():
    snap = _snapshot(created_at=_PHASE_1_CUTOVER + timedelta(days=1))
    assert not _gv_module._is_pre_existing(snap)


def test_pre_existing_naive_datetime_assumed_utc():
    snap = _snapshot(created_at=(_PHASE_1_CUTOVER - timedelta(days=1)).replace(tzinfo=None))
    assert _gv_module._is_pre_existing(snap)


def test_pre_existing_non_datetime_false():
    snap = _snapshot(created_at="2026-07-21")
    assert not _gv_module._is_pre_existing(snap)


# ---------------------------------------------------------------------------
# INPUT_NULL_PAYLOAD
# ---------------------------------------------------------------------------


async def test_validate_for_run_null_payload_is_error():
    """validate_for_run with input_payload=None emits INPUT_NULL_PAYLOAD."""
    snap = _snapshot(
        graph_json={"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b", "type": "normal"}]},
        created_at=_PHASE_1_CUTOVER + timedelta(days=1),
    )
    session = AsyncMock()
    session.execute = AsyncMock()
    result = await GraphValidator().validate_for_run(snap, None, session)
    assert not result.is_valid
    assert any(i.code == "INPUT_NULL_PAYLOAD" for i in result.issues)


# ---------------------------------------------------------------------------
# TOPOLOGY_NODE_MISSING_ID
# ---------------------------------------------------------------------------


async def test_topology_node_missing_id_is_error():
    snap = _snapshot(graph_json={"nodes": [{}], "edges": []})
    session = AsyncMock()
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_NODE_MISSING_ID" for i in result.issues)


async def test_topology_diamond_bfs_skips_visited():
    """Diamond graph queues 'c' twice; BFS skips the second visit without error."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "a", "target": "c", "type": "normal"},
                {"source": "b", "target": "c", "type": "normal"},
            ],
        }
    )
    session = AsyncMock()
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# _build_schema_pins_map — node without id
# ---------------------------------------------------------------------------


def test_build_schema_pins_map_node_without_id_skipped():
    graph = {"nodes": [{"id": "", "input_schema_pin": {"schema_id": str(uuid.uuid4()), "schema_version": "1.0"}}]}
    result = GraphValidator._build_schema_pins_map(graph)
    assert result == {}


# ---------------------------------------------------------------------------
# Deferred schema keywords
# ---------------------------------------------------------------------------


async def test_schema_deferred_keywords_warn():
    """Schemas with $ref/oneOf produce a SCHEMA_CHECK_DEFERRED warning."""
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()
    session = _schema_version_session(
        [
            _schema_row(out_id, {"type": "object", "$ref": "#/definitions/x"}),
            _schema_row(in_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
        ]
    )
    result = ValidationResult()
    await GraphValidator()._check_schema_compatibility_deep(
        {
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        session,
        result,
    )
    assert any(i.code == "SCHEMA_CHECK_DEFERRED" for i in result.issues)


async def test_schema_compatibility_deep_skips_missing_pins():
    """Edges whose endpoints lack schema pins are skipped by deep check."""
    snap = _snapshot(
        graph_json={"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b", "type": "normal"}]},
        created_at=_PHASE_1_CUTOVER + timedelta(days=1),
    )
    session = AsyncMock()
    session.execute = AsyncMock()
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# _check_schema_fields — nullable output lists and enums
# ---------------------------------------------------------------------------


def test_nullable_output_list_with_null_input():
    """["string", "null"] output → ["string", "null"] input is compatible."""
    gv = GraphValidator()
    errors = gv._check_schema_fields({"type": ["string", "null"]}, {"type": ["string", "null"]})
    assert not errors


def test_nullable_output_not_in_input_types():
    """["string", "null"] output → ["string"] input rejects via not-in-types."""
    gv = GraphValidator()
    errors = gv._check_schema_fields({"type": ["string", "null"]}, {"type": ["string"]})
    assert any("null" in e and "not in input types" in e for e in errors)


def test_nullable_output_type_mismatch():
    """["string", "null"] output → integer input rejects the string type."""
    gv = GraphValidator()
    errors = gv._check_schema_fields({"type": ["string", "null"]}, {"type": "integer"})
    assert any("type mismatch 'string' -> 'integer'" in e for e in errors)
    assert any("output allows null but input does not" in e for e in errors)


def test_enum_subset_compatible():
    gv = GraphValidator()
    errors = gv._check_schema_fields(
        {"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a", "b", "c"]}
    )
    assert not errors


def test_enum_not_subset_incompatible():
    gv = GraphValidator()
    errors = gv._check_schema_fields({"type": "string", "enum": ["a", "z"]}, {"type": "string", "enum": ["a", "b"]})
    assert any("enum" in e for e in errors)


def test_schema_fields_depth_limit_no_error():
    """Deeply nested schemas beyond the recursion cap produce no error."""
    deep_out: dict[str, Any] = {"type": "object", "properties": {}}
    deep_in: dict[str, Any] = {"type": "object", "properties": {}}
    for _ in range(25):
        deep_out = {"type": "object", "properties": {"child": deep_out}}
        deep_in = {"type": "object", "properties": {"child": deep_in}}
    gv = GraphValidator()
    errors = gv._check_schema_fields(deep_out, deep_in)
    assert not errors


# ---------------------------------------------------------------------------
# _resolve_schema_definitions — empty pins
# ---------------------------------------------------------------------------


async def test_resolve_schema_definitions_empty_pins():
    session = AsyncMock()
    session.execute = AsyncMock()
    definitions = await GraphValidator()._resolve_schema_definitions({}, session)
    assert definitions == {}
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _check_input_schema_compatibility — no nodes / missing schema
# ---------------------------------------------------------------------------


async def test_input_schema_check_no_nodes_skipped():
    session = AsyncMock()
    session.execute = AsyncMock()
    result = ValidationResult()
    await GraphValidator()._check_input_schema_compatibility({"nodes": [], "edges": []}, {"x": 1}, session, result)
    assert result.is_valid
    session.execute.assert_not_called()


async def test_input_schema_check_missing_schema_skipped():
    """Input pin referencing a missing schema version does not error."""
    sid = str(uuid.uuid4())
    session = AsyncMock()
    exc = MagicMock()
    exc.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=exc)
    result = ValidationResult()
    await GraphValidator()._check_input_schema_compatibility(
        {
            "nodes": [
                {"id": "a", "input_schema_pin": {"schema_id": sid, "schema_version": "9.9"}},
                {"id": "b"},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        {},
        session,
        result,
    )
    assert result.is_valid


async def test_input_schema_check_uuid_schema_id():
    """A schema_id already parsed as a UUID is used directly (no re-parse)."""
    sid = uuid.uuid4()
    session = AsyncMock()
    sv = MagicMock()
    sv.definition_json = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    exc = MagicMock()
    exc.scalar_one_or_none.return_value = sv
    session.execute = AsyncMock(return_value=exc)
    result = ValidationResult()
    await GraphValidator()._check_input_schema_compatibility(
        {
            "nodes": [
                {"id": "a", "input_schema_pin": {"schema_id": sid, "schema_version": "1.0"}},
                {"id": "b"},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        {"x": "hello"},
        session,
        result,
    )
    assert result.is_valid
    assert not result.issues


async def test_input_schema_check_mismatched_payload_errors():
    """A payload violating the input schema produces INPUT_SCHEMA_MISMATCH."""
    sid = str(uuid.uuid4())
    session = AsyncMock()
    sv = MagicMock()
    sv.definition_json = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    exc = MagicMock()
    exc.scalar_one_or_none.return_value = sv
    session.execute = AsyncMock(return_value=exc)
    result = ValidationResult()
    await GraphValidator()._check_input_schema_compatibility(
        {
            "nodes": [
                {"id": "a", "input_schema_pin": {"schema_id": sid, "schema_version": "1.0"}},
                {"id": "b"},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        {"other": 1},
        session,
        result,
    )
    assert not result.is_valid
    assert any(i.code == "INPUT_SCHEMA_MISMATCH" for i in result.issues)


# ---------------------------------------------------------------------------
# _validate_payload — type edge cases
# ---------------------------------------------------------------------------


def test_validate_payload_missing_schema_definition():
    gv = GraphValidator()
    assert not gv._validate_payload({"a": 1}, None)


def test_validate_payload_non_dict_properties():
    gv = GraphValidator()
    assert not gv._validate_payload({"a": 1}, {"type": "object", "properties": "nope"})


def test_validate_payload_non_dict_field_def():
    gv = GraphValidator()
    errors = gv._validate_payload({"a": 1}, {"type": "object", "properties": {"a": "not-a-dict"}})
    assert errors == []


def test_validate_payload_no_expected_type():
    gv = GraphValidator()
    errors = gv._validate_payload({"a": 1}, {"type": "object", "properties": {"a": {}}})
    assert errors == []


def test_validate_payload_bool_not_integer():
    gv = GraphValidator()
    errors = gv._validate_payload({"a": True}, {"type": "object", "properties": {"a": {"type": "integer"}}})
    assert any("expected type 'integer'" in e for e in errors)


def test_validate_payload_bool_not_number():
    gv = GraphValidator()
    errors = gv._validate_payload({"a": True}, {"type": "object", "properties": {"a": {"type": "number"}}})
    assert any("expected type 'number'" in e for e in errors)


def test_validate_payload_integer_ok_for_number():
    gv = GraphValidator()
    errors = gv._validate_payload({"a": 5}, {"type": "object", "properties": {"a": {"type": "number"}}})
    assert errors == []


def test_validate_payload_missing_required_field():
    gv = GraphValidator()
    errors = gv._validate_payload(
        {"a": 1},
        {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "required": ["b"]},
    )
    assert any("Missing required field 'b'" in e for e in errors)


def test_validate_payload_required_field_present():
    gv = GraphValidator()
    errors = gv._validate_payload(
        {"a": 1, "b": "x"},
        {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "required": ["b"]},
    )
    assert errors == []


# ---------------------------------------------------------------------------
# Connector / model backend empty + invalid-id short-circuits
# ---------------------------------------------------------------------------


async def test_connector_bindings_empty_no_db_call():
    session = AsyncMock()
    result = ValidationResult()
    await GraphValidator()._check_connector_bindings([], session, result)
    assert result.is_valid
    session.execute.assert_not_called()


async def test_connector_bindings_invalid_ids_no_db_call():
    session = AsyncMock()
    result = ValidationResult()
    await GraphValidator()._check_connector_bindings(
        [{"node_id": "a", "connector_instance_id": "not-a-uuid"}],
        session,
        result,
    )
    assert result.is_valid
    session.execute.assert_not_called()


async def test_model_backends_empty_no_db_call():
    session = AsyncMock()
    result = ValidationResult()
    await GraphValidator()._check_model_backends([], session, result)
    assert result.is_valid
    session.execute.assert_not_called()


async def test_model_backends_invalid_ids_no_db_call():
    session = AsyncMock()
    result = ValidationResult()
    await GraphValidator()._check_model_backends(
        [{"node_id": "a", "model_backend_id": "not-a-uuid"}],
        session,
        result,
    )
    assert result.is_valid
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _check_llm_routing — edge_type fallback
# ---------------------------------------------------------------------------


def test_llm_routing_edge_type_fallback():
    """Outgoing edges keyed by 'edge_type' are recognized for LLM routing."""
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [{"id": "a", "routing_mode": "llm", "routing_prompt": "route", "default_target": "b"}],
        [{"source": "a", "target": "b", "edge_type": "reject"}],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_llm_routing_source_node_id_fallback():
    """Edges keyed by 'source_node_id' are attributed to the routing node."""
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [{"id": "a", "routing_mode": "llm", "routing_prompt": "route", "default_target": "b"}],
        [{"source_node_id": "a", "target": "b", "routing_label": "cont"}],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_llm_routing_edges_from_other_nodes_ignored():
    """Edges not originating from the routing node are ignored."""
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [{"id": "a", "routing_mode": "llm", "routing_prompt": "route", "default_target": "b"}],
        [{"source": "c", "target": "b", "routing_label": "cont"}],
        {"a", "b", "c"},
        result,
    )
    assert result.is_valid


async def test_deep_schema_skipped_edge_type_not_checked():
    """Reject edges are skipped by the deep schema check."""
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()
    session = _schema_version_session(
        [
            _schema_row(out_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
            _schema_row(in_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
        ]
    )
    result = ValidationResult()
    await GraphValidator()._check_schema_compatibility_deep(
        {
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "reject"}],
        },
        session,
        result,
    )
    assert result.is_valid


# ---------------------------------------------------------------------------
# Deep schema check — missing definitions skipped
# ---------------------------------------------------------------------------


async def test_deep_schema_missing_definition_skipped():
    """Edges whose schema definition could not be resolved are skipped."""
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()
    session = _schema_version_session(
        [
            _schema_row(out_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
            _schema_row(in_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
        ]
    )
    result = ValidationResult()
    await GraphValidator()._check_schema_compatibility_deep(
        {
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        session,
        result,
    )
    assert result.is_valid


async def test_deep_schema_resolution_missing_definition_skipped():
    """When one endpoint's schema version is missing, the edge is skipped."""
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()
    session = _schema_version_session(
        [
            _schema_row(out_id, {"type": "object", "properties": {"x": {"type": "string"}}}),
            # in_id version not found -> definitions.get(in_id) == {}
        ]
    )
    result = ValidationResult()
    await GraphValidator()._check_schema_compatibility_deep(
        {
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        session,
        result,
    )
    assert result.is_valid


# ---------------------------------------------------------------------------
# Nullable handling — single output type not in input type list
# ---------------------------------------------------------------------------


def test_single_output_type_not_in_input_type_list():
    """A single output type absent from an input type list is rejected."""
    gv = GraphValidator()
    errors = gv._check_schema_fields({"type": "integer"}, {"type": ["string", "number"]})
    assert any("not in input types" in e for e in errors)


# ---------------------------------------------------------------------------
# Composite node with a mix of refs and no-refs
# ---------------------------------------------------------------------------


async def test_composite_mixed_ref_and_no_ref_nodes():
    """A graph with one valid ref and one ref-less composite node is fine."""
    valid_ref = uuid.uuid4()
    template = MagicMock()
    template.id = valid_ref
    template.parameter_ports_json = []
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [template]
    exc = MagicMock()
    exc.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exc)

    result = ValidationResult()
    await GraphValidator()._check_composite_nodes(
        {
            "nodes": [
                {"id": "n1", "node_type": "composite", "composite_ref": str(valid_ref)},
                {"id": "n2", "node_type": "composite"},
            ],
            "edges": [],
        },
        session,
        result,
    )
    assert result.is_valid


# ---------------------------------------------------------------------------
# try_parse_uuids / validate_node_category
# ---------------------------------------------------------------------------


def test_try_parse_uuids_separates_valid_and_invalid():
    good = uuid.uuid4()
    valid, invalid = try_parse_uuids([str(good), "bad", None, 123])
    assert valid == {good}
    assert len(invalid) == 3


# ---------------------------------------------------------------------------
# source_node_id fallback on edges
# ---------------------------------------------------------------------------


async def test_conditional_edge_uses_source_node_id_fallback():
    """Conditional edges without 'source' fall back to 'source_node_id'."""
    result = ValidationResult()
    GraphValidator._check_jmespath_conditional(
        {
            "source_node_id": "a",
            "target": "b",
            "type": "conditional",
            "condition_expression": "",
        },
        result,
    )
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "CONDITION_MISSING_EXPRESSION")
    assert issue.node_id == "a"


async def test_hitl_eval_condition_uses_source_node_id_fallback():
    """HITL eval-condition errors fall back to 'source_node_id'."""
    result = ValidationResult()
    GraphValidator._check_hitl_eval_condition(
        {
            "source_node_id": "a",
            "target": "b",
            "type": "normal",
            "hitl_gate_config": {"eval_condition": {"threshold": 0.5, "operator": "gte"}},
        },
        result,
    )
    issue = next(i for i in result.issues if i.code == "HITL_EVAL_CONDITION_MISSING_NAME")
    assert issue.node_id == "a"


# ---------------------------------------------------------------------------
# Mixed valid + invalid connector / model backend ids
# ---------------------------------------------------------------------------


async def test_connector_binding_mixed_valid_and_invalid():
    """Invalid connector ids are skipped while valid ones are still checked."""
    good_id = uuid.uuid4()
    conn = MagicMock()
    conn.id = good_id
    conn.name = "conn"
    conn.status = "active"
    conn.allowed_operations = []
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [conn]
    exc = MagicMock()
    exc.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exc)

    result = ValidationResult()
    await GraphValidator()._check_connector_bindings(
        [
            {"node_id": "a", "connector_instance_id": str(good_id), "required_operations": []},
            {"node_id": "b", "connector_instance_id": "not-a-uuid", "required_operations": []},
        ],
        session,
        result,
    )
    assert result.is_valid


async def test_model_backend_mixed_valid_and_invalid():
    """Invalid model backend ids are skipped while valid ones are still checked."""
    good_id = uuid.uuid4()
    backend = MagicMock()
    backend.id = good_id
    backend.name = "backend"
    backend.status = "active"
    backend.last_health_check_error = None
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [backend]
    exc = MagicMock()
    exc.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exc)

    result = ValidationResult()
    await GraphValidator()._check_model_backends(
        [
            {"node_id": "a", "model_backend_id": str(good_id)},
            {"node_id": "b", "model_backend_id": "not-a-uuid"},
        ],
        session,
        result,
    )
    assert result.is_valid


# ---------------------------------------------------------------------------
# Composite node with node_type composite but no/invalid ref
# ---------------------------------------------------------------------------


async def test_composite_node_without_ref_skipped():
    """A composite-typed node with no composite_ref is skipped silently."""
    session = AsyncMock()
    result = ValidationResult()
    await GraphValidator()._check_composite_nodes(
        {"nodes": [{"id": "n1", "node_type": "composite"}], "edges": []},
        session,
        result,
    )
    assert result.is_valid
    session.execute.assert_not_called()


async def test_composite_node_invalid_ref_skipped_in_second_pass():
    """Invalid-ref nodes are skipped during template lookup (no crash)."""
    valid_ref = uuid.uuid4()
    template = MagicMock()
    template.id = valid_ref
    template.parameter_ports_json = []
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [template]
    exc = MagicMock()
    exc.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exc)

    result = ValidationResult()
    await GraphValidator()._check_composite_nodes(
        {
            "nodes": [
                {"id": "n1", "node_type": "composite", "composite_ref": str(valid_ref)},
                {"id": "n2", "node_type": "composite", "composite_ref": "not-a-uuid"},
            ],
            "edges": [],
        },
        session,
        result,
    )
    assert "COMPOSITE_INVALID_REF" in {i.code for i in result.issues}
    assert "COMPOSITE_TEMPLATE_NOT_FOUND" not in {i.code for i in result.issues}


async def test_validate_node_category_no_category_id():
    session = AsyncMock()
    result = await validate_node_category({"id": "n1"}, None, session)
    assert result.is_valid
    session.execute.assert_not_called()


async def test_validate_node_category_invalid_id():
    session = AsyncMock()
    result = await validate_node_category({"id": "n1"}, "not-a-uuid", session)
    assert not result.is_valid
    invalid = [i for i in result.issues if i.code == "CATEGORY_INVALID_ID"]
    assert invalid
    assert invalid[0].node_id == "n1"
    assert "not-a-uuid" in invalid[0].message
    session.execute.assert_not_called()


async def test_validate_node_category_not_found():
    cat_id = uuid.uuid4()
    session = AsyncMock()
    exc = MagicMock()
    exc.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=exc)
    result = await validate_node_category({"id": "n1"}, str(cat_id), session)
    assert not result.is_valid
    missing = [i for i in result.issues if i.code == "CATEGORY_NOT_FOUND"]
    assert missing
    assert missing[0].node_id == "n1"
    assert str(cat_id) in missing[0].message


async def test_validate_node_category_found():
    cat_id = uuid.uuid4()
    cat = MagicMock()
    cat.id = cat_id
    session = AsyncMock()
    exc = MagicMock()
    exc.scalar_one_or_none.return_value = cat
    session.execute = AsyncMock(return_value=exc)
    result = await validate_node_category({"id": "n1"}, str(cat_id), session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_row(schema_id: uuid.UUID, definition_json: dict[str, Any]) -> MagicMock:
    sv = MagicMock()
    sv.schema_id = schema_id
    sv.version = "1.0"
    sv.definition_json = definition_json
    return sv


def _schema_version_session(rows: list[MagicMock]) -> AsyncMock:
    session = AsyncMock()
    _rows = list(rows)

    def _exec(*_a, **_kw):
        r = MagicMock()
        row = _rows.pop(0) if _rows else None
        r.scalar_one_or_none.return_value = row
        r.scalar_one.return_value = row
        return r

    session.execute = AsyncMock(side_effect=_exec)
    return session
