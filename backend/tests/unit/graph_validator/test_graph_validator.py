"""Unit tests for GraphValidator."""

import sys
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    cid: uuid.UUID,
    *,
    status: str = "active",
    allowed_operations: list[str] | None = None,
    config_json: dict[str, Any] | None = None,
) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.name = f"conn-{cid}"
    c.status = status
    c.allowed_operations = allowed_operations or []
    c.config_json = config_json or {}
    return c


def _model_backend(
    bid: uuid.UUID,
    *,
    status: str = "active",
    last_health_check_error: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id = bid
    m.name = f"backend-{bid}"
    m.status = status
    m.last_health_check_error = last_health_check_error
    return m


_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_SIMPLE_GRAPH: dict[str, Any] = {
    "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
    "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
}

_SINGLE_NODE: dict[str, Any] = {"nodes": [{"id": _UUID_A}], "edges": []}


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


@pytest.mark.parametrize(
    ("graph_json", "expected_code"),
    [
        ({"nodes": [], "edges": []}, "TOPOLOGY_NO_NODES"),
        ({"nodes": [{"id": "a"}], "edges": [{"source": "x", "target": "a"}]}, "TOPOLOGY_UNKNOWN_SOURCE"),
        ({"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "z"}]}, "TOPOLOGY_UNKNOWN_TARGET"),
        (
            {
                "nodes": [{"id": "a"}, {"id": "b"}],
                "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
            },
            "TOPOLOGY_CYCLE",
        ),
    ],
)
async def test_topology_errors(graph_json, expected_code):
    snap = _snapshot(graph_json=graph_json)
    session = _session_returning([])
    result = await GraphValidator().validate(snap, session)
    assert not result.is_valid
    assert any(i.code == expected_code for i in result.issues)


async def test_topology_null_source_uses_missing_value_marker():
    snap = _snapshot(
        graph_json={
            "nodes": [{"id": "a"}],
            "edges": [{"source": None, "target": "a"}],
        }
    )
    result = await GraphValidator().validate(snap, _session_returning([]))

    issue = next(i for i in result.issues if i.code == "TOPOLOGY_UNKNOWN_SOURCE")
    assert "'?'" in issue.message
    assert "None" not in issue.message


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
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    snap = _snapshot(
        graph_json={
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": sid_a, "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": sid_b, "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
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


def test_schema_compatibility_matching_schemas_no_issue():
    """An edge whose output and input pins resolve to the same schema is valid."""
    schema_id = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"id": "a", "output_schema_pin": {"schema_id": schema_id, "schema_version": "1.0"}},
            {"id": "b", "input_schema_pin": {"schema_id": schema_id, "schema_version": "1.0"}},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    result = ValidationResult()
    GraphValidator()._check_schema_compatibility(graph, result)
    assert result.is_valid
    assert not any(i.code == "SCHEMA_INCOMPATIBLE" for i in result.issues)


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


def test_nesting_depth_recursion_guard_caps_depth():
    """A path longer than the internal recursion budget caps depth at 1000.

    The ``_remaining`` guard in ``_max_depth`` returns 0 once the budget is
    exhausted so a pathological chain cannot recurse forever. The recursion
    limit is temporarily raised so the guard (not Python's own RecursionError)
    bounds the walk.
    """
    chain_len = 1005
    nodes = [{"id": str(i)} for i in range(chain_len)]
    edges = [{"source": str(i), "target": str(i + 1), "type": "normal"} for i in range(chain_len - 1)]
    adj: dict[str, list[str]] = {str(n["id"]): [] for n in nodes}
    for edge in edges:
        adj[str(edge["source"])].append(str(edge["target"]))

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(chain_len * 4)
    try:
        result = ValidationResult()
        GraphValidator()._check_nesting_depth(adj, ["0"], result)
    finally:
        sys.setrecursionlimit(old_limit)

    assert not result.is_valid
    depth_issue = next(i for i in result.issues if i.code == "TOPOLOGY_NESTING_EXCEEDED")
    assert "1000" in depth_issue.message


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
    """Mock session whose execute returns SchemaVersion-like rows via scalars().all()
    AND scalar_one_or_none() (both used by different code paths)."""
    session = AsyncMock()
    if not rows:
        exc_result = MagicMock()
        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        exc_result.scalars.return_value = scalars_result
        exc_result.scalar_one_or_none.return_value = None
        exc_result.scalar_one.return_value = None
        session.execute = AsyncMock(return_value=exc_result)
        return session
    if len(rows) == 1:
        exc_result = MagicMock()
        scalars_result = MagicMock()
        scalars_result.all.return_value = rows
        exc_result.scalars.return_value = scalars_result
        exc_result.scalar_one_or_none.return_value = rows[0]
        exc_result.scalar_one.return_value = rows[0]
        session.execute = AsyncMock(return_value=exc_result)
    else:
        _rows = list(rows)

        def _execute_side(*_a, **_kw):
            r = MagicMock()
            row = _rows.pop(0) if _rows else None
            r.scalar_one_or_none.return_value = row
            r.scalar_one.return_value = row
            s = MagicMock()
            s.all.return_value = [row] if row else []
            r.scalars.return_value = s
            return r

        session.execute = AsyncMock(side_effect=_execute_side)
    return session


async def test_schema_field_presence_valid():
    """Output schema fields are all present in input schema."""
    shared_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json={
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": shared_id, "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": shared_id, "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
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


async def test_schema_extra_field_triggers_incompatible_with_addl_props_false():
    """Extra output field + input with additionalProperties: false → error."""
    out_id = str(uuid.uuid4())
    in_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json={
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": out_id, "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": in_id, "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(out_id),
                {"type": "object", "properties": {"secret_field": {"type": "string"}}},
            ),
            _schema_version_row(
                uuid.UUID(in_id),
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "SCHEMA_FIELD_INCOMPATIBLE" for i in result.issues)


async def test_schema_field_type_mismatch_is_error():
    """Output field type different from input field type is an error."""
    out_id = str(uuid.uuid4())
    in_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json={
            "nodes": [
                {"id": "a", "output_schema_pin": {"schema_id": out_id, "schema_version": "1.0"}},
                {"id": "b", "input_schema_pin": {"schema_id": in_id, "schema_version": "1.0"}},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
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
    assert any(i.code == "SCHEMA_FIELD_INCOMPATIBLE" for i in result.issues)


# ---------------------------------------------------------------------------
# Input payload validation
# ---------------------------------------------------------------------------


async def test_input_payload_matches_entry_schema():
    """Valid input payload against entry node schema is ok."""
    schema_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json={
            "nodes": [
                {"id": "a", "input_schema_pin": {"schema_id": schema_id, "schema_version": "1.0"}},
                {"id": "b"},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
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
        graph_json={
            "nodes": [
                {"id": "a", "input_schema_pin": {"schema_id": schema_id, "schema_version": "1.0"}},
                {"id": "b"},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
    )
    session = _session_returning_schema_versions(
        [
            _schema_version_row(
                uuid.UUID(schema_id),
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            )
        ]
    )
    result = await GraphValidator().validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "INPUT_SCHEMA_MISMATCH" for i in result.issues)


async def test_input_payload_type_mismatch_is_error():
    """Wrong type for input payload field is an error."""
    schema_id = str(uuid.uuid4())
    snap = _snapshot(
        graph_json={
            "nodes": [
                {"id": "a", "input_schema_pin": {"schema_id": schema_id, "schema_version": "1.0"}},
                {"id": "b"},
            ],
            "edges": [{"source": "a", "target": "b", "type": "normal"}],
        },
        schema_pins=[],
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
    assert any(i.code == "INPUT_SCHEMA_MISMATCH" for i in result.issues)


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


# ---------------------------------------------------------------------------
# Guardrail per-node cap (FAR-223 item 7)
# ---------------------------------------------------------------------------


def _guardrail_row(node_id, name, *, cap=None):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    row.pipeline_id = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    row.node_id = node_id
    row.name = name
    row.eval_type = "guardrail"
    row.config_json = (
        {"action": "observe", "max_guardrails_per_node": cap} if cap is not None else {"action": "observe"}
    )
    row.failure_behaviour = "warn"
    row.pass_threshold = None
    row.suite_id = None
    return row


async def test_guardrail_cap_exceeded_rejected_at_graph_save():
    rows = [_guardrail_row(None, f"g-{i}") for i in range(9)]
    result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=rows
    )
    assert not result.is_valid
    assert any(i.code == "GUARDRAIL_CAP_EXCEEDED" for i in result.issues)


async def test_guardrail_cap_within_budget_passes_graph_save():
    rows = [_guardrail_row(None, f"g-{i}") for i in range(8)]
    result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=rows
    )
    assert result.is_valid
    assert not any(i.code == "GUARDRAIL_CAP_EXCEEDED" for i in result.issues)


async def test_guardrail_cap_feature_off_never_violates():
    rows = [_guardrail_row(None, f"g-{i}", cap=0) for i in range(12)]
    result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=rows
    )
    assert result.is_valid


async def test_guardrail_cap_configurable_cap_enforced_at_graph_save():
    """A RAISED org cap (not just the default 8) is honoured at graph-save:
    exactly cap rows pass, cap+1 rows reject with GUARDRAIL_CAP_EXCEEDED."""
    under = [_guardrail_row(None, f"g-{i}", cap=4) for i in range(4)]
    under_result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=under
    )
    assert under_result.is_valid
    assert not any(i.code == "GUARDRAIL_CAP_EXCEEDED" for i in under_result.issues)

    over = [_guardrail_row(None, f"g-{i}", cap=4) for i in range(5)]
    over_result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=over
    )
    assert not over_result.is_valid
    assert any(i.code == "GUARDRAIL_CAP_EXCEEDED" for i in over_result.issues)


async def test_guardrail_cap_no_rows_is_skipped():
    result = await GraphValidator().validate_definition(_SINGLE_NODE, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid


# ---------------------------------------------------------------------------
# Guardrail redact+correct hard-block (FAR-210 T2b)
# ---------------------------------------------------------------------------


def _redact_correction_row(node_id, name, *, action="redact", has_correction=True):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    row.pipeline_id = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    row.node_id = node_id
    row.name = name
    row.eval_type = "guardrail"
    config = {"action": action}
    if has_correction:
        config["correction"] = {"id": "corr-1", "model_backend_id": "mb-1"}
    row.config_json = config
    row.failure_behaviour = "warn"
    row.pass_threshold = None
    row.suite_id = None
    return row


async def test_redact_correct_hard_blocked_at_graph_save():
    """A 'correction' block on a 'redact'-action guardrail is rejected at
    graph-save (exfiltration channel) — matches the runtime
    RedactCorrectBlockedError backstop."""
    rows = [_redact_correction_row(None, "g-redact-correct")]
    result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=rows
    )
    assert not result.is_valid
    assert any(i.code == "REDACT_CORRECT_BLOCKED" for i in result.issues)


async def test_redact_without_correction_passes_graph_save():
    """A plain 'redact'-action guardrail (no correction block) is valid."""
    rows = [_redact_correction_row(None, "g-redact-only", has_correction=False)]
    result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=rows
    )
    assert result.is_valid
    assert not any(i.code == "REDACT_CORRECT_BLOCKED" for i in result.issues)


async def test_non_redact_correction_passes_graph_save():
    """A correction on a non-redact guardrail is NOT the exfiltration channel
    the hard-block protects — it stays valid at graph-save."""
    rows = [_redact_correction_row(None, "g-block-correct", action="block")]
    result = await GraphValidator().validate_definition(
        _SINGLE_NODE, _session_returning([]), guardrail_definitions=rows
    )
    assert result.is_valid
    assert not any(i.code == "REDACT_CORRECT_BLOCKED" for i in result.issues)


# ---------------------------------------------------------------------------
# Sandbox loop_intercept config (FAR-211 T3)
# ---------------------------------------------------------------------------


def _sandbox_node(**overrides: Any) -> dict[str, Any]:
    """A minimally-valid sandbox_agent node for graph validation."""
    node: dict[str, Any] = {
        "id": _UUID_A,
        "node_type": "sandbox_agent",
        "agent_command": "opencode run --format json",
        "agent_prompt": "Do the thing",
        "template_id": "opencode",
    }
    node.update(overrides)
    return node


async def test_loop_intercept_valid_config_passes_graph_save():
    graph = {"nodes": [_sandbox_node(loop_intercept={"enabled": True, "latency_budget_ms": 300})], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid
    assert not any(i.code == "SANDBOX_LOOP_INTERCEPT_MALFORMED" for i in result.issues)


async def test_loop_intercept_absent_passes_graph_save():
    """Absent config (the default) is not an error — interception is opt-in."""
    graph = {"nodes": [_sandbox_node()], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid


async def test_loop_intercept_disabled_false_passes_graph_save():
    graph = {"nodes": [_sandbox_node(loop_intercept=False)], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid


async def test_loop_intercept_non_dict_is_hard_error():
    """A declared loop_intercept control that is NOT an object is a hard ERROR —
    a declared control must never silently no-op because its shape was invalid."""
    graph = {"nodes": [_sandbox_node(loop_intercept="block")], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_LOOP_INTERCEPT_MALFORMED" for i in result.issues)


async def test_loop_intercept_malformed_field_is_hard_error():
    """A malformed field value (e.g. latency_budget_ms out of range) is a hard
    ERROR at graph-save — mirrors the runtime LoopInterceptConfigError."""
    graph = {
        "nodes": [_sandbox_node(loop_intercept={"latency_budget_ms": 0})],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_LOOP_INTERCEPT_MALFORMED" for i in result.issues)


async def test_loop_intercept_empty_patterns_is_warning_not_error():
    """An empty intercepted_tool_patterns list is valid shape (no error) but
    warns that nothing will actually be intercepted."""
    graph = {"nodes": [_sandbox_node(loop_intercept={"intercepted_tool_patterns": []})], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid
    assert any(i.code == "SANDBOX_LOOP_INTERCEPT_EMPTY_PATTERNS" for i in result.issues)
    assert not any(i.code == "SANDBOX_LOOP_INTERCEPT_MALFORMED" for i in result.issues)


# ---------------------------------------------------------------------------
# FAR-296 Phase 3a: egress policy + resource limits
# ---------------------------------------------------------------------------


async def test_sandbox_egress_valid_values_pass():
    """Valid egress_policy values (None / default / deny_all) pass graph save."""
    for policy in (None, "default", "deny_all"):
        overrides = {} if policy is None else {"egress_policy": policy}
        graph = {"nodes": [_sandbox_node(**overrides)], "edges": []}
        result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
        assert result.is_valid
        assert not any(i.code == "SANDBOX_EGRESS_POLICY_INVALID" for i in result.issues)


async def test_sandbox_egress_invalid_value_is_error():
    """An egress_policy outside default/deny_all/selected is a hard error (fail-closed)."""
    graph = {"nodes": [_sandbox_node(egress_policy="allow_all")], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_EGRESS_POLICY_INVALID" for i in result.issues)


async def test_sandbox_egress_selected_with_allowlist_passes():
    """egress_policy='selected' WITH a valid host:port allowlist passes graph
    validation (FAR-296 Phase 3b-3) — but warns that the allowlist is
    metadata-only and 'selected' currently denies ALL egress."""
    graph = {
        "nodes": [
            _sandbox_node(
                egress_policy="selected",
                egress_allowlist=[{"host": "api.github.com", "port": 443}],
            )
        ],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid
    assert any(i.code == "SANDBOX_EGRESS_SELECTED_METADATA_ONLY" for i in result.issues)
    assert not any(i.code.startswith("SANDBOX_EGRESS_") and i.severity == "error" for i in result.issues)


async def test_sandbox_egress_non_selected_has_no_metadata_only_warning():
    """The metadata-only limitation warning fires ONLY for egress_policy='selected'."""
    for policy in (None, "default", "deny_all"):
        overrides = {} if policy is None else {"egress_policy": policy}
        graph = {"nodes": [_sandbox_node(**overrides)], "edges": []}
        result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
        assert result.is_valid
        assert not any(i.code == "SANDBOX_EGRESS_SELECTED_METADATA_ONLY" for i in result.issues)


async def test_sandbox_egress_selected_without_allowlist_is_error():
    """egress_policy='selected' REQUIRES a non-empty allowlist (fail-closed)."""
    graph = {"nodes": [_sandbox_node(egress_policy="selected")], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_EGRESS_ALLOWLIST_INVALID" for i in result.issues)


async def test_sandbox_egress_allowlist_without_selected_is_error():
    """An allowlist on a non-selected policy is a hard error (it would no-op)."""
    allowlist = [{"host": "api.github.com", "port": 443}]
    for policy in ("default", "deny_all"):
        graph = {"nodes": [_sandbox_node(egress_policy=policy, egress_allowlist=allowlist)], "edges": []}
        result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
        assert not result.is_valid
        assert any(i.code == "SANDBOX_EGRESS_ALLOWLIST_INVALID" for i in result.issues)


async def test_sandbox_egress_invalid_allowlist_entry_is_error():
    """A malformed allowlist entry (bad port) is a hard error."""
    graph = {
        "nodes": [
            _sandbox_node(
                egress_policy="selected",
                egress_allowlist=[{"host": "api.github.com", "port": 0}],
            )
        ],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_EGRESS_ALLOWLIST_INVALID" for i in result.issues)


async def test_sandbox_resource_limits_known_keys_pass():
    """resource_limits with known keys passes graph save."""
    graph = {
        "nodes": [_sandbox_node(resource_limits={"cpu_count": 2, "memory_mb": 512})],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid
    assert not any(i.code.startswith("SANDBOX_RESOURCE_LIMITS_") for i in result.issues)


async def test_sandbox_resource_limits_unknown_key_is_error():
    """An unknown resource_limits key is a hard error (fail-closed, never dropped)."""
    graph = {"nodes": [_sandbox_node(resource_limits={"gpu": 1})], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_RESOURCE_LIMITS_UNKNOWN_KEY" for i in result.issues)


async def test_sandbox_resource_limits_non_dict_is_error():
    """A non-dict resource_limits value is a hard error."""
    graph = {"nodes": [_sandbox_node(resource_limits=[1, 2])], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_RESOURCE_LIMITS_INVALID" for i in result.issues)


async def test_sandbox_wallclock_budget_positive_int_passes():
    """A positive-int wallclock_budget_seconds passes graph save (FAR-296 Phase 4a)."""
    graph = {"nodes": [_sandbox_node(wallclock_budget_seconds=120)], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert result.is_valid
    assert not any(i.code.startswith("SANDBOX_WALLCLOCK_BUDGET_") for i in result.issues)


async def test_sandbox_wallclock_budget_zero_is_error():
    """wallclock_budget_seconds=0 is rejected (fail-closed) — a 0 budget is
    never a valid spend cap."""
    graph = {"nodes": [_sandbox_node(wallclock_budget_seconds=0)], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_WALLCLOCK_BUDGET_INVALID" for i in result.issues)


async def test_sandbox_wallclock_budget_negative_is_error():
    """wallclock_budget_seconds < 0 is rejected (fail-closed)."""
    graph = {"nodes": [_sandbox_node(wallclock_budget_seconds=-5)], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_WALLCLOCK_BUDGET_INVALID" for i in result.issues)


async def test_sandbox_wallclock_budget_non_int_is_error():
    """wallclock_budget_seconds that is not an int (e.g. a string) is rejected
    (fail-closed) — a budget that cannot be compared to the wall clock must
    never silently no-op the spend cap."""
    graph = {"nodes": [_sandbox_node(wallclock_budget_seconds="120")], "edges": []}
    result = await GraphValidator().validate_definition(graph, _session_returning([]), guardrail_definitions=[])
    assert not result.is_valid
    assert any(i.code == "SANDBOX_WALLCLOCK_BUDGET_INVALID" for i in result.issues)


# ---------------------------------------------------------------------------
# Node send-budget reconcile (FAR-410 / FAR-411)
# ---------------------------------------------------------------------------


async def test_node_send_budget_oversubscribed_warns_with_retry_multiplier():
    """fan_out.max_cardinality x per_item_timeout x (max_retries+1) must fit timeout_seconds."""
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={
            "fan_out": {
                "enabled": True,
                "items_path": "data.items",
                "max_cardinality": 100,
                "per_item_timeout": 5,
                "max_retries": 2,
            },
        },
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid  # a warning is not a blocker
    oversub = [i for i in result.issues if i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED"]
    assert len(oversub) == 1
    # 100 * 5 * 3 (2 retries + 1) = 1500s > 30s budget.
    assert oversub[0].node_id == "fanout-node"


async def test_node_send_budget_within_budget_no_warning():
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={
            "fan_out": {
                "enabled": True,
                "items_path": "data.items",
                "max_cardinality": 2,
                "per_item_timeout": 1,
                "max_retries": 0,
            }
        },
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_retries_zero_uses_single_attempt():
    """fan_out.max_retries=0 collapses the multiplier to a single attempt per item."""
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={
            "fan_out": {
                "enabled": True,
                "items_path": "data.items",
                "max_cardinality": 100,
                "per_item_timeout": 5,
                "max_retries": 0,
            }
        },
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    # 100 * 5 * 1 = 500s > 30s — still over budget, but the multiplier is 1.
    oversub = [i for i in result.issues if i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED"]
    assert len(oversub) == 1
    assert "attempts=1" in oversub[0].message


async def test_node_send_budget_absent_keys_no_warning():
    """Nodes bound to a connector without fan_out config (or with no binding) are untouched."""
    graph = {"nodes": [{"id": "plain-node"}], "edges": []}
    result = await GraphValidator().validate_definition(
        graph, _session_returning([]), connector_bindings=[], guardrail_definitions=[]
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_no_fanout_config_skipped():
    """A bound connector with no fan_out config produces no send-budget warning."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, config_json={})
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_uses_timeout_seconds_not_flat_keys():
    """The reconcile reads config_json.fan_out, NOT fabricated flat node keys.

    A node carrying the old flat keys (fanout_cardinality etc.) but no fan_out
    connector config must NOT warn — that namespace was a dead check.
    """
    graph = {
        "nodes": [
            {
                "id": "fanout-node",
                "timeout_seconds": 30,
                "fanout_cardinality": 1000,
                "per_item_budget": 60,
                "node_wait_for": 1,
            }
        ],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph, _session_returning([]), connector_bindings=[], guardrail_definitions=[]
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_minimal_fanout_defaults_warn():
    """A minimal fan_out config (enabled + items_path only) is reconciled with
    the connector's EFFECTIVE defaults (max_cardinality=1000,
    per_item_timeout=default timeout 30s), which oversubscribes a realistic node.

    Previously this config was silently skipped because both keys were absent,
    even though the connector genuinely attempts up to 1000 x 30 x 3 = 90000s
    against a 60-300s node timeout.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={"fan_out": {"enabled": True, "items_path": "data.items"}},
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 60}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid  # a warning is not a blocker
    oversub = [i for i in result.issues if i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED"]
    assert len(oversub) == 1
    assert oversub[0].node_id == "fanout-node"
    # 1000 * 30 * 3 (default attempts) = 90000s > 60s budget.
    assert "max_cardinality=1000.0" in oversub[0].message
    assert "per_item_timeout=30.0" in oversub[0].message


async def test_node_send_budget_minimal_fanout_uses_connector_timeout():
    """A fan_out config that omits per_item_timeout but sets max_cardinality
    substitutes the connector's single source of truth for the per-item timeout
    — its ``_DEFAULT_TIMEOUT`` (30.0s), NOT any top-level ``timeout`` config key.

    The connector NEVER reads ``config_json["timeout"]``: the timeout is a
    constructor parameter defaulted to ``_DEFAULT_TIMEOUT`` that the production
    composition root never overrides, so the connector always executes 30.0s per
    item. A top-level ``timeout: 50`` is present here as dead, ignored config to
    prove the reconcile does not let it diverge and under-warn.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={"fan_out": {"enabled": True, "items_path": "data.items", "max_cardinality": 100}, "timeout": 50},
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 300}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid
    oversub = [i for i in result.issues if i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED"]
    assert len(oversub) == 1
    # 100 * 30.0 (connector default, ignoring the dead top-level timeout:50) * 3 = 9000s > 300s.
    assert "per_item_timeout=30.0" in oversub[0].message


async def test_node_send_budget_explicit_malformed_value_skipped():
    """An explicitly-present but malformed fan_out value is still skipped
    (the _as_positive_number guard), not defaulted."""
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={"fan_out": {"enabled": True, "max_cardinality": "many", "per_item_timeout": 5}},
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_disabled_fanout_skipped():
    """A present-but-inert ``fan_out`` dict (``{"enabled": False}``) runs as a
    SINGLE call — the connector only fans out when ``items_path`` is truthy
    (write() gates on ``_fanout_enabled AND _fanout_items_path``, and
    ``_fanout_enabled = bool(enabled or items_path)`` so the effective
    predicate reduces to ``bool(items_path)``) — so it must not be reconciled
    against a fan-out send budget.

    Before the activation-predicate gate this spuriously warned against
    1000 x 30 x 3 for a connector that never fans out (FAR-411).
    """
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={"fan_out": {"enabled": False}},
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_enabled_no_items_path_skipped():
    """``{"fan_out": {"enabled": true}}`` with NO ``items_path`` runs as a SINGLE
    call — ``enabled`` alone is NOT sufficient to fan out (write() gated on
    ``_fanout_enabled AND _fanout_items_path`` where the latter is ``items_path``
    itself) — so it must not be reconciled against a fan-out send budget.

    Before the effective-predicate gate this spuriously warned against the
    connector DEFAULTS (1000 x 30 x 3) for a connector that never fans out
    (FAR-411).
    """
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={"fan_out": {"enabled": True}},
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid
    assert not any(i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED" for i in result.issues)


async def test_node_send_budget_enabled_false_with_items_path_warns():
    """``{"fan_out": {"enabled": false, "items_path": "data.items"}}`` STILL fans
    out — the effective predicate is simply ``bool(items_path)`` — so it must be
    reconciled against a fan-out send budget and warn when oversubscribed.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(
        cid,
        config_json={"fan_out": {"enabled": False, "items_path": "data.items"}},
    )
    graph = {
        "nodes": [{"id": "fanout-node", "timeout_seconds": 30}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(
        graph,
        _session_returning([instance]),
        connector_bindings=[{"node_id": "fanout-node", "connector_instance_id": str(cid)}],
        guardrail_definitions=[],
    )
    assert result.is_valid  # a warning is not a blocker
    oversub = [i for i in result.issues if i.code == "NODE_SEND_BUDGET_OVERSUBSCRIBED"]
    assert len(oversub) == 1
    assert oversub[0].node_id == "fanout-node"
