"""Unit tests for composite node and output-validation checks.

Covers ``_check_composite_nodes`` (COMPOSITE_* codes) and the
output-validation helpers (``_check_output_validation``, ``_check_regex_eval``,
``_check_json_schema_eval``).
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


def _template(
    template_id: uuid.UUID,
    *,
    parameter_ports: list[dict[str, Any]] | None = None,
) -> MagicMock:
    t = MagicMock()
    t.id = template_id
    t.parameter_ports_json = parameter_ports or []
    return t


def _node(nid: str, *, composite_ref: str | None = None, **extra) -> dict:
    node = {"id": nid, "node_type": "composite"}
    if composite_ref is not None:
        node["composite_ref"] = composite_ref
    node.update(extra)
    return node


def _session_with_templates(rows: list[MagicMock]) -> AsyncMock:
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    exc = MagicMock()
    exc.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exc)
    return session


async def _run(graph_json: dict[str, Any], session: AsyncMock) -> ValidationResult:
    result = ValidationResult()
    await GraphValidator()._check_composite_nodes(graph_json, session, result)
    return result


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


async def test_no_composite_nodes_skipped():
    graph = {"nodes": [{"id": "n1", "node_type": "agent"}], "edges": []}
    session = AsyncMock()
    result = await _run(graph, session)
    assert result.is_valid
    session.execute.assert_not_called()


async def test_empty_nodes_skipped():
    result = await _run({"nodes": [], "edges": []}, AsyncMock())
    assert result.is_valid


# ---------------------------------------------------------------------------
# Invalid ref
# ---------------------------------------------------------------------------


async def test_invalid_composite_ref_is_error():
    graph = {"nodes": [_node("n1", composite_ref="not-a-uuid")], "edges": []}
    session = AsyncMock()
    result = await _run(graph, session)
    assert "COMPOSITE_INVALID_REF" in _codes(result)
    assert not result.is_valid
    session.execute.assert_not_called()


async def test_invalid_ref_reports_node():
    graph = {"nodes": [_node("my-node", composite_ref="bad")], "edges": []}
    session = AsyncMock()
    result = await _run(graph, session)
    issue = next(i for i in result.issues if i.code == "COMPOSITE_INVALID_REF")
    assert issue.node_id == "my-node"


# ---------------------------------------------------------------------------
# Template not found
# ---------------------------------------------------------------------------


async def test_template_not_found_is_error():
    ref = uuid.uuid4()
    graph = {"nodes": [_node("n1", composite_ref=str(ref))], "edges": []}
    session = _session_with_templates([])
    result = await _run(graph, session)
    assert "COMPOSITE_TEMPLATE_NOT_FOUND" in _codes(result)
    assert not result.is_valid


async def test_template_not_found_reports_node_and_ref():
    ref = uuid.uuid4()
    graph = {"nodes": [_node("n1", composite_ref=str(ref))], "edges": []}
    session = _session_with_templates([])
    result = await _run(graph, session)
    issue = next(i for i in result.issues if i.code == "COMPOSITE_TEMPLATE_NOT_FOUND")
    assert issue.node_id == "n1"
    assert str(ref) in issue.message


# ---------------------------------------------------------------------------
# Missing required parameter
# ---------------------------------------------------------------------------


async def test_missing_required_parameter_is_error():
    ref = uuid.uuid4()
    graph = {"nodes": [_node("n1", composite_ref=str(ref))], "edges": []}
    session = _session_with_templates([_template(ref, parameter_ports=[{"name": "query", "required": True}])])
    result = await _run(graph, session)
    assert "COMPOSITE_MISSING_PARAMETER" in _codes(result)
    assert not result.is_valid


async def test_required_parameter_provided_is_valid():
    ref = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", composite_ref=str(ref), composite_parameter_values={"query": "find"}),
        ],
        "edges": [],
    }
    session = _session_with_templates([_template(ref, parameter_ports=[{"name": "query", "required": True}])])
    result = await _run(graph, session)
    assert result.is_valid


async def test_optional_parameter_absent_is_valid():
    ref = uuid.uuid4()
    graph = {"nodes": [_node("n1", composite_ref=str(ref))], "edges": []}
    session = _session_with_templates([_template(ref, parameter_ports=[{"name": "query", "required": False}])])
    result = await _run(graph, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Output validation — retries range
# ---------------------------------------------------------------------------


_REF = uuid.uuid4()


def _composite_with_output_validation(ov: dict[str, Any]) -> dict:
    return _node("n1", composite_ref=str(_REF), output_validation=ov)


def _template_session() -> AsyncMock:
    return _session_with_templates([_template(_REF)])


async def test_output_validation_retries_too_high():
    graph = {
        "nodes": [
            _composite_with_output_validation({"max_validation_retries": 6, "eval_definitions": []}),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_RETRIES_RANGE" in _codes(result)


async def test_output_validation_retries_negative():
    graph = {
        "nodes": [
            _composite_with_output_validation({"max_validation_retries": -1, "eval_definitions": []}),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_RETRIES_RANGE" in _codes(result)


async def test_output_validation_retries_bool():
    graph = {
        "nodes": [
            _composite_with_output_validation({"max_validation_retries": True, "eval_definitions": []}),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_RETRIES_RANGE" in _codes(result)


async def test_output_validation_retries_valid():
    graph = {
        "nodes": [
            _composite_with_output_validation({"max_validation_retries": 3, "eval_definitions": []}),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_RETRIES_RANGE" not in _codes(result)


# ---------------------------------------------------------------------------
# Output validation — eval type / behaviour
# ---------------------------------------------------------------------------


async def test_output_validation_invalid_eval_type():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "name": "e1", "type": "rubber_stamp"}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_INVALID_TYPE" in _codes(result)


async def test_output_validation_invalid_behaviour():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "name": "e1", "type": "regex", "failure_behaviour": "explode"}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_INVALID_BEHAVIOUR" in _codes(result)


async def test_output_validation_valid_type_and_behaviour():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {
                    "eval_definitions": [
                        {
                            "id": "e1",
                            "name": "e1",
                            "type": "regex",
                            "failure_behaviour": "retry",
                            "config": {"field": "result", "pattern": "^ok$"},
                        }
                    ]
                }
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Output validation — regex evals
# ---------------------------------------------------------------------------


async def test_regex_eval_missing_field():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "regex", "config": {"pattern": "^ok$"}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_REGEX_NO_FIELD" in _codes(result)


async def test_regex_eval_missing_pattern():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "regex", "config": {"field": "result"}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_REGEX_NO_PATTERN" in _codes(result)


async def test_regex_eval_empty_pattern():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "regex", "config": {"field": "r", "pattern": ""}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_REGEX_NO_PATTERN" in _codes(result)


async def test_regex_eval_invalid_pattern():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "regex", "config": {"field": "r", "pattern": "["}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_REGEX_INVALID" in _codes(result)


async def test_regex_eval_non_string_pattern():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "regex", "config": {"field": "r", "pattern": 123}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_REGEX_INVALID_TYPE" in _codes(result)


async def test_regex_eval_valid_config():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "regex", "config": {"field": "r", "pattern": "^ok$"}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Output validation — json_schema evals
# ---------------------------------------------------------------------------


async def test_json_schema_eval_missing_schema():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "json_schema", "config": {}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_SCHEMA_MISSING" in _codes(result)


async def test_json_schema_eval_with_inline_schema():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "json_schema", "config": {"schema": {"type": "object"}}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_SCHEMA_MISSING" not in _codes(result)


async def test_json_schema_eval_with_schema_ref():
    graph = {
        "nodes": [
            _composite_with_output_validation(
                {"eval_definitions": [{"id": "e1", "type": "json_schema", "config": {"schema_ref": "abc"}}]}
            ),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert "COMPOSITE_VALIDATION_SCHEMA_MISSING" not in _codes(result)


# ---------------------------------------------------------------------------
# eval_definitions not a list
# ---------------------------------------------------------------------------


async def test_eval_definitions_not_list_skipped():
    graph = {
        "nodes": [
            _composite_with_output_validation({"eval_definitions": "oops"}),
        ],
        "edges": [],
    }
    session = _template_session()
    result = await _run(graph, session)
    assert result.is_valid
    assert not result.issues


# ---------------------------------------------------------------------------
# validate() end-to-end integration
# ---------------------------------------------------------------------------


async def test_validate_includes_composite_check():
    ref = uuid.uuid4()
    snap = MagicMock()
    snap.graph_json = {
        "nodes": [_node("n1", composite_ref=str(ref))],
        "edges": [],
    }
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = None
    session = _session_with_templates([])

    result = await GraphValidator().validate(snap, session)
    assert "COMPOSITE_TEMPLATE_NOT_FOUND" in _codes(result)
    assert not result.is_valid
