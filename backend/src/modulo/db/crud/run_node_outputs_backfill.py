"""run_node_outputs_backfill — the catch-up sweep body (FAR-583).

The batched backfill helper (:func:`backfill_run_node_outputs_batch`)
extracted from :mod:`modulo.db.crud.run_node_outputs` at B1 (SRP: the storage
chokepoint module keeps the upserts / REPLACE-write helpers / reassembly
readers; this module owns the SWEEP ORCHESTRATION that selects and heals
legacy-blob-carrying runs into the new table). Split in the same PR that cut
the legacy columns' ORM mapping — the sweep's selection legs read the legacy
``runs`` blob columns through the raw Core table
:data:`modulo.db.crud.run_node_outputs.RUNS_LEGACY_TABLE`.

Selection is driven by the NOT-EXISTS trigger legs + jsonb-native object
predicates on Postgres, the per-tick cap, and ``ORDER BY completed_at ASC``
— NO high-water filter (qa M20: ``completed_at > mark`` could permanently
miss runs whose terminalizing transaction STARTED before the mark advanced;
``completed_at`` is transaction-start ``now()``). The mark is still
accepted/returned for observability. A partial index on
``(organisation_id, completed_at) WHERE status IN (terminal)`` is the
documented post-deploy ops step backing the selection.

Per run the FULL representation is written idempotently (``ON CONFLICT
DO NOTHING``): per-node ``__final__`` rows from the UNION of both dicts, the
metadata row when either side is ``{}``, and one marker row per legacy
markers key. QUARANTINE, never silent drops (qa M1c/d): junk sides,
sentinel-namespace squatting, and zero-representable rows are copied to
``run_node_outputs_quarantine`` and excluded from every future selection.
Re-terminalization ghost protection (FAR-583): the pre-insert re-check of
the batch's ``max(updated_at)`` skips runs a concurrent writer owns this
pass.

The sweep dies with the columns at B2a/B2b (B2a removes the trigger wiring;
B2b drops the columns and the selection legs).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run_node_outputs import (
    QUARANTINE_TABLE,
    RUNS_LEGACY_TABLE,
    NodeOutputWrite,
    assert_write_org,
    dialect_insert,
    parse_marker_node_id,
    resolve_dialect,
    upsert_rows,
)
from modulo.db.models.run import TERMINAL_STATUSES
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, UNKNOWN_NODE_ID, RunNodeOutput

_log = logging.getLogger(__name__)

__all__ = ["backfill_run_node_outputs_batch"]


# "A side holds a meaningful value": ``CAST(col AS TEXT) != 'null'``. The
# legacy columns hold one of three states — SQL NULL (side absent), the JSON
# null VALUE (an explicitly-None side), or a real value. The text-cast
# comparison is dialect-portable: Postgres renders ``col::text``
# (jsonb null's text IS 'null'), SQLite's TEXT-backed JSON already IS text,
# and SQL NULL compares NULL (excluded). A direct JSON-typed cast cannot be
# used — SQLite's CAST to the non-native JSON type name collapses to NUMERIC
# affinity ('null' -> 0), making every TEXT compare non-equal.
_JSON_NULL_TEXT = "null"


def _side_present(column: Any) -> Any:
    return cast(column, String) != _JSON_NULL_TEXT


# A side holds '{}': an explicit EMPTY dict. For outputs/telemetry that is
# meaningful (it produces the metadata row); for MARKERS it means "no
# markers" and is NOT representable (the same definition as migration 0192's
# _ANY_BLOB_OBJECT_SQL, which excludes markers = '{}'). Selecting a
# '{}'-markers-only run would write zero rows every pass — an un-healable
# zombie the sweep would re-select on every tick.
_MARKERS_EMPTY_TEXT = "{}"


def _markers_present(column: Any) -> Any:
    return and_(_side_present(column), cast(column, String) != _MARKERS_EMPTY_TEXT)


# Postgres-native "object side" predicates (qa M1a): the sweep's trigger
# requires a side to be a JSON OBJECT (a jsonb array or scalar is not a
# representable blob — selecting one writes zero rows forever). The text-cast
# heuristic above counts arrays/scalars as present (their text is not 'null'),
# so on Postgres the trigger uses jsonb_typeof instead. jsonb_typeof(SQL NULL)
# is SQL NULL -> the comparison is NULL -> excluded by the WHERE, so no
# explicit IS NOT NULL is needed. The markers side additionally excludes '{}'
# ("no markers" is not representable).
_JSONB_OBJECT = "object"


def _pg_side_object(column: Any) -> Any:
    return func.jsonb_typeof(column) == _JSONB_OBJECT


def _pg_markers_object_nonempty(column: Any) -> Any:
    return and_(_pg_side_object(column), cast(column, String) != _MARKERS_EMPTY_TEXT)


def _decode_blob_side(raw: Any) -> tuple[dict[str, Any] | None, bool]:
    """Decode one legacy blob column value for the sweep body.

    Returns ``(dict_or_None, is_anomaly)``: a SQL NULL / JSON-null VALUE /
    ``'null'``-text side decodes to ``(None, False)`` (ABSENT); a dict-shaped
    side decodes to ``(dict, False)``; anything else (a jsonb array or
    scalar — legacy junk the writers never produce) decodes to
    ``(None, True)``. qa M1c: junk sides are QUARANTINED by the sweep body,
    never silently treated as absent (which would re-select the run on every
    tick — the cap-starvation bug).
    """
    if raw is None:
        return None, False
    value: Any = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, True
    if value is None:
        return None, False  # explicit JSON null value = absent side
    if isinstance(value, dict):
        return value, False
    return None, True


def _has_sentinel_node_id(mapping: dict[str, Any] | None) -> bool:
    if not mapping:
        return False
    return any(key.startswith("__") for key in mapping)


def _markers_have_sentinel_node_id(markers: dict[str, Any] | None) -> bool:
    """True when a PARSEABLE marker key derives a ``__``-prefixed node id.

    Unparseable keys map to ``__unknown__`` (the allowed sentinel row) and do
    NOT quarantine the run — the evidence row is exactly what the design
    wants to keep.
    """
    if not markers:
        return False
    for key in markers:
        if key.startswith("__"):
            continue  # grammar-violating keys become __unknown__ rows
        node_id = parse_marker_node_id(key)
        if node_id != UNKNOWN_NODE_ID and node_id.startswith("__"):
            return True
    return False


async def _quarantine_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    legacy_outputs: Any,
    legacy_telemetry: Any,
    legacy_markers: Any,
    reason: str,
) -> None:
    """Copy a run's legacy blobs to the quarantine side table (idempotent).

    Mirrors migration 0192's quarantine step for data the sweep encounters
    post-deploy: the blobs are preserved AS-IS (evidence), the run is
    EXCLUDED from all future sweep selections (the NOT-EXISTS trigger leg on
    ``run_node_outputs_quarantine``), and the reason is logged — data
    anomalies never abort the sweep, and never silently re-select forever.
    """
    insert_factory = dialect_insert(resolve_dialect(session))
    stmt = (
        insert_factory(QUARANTINE_TABLE)
        .values(
            run_id=run_id,
            organisation_id=organisation_id,
            legacy_outputs_json=legacy_outputs,
            legacy_node_telemetry_json=legacy_telemetry,
            legacy_raw_output_markers=legacy_markers,
            quarantined_at=func.now(),
        )
        .on_conflict_do_nothing(index_elements=["run_id"])
    )
    await session.execute(stmt)
    _log.warning("run_node_outputs.sweep_quarantined run=%s org=%s reason=%s", run_id, organisation_id, reason)


async def _existing_row_updated_at(
    session: AsyncSession, run_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime | None]:
    """Per-run ``max(updated_at)`` over the run's new-table rows (None = none).

    Used twice by the backfill: the census baseline and the pre-insert
    re-terminalization ghost re-check. Module-level so tests can simulate a
    concurrent commit between the two reads.
    """
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(RunNodeOutput.run_id, func.max(RunNodeOutput.updated_at))
            .where(RunNodeOutput.run_id.in_(list(run_ids)))
            .group_by(RunNodeOutput.run_id)
        )
    ).all()
    return {uuid.UUID(str(r.run_id)): r.updated_at for r in rows}


async def backfill_run_node_outputs_batch(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    high_water_mark: datetime | None = None,
    cap: int = 500,
) -> dict[str, Any]:
    """Backfill one org's TERMINAL runs into the new table (sweep body).

    Selection: ``organisation_id == :org AND status IN (TERMINAL_STATUSES)
    ORDER BY completed_at ASC LIMIT :cap``, gated by three SQL-side trigger
    legs so a drained org re-scans almost nothing on the steady-state tick:

    * blobs leg — a meaningful legacy blob side AND no ``__final__`` row for
      the run (the writers are all-or-nothing per run inside one
      transaction, so a ``__final__`` row's presence proves the
      outputs/telemetry/metadata representation was written);
    * markers leg — a meaningful markers side AND no attempt-keyed marker
      row for the run;
    * quarantine exclusion — the run is not in
      ``run_node_outputs_quarantine`` (quarantined anomalies are never
      re-selected — qa M1b).

    "Meaningful side" is DIALECT-BRANCHED (qa M1a): on Postgres the trigger
    requires a JSON OBJECT (``jsonb_typeof(col) = 'object'``, markers
    additionally ``<> '{}'``) — a jsonb array or scalar is NOT a
    representable blob and is never selected; on SQLite the portable
    text-cast heuristic stands (a junk side is then caught and quarantined
    by the body below).

    NO HIGH-WATER FILTER (qa M20): ``completed_at > mark`` could
    permanently miss runs whose terminalizing transaction STARTED before the
    mark advanced (``completed_at`` is transaction-start ``now()``) —
    the trigger legs + the per-tick cap + ``ORDER BY completed_at ASC``
    bound the work instead. *high_water_mark* is still accepted (call-site
    contract compatibility) and ``new_high_water`` is still returned (the
    max ``completed_at`` in the batch — observability only). A partial
    index on ``(organisation_id, completed_at) WHERE status IN (terminal)``
    is the documented post-deploy ops step backing this selection.

    Per run the FULL representation is written idempotently (``ON CONFLICT
    DO NOTHING``): per-node ``__final__`` rows from the UNION of both dicts
    (values as-is; per-side SQL NULL when a side lacks the key), the
    metadata row when either side is ``{}``, and one marker row per legacy
    markers key (node id via the twin parser; unparseable keys kept as
    ``__unknown__`` rows).

    QUARANTINE, never silent drops (qa M1c/d): a run whose decoded sides
    are junk (non-dict JSON — array/scalar), whose node ids or DERIVED
    marker node ids squat the ``__`` sentinel namespace, whose marker
    attempt keys are ``__``-prefixed, or which would otherwise produce ZERO
    rows is copied to ``run_node_outputs_quarantine`` (idempotently, blobs
    preserved AS-IS, reason logged), counted in ``runs_quarantined``, and —
    via the selection exclusion — never re-selected on a later tick. There
    are no silent-continue paths left in the body: every selected run ends
    backfilled, ghost-skipped (a concurrent writer owns it), healed-skip
    (already fully represented), or quarantined.

    Re-terminalization ghost protection (FAR-583): immediately before the
    INSERT batch the run's new-table ``max(updated_at)`` is re-checked
    against the value captured by the batch census; a run whose rows moved
    (a concurrent dual-write/REPLACE committed between census and insert) is
    SKIPPED — the concurrent writer owns the run's rows this pass.

    Returns ``{"runs_selected", "runs_backfilled", "runs_skipped_healed",
    "runs_skipped_ghost", "runs_quarantined", "rows_written",
    "unknown_marker_keys", "new_high_water"}``. The ``status='unknown'``
    markers leg lives in migration 0192 only: the sweep heals TERMINAL
    runs, whose markers are complete.
    """
    await assert_write_org(session, organisation_id)
    dialect = resolve_dialect(session)

    # The __final__ row kind carries the outputs/telemetry/metadata
    # representation; the marker rows carry non-__final__ attempt keys. The
    # inner runs-side reference auto-correlates to the outer runs FROM
    # (retrieved through the Core legacy table since B1's ORM cut).
    final_row_exists = exists().where(
        RunNodeOutput.run_id == RUNS_LEGACY_TABLE.c.id,
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
    )
    marker_row_exists = exists().where(
        RunNodeOutput.run_id == RUNS_LEGACY_TABLE.c.id,
        RunNodeOutput.attempt_key != FINAL_ATTEMPT_KEY,
    )
    quarantined_exists = exists().where(QUARANTINE_TABLE.c.run_id == RUNS_LEGACY_TABLE.c.id)

    run_stmt = (
        select(
            RUNS_LEGACY_TABLE.c.id,
            RUNS_LEGACY_TABLE.c.outputs_json,
            RUNS_LEGACY_TABLE.c.node_telemetry_json,
            RUNS_LEGACY_TABLE.c.raw_output_markers,
            RUNS_LEGACY_TABLE.c.completed_at,
        )
        .where(
            RUNS_LEGACY_TABLE.c.organisation_id == organisation_id,
            RUNS_LEGACY_TABLE.c.status.in_(sorted(TERMINAL_STATUSES)),
        )
        .order_by(RUNS_LEGACY_TABLE.c.completed_at.asc())
        .limit(cap)
    )
    # Trigger predicate: only runs the sweep can actually heal enter the
    # batch (absent representation OR absent marker rows) — a drained org
    # selects nothing on the steady-state tick. Postgres branches to the
    # jsonb-native OBJECT predicates (a jsonb array/scalar is never a
    # representable blob side); SQLite keeps the portable text-cast
    # heuristic (junk sides are quarantined by the body).
    if dialect == "postgresql":
        side_meaningful = or_(
            _pg_side_object(RUNS_LEGACY_TABLE.c.outputs_json),
            _pg_side_object(RUNS_LEGACY_TABLE.c.node_telemetry_json),
            _pg_markers_object_nonempty(RUNS_LEGACY_TABLE.c.raw_output_markers),
        )
        markers_meaningful = _pg_markers_object_nonempty(RUNS_LEGACY_TABLE.c.raw_output_markers)
    else:
        side_meaningful = or_(
            _side_present(RUNS_LEGACY_TABLE.c.outputs_json),
            _side_present(RUNS_LEGACY_TABLE.c.node_telemetry_json),
            _markers_present(RUNS_LEGACY_TABLE.c.raw_output_markers),
        )
        markers_meaningful = _markers_present(RUNS_LEGACY_TABLE.c.raw_output_markers)
    run_stmt = run_stmt.where(
        or_(
            and_(side_meaningful, ~final_row_exists),
            and_(markers_meaningful, ~marker_row_exists),
        ),
        ~quarantined_exists,
    )
    batch = (await session.execute(run_stmt)).all()

    counts: dict[str, Any] = {
        "runs_selected": len(batch),
        "runs_backfilled": 0,
        "runs_skipped_healed": 0,
        "runs_skipped_ghost": 0,
        "runs_quarantined": 0,
        "rows_written": 0,
        "unknown_marker_keys": 0,
        "new_high_water": high_water_mark,
    }
    max_completed: datetime | None = None

    if not batch:
        return counts

    # ONE batched census of the batch's existing rows: the key set (for the
    # per-run already-healed skip) and the max(updated_at) (the baseline the
    # pre-insert ghost re-check compares against).
    captured_keys: dict[uuid.UUID, set[tuple[str, str]]] = {}
    captured_updated: dict[uuid.UUID, datetime | None] = {}
    census = (
        await session.execute(
            select(
                RunNodeOutput.run_id,
                RunNodeOutput.node_id,
                RunNodeOutput.attempt_key,
                RunNodeOutput.updated_at,
            ).where(RunNodeOutput.run_id.in_([row.id for row in batch]))
        )
    ).all()
    for r in census:
        run_key = uuid.UUID(str(r.run_id))
        captured_keys.setdefault(run_key, set()).add((r.node_id, r.attempt_key))
        previous = captured_updated.get(run_key)
        if previous is None or (r.updated_at is not None and r.updated_at > previous):
            captured_updated[run_key] = r.updated_at

    insert_bound: list[tuple[uuid.UUID, list[NodeOutputWrite]]] = []

    for run_row in batch:
        run_id: uuid.UUID = run_row.id
        outputs, outputs_junk = _decode_blob_side(run_row.outputs_json)
        telemetry, telemetry_junk = _decode_blob_side(run_row.node_telemetry_json)
        markers, markers_junk = _decode_blob_side(run_row.raw_output_markers)
        completed_at = run_row.completed_at
        if max_completed is None or (completed_at is not None and completed_at > max_completed):
            max_completed = completed_at

        # Quarantine (qa M1c): junk (non-dict) blob sides, sentinel-namespace
        # node ids, __-prefixed marker attempt keys, and parseable marker
        # keys deriving __-prefixed node ids all copy the run's blobs aside
        # and remove it from every future selection. Never silently dropped,
        # never re-selected forever, never aborting on data.
        blob_anomaly = outputs_junk or telemetry_junk or markers_junk
        marker_keys_sentinel = markers is not None and any(key.startswith("__") for key in markers)
        if (
            blob_anomaly
            or marker_keys_sentinel
            or _has_sentinel_node_id(outputs)
            or _has_sentinel_node_id(telemetry)
            or _markers_have_sentinel_node_id(markers)
        ):
            if blob_anomaly:
                reason = "non-dict legacy blob side (jsonb array/scalar)"
            elif marker_keys_sentinel:
                reason = "'__'-prefixed marker attempt key"
            else:
                reason = "'__'-prefixed node id in a legacy blob dict"
            await _quarantine_run(
                session,
                run_id=run_id,
                organisation_id=organisation_id,
                legacy_outputs=run_row.outputs_json,
                legacy_telemetry=run_row.node_telemetry_json,
                legacy_markers=run_row.raw_output_markers,
                reason=reason,
            )
            counts["runs_quarantined"] += 1
            continue

        rows: list[NodeOutputWrite] = []
        outputs_keys = set(outputs) if outputs is not None else set()
        telemetry_keys = set(telemetry) if telemetry is not None else set()
        for node_id in sorted(outputs_keys | telemetry_keys):
            row_kwargs: dict[str, Any] = {"node_id": node_id}
            if node_id in outputs_keys:
                row_kwargs["outputs"] = outputs[node_id]  # type: ignore[index]
            if node_id in telemetry_keys:
                row_kwargs["telemetry"] = telemetry[node_id]  # type: ignore[index]
            rows.append(NodeOutputWrite(**row_kwargs))

        if outputs == {} or telemetry == {}:
            payload = {"empty_outputs": outputs == {}, "empty_telemetry": telemetry == {}}
            rows.append(NodeOutputWrite(node_id=META_NODE_ID, outputs=payload))

        if markers:
            for key, value in markers.items():
                node_id = parse_marker_node_id(key)
                rows.append(NodeOutputWrite(node_id=node_id, attempt_key=key, markers=value))
                if node_id == UNKNOWN_NODE_ID:
                    counts["unknown_marker_keys"] += 1

        if not rows:
            # Defensive: nothing representable at all (unreachable by the
            # trigger's construction — an object side always yields at least
            # the metadata row). Quarantined rather than silently continued:
            # a silent continue would leave the run re-selectable forever
            # (the cap-starvation zombie qa M1 targets).
            await _quarantine_run(
                session,
                run_id=run_id,
                organisation_id=organisation_id,
                legacy_outputs=run_row.outputs_json,
                legacy_telemetry=run_row.node_telemetry_json,
                legacy_markers=run_row.raw_output_markers,
                reason="no representable blob content",
            )
            counts["runs_quarantined"] += 1
            continue

        # Already-healed skip: every row this pass would write is already
        # present (ON CONFLICT DO NOTHING would no-op) — skip without
        # touching the table. Marker key-set parity is inherent: a missing
        # marker key is a would-write key not in the captured set.
        would_write = {(row.node_id, row.attempt_key) for row in rows}
        if would_write and would_write <= captured_keys.get(run_id, set()):
            counts["runs_skipped_healed"] += 1
            continue

        insert_bound.append((run_id, rows))

    if insert_bound:
        # Re-terminalization ghost protection: re-check max(updated_at) for
        # the insert-bound runs immediately before the INSERT batch; a run
        # whose rows moved since the census is owned by a concurrent writer
        # this pass — skip it (ON CONFLICT DO NOTHING alone would keep the
        # concurrent rows, but the skip keeps the counters honest and avoids
        # racing the in-flight REPLACE write).
        fresh_updated = await _existing_row_updated_at(session, [run_id for run_id, _ in insert_bound])

        for run_id, rows in insert_bound:
            if fresh_updated.get(run_id) != captured_updated.get(run_id):
                counts["runs_skipped_ghost"] += 1
                continue
            await upsert_rows(session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=True)
            counts["runs_backfilled"] += 1
            counts["rows_written"] += len(rows)

    counts["new_high_water"] = max_completed
    return counts
