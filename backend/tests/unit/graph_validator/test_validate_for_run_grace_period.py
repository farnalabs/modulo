"""``validate_for_run`` pre-Phase-1 grace-period marker (``SCHEMA_DEGRADED``).

``validate_for_run`` returns error-severity issues only, with one deliberate
exception: the ``SCHEMA_DEGRADED`` marker a snapshot that pre-dates the Phase 1
cutover records when it hits a schema incompatibility. That marker is the ONLY
thing on the returned result that separates a grace-period degradation from a
plain block — the accompanying log line is not on the result path, so before
the marker survived the warning strip a caller saw two byte-identical blocking
results and had no way to tell them apart.

These tests pin:

* the marker is present for a pre-existing snapshot with incompatible schemas,
* it is absent for a post-cutover snapshot (the same graph, same schemas),
* it is absent when nothing was degraded,
* it is warning-severity and does not itself decide validity, while ordinary
  advisory warnings are still stripped,
* a schema degradation does not fabricate an "input schema" degradation log
  when the input schema is in fact clean.
"""

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator

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


def _schema_row(schema_id: uuid.UUID, definition_json: dict) -> MagicMock:
    row = MagicMock()
    row.schema_id = schema_id
    row.version = "1.0"
    row.definition_json = definition_json
    return row


def _session_returning(rows: list[MagicMock]) -> AsyncMock:
    """Session whose ``scalar_one_or_none`` yields *rows* in call order.

    Degrades to ``None``/``[]`` once the rows are consumed instead of raising
    ``StopIteration``, so a later check that legitimately queries the session
    does not turn a green run into a confusing harness failure.
    """
    queue = list(rows)

    def _execute(_stmt):
        row = queue.pop(0) if queue else None
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = row
        scalar_result.scalar_one.return_value = row
        scalars = MagicMock()
        scalars.all.return_value = [row] if row is not None else []
        scalar_result.scalars.return_value = scalars
        return scalar_result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session


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


def _codes(result) -> set[str]:
    return {i.code for i in result.issues}


async def test_pre_existing_incompatibility_returns_degraded_marker(caplog):
    """A pre-cutover snapshot blocks AND carries SCHEMA_DEGRADED on the result."""
    caplog.set_level(logging.WARNING)
    graph, rows = _incompatible_graph()
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    # The invalid/valid outcome is unchanged: schema errors still block.
    assert not result.is_valid
    assert "SCHEMA_FIELD_INCOMPATIBLE" in _codes(result)
    # The grace-period distinction is now observable to the caller.
    assert "SCHEMA_DEGRADED" in _codes(result)
    assert any("Schema incompatibility in pipeline" in r.getMessage() for r in caplog.records)


async def test_post_cutover_incompatibility_has_no_degraded_marker():
    """The identical graph on a post-cutover snapshot blocks WITHOUT the marker.

    This is the other half of the distinction: without a marker the two results
    are indistinguishable to a caller.
    """
    graph, rows = _incompatible_graph()
    snap = _snapshot(graph, created_at=_POST_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    assert "SCHEMA_FIELD_INCOMPATIBLE" in _codes(result)
    assert "SCHEMA_DEGRADED" not in _codes(result)


async def test_clean_pre_existing_snapshot_has_no_degraded_marker(caplog):
    """The marker means "degraded", not "old" — a clean pre-cutover run gets none."""
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
    assert "SCHEMA_DEGRADED" not in _codes(result)


async def test_marker_is_warning_and_ordinary_warnings_are_stripped():
    """The marker survives the strip; a normal advisory warning does not.

    ``validate_for_run`` keeps its "errors only" contract for everything except
    the grace-period marker, and the marker is warning-severity — it does not
    flip validity on its own.
    """
    graph, rows = _incompatible_graph(extra_unreachable_node=True)
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    # Ordinary advisory warnings are still stripped (node "c" is unreachable).
    assert "TOPOLOGY_UNREACHABLE" not in _codes(result)
    # ...but the grace-period marker is not.
    assert "SCHEMA_DEGRADED" in _codes(result)
    marker = next(i for i in result.issues if i.code == "SCHEMA_DEGRADED")
    assert marker.severity == "warning"
    # Warning-severity: only the schema error makes this invalid.
    assert [i.code for i in result.issues if i.severity == "error"] == ["SCHEMA_FIELD_INCOMPATIBLE"]


async def test_schema_degradation_does_not_fabricate_input_schema_log(caplog):
    """A schema degradation must not also log an input-schema degradation.

    The grace period is keyed on what *each* check found. Keying on overall
    invalidity made an already-invalid result re-trigger the input-schema
    branch, logging a degradation that never happened (and duplicating the
    marker) whenever the first node carried no input pin at all.
    """
    caplog.set_level(logging.WARNING)
    graph, rows = _incompatible_graph()
    # Node "a" is first and carries only an output pin, so the input-schema
    # check returns early and finds nothing to complain about.
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, {}, _session_returning(rows))

    assert not result.is_valid
    assert "SCHEMA_DEGRADED" in _codes(result)
    assert not [r for r in caplog.records if "Input schema incompatibility" in r.getMessage()]


async def test_marker_reaches_caller_on_null_payload_early_return():
    """The marker survives the INPUT_NULL_PAYLOAD early return too.

    ``input_payload=None`` returns straight after the schema check; the strip
    must not drop the grace-period marker on that path either, or the
    distinction vanishes exactly where a caller needs it.
    """
    graph, rows = _incompatible_graph()
    snap = _snapshot(graph, created_at=_PRE_CUTOVER)

    result = await GraphValidator().validate_for_run(snap, None, _session_returning(rows))

    assert not result.is_valid
    assert "INPUT_NULL_PAYLOAD" in _codes(result)
    assert "SCHEMA_DEGRADED" in _codes(result)
