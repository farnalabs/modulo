"""Unit tests for GraphValidator."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator, ValidationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(
    graph_json: dict[str, Any] | None = None,
    schema_pins: list[dict[str, Any]] | None = None,
    connector_bindings: list[dict[str, Any]] | None = None,
    model_backend_pins: list[dict[str, Any]] | None = None,
) -> MagicMock:
    snap = MagicMock()
    snap.graph_json = graph_json or {"nodes": [], "edges": []}
    snap.schema_pins_json = schema_pins or []
    snap.connector_bindings_json = connector_bindings or []
    snap.model_backend_pins_json = model_backend_pins or []
    return snap


def _session_returning(rows: list[Any]) -> AsyncMock:
    """Mock session whose execute() returns the given rows via .scalars().all()."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _connector_instance(
    cid: uuid.UUID, *, status: str = "active", allowed_operations: list[str] | None = None
) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.name = f"conn-{cid}"
    c.status = status
    c.allowed_operations = allowed_operations or []
    return c


def _model_backend(
    bid: uuid.UUID, *, status: str = "active", last_health_check_error: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id = bid
    m.name = f"backend-{bid}"
    m.status = status
    m.last_health_check_error = last_health_check_error
    return m


_SIMPLE_GRAPH: dict[str, Any] = {
    "nodes": [{"id": "a"}, {"id": "b"}],
    "edges": [{"source": "a", "target": "b", "type": "normal"}],
}

_SINGLE_NODE: dict[str, Any] = {"nodes": [{"id": "a"}], "edges": []}


# ---------------------------------------------------------------------------
# Topology — happy path
# ---------------------------------------------------------------------------


async def test_topology_valid_linear_graph():
    snap = _snapshot(graph_json=_SIMPLE_GRAPH)
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid
    assert not result.issues


async def test_topology_single_node_no_edges():
    snap = _snapshot(graph_json=_SINGLE_NODE)
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Topology — errors
# ---------------------------------------------------------------------------


async def test_topology_no_nodes_is_error():
    snap = _snapshot(graph_json={"nodes": [], "edges": []})
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_NO_NODES" for i in result.issues)


async def test_topology_edge_unknown_source():
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}],
            "edges": [{"source": "x", "target": "a"}],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_UNKNOWN_SOURCE" for i in result.issues)


async def test_topology_edge_unknown_target():
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}],
            "edges": [{"source": "a", "target": "z"}],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_UNKNOWN_TARGET" for i in result.issues)


async def test_topology_cycle_detected():
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "a"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_CYCLE" for i in result.issues)


async def test_topology_unreachable_node_is_warning():
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [{"source": "a", "target": "b"}],
            # "c" has no incoming or outgoing edges from entry — but it's a
            # separate root, so it would only be flagged if unreachable from
            # the entry. Since "a" is entry, "c" is unreachable.
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid  # warnings don't make it invalid
    unreachable = [i for i in result.issues if i.code == "TOPOLOGY_UNREACHABLE"]
    assert len(unreachable) == 1
    assert unreachable[0].node_id == "c"


async def test_topology_reject_edges_excluded_from_reachability():
    """Reject edges are skip-listed from reachability (handled in phase3)."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "a", "target": "c", "type": "reject"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    # "c" is only reachable via a reject edge — should be a warning
    unreachable = [i for i in result.issues if i.code == "TOPOLOGY_UNREACHABLE"]
    assert any(i.node_id == "c" for i in unreachable)


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------


async def test_schema_compatible_edge_no_issue():
    schema_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": schema_id},
            {"node_id": "b", "direction": "input", "schema_id": schema_id},
        ],
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid
    assert not any(i.code == "SCHEMA_INCOMPATIBLE" for i in result.issues)


async def test_schema_incompatible_edge_is_error():
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": str(uuid.uuid4())},
            {"node_id": "b", "direction": "input", "schema_id": str(uuid.uuid4())},
        ],
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "SCHEMA_INCOMPATIBLE" for i in result.issues)


async def test_schema_missing_pins_skipped():
    """If schema pins are absent for a node, that edge is skipped (not an error)."""
    snap = _snapshot(graph_json=_SIMPLE_GRAPH, schema_pins=[])
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_schema_reject_edges_excluded():
    """Schema compatibility is not checked on reject edges."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source": "a", "target": "b", "type": "reject"}],
        },
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": str(uuid.uuid4())},
            {"node_id": "b", "direction": "input", "schema_id": str(uuid.uuid4())},
        ],
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid  # reject edge not schema-checked


# ---------------------------------------------------------------------------
# Connector bindings
# ---------------------------------------------------------------------------


async def test_connector_binding_active_is_valid():
    cid = uuid.uuid4()
    instance = _connector_instance(cid, status="active", allowed_operations=["read"])
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        connector_bindings=[
            {
                "node_id": "a",
                "connector_instance_id": str(cid),
                "required_operations": ["read"],
            }
        ],
    )
    session = _session_returning([instance])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_connector_not_found_is_error():
    cid = uuid.uuid4()
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        connector_bindings=[{"node_id": "a", "connector_instance_id": str(cid), "required_operations": []}],
    )
    session = _session_returning([])  # no rows returned
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONNECTOR_NOT_FOUND" for i in result.issues)


async def test_connector_inactive_is_error():
    cid = uuid.uuid4()
    instance = _connector_instance(cid, status="disabled")
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        connector_bindings=[{"node_id": "a", "connector_instance_id": str(cid), "required_operations": []}],
    )
    session = _session_returning([instance])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONNECTOR_INACTIVE" for i in result.issues)


async def test_connector_missing_operations_is_error():
    cid = uuid.uuid4()
    instance = _connector_instance(cid, status="active", allowed_operations=["read"])
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        connector_bindings=[
            {
                "node_id": "a",
                "connector_instance_id": str(cid),
                "required_operations": ["read", "write"],
            }
        ],
    )
    session = _session_returning([instance])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONNECTOR_MISSING_OPERATIONS" for i in result.issues)


async def test_connector_empty_bindings_skipped():
    snap = _snapshot(graph_json=_SINGLE_NODE, connector_bindings=[])
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Model backend health
# ---------------------------------------------------------------------------


async def test_model_backend_active_is_valid():
    bid = uuid.uuid4()
    backend = _model_backend(bid, status="active")
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([backend])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_model_backend_not_found_is_error():
    bid = uuid.uuid4()
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "MODEL_BACKEND_NOT_FOUND" for i in result.issues)


async def test_model_backend_inactive_is_error():
    bid = uuid.uuid4()
    backend = _model_backend(bid, status="disabled")
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([backend])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "MODEL_BACKEND_INACTIVE" for i in result.issues)


async def test_model_backend_empty_pins_skipped():
    snap = _snapshot(graph_json=_SINGLE_NODE, model_backend_pins=[])
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_model_backend_healthy_no_error():
    bid = uuid.uuid4()
    backend = _model_backend(bid, status="active", last_health_check_error=None)
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([backend])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_model_backend_healthy_empty_error():
    bid = uuid.uuid4()
    backend = _model_backend(bid, status="active", last_health_check_error="")
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([backend])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_model_backend_unhealthy_blocks():
    bid = uuid.uuid4()
    backend = _model_backend(bid, status="active", last_health_check_error="Connection refused")
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([backend])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "MODEL_BACKEND_UNHEALTHY" for i in result.issues)
    health_issue = next(i for i in result.issues if i.code == "MODEL_BACKEND_UNHEALTHY")
    assert f"Model backend '{backend.name}' (id={bid})" in health_issue.message
    assert "Connection refused" in health_issue.message


async def test_model_backend_unhealthy_in_run_validation():
    bid = uuid.uuid4()
    backend = _model_backend(bid, status="active", last_health_check_error="Timeout")
    snap = _snapshot(
        graph_json=_SINGLE_NODE,
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = _session_returning([backend])
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "MODEL_BACKEND_UNHEALTHY" for i in result.issues)


# ---------------------------------------------------------------------------
# ValidationResult helpers
# ---------------------------------------------------------------------------


def test_validation_result_is_valid_with_only_warnings():
    r = ValidationResult()
    r.warning("W001", "minor thing")
    assert r.is_valid


def test_validation_result_is_invalid_with_error():
    r = ValidationResult()
    r.error("E001", "bad thing")
    assert not r.is_valid


def test_validation_result_collects_multiple_issues():
    r = ValidationResult()
    r.error("E001", "first")
    r.warning("W001", "second")
    r.error("E002", "third")
    assert len(r.issues) == 3


# ---------------------------------------------------------------------------
# Topology short-circuit on errors
# ---------------------------------------------------------------------------


async def test_topology_errors_prevent_further_checks():
    """When topology fails, connector/backend checks are skipped (no extra DB calls)."""
    bid = uuid.uuid4()
    snap = _snapshot(
        graph_json={"nodes": [], "edges": []},  # topology error
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = AsyncMock()
    session.execute = AsyncMock()
    await GraphValidator().validate(snap, session)

    # session.execute should NOT have been called — topology failed first
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Nesting depth
# ---------------------------------------------------------------------------


async def test_nesting_depth_within_limit():
    """Depth 2 (a→b→c) is within max depth of 3."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "b", "target": "c", "type": "normal"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid
    assert not any(i.code == "TOPOLOGY_NESTING_EXCEEDED" for i in result.issues)


async def test_nesting_depth_exactly_max():
    """Depth 3 (a→b→c→d) is at the max limit (MAX_NESTING_DEPTH)."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "b", "target": "c", "type": "normal"},
                {"source": "c", "target": "d", "type": "normal"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid  # depth=3 <= max(3), allowed


async def test_nesting_depth_exceeded_is_error():
    """Depth 4 (a→b→c→d→e) exceeds max depth of 3."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}, {"id": "e"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "b", "target": "c", "type": "normal"},
                {"source": "c", "target": "d", "type": "normal"},
                {"source": "d", "target": "e", "type": "normal"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_NESTING_EXCEEDED" for i in result.issues)


# ---------------------------------------------------------------------------
# Kickback edges
# ---------------------------------------------------------------------------


async def test_kickback_edge_does_not_create_cycle():
    """Kickback edges are excluded from topology flow, so they don't create cycles."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "b", "target": "a", "type": "kickback"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid
    assert not any(i.code == "TOPOLOGY_CYCLE" for i in result.issues)


async def test_kickback_edge_to_self_is_not_cycle():
    """A self-loop kickback edge is excluded from topology."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}],
            "edges": [
                {"source": "a", "target": "a", "type": "kickback"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_kickback_edge_excluded_from_schema_check():
    """Schema compatibility is not checked on kickback edges."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source": "a", "target": "b", "type": "kickback"}],
        },
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": str(uuid.uuid4())},
            {"node_id": "b", "direction": "input", "schema_id": str(uuid.uuid4())},
        ],
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Deep schema compatibility (field presence + type)
# ---------------------------------------------------------------------------


def _schema_version_row(
    schema_id: uuid.UUID,
    definition_json: dict[str, Any],
    *,
    version_number: int = 1,
    published: bool = True,
) -> MagicMock:
    sv = MagicMock()
    sv.schema_id = schema_id
    sv.version_number = version_number
    sv.definition_json = definition_json
    sv.published = published
    sv.version = "1.0"
    return sv


def _session_returning_schema_versions(rows: list[MagicMock]) -> AsyncMock:
    """Mock session whose execute returns SchemaVersion-like rows."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


async def test_schema_field_presence_valid():
    """Output schema fields are all present in input schema."""
    shared_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": shared_id},
            {"node_id": "b", "direction": "input", "schema_id": shared_id},
        ],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(shared_id),
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
                },
            )
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert result.is_valid
    assert not any(i.code.startswith("SCHEMA_") for i in result.issues)


async def test_schema_missing_field_is_error():
    """Output schema field not present in input schema is an error."""
    out_id = str(uuid.uuid4())
    in_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": out_id},
            {"node_id": "b", "direction": "input", "schema_id": in_id},
        ],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(out_id),
                {"type": "object", "properties": {"secret_field": {"type": "string"}}},
            ),
            _schema_version_row(
                uuid.UUID(in_id),
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ),
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "SCHEMA_MISSING_FIELD" for i in result.issues)


async def test_schema_field_type_mismatch_is_error():
    """Output field type different from input field type is an error."""
    out_id = str(uuid.uuid4())
    in_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "output", "schema_id": out_id},
            {"node_id": "b", "direction": "input", "schema_id": in_id},
        ],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(out_id),
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ),
            _schema_version_row(
                uuid.UUID(in_id),
                {"type": "object", "properties": {"name": {"type": "integer"}}},
            ),
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "SCHEMA_FIELD_TYPE_MISMATCH" for i in result.issues)


# ---------------------------------------------------------------------------
# Input payload validation
# ---------------------------------------------------------------------------


async def test_input_payload_matches_entry_schema():
    """Valid input payload against entry node schema is ok."""
    schema_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "input", "schema_id": schema_id},
        ],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(schema_id),
                {"type": "object", "properties": {"name": {"type": "string"}}},
            )
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {"name": "hello"}, session)
    assert result.is_valid


async def test_input_payload_missing_field_is_error():
    """Missing required field in input payload is an error."""
    schema_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "input", "schema_id": schema_id},
        ],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(schema_id),
                {"type": "object", "properties": {"name": {"type": "string"}}},
            )
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "INPUT_MISSING_FIELD" for i in result.issues)


async def test_input_payload_type_mismatch_is_error():
    """Wrong type for input payload field is an error."""
    schema_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[
            {"node_id": "a", "direction": "input", "schema_id": schema_id},
        ],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(schema_id),
                {"type": "object", "properties": {"count": {"type": "integer"}}},
            )
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {"count": "not_an_int"}, session)
    assert not result.is_valid
    assert any(i.code == "INPUT_FIELD_TYPE_MISMATCH" for i in result.issues)


async def test_input_payload_no_schema_pins_skipped():
    """If entry node has no schema pins, input validation is skipped."""
    snap = _snapshot(
        graph_json=_SIMPLE_GRAPH,
        schema_pins=[],
    )
    session = _session_returning_schema_versions([])
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert result.is_valid


async def test_validate_for_run_blocks_on_topology_error():
    """validate_for_run returns early on topology error (no DB calls)."""
    bid = uuid.uuid4()
    snap = _snapshot(
        graph_json={"nodes": [], "edges": []},
        model_backend_pins=[{"node_id": "a", "model_backend_id": str(bid)}],
    )
    session = AsyncMock()
    session.execute = AsyncMock()
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    session.execute.assert_not_called()


async def test_validate_for_run_skips_warnings():
    """validate_for_run does not return warnings (only errors matter for blocking)."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [{"source": "a", "target": "b"}],
        },
        schema_pins=[],
    )
    session = _session_returning([])
    result = await GraphValidator().validate_for_run(snap, {}, session)
    # Unreachable node "c" is a warning, not an error — should not block
    assert result.is_valid
    assert not any(i.code == "TOPOLOGY_UNREACHABLE" for i in result.issues)


# ---------------------------------------------------------------------------
# Conditional edge expression validation
# ---------------------------------------------------------------------------


async def test_conditional_edge_valid_expression():
    """A valid JMESPath expression on a conditional edge is accepted."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {
                    "source": "a",
                    "target": "b",
                    "type": "conditional",
                    "condition_expression": "artifacts[-1].status == 'passed'",
                },
                {"source": "a", "target": "c", "type": "normal"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid
    assert not any(i.code.startswith("CONDITION_") for i in result.issues)


async def test_conditional_edge_missing_expression_is_error():
    """A conditional edge without a condition_expression is an error."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONDITION_MISSING_EXPRESSION" for i in result.issues)


async def test_conditional_edge_empty_expression_is_error():
    """An empty condition_expression is also rejected."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional", "condition_expression": ""},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONDITION_MISSING_EXPRESSION" for i in result.issues)


async def test_conditional_edge_invalid_jmespath_is_error():
    """An unparseable JMESPath expression is rejected."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional", "condition_expression": "artifacts[[].broken"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONDITION_INVALID_EXPRESSION" for i in result.issues)


async def test_conditional_edge_does_not_create_cycle():
    """Conditional edges are forwarding edges and do contribute to topology flow."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional", "condition_expression": "true"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid


async def test_conditional_edge_whitespace_only_expression_is_error():
    """A condition_expression containing only whitespace is treated as missing."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional", "condition_expression": "   "},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONDITION_MISSING_EXPRESSION" for i in result.issues)


async def test_conditional_edge_mixed_valid_and_invalid():
    """Multiple conditional edges: only the invalid one raises an error."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional", "condition_expression": "true"},
                {"source": "a", "target": "c", "type": "conditional", "condition_expression": "artifacts[[].broken"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CONDITION_INVALID_EXPRESSION" for i in result.issues)


async def test_conditional_edge_normal_edges_still_checked():
    """Normal edges alongside conditional edges still participate in topology."""
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"source": "a", "target": "b", "type": "conditional", "condition_expression": "true"},
                {"source": "a", "target": "c", "type": "normal"},
            ],
        }
    )
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert result.is_valid
