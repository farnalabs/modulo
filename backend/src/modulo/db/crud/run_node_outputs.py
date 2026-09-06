"""run_node_outputs — the single chokepoint for the per-node blob store (FAR-583).

Every read and write of the ``run_node_outputs`` table goes through this
module. It provides:

* dialect-guarded upserts (Postgres/SQLite ``ON CONFLICT``; MariaDB explicitly
  guarded out — no ``on_conflict`` support and deprecated) with an explicit
  ``updated_at = current_timestamp()`` in ``set_`` (the ``system_config``
  precedent: ``TimestampMixin.onupdate`` does not fire for Core upserts);
* the REPLACE-write helpers the dual-write chokepoints call
  (``replace_run_node_outputs`` / ``write_run_markers``) — upsert + delete-
  absent, delete-absent ordered AFTER upserts, metadata flags re-derived from
  full run state;
* reassembly readers returning the LEGACY dict shapes (``outputs_dict`` /
  ``telemetry_dict`` / ``markers_dict``) with PYTHON jsonb-canonical key
  ordering (sorted by ``(len(key.encode()), key.encode())``) so
  ``json.dumps(reassembled, default=str)`` is byte-identical to the legacy
  ``json.dumps`` bytes (PG jsonb orders keys length-then-bytewise; the
  server-side ``ORDER BY length(key), key COLLATE "C"`` is PG-only, so the
  ordering happens in Python — dialect-neutral);
* EMPTY/MISMATCH read-fallback helpers: a raw parameterised SELECT of the
  legacy ``runs`` columns, served when the reassembly yields nothing for a
  blob side (pre-sweep stragglers, kill-switch-off mode) or — for markers —
  when the new-table key set mismatches the legacy key set (partial-row
  truncation hole). Removed at B2b together with the columns;
* the batched backfill helper with high-water selection on ``completed_at``
  that the catch-up sweep (dispatcher_reconcile wiring, pass 2) reuses.

Representation invariants (the lossless mapping):

* SQL NULL on a side = that side's key is ABSENT from the legacy dict; a
  stored JSON ``null`` value = the key is present with an explicit ``null``
  value. Both decode to Python ``None``, so every row fetch carries
  ``IS NULL`` flags alongside the values — the distinction is load bearing
  for the byte-identical round-trip. (For this reason writers must never
  serialise a bare ``None`` into an absent side: ``update().values(col=None)``
  would persist the JSON null VALUE — use :func:`_sql_null`.)
* The metadata row (``('__run_meta__', '__final__')``) exists iff at least
  one legacy side was ``{}`` (explicit empty) — legacy ``{}`` and legacy
  NULL are DIFFERENT values and the metadata row is what preserves that
  distinction after the legacy columns drop (B2b).

RLS discipline: WRITE paths REQUIRE a bound, matching RLS org context (a NULL
context raises :class:`OutputsRlsMismatch` — the Python gate must catch what
the database does not: Postgres' strict fail-closed policy raises 42501 on a
NULL-context write anyway, but SQLite's tenant filter does not cover INSERT);
READ paths skip the consistency check when the session has no org context and
raise on a mismatched one.

Sentinel discipline: node ids / attempt keys under the ``__`` namespace are
reserved (``__run_meta__`` / ``__final__`` / ``__unknown__``). Writers raise
:class:`OutputsSentinelViolation` instead of silently squatting the namespace;
unparseable legacy marker keys are preserved as ``(node_id='__unknown__',
attempt_key=<original key>)`` — evidence kept, never dropped (FAR-188).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import JSON, String, and_, cast, delete, exists, func, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_node_outputs import (
    FINAL_ATTEMPT_KEY,
    META_NODE_ID,
    UNKNOWN_NODE_ID,
    RunNodeOutput,
)
from modulo.db.rls import OutputsRlsMismatch, read_rls_org

__all__ = [
    "DualWriteError",
    "OutputsSentinelViolation",
    "RunBlobs",
    "backfill_run_node_outputs_batch",
    "parse_marker_node_id",
    "read_node_output_blob_bytes",
    "read_run_blobs_with_fallback",
    "read_run_markers_with_fallback",
    "read_run_node_outputs_raw",
    "read_run_outputs_with_fallback",
    "read_run_telemetry_with_fallback",
    "replace_run_node_outputs",
    "write_run_markers",
]


class OutputsSentinelViolation(RuntimeError):  # noqa: N818  # name mandated by the reviewed FAR-583 design
    """A write tried to squat the reserved ``__`` sentinel namespace.

    Raised for node ids starting with ``__`` in an incoming outputs/telemetry
    dict and for attempt keys starting with ``__`` in an incoming markers dict
    (a key literally named ``__final__`` would also violate the
    ``ck_run_node_outputs_final_no_markers`` CHECK). The graph validator
    rejects ``__``-prefixed node ids at save time; this is the storage-side
    backstop for legacy data paths.
    """


class DualWriteError(RuntimeError):
    """The new-table dual-write leg failed after its bounded retry (FAR-583).

    Raised by the dual-write chokepoints (``crud.run``'s status write and the
    recovery marker write) when the ``run_node_outputs`` REPLACE write could
    not be completed inside the caller's transaction — including after ONE
    bounded in-session retry of a retryable SQLSTATE. Fail-closed contract:
    the raising chokepoint leaves its savepoint rolled back and lets the
    exception propagate, so the caller's transaction rolls back cleanly and
    the legacy run row is never half-written.

    Carries the orchestration context so the core-side orchestrator
    (:mod:`modulo.core.run_outputs_dualwrite`) can terminalize the run and
    emit the failure event without re-deriving it. ``sqlstate`` is the
    SQLSTATE of the LAST failure (None for non-SQL failures, e.g. a dialect
    guard or an RLS org mismatch surfacing through the savepoint).
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: uuid.UUID,
        organisation_id: uuid.UUID | None,
        claim_token: str | None = None,
        sqlstate: str | None = None,
        origin: str = "update_run_status",
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.organisation_id = organisation_id
        self.claim_token = claim_token
        self.sqlstate = sqlstate
        self.origin = origin


class RunBlobs(NamedTuple):
    """The legacy dict shapes reassembled for one run.

    Each member is ``None`` (side absent / legacy NULL), ``{}`` (legacy
    explicit empty dict — only distinguishable via the metadata row), or the
    reassembled dict. Key ordering is jsonb-canonical so the serialised
    bytes round-trip.
    """

    outputs: dict[str, Any] | None
    telemetry: dict[str, Any] | None
    markers: dict[str, Any] | None


# Anchored marker-key grammar (node_runner): run:<uuid>:node:<node_id>:<suffix>
# where the suffix is a claim count, sha256 prefix, 'claim-unknown', 'fallback'
# or 'connector'. MUST stay byte-identical to migration 0176's bound regex
# (asserted by tests/unit/db/test_migration_run_node_outputs.py).
_MARKER_KEY_RE = r"^run:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:node:.+$"
_MARKER_KEY_PREFIX_RE = re.compile(_MARKER_KEY_RE)
# len("run:") + len(uuid) + len(":node:")
_MARKER_KEY_PREFIX_LEN = 46


def parse_marker_node_id(attempt_key: str) -> str:
    """Derive the node id from a legacy marker attempt key.

    Anchored twin parser: the key must start with the literal
    ``run:<uuid>:node:`` prefix, then the node id is everything before the
    LAST ``:`` (rsplit) so colon-containing node ids are safe; the suffix must
    be non-empty. Anything else is unparseable and maps to
    ``UNKNOWN_NODE_ID`` (the caller preserves the FULL original key as the
    attempt_key — evidence kept, never dropped).
    """
    if _MARKER_KEY_PREFIX_RE.match(attempt_key) is None:
        return UNKNOWN_NODE_ID
    remainder = attempt_key[_MARKER_KEY_PREFIX_LEN:]
    node_id, sep, suffix = remainder.rpartition(":")
    if not sep or not node_id or not suffix:
        return UNKNOWN_NODE_ID
    return node_id


def _jsonb_canonical_key(key: str) -> tuple[int, bytes]:
    """PG jsonb ordering: length-first, then bytewise."""
    encoded = key.encode("utf-8")
    return (len(encoded), encoded)


def _json_bytes(value: Any) -> int:
    """Byte size of a JSON-serialisable value — mirrors run_retention._json_bytes."""
    if value is None:
        return 0
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 0


# Sentinel for "this side is NOT being written" — an omitted side is SQL NULL
# on insert and untouched on conflict. Distinct from a written ``None`` VALUE
# (explicit JSON null).
_SIDE_ABSENT: Any = object()


@dataclass(frozen=True)
class NodeOutputWrite:
    """One row to upsert. Sides left at :data:`_SIDE_ABSENT` are not written."""

    node_id: str
    attempt_key: str = FINAL_ATTEMPT_KEY
    outputs: Any = _SIDE_ABSENT
    telemetry: Any = _SIDE_ABSENT
    markers: Any = _SIDE_ABSENT


@dataclass
class _AssemblyInfo:
    out_present: bool = False
    telemetry_present: bool = False
    markers_present: bool = False
    meta_exists: bool = False


def _sql_null() -> Any:
    """A dialect-portable SQL NULL for a JSON column.

    ``update().values(json_col=None)`` serialises to the JSON ``null`` VALUE
    (SQLAlchemy's JSON type persists ``None`` as ``'null'`` unless
    ``none_as_null=True``) — which in this schema means "present, value
    null", NOT "side absent". ``CAST(NULL AS JSON)`` binds an unprocessed
    NULL expression.
    """
    return cast(null(), JSON)


# "A side holds a meaningful value": ``CAST(col AS TEXT) != 'null'``. The
# legacy columns hold one of three states — SQL NULL (side absent), the JSON
# null VALUE (an explicitly-None side: SQLAlchemy's JSON type serialises
# ``None`` as the 'null' VALUE, see :func:`_sql_null`), or a real value. The
# text-cast comparison is dialect-portable: Postgres renders ``col::text``
# (jsonb null's text IS 'null'), SQLite's TEXT-backed JSON already IS text,
# and SQL NULL compares NULL (excluded). A direct JSON-typed cast cannot be
# used — SQLite's CAST to the non-native JSON type name collapses to NUMERIC
# affinity ('null' -> 0), making every TEXT compare non-equal.
_JSON_NULL_TEXT = "null"


def _side_present(column: Any) -> Any:
    return cast(column, String) != _JSON_NULL_TEXT


# A side holds '{}': an explicit EMPTY dict. For outputs/telemetry that is
# meaningful (it produces the metadata row); for MARKERS it means "no
# markers" and is NOT representable (the same definition as migration 0176's
# _ANY_BLOB_OBJECT_SQL, which excludes markers = '{}'). Selecting a
# '{}'-markers-only run would write zero rows every pass — an un-healable
# zombie the sweep would re-select on every tick.
_MARKERS_EMPTY_TEXT = "{}"


def _markers_present(column: Any) -> Any:
    return and_(_side_present(column), cast(column, String) != _MARKERS_EMPTY_TEXT)


async def _resolve_dialect(session: AsyncSession) -> str:
    bind = session.get_bind()
    if asyncio.iscoroutine(bind):  # AsyncSession get_bind is sync in SA 2.x; defensive
        bind = await bind
    return bind.dialect.name


def _dialect_insert(dialect: str) -> Any:
    """Dialect-guarded insert factory with ON CONFLICT support.

    MariaDB is explicitly guarded out: deprecated, untested, and its
    ``INSERT ... ON DUPLICATE KEY UPDATE`` has different semantics.
    """
    if dialect == "postgresql":
        return pg_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise NotImplementedError(
        f"run_node_outputs writes are not supported on the {dialect!r} backend (postgres/sqlite only)"
    )


def _validate_sentinel_keys(mapping: dict[str, Any] | None, *, kind: str) -> None:
    if not mapping:
        return
    for key in mapping:
        if key.startswith("__"):
            raise OutputsSentinelViolation(
                f"{kind} key {key!r} squats the reserved '__' sentinel namespace "
                "(run_node_outputs metadata/unknown sentinels)"
            )


async def _assert_write_org(session: AsyncSession, organisation_id: uuid.UUID | None) -> None:
    """WRITE paths REQUIRE a bound, matching RLS org context (fail-closed)."""
    session_org = await read_rls_org(session)
    if session_org is None:
        raise OutputsRlsMismatch(
            "run_node_outputs write requires a bound RLS organisation context; "
            "wrap the call in `async with session.begin():` + set_rls_org(session, org)"
        )
    if organisation_id is None:
        raise OutputsRlsMismatch("run_node_outputs write requires an explicit organisation_id")
    if session_org != organisation_id:
        raise OutputsRlsMismatch(
            f"run_node_outputs org mismatch: session org {session_org} != row org {organisation_id}"
        )


def _assert_read_org(
    session_org: uuid.UUID | None,
    row_org: uuid.UUID | None,
    run_id: uuid.UUID,
) -> None:
    """READ paths: no session org -> skip (the caller's RLS scope governs);
    mismatched org -> raise."""
    if session_org is not None and row_org is not None and session_org != row_org:
        raise OutputsRlsMismatch(
            f"run_node_outputs org mismatch on read of run {run_id}: session org {session_org} != row org {row_org}"
        )


async def _upsert_rows(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    rows: Sequence[NodeOutputWrite],
    ignore_conflicts: bool,
) -> None:
    """Upsert rows in one executemany per side-signature group.

    Rows are grouped by which sides they write so every statement carries a
    single uniform column set (executemany requires homogeneous params). On
    conflict the present sides are overwritten from ``excluded`` and
    ``updated_at`` is stamped explicitly (TimestampMixin.onupdate never fires
    for Core upserts).
    """
    if not rows:
        return
    insert_factory = _dialect_insert(await _resolve_dialect(session))
    groups: dict[tuple[bool, bool, bool], list[NodeOutputWrite]] = {}
    for row in rows:
        flags = (
            row.outputs is not _SIDE_ABSENT,
            row.telemetry is not _SIDE_ABSENT,
            row.markers is not _SIDE_ABSENT,
        )
        groups.setdefault(flags, []).append(row)

    for (has_out, has_tel, has_markers), group in groups.items():
        values: dict[str, Any] = {"run_id": run_id, "organisation_id": organisation_id}
        if has_out:
            values["outputs_json"] = None
        if has_tel:
            values["node_telemetry_json"] = None
        if has_markers:
            values["raw_output_markers"] = None
        stmt = insert_factory(RunNodeOutput).values(**values)
        params: list[dict[str, Any]] = []
        for row in group:
            item: dict[str, Any] = {"node_id": row.node_id, "attempt_key": row.attempt_key}
            if has_out:
                item["outputs_json"] = row.outputs
            if has_tel:
                item["node_telemetry_json"] = row.telemetry
            if has_markers:
                item["raw_output_markers"] = row.markers
            params.append(item)
        if ignore_conflicts:
            stmt = stmt.on_conflict_do_nothing(index_elements=["run_id", "node_id", "attempt_key"])
        else:
            set_: dict[str, Any] = {"updated_at": func.current_timestamp()}
            if has_out:
                set_["outputs_json"] = stmt.excluded.outputs_json
            if has_tel:
                set_["node_telemetry_json"] = stmt.excluded.node_telemetry_json
            if has_markers:
                set_["raw_output_markers"] = stmt.excluded.raw_output_markers
            stmt = stmt.on_conflict_do_update(index_elements=["run_id", "node_id", "attempt_key"], set_=set_)
        await session.execute(stmt, params)


async def _blank_absent_side(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    side: str,
    keys: set[str],
) -> None:
    """SQL-NULL the given side on '__final__' rows whose node_id is absent.

    A node may carry BOTH sides on one row (the union-of-keys encoding), so a
    shrinking dict must BLANK the side rather than delete the row — deleting
    would silently drop the other side's value. Rows left with no value at
    all are removed by :func:`_drop_dead_final_rows` right after. Runs AFTER
    the upserts (delete-absent ordering).
    """
    column = getattr(RunNodeOutput, side)
    stmt = update(RunNodeOutput).where(
        RunNodeOutput.run_id == run_id,
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
        RunNodeOutput.node_id != META_NODE_ID,
        column.is_not(None),
    )
    if keys:
        stmt = stmt.where(RunNodeOutput.node_id.not_in(keys))
    await session.execute(stmt.values(**{side: _sql_null()}))


async def _drop_dead_final_rows(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Delete '__final__' rows left with every side SQL NULL after blanking."""
    await session.execute(
        delete(RunNodeOutput).where(
            RunNodeOutput.run_id == run_id,
            RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
            RunNodeOutput.node_id != META_NODE_ID,
            RunNodeOutput.outputs_json.is_(None),
            RunNodeOutput.node_telemetry_json.is_(None),
            RunNodeOutput.raw_output_markers.is_(None),
        )
    )


async def replace_run_node_outputs(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    outputs: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
) -> None:
    """REPLACE-write the '__final__' + metadata rows for one run.

    Mirrors the legacy column semantics losslessly: a non-None dict fully
    replaces that side (absent keys blanked — see :func:`_blank_absent_side`);
    a None dict leaves that side untouched (the legacy write leaves the
    column alone). The metadata row is upserted iff at least one side is
    ``{}``, its flags re-derived from full run state: a provided side derives
    its flag from the incoming dict; an untouched (None) side keeps the flag
    already stored (``False`` when no metadata row exists — that side is
    populated-or-NULL, never ``{}``).
    """
    _validate_sentinel_keys(outputs, kind="outputs node id")
    _validate_sentinel_keys(telemetry, kind="telemetry node id")
    await _assert_write_org(session, organisation_id)

    outputs_map: dict[str, Any] = outputs if outputs is not None else {}
    telemetry_map: dict[str, Any] = telemetry if telemetry is not None else {}

    rows: list[NodeOutputWrite] = []
    for node_id in sorted(set(outputs_map) | set(telemetry_map)):
        row_kwargs: dict[str, Any] = {"node_id": node_id}
        if outputs is not None and node_id in outputs_map:
            row_kwargs["outputs"] = outputs_map[node_id]
        if telemetry is not None and node_id in telemetry_map:
            row_kwargs["telemetry"] = telemetry_map[node_id]
        rows.append(NodeOutputWrite(**row_kwargs))
    if rows:
        await _upsert_rows(session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=False)

    # Delete-absent AFTER upserts (design order): blank shrinking sides, then
    # drop rows left with no value at all.
    if outputs is not None:
        await _blank_absent_side(session, run_id=run_id, side="outputs_json", keys=set(outputs_map))
    if telemetry is not None:
        await _blank_absent_side(session, run_id=run_id, side="node_telemetry_json", keys=set(telemetry_map))
    await _drop_dead_final_rows(session, run_id=run_id)
    await _refresh_metadata_row(
        session,
        run_id=run_id,
        organisation_id=organisation_id,
        outputs=outputs,
        telemetry=telemetry,
    )


async def _refresh_metadata_row(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    outputs: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
) -> None:
    meta_row = (
        await session.execute(
            select(RunNodeOutput).where(
                RunNodeOutput.run_id == run_id,
                RunNodeOutput.node_id == META_NODE_ID,
                RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
            )
        )
    ).scalar_one_or_none()
    existing_flags: dict[str, Any] | None = None
    if meta_row is not None and isinstance(meta_row.outputs_json, dict):
        existing_flags = meta_row.outputs_json

    if outputs is not None:
        empty_outputs = outputs == {}
    else:
        empty_outputs = bool(existing_flags.get("empty_outputs")) if existing_flags is not None else False
    if telemetry is not None:
        empty_telemetry = telemetry == {}
    else:
        empty_telemetry = bool(existing_flags.get("empty_telemetry")) if existing_flags is not None else False

    if empty_outputs or empty_telemetry:
        payload = {"empty_outputs": empty_outputs, "empty_telemetry": empty_telemetry}
        await _upsert_rows(
            session,
            run_id=run_id,
            organisation_id=organisation_id,
            rows=[NodeOutputWrite(node_id=META_NODE_ID, outputs=payload)],
            ignore_conflicts=False,
        )
    elif meta_row is not None:
        # No side is `{}` any more — a stale metadata row would make
        # reassembly serve `{}` for a side that is populated-or-NULL. Drop it.
        await session.execute(
            delete(RunNodeOutput).where(
                RunNodeOutput.run_id == run_id,
                RunNodeOutput.node_id == META_NODE_ID,
                RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
            )
        )


async def write_run_markers(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    markers: dict[str, Any],
) -> None:
    """Write the POST-MERGE markers dict as one row per attempt key.

    The caller performs the legacy read-merge-write first and hands the
    MERGED dict here — the stored marker is the post-merge value (prior
    ``pr_url`` preserved, ``delivery_done`` monotone). Attempt keys are the
    FULL original keys; the node id is derived by the anchored twin parser
    (:func:`parse_marker_node_id`), with unparseable keys stored as
    ``('__unknown__', <full key>)`` rows. Absent keys are deleted AFTER the
    upserts (delete-absent ordering).
    """
    _validate_sentinel_keys(markers, kind="marker attempt key")
    await _assert_write_org(session, organisation_id)

    rows = [
        NodeOutputWrite(node_id=parse_marker_node_id(key), attempt_key=key, markers=value)
        for key, value in markers.items()
    ]
    if rows:
        await _upsert_rows(session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=False)

    marker_delete = delete(RunNodeOutput).where(
        RunNodeOutput.run_id == run_id,
        RunNodeOutput.raw_output_markers.is_not(None),
    )
    if markers:
        marker_delete = marker_delete.where(RunNodeOutput.attempt_key.not_in(set(markers)))
    await session.execute(marker_delete)


_ROW_COLUMNS: tuple[Any, ...] = (
    RunNodeOutput.run_id,
    RunNodeOutput.node_id,
    RunNodeOutput.attempt_key,
    RunNodeOutput.organisation_id,
    RunNodeOutput.outputs_json,
    RunNodeOutput.node_telemetry_json,
    RunNodeOutput.raw_output_markers,
    RunNodeOutput.outputs_json.is_(None).label("outputs_absent"),
    RunNodeOutput.node_telemetry_json.is_(None).label("telemetry_absent"),
    RunNodeOutput.raw_output_markers.is_(None).label("markers_absent"),
)


async def _fetch_output_rows(session: AsyncSession, run_id: uuid.UUID) -> Sequence[Any]:
    """ONE batched query per read (never per-node lazy loads — MissingGreenlet
    under asyncio). The ``*_absent`` flags distinguish SQL NULL ("side
    absent") from a stored JSON ``null`` value ("present, value null") — both
    decode to Python ``None``, and the distinction is load bearing for the
    lossless round-trip."""
    return list((await session.execute(select(*_ROW_COLUMNS).where(RunNodeOutput.run_id == run_id))).all())


def _assemble(rows: Sequence[Any]) -> tuple[RunBlobs, _AssemblyInfo]:
    info = _AssemblyInfo()
    out_items: list[tuple[str, Any]] = []
    tel_items: list[tuple[str, Any]] = []
    marker_items: list[tuple[str, Any]] = []
    meta_flags: dict[str, Any] | None = None

    for r in rows:
        if r.attempt_key == FINAL_ATTEMPT_KEY and r.node_id == META_NODE_ID:
            info.meta_exists = True
            flags = r.outputs_json
            if not isinstance(flags, dict) or "empty_outputs" not in flags or "empty_telemetry" not in flags:
                raise OutputsSentinelViolation(
                    "run_node_outputs metadata row has a malformed flags payload "
                    f"(expected empty_outputs/empty_telemetry booleans, got {flags!r})"
                )
            meta_flags = flags
            continue
        if r.attempt_key == FINAL_ATTEMPT_KEY:
            if not r.outputs_absent:
                info.out_present = True
                out_items.append((r.node_id, r.outputs_json))
            if not r.telemetry_absent:
                info.telemetry_present = True
                tel_items.append((r.node_id, r.node_telemetry_json))
            continue
        if not r.markers_absent:
            info.markers_present = True
            marker_items.append((r.attempt_key, r.raw_output_markers))

    if meta_flags is not None and meta_flags["empty_outputs"] is True:
        # Flag wins over rows (design): the flags are writer-derived, so
        # `True` + present rows is an inconsistent state we resolve to `{}`.
        outputs: dict[str, Any] | None = {}
    elif out_items:
        outputs = dict(sorted(out_items, key=lambda item: _jsonb_canonical_key(item[0])))
    else:
        outputs = None

    if meta_flags is not None and meta_flags["empty_telemetry"] is True:
        telemetry: dict[str, Any] | None = {}
    elif tel_items:
        telemetry = dict(sorted(tel_items, key=lambda item: _jsonb_canonical_key(item[0])))
    else:
        telemetry = None

    markers: dict[str, Any] | None = None
    if marker_items:
        markers = dict(sorted(marker_items, key=lambda item: _jsonb_canonical_key(item[0])))

    return RunBlobs(outputs=outputs, telemetry=telemetry, markers=markers), info


async def read_run_node_outputs_raw(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> RunBlobs:
    """Reassemble the legacy dict shapes from the new table — NO fallback.

    Used by callers that must see exactly what the new table holds (sweep
    parity checks). Raises :class:`OutputsRlsMismatch` when the session org
    disagrees with the rows' org; a session with no org context skips the
    check (the callers' RLS scope governs).
    """
    rows = await _fetch_output_rows(session, run_id)
    session_org = await read_rls_org(session)
    _assert_read_org(session_org, rows[0].organisation_id if rows else organisation_id, run_id)
    blobs, _info = _assemble(rows)
    return blobs


def _ensure_dict(value: Any) -> dict[str, Any] | None:
    """Coerce a legacy blob column value to a dict or None.

    Legacy rows hold a Python dict (decoded by the driver's JSON codec), the
    JSON ``null`` value (decodes to ``None`` — the ORM writes ``None`` as
    JSON null, which means ABSENT for the legacy columns), or a raw JSON
    TEXT string on SQLite drivers without codecs. Anything else (legacy
    junk) is not a dict-shaped blob and reads as ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        with suppress(ValueError):
            value = json.loads(value)
    if isinstance(value, dict):
        return value
    return None


async def _read_legacy_run_blobs(session: AsyncSession, run_id: uuid.UUID) -> RunBlobs:
    """Single parameterised SELECT of the legacy ``runs`` blob columns.

    The one sanctioned legacy read in this module (the EMPTY/MISMATCH
    fallback; B2b removes it with the columns). Deliberately a COLUMN-level
    select (three columns, bound ``run_id`` — never f-stringed): it stays a
    single statement on every dialect and picks up the runs RLS policy on
    Postgres and the ORM tenant filter on generic backends (a raw text()
    SELECT would bypass the latter AND could not bind the UUID portably —
    SQLite stores ``Uuid`` in a non-text form the DBAPI cannot match against
    a plain string parameter).
    """
    row = (
        await session.execute(
            select(Run.outputs_json, Run.node_telemetry_json, Run.raw_output_markers).where(Run.id == run_id)
        )
    ).first()
    if row is None:
        return RunBlobs(outputs=None, telemetry=None, markers=None)
    return RunBlobs(
        outputs=_ensure_dict(row.outputs_json),
        telemetry=_ensure_dict(row.node_telemetry_json),
        markers=_ensure_dict(row.raw_output_markers),
    )


async def read_run_blobs_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> RunBlobs:
    """Reassemble with the EMPTY/MISMATCH legacy fallback applied.

    outputs/telemetry: the legacy column is served only when the reassembly
    yields NOTHING for the side (no node rows AND no metadata row) — pre-sweep
    stragglers / kill-switch-off mode. (When the metadata row exists, the
    backfill/writer invariants guarantee the reassembled value already
    matches the legacy column, including the ``{}``-vs-``None`` distinction.)
    markers: the fallback triggers on ABSENCE of rows, or on a key-set
    MISMATCH while the legacy column actually holds markers (per-run
    comparison — closing the partial-row truncation hole). A legacy column
    that is NULL/``{}`` is NOT authoritative over present rows: serving it
    would destroy the just-written markers. The legacy read is one extra
    parameterised SELECT per call; B2b removes it together with the columns.
    """
    rows = await _fetch_output_rows(session, run_id)
    session_org = await read_rls_org(session)
    _assert_read_org(session_org, rows[0].organisation_id if rows else organisation_id, run_id)
    blobs, info = _assemble(rows)
    legacy = await _read_legacy_run_blobs(session, run_id)

    outputs = blobs.outputs if (info.out_present or info.meta_exists) else legacy.outputs
    telemetry = blobs.telemetry if (info.telemetry_present or info.meta_exists) else legacy.telemetry

    if not info.markers_present:
        markers = legacy.markers
    else:
        # Truncation hole: fall back to legacy ONLY when legacy actually
        # holds markers whose key set differs from the rows. A legacy column
        # that is NULL/`{}` carries no truth to fall back to — serving it
        # over present rows would destroy the just-written markers.
        legacy_markers = legacy.markers
        reassembled_keys = set(blobs.markers) if blobs.markers is not None else set()
        legacy_keys = set(legacy_markers) if legacy_markers else set()
        truncated = bool(legacy_keys) and legacy_keys != reassembled_keys
        markers = legacy_markers if truncated else blobs.markers

    return RunBlobs(outputs=outputs, telemetry=telemetry, markers=markers)


async def read_run_outputs_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled outputs dict (legacy shape) with the empty-fallback."""
    return (await read_run_blobs_with_fallback(session, run_id=run_id, organisation_id=organisation_id)).outputs


async def read_run_telemetry_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled telemetry dict (legacy shape) with the empty-fallback."""
    return (await read_run_blobs_with_fallback(session, run_id=run_id, organisation_id=organisation_id)).telemetry


async def read_run_markers_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled flat markers dict with the MISMATCH fallback."""
    return (await read_run_blobs_with_fallback(session, run_id=run_id, organisation_id=organisation_id)).markers


async def read_node_output_blob_bytes(
    session: AsyncSession,
    run_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Per-run summed blob bytes for retention accounting — EXCLUDES metadata
    rows (the flags payload overhead would skew the accounting). One batched
    query; Python-side ``len(json.dumps(value, default=str))`` per present
    side (the ``run_retention._json_bytes`` formula)."""
    if not run_ids:
        return {}
    rows = (await session.execute(select(*_ROW_COLUMNS).where(RunNodeOutput.run_id.in_(list(run_ids))))).all()
    totals: dict[uuid.UUID, int] = {}
    for r in rows:
        if r.attempt_key == FINAL_ATTEMPT_KEY and r.node_id == META_NODE_ID:
            continue
        run_key = uuid.UUID(str(r.run_id))
        total = totals.get(run_key, 0)
        total += 0 if r.outputs_absent else _json_bytes(r.outputs_json)
        total += 0 if r.telemetry_absent else _json_bytes(r.node_telemetry_json)
        total += 0 if r.markers_absent else _json_bytes(r.raw_output_markers)
        totals[run_key] = total
    return totals


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

    Selection (high-water): ``organisation_id == :org AND status IN
    (TERMINAL_STATUSES) AND completed_at > :mark ORDER BY completed_at ASC
    LIMIT :cap`` — the mark advances to the max ``completed_at`` in the
    batch. Two NOT-EXISTS trigger legs keep already-healed runs out of the
    batch entirely (the design's trigger predicate, SQL-side so a drained
    org re-scans almost nothing on the steady-state tick):

    * blobs leg — any legacy blob non-NULL AND no ``__final__`` row for the
      run (the writers are all-or-nothing per run inside one transaction, so
      a ``__final__`` row's presence proves the outputs/telemetry/metadata
      representation was written);
    * markers leg — markers non-NULL AND no attempt-keyed marker row for the
      run.

    Per run the FULL representation is written idempotently (``ON CONFLICT
    DO NOTHING``): per-node ``__final__`` rows from the UNION of both dicts
    (values as-is; per-side SQL NULL when a side lacks the key), the
    metadata row when either side is ``{}``, and one marker row per legacy
    markers key (node id via the twin parser; unparseable keys kept as
    ``__unknown__`` rows). Runs whose legacy dicts carry ``__``-prefixed
    node ids are SKIPPED and counted (migration 0176 quarantines them to the
    side table; the sweep never touches them), never aborting on data.

    Re-terminalization ghost protection (FAR-583): immediately before the
    INSERT batch the run's new-table ``max(updated_at)`` is re-checked
    against the value captured by the batch census; a run whose rows moved
    (a concurrent dual-write/REPLACE committed between census and insert) is
    SKIPPED — the concurrent writer owns the run's rows this pass.

    Returns ``{"runs_selected", "runs_backfilled", "runs_skipped_healed",
    "runs_skipped_ghost", "runs_quarantined", "rows_written",
    "unknown_marker_keys", "new_high_water"}`` — the last is a ``datetime``
    (or the unchanged mark). The ``status='unknown'`` markers leg lives in
    migration 0176 only: the sweep heals TERMINAL runs, whose markers are
    complete. Ties at the same ``completed_at`` are consumed by the strict
    ``> mark`` advance one batch at a time; a batch boundary inside a tie
    skips the remainder of that tie until the next mark-less pass.
    """
    await _assert_write_org(session, organisation_id)

    # The __final__ row kind carries the outputs/telemetry/metadata
    # representation; the marker rows carry non-__final__ attempt keys. The
    # inner ``Run.id`` reference auto-correlates to the outer runs FROM.
    final_row_exists = exists().where(
        RunNodeOutput.run_id == Run.id,
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
    )
    marker_row_exists = exists().where(
        RunNodeOutput.run_id == Run.id,
        RunNodeOutput.attempt_key != FINAL_ATTEMPT_KEY,
    )

    run_stmt = (
        select(Run.id, Run.outputs_json, Run.node_telemetry_json, Run.raw_output_markers, Run.completed_at)
        .where(
            Run.organisation_id == organisation_id,
            Run.status.in_(sorted(TERMINAL_STATUSES)),
        )
        .order_by(Run.completed_at.asc())
        .limit(cap)
    )
    if high_water_mark is not None:
        run_stmt = run_stmt.where(Run.completed_at > high_water_mark)
    # Trigger predicate: only runs the sweep can actually heal enter the
    # batch (absent representation OR absent marker rows) — a drained org
    # selects nothing on the steady-state tick. A side counts as PRESENT only
    # when it holds a real JSON value (see _side_present); the MARKERS side
    # additionally excludes '{}' ("no markers" is not representable — see
    # _markers_present).
    run_stmt = run_stmt.where(
        or_(
            and_(
                or_(
                    _side_present(Run.outputs_json),
                    _side_present(Run.node_telemetry_json),
                    _markers_present(Run.raw_output_markers),
                ),
                ~final_row_exists,
            ),
            and_(_markers_present(Run.raw_output_markers), ~marker_row_exists),
        )
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
    max_completed: datetime | None = high_water_mark

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
        outputs = _ensure_dict(run_row.outputs_json)
        telemetry = _ensure_dict(run_row.node_telemetry_json)
        markers = _ensure_dict(run_row.raw_output_markers)
        completed_at = run_row.completed_at
        if max_completed is None or (completed_at is not None and completed_at > max_completed):
            max_completed = completed_at

        if (
            _has_sentinel_node_id(outputs)
            or _has_sentinel_node_id(telemetry)
            or _markers_have_sentinel_node_id(markers)
        ):
            counts["runs_quarantined"] += 1
            continue

        if outputs is None and telemetry is None and not (isinstance(markers, dict) and markers):
            # No meaningful legacy blob — nothing to represent.
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
                if key.startswith("__"):
                    # Sentinel-namespace attempt key — '__final__' would also
                    # violate ck_run_node_outputs_final_no_markers. Skipped,
                    # never aborting the sweep (data never aborts).
                    continue
                node_id = parse_marker_node_id(key)
                rows.append(NodeOutputWrite(node_id=node_id, attempt_key=key, markers=value))
                if node_id == UNKNOWN_NODE_ID:
                    counts["unknown_marker_keys"] += 1

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
            await _upsert_rows(
                session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=True
            )
            counts["runs_backfilled"] += 1
            counts["rows_written"] += len(rows)

    counts["new_high_water"] = max_completed
    return counts
