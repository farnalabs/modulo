"""``validate_for_run`` pre-Phase-1 snapshot blocking marker (``SCHEMA_PRE_EXISTING_BLOCKED``).

``validate_for_run`` returns error-severity issues only. A snapshot that
pre-dates the Phase 1 cutover and hits a schema incompatibility blocks the run
like any other, and carries a distinct ``SCHEMA_PRE_EXISTING_BLOCKED`` error on
the returned result (FAR-1760). That marker is the ONLY thing on the result path
that separates a pre-existing block from a plain block: the accompanying "Run is
blocked" log line is not on the result path, so without the marker a caller saw
two byte-identical blocking results and had no way to tell them apart.

``test_schema_enforcement.py`` (FAR-1760 section) already pins the base cases —
the marker is present for a pre-cutover block and absent for a post-cutover one.
The tests here pin the properties that distinguish the marker from an ordinary
advisory warning and that a naive implementation could break:

* it is error-severity and survives the warning strip, while ordinary advisory
  warnings are still stripped,
* a schema block does not also fabricate an "input schema" block,
* a clean pre-cutover snapshot carries no marker (the marker means "pre-existing
  block", not "old"),
* a ``None`` payload blocks on its own check and never acquires the marker.
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
from tests.unit.graph_validator._helpers import (
    snapshot as _snapshot,
)

_PRE_CUTOVER = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)

_STRING_VS_INT = (
    {"type": "object", "properties": {"x": {"type": "string"}}},
    {"type": "object", "properties": {"x": {"type": "integer"}}},
)


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


async def test_null_payload_blocks_without_a_pre_existing_marker():
    """A ``None`` payload blocks on its own check and never acquires the marker.

    The marker means "pre-existing snapshot *schema/input* block". A ``None``
    payload is rejected by ``INPUT_NULL_PAYLOAD`` before any input-schema
    comparison runs, so a pre-cutover snapshot with a *compatible* schema
    reports only ``INPUT_NULL_PAYLOAD`` — attaching ``SCHEMA_PRE_EXISTING_BLOCKED``
    here would falsely imply a schema incompatibility that was never found.
    """
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

    result = await GraphValidator().validate_for_run(snap, None, _session_returning([_schema_row(out_id, shared)]))

    assert not result.is_valid
    assert "INPUT_NULL_PAYLOAD" in _codes(result)
    assert "SCHEMA_PRE_EXISTING_BLOCKED" not in _codes(result)
