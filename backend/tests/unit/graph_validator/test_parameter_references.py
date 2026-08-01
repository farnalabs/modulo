"""Unit tests for parameter schema / set reference validation.

Covers ``_check_parameter_references`` (PARAMETER_* codes) on agent nodes.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


def _schema(
    schema_id: uuid.UUID,
    *,
    version: int = 1,
    parameters: list[dict[str, Any]] | None = None,
) -> MagicMock:
    s = MagicMock()
    s.id = schema_id
    s.version = version
    s.parameters = parameters or []
    return s


def _set(
    set_id: uuid.UUID,
    schema_id: uuid.UUID,
    *,
    schema_version: int = 1,
    values: dict[str, Any] | None = None,
) -> MagicMock:
    ps = MagicMock()
    ps.id = set_id
    ps.parameter_schema_id = schema_id
    ps.schema_version = schema_version
    ps.values = values or {}
    return ps


def _node(nid: str, **refs) -> dict:
    node = {"id": nid, "node_type": "agent"}
    node.update(refs)
    return node


def _session_with(schema_rows: list[MagicMock] | None = None, set_rows: list[MagicMock] | None = None) -> AsyncMock:
    """Mock session that returns schema rows on first execute and set rows on second."""
    session = AsyncMock()
    calls: list[list[MagicMock]] = []
    if schema_rows is not None:
        calls.append(schema_rows)
    if set_rows is not None:
        calls.append(set_rows)

    if len(calls) == 1:

        def _exec(*_a, **_kw):
            r = MagicMock()
            s = MagicMock()
            s.all.return_value = calls[0]
            r.scalars.return_value = s
            return r

        session.execute = AsyncMock(side_effect=_exec)
    elif len(calls) == 2:

        def _exec(*_a, **_kw):
            rows = calls.pop(0)
            r = MagicMock()
            s = MagicMock()
            s.all.return_value = rows
            r.scalars.return_value = s
            return r

        session.execute = AsyncMock(side_effect=_exec)
    else:

        def _exec(*_a, **_kw):
            r = MagicMock()
            s = MagicMock()
            s.all.return_value = []
            r.scalars.return_value = s
            return r

        session.execute = AsyncMock(side_effect=_exec)
    return session


async def _run(graph_json: dict[str, Any], session: AsyncMock) -> ValidationResult:
    result = ValidationResult()
    await GraphValidator()._check_parameter_references(graph_json, session, result)
    return result


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


async def test_no_references_skipped():
    graph = {"nodes": [_node("n1")], "edges": []}
    session = AsyncMock()
    result = await _run(graph, session)
    assert result.is_valid
    session.execute.assert_not_called()


async def test_empty_nodes_skipped():
    result = await _run({"nodes": [], "edges": []}, AsyncMock())
    assert result.is_valid


# ---------------------------------------------------------------------------
# Invalid / missing schema ids
# ---------------------------------------------------------------------------


async def test_invalid_schema_id_is_error():
    valid_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n-valid", parameter_schema_id=str(valid_id)),
            _node("n1", parameter_schema_id="not-a-uuid"),
        ],
        "edges": [],
    }
    session = _session_with(schema_rows=[_schema(valid_id)])
    result = await _run(graph, session)
    assert "PARAMETER_SCHEMA_INVALID_ID" in _codes(result)
    assert not result.is_valid


async def test_missing_schema_is_error():
    sid = uuid.uuid4()
    graph = {"nodes": [_node("n1", parameter_schema_id=str(sid))], "edges": []}
    session = _session_with(schema_rows=[])
    result = await _run(graph, session)
    assert "PARAMETER_SCHEMA_NOT_FOUND" in _codes(result)
    assert not result.is_valid


async def test_schema_not_found_reports_node_and_id():
    sid = uuid.uuid4()
    graph = {"nodes": [_node("my-node", parameter_schema_id=str(sid))], "edges": []}
    session = _session_with(schema_rows=[])
    result = await _run(graph, session)
    issue = next(i for i in result.issues if i.code == "PARAMETER_SCHEMA_NOT_FOUND")
    assert issue.node_id == "my-node"
    assert str(sid) in issue.message


# ---------------------------------------------------------------------------
# Invalid / missing set ids
# ---------------------------------------------------------------------------


async def test_invalid_set_id_is_error():
    valid_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n-valid", parameter_set_id=str(valid_id)),
            _node("n1", parameter_set_id="not-a-uuid"),
        ],
        "edges": [],
    }
    session = _session_with(set_rows=[_set(valid_id, valid_id)])
    result = await _run(graph, session)
    assert "PARAMETER_SET_INVALID_ID" in _codes(result)


async def test_missing_set_is_error():
    sid = uuid.uuid4()
    graph = {"nodes": [_node("n1", parameter_set_id=str(sid))], "edges": []}
    session = _session_with(set_rows=[])
    result = await _run(graph, session)
    assert "PARAMETER_SET_NOT_FOUND" in _codes(result)


# ---------------------------------------------------------------------------
# Schema / set mismatch
# ---------------------------------------------------------------------------


async def test_set_schema_mismatch_is_error():
    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    other_schema_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", parameter_schema_id=str(schema_id), parameter_set_id=str(set_id)),
        ],
        "edges": [],
    }
    session = _session_with(
        schema_rows=[_schema(schema_id)],
        set_rows=[_set(set_id, other_schema_id)],
    )
    result = await _run(graph, session)
    assert "PARAMETER_SET_SCHEMA_MISMATCH" in _codes(result)


async def test_set_matches_schema_is_valid():
    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", parameter_schema_id=str(schema_id), parameter_set_id=str(set_id)),
        ],
        "edges": [],
    }
    session = _session_with(
        schema_rows=[_schema(schema_id, parameters=[{"name": "temperature"}])],
        set_rows=[_set(set_id, schema_id, values={"temperature": 0.7})],
    )
    result = await _run(graph, session)
    assert result.is_valid
    assert not result.issues


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------


async def test_schema_drift_warns():
    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", parameter_schema_id=str(schema_id), parameter_set_id=str(set_id)),
        ],
        "edges": [],
    }
    session = _session_with(
        schema_rows=[_schema(schema_id, version=3, parameters=[{"name": "temperature"}])],
        set_rows=[_set(set_id, schema_id, schema_version=1, values={"temperature": 0.7})],
    )
    result = await _run(graph, session)
    assert "PARAMETER_SCHEMA_DRIFT" in _codes(result)
    assert result.is_valid  # drift is a warning


async def test_schema_drift_composite_warns():
    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", parameter_schema_id=str(schema_id), parameter_set_id=str(set_id)),
        ],
        "edges": [],
    }
    session = _session_with(
        schema_rows=[
            _schema(schema_id, version=1, parameters=[{"name": "temperature"}, {"name": "top_p"}]),
        ],
        set_rows=[_set(set_id, schema_id, schema_version=1, values={"temperature": 0.7})],
    )
    result = await _run(graph, session)
    assert "PARAMETER_SCHEMA_DRIFT_COMPOSITE" in _codes(result)
    assert "top_p" in next(i.message for i in result.issues if i.code == "PARAMETER_SCHEMA_DRIFT_COMPOSITE")


async def test_set_with_all_schema_params_no_composite_drift():
    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", parameter_schema_id=str(schema_id), parameter_set_id=str(set_id)),
        ],
        "edges": [],
    }
    session = _session_with(
        schema_rows=[_schema(schema_id, version=1, parameters=[{"name": "temperature"}])],
        set_rows=[_set(set_id, schema_id, schema_version=1, values={"temperature": 0.7})],
    )
    result = await _run(graph, session)
    assert "PARAMETER_SCHEMA_DRIFT_COMPOSITE" not in _codes(result)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Multiple nodes with mixed validity
# ---------------------------------------------------------------------------


async def test_multiple_nodes_references_checked():
    sid = uuid.uuid4()
    bad_id = uuid.uuid4()
    graph = {
        "nodes": [
            _node("n1", parameter_schema_id=str(sid)),
            _node("n2", parameter_schema_id="not-a-uuid"),
            _node("n3", parameter_set_id=str(bad_id)),
        ],
        "edges": [],
    }
    session = _session_with(
        schema_rows=[_schema(sid)],
        set_rows=[],
    )
    result = await _run(graph, session)
    codes = _codes(result)
    assert "PARAMETER_SCHEMA_NOT_FOUND" not in codes  # sid found
    assert "PARAMETER_SCHEMA_INVALID_ID" in codes  # n2
    assert "PARAMETER_SET_NOT_FOUND" in codes  # n3
