"""``validate_for_run`` pre-Phase-1 snapshot blocking marker (``SCHEMA_PRE_EXISTING_BLOCKED``).

``validate_for_run`` returns error-severity issues only. A snapshot that
pre-dates the Phase 1 cutover and hits a schema incompatibility blocks the run
like any other — but it also carries a distinct ``SCHEMA_PRE_EXISTING_BLOCKED``
error on the returned result (FAR-1760). That marker is the ONLY thing on the
result path that separates a pre-existing block from a plain block: the
accompanying "Run is blocked" log line is not on the result path, so without
the marker a caller saw two byte-identical blocking results and had no way to
tell them apart. There is no degraded-mode proceed.

These tests pin:

* the marker is present for a pre-existing snapshot with incompatible schemas,
* it is absent for a post-cutover snapshot (the same graph, same schemas),
* it is absent when nothing was incompatible,
* it is error-severity and survives the warning strip, while ordinary advisory
  warnings are still stripped,
* a schema block does not also fabricate an "input schema" block,
* the marker reaches the caller when the payload is ``None``.
"""

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from modulo.core.graph_validator import GraphValidator
from tests.unit.graph_validator._helpers import (
    codes_of as _codes,
)
from tests.unit.graph_validator._helpers import (
    schema_version_row as _schema_row,
)
from tests.unit.graph_validator._helpers import (
    schema_version_session as _session_returning,
)

_PRE_CUTOVER = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
_POST_CUTOVER = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)

_STRING_VS_INT = (
    {"type": "object", "properties": {"x": {"type": "string"}}},
    {"type": "object", "properties": {"x": {"type": "integer"}}},
)


def _snapshot(graph_json: dict, *, created_at: datetime):
    snap = MagicMock()
    snap.graph_json = graph_json
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.created_at = created_at
    snap.pipeline_id = uuid.uuid4()
    snap.id = uuid.uuid4()
    snap.environment_profile_id = None
    return snap


def _incompatible_graph(*, extra_unreachable_node: bool = False) -> tuple[dict, list[MagicMock]]:
    out_id = uuid.uuid4()
    in_id = uuid.uuid4()
    nodes = [
        {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
        {"id": "b", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
    ]
    if extra_unreachable_node:
        nodes.append({"id": "c"})
    graph = {
        "nodes": nodes,
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    rows = [_schema_row(out_id, _STRING_VS_INT[0]), _schema_row(in_id, _STRING_VS_INT[1])]
    return graph, rows


async def test_pre_existing_incompatibility_returns_blocked_marker(caplog):
    """A pre-cutover snapshot blocks AND carries SCHEMA_PRE_EXISTING_BLOCKED on the result."""
    caplog.set_level(logging.WARNING)
    graph, rows = _incompatible_graph()
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    # The invalid/valid outcome: schema errors still block.
    assert not result.is_valid
    assert "SCHEMA_FIELD_INCOMPATIBLE" in _codes(result)
    # The pre-existing distinction is now observable to the caller.
    assert "SCHEMA_PRE_EXISTING_BLOCKED" in _codes(result)
    assert any("Schema incompatibility in pipeline" in r.getMessage() for r in caplog.records)


async def test_post_cutover_incompatibility_has_no_blocked_marker():
    """The identical graph on a post-cutover snapshot blocks WITHOUT the marker.

    This is the other half of the distinction: without a marker the two results
    are indistinguishable to a caller.
    """
    graph, rows = _incompatible_graph()
    snap = _snapshot(graph, created_at=_POST_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    assert "SCHEMA_FIELD_INCOMPATIBLE" in _codes(result)
    assert "SCHEMA_PRE_EXISTING_BLOCKED" not in _codes(result)


async def test_clean_pre_existing_snapshot_has_no_blocked_marker(caplog):
    """The marker means "pre-existing block", not "old" — a clean pre-cutover run gets none."""
    caplog.set_level(logging.WARNING)
    out_id = uuid.uuid4()
    graph = {
        "nodes": [
            {"id": "a", "output_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
            {"id": "b", "input_schema_pin": {"schema_id": str(out_id), "schema_version": "1.0"}},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    shared = {"type": "object", "properties": {"name": {"type": "string"}}}
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning([_schema_row(out_id, shared)]))

    assert result.is_valid
    assert "SCHEMA_PRE_EXISTING_BLOCKED" not in _codes(result)


async def test_marker_is_error_and_ordinary_warnings_are_stripped():
    """The marker survives the strip; a normal advisory warning does not.

    ``validate_for_run`` keeps its "errors only" contract, so the pre-existing
    marker travels as an error-severity issue — the same channel as the schema
    error that makes the result invalid — while advisory warnings are dropped.
    """
    graph, rows = _incompatible_graph(extra_unreachable_node=True)
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    # Ordinary advisory warnings are still stripped (node "c" is unreachable).
    assert "TOPOLOGY_UNREACHABLE" not in _codes(result)
    # ...but the pre-existing marker is not.
    assert "SCHEMA_PRE_EXISTING_BLOCKED" in _codes(result)
    marker = next(i for i in result.issues if i.code == "SCHEMA_PRE_EXISTING_BLOCKED")
    assert marker.severity == "error"


async def test_schema_block_does_not_fabricate_input_schema_log(caplog):
    """A schema block must not also log an input-schema block.

    The schema check blocks and returns before the input-schema check runs, so
    an already-invalid result can never re-trigger the input-schema branch,
    logging a block that never happened.
    """
    caplog.set_level(logging.WARNING)
    graph, rows = _incompatible_graph()
    # Node "a" is first and carries only an output pin, so the input-schema
    # check would return early and find nothing to complain about anyway.
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    assert "SCHEMA_PRE_EXISTING_BLOCKED" in _codes(result)
    assert not [r for r in caplog.records if "Input schema incompatibility" in r.getMessage()]


async def test_marker_reaches_caller_when_payload_is_null():
    """The marker survives to the caller even when the input payload is ``None``.

    A pre-cutover snapshot with an incompatible schema blocks on the schema
    check and returns the marker; the ``None`` payload never reaches its own
    check, but the caller still sees the pre-existing distinction.
    """
    graph, rows = _incompatible_graph()
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, None, _session_returning(rows))

    assert not result.is_valid
    assert "SCHEMA_PRE_EXISTING_BLOCKED" in _codes(result)


async def test_input_schema_block_marker_on_pre_existing_snapshot(caplog):
    """A pre-cutover snapshot whose INPUT schema is incompatible records the marker.

    This is the input-schema half of the pre-existing carve-out, independent of
    the field-level schema check: a graph with no output→input edges passes
    schema compatibility, so the input check is the only source of errors. Its
    block must still be observable to the caller as SCHEMA_PRE_EXISTING_BLOCKED,
    and must be announced with the input-schema log line.
    """
    caplog.set_level(logging.WARNING)
    in_id = uuid.uuid4()
    graph = {
        "nodes": [
            {"id": "entry", "input_schema_pin": {"schema_id": str(in_id), "schema_version": "1.0"}},
        ],
        "edges": [],
    }
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)
    # One row for the pin-definition resolve, one for the input-schema lookup.
    rows = [_schema_row(in_id, schema), _schema_row(in_id, schema)]

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    assert "INPUT_SCHEMA_MISMATCH" in _codes(result)
    assert "SCHEMA_PRE_EXISTING_BLOCKED" in _codes(result)
    assert any("Input schema incompatibility in pipeline" in r.getMessage() for r in caplog.records)
