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
  legacy ``runs`` columns, served per the DIRECTION-AWARE subset rule
  (see :func:`read_run_blobs_with_fallback`); removed at B2b together with
  the columns. B1 NOTE (design-doc addendum): these fallback readers read the
  legacy columns via ORM attributes — B1 MUST convert them to raw
  parameterised SQL BEFORE dropping the model columns, so the fallback
  survives until B2b;
* the batched backfill helper (the catch-up sweep body) whose selection is
  driven by the NOT-EXISTS trigger legs + jsonb-native object predicates on
  Postgres, the per-tick cap, and ``ORDER BY completed_at ASC`` — NO
  high-water filter (qa M20: ``completed_at > mark`` could permanently miss
  runs whose terminalizing transaction STARTED before the mark advanced;
  ``completed_at`` is transaction-start ``now()``). The mark is still
  accepted/returned for observability. A partial index on
  ``(organisation_id, completed_at) WHERE status IN (terminal)`` is the
  documented post-deploy ops step backing the selection.

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

import json
import logging
import re
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Uuid,
    and_,
    cast,
    delete,
    exists,
    func,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_node_outputs import (
    FINAL_ATTEMPT_KEY,
    META_NODE_ID,
    UNKNOWN_NODE_ID,
    RunNodeOutput,
    json_bytes,
)
from modulo.db.rls import OutputsRlsMismatch, read_rls_org

_log = logging.getLogger(__name__)

__all__ = [
    "DualWriteError",
    "OutputsSentinelViolation",
    "RunBlobs",
    "backfill_run_node_outputs_batch",
    "parse_marker_node_id",
    "read_node_output_blob_bytes",
    "read_run_blobs_with_fallback",
    "read_run_markers_fenced",
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


def _resolve_dialect(session: AsyncSession) -> str:
    """The bind's dialect name (SQLAlchemy 2.x ``AsyncSession.get_bind`` is
    sync — the defensive iscoroutine dance the 1.x habits suggested is dead
    code and was removed)."""
    return session.get_bind().dialect.name


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


# The quarantine side table (migration 0176) as a CORE-ONLY Table —
# deliberately NOT an ORM model (ops/remediation surface: written by the
# migration + this sweep body, read by ops SQL only). Column set matches
# migration 0176's DDL exactly (JSONB on Postgres via the variant, generic
# JSON elsewhere).
_QUARANTINE_METADATA = MetaData()
QUARANTINE_TABLE = Table(
    "run_node_outputs_quarantine",
    _QUARANTINE_METADATA,
    Column("run_id", Uuid(), primary_key=True),
    Column("organisation_id", Uuid(), nullable=False),
    Column("legacy_outputs_json", JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    Column("legacy_node_telemetry_json", JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    Column("legacy_raw_output_markers", JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    Column("quarantined_at", DateTime(timezone=True), nullable=False),
)


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

    Mirrors migration 0176's quarantine step for data the sweep encounters
    post-deploy: the blobs are preserved AS-IS (evidence), the run is
    EXCLUDED from all future sweep selections (the NOT-EXISTS trigger leg on
    ``run_node_outputs_quarantine``), and the reason is logged — data
    anomalies never abort the sweep, and never silently re-select forever.
    """
    insert_factory = _dialect_insert(_resolve_dialect(session))
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


def _validate_sentinel_keys(
    mapping: dict[str, Any] | None,
    *,
    kind: str,
    attempt_keys: bool = False,
) -> None:
    """Reject sentinel-namespace squatting in an incoming write.

    * Every key starting with ``__`` is rejected for both kinds.
    * ``attempt_keys=True`` (markers): a PARSEABLE key whose DERIVED node id
      is ``__``-prefixed is also rejected (qa M19b — sentinel squatting via
      the grammar: ``run:<uuid>:node:__sneaky__:1`` would land on the
      reserved node-id namespace). Unparseable keys map to ``__unknown__``,
      which is the ALLOWED sentinel row (evidence preserved, FAR-188).
    """
    if not mapping:
        return
    for key in mapping:
        if key.startswith("__"):
            raise OutputsSentinelViolation(
                f"{kind} key {key!r} squats the reserved '__' sentinel namespace "
                "(run_node_outputs metadata/unknown sentinels)"
            )
        if attempt_keys:
            node_id = parse_marker_node_id(key)
            if node_id != UNKNOWN_NODE_ID and node_id.startswith("__"):
                raise OutputsSentinelViolation(
                    f"{kind} key {key!r} derives node id {node_id!r}, squatting the reserved '__' sentinel namespace"
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
    insert_factory = _dialect_insert(_resolve_dialect(session))
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
    inherited_outputs: dict[str, Any] | None = None,
    inherited_telemetry: dict[str, Any] | None = None,
) -> dict[str, int]:
    """REPLACE-write the '__final__' + metadata rows for one run.

    Mirrors the legacy column semantics losslessly: a non-None dict fully
    replaces that side (absent keys blanked — see :func:`_blank_absent_side`);
    a None dict leaves that side untouched (the legacy write leaves the
    column alone). The metadata row is upserted iff at least one side is
    ``{}``, its flags re-derived from full run state: a provided side derives
    its flag from the incoming dict; an untouched (None) side keeps the flag
    already stored (``False`` when no metadata row exists — that side is
    populated-or-NULL, never ``{}``).

    INHERITED SENTINEL KEYS (qa M19): the incoming dicts may still carry
    ``__``-prefixed node ids INHERITED from the already-stored legacy state
    (pre-0176 legacy data) — without special handling every such REPLACE
    would raise and leave the run permanently un-finalizable. Keys present
    in *inherited_outputs* / *inherited_telemetry* (the caller-captured
    PRE-WRITE legacy dicts) are FILTERED from the new-table write — the
    caller's legacy write retains them — and counted in the returned
    ``outputs_dual_write_sentinel_filtered`` (the core orchestrator wires the
    counter). A ``__``-prefixed key NOT already stored (newly introduced by
    the caller) still raises :class:`OutputsSentinelViolation`; when the
    caller captured no legacy dict (``None``), every ``__``-prefixed key
    raises (fail-closed — nothing proves it was inherited).

    Returns ``{"outputs_dual_write_sentinel_filtered": <int>}``.
    """
    filtered = _split_inherited_sentinel_keys(outputs, inherited_outputs, kind="outputs node id")
    filtered |= _split_inherited_sentinel_keys(telemetry, inherited_telemetry, kind="telemetry node id")
    await _assert_write_org(session, organisation_id)

    outputs_map: dict[str, Any] = {k: v for k, v in (outputs or {}).items() if k not in filtered}
    telemetry_map: dict[str, Any] = {k: v for k, v in (telemetry or {}).items() if k not in filtered}

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
    # drop rows left with no value at all. The blanking key sets are the
    # FILTERED maps — the new table only ever holds non-sentinel keys.
    if outputs is not None:
        await _blank_absent_side(session, run_id=run_id, side="outputs_json", keys=set(outputs_map))
    if telemetry is not None:
        await _blank_absent_side(session, run_id=run_id, side="node_telemetry_json", keys=set(telemetry_map))
    await _drop_dead_final_rows(session, run_id=run_id)
    # Metadata flags derive from the FULL incoming dicts (pre-filter): the
    # flags describe the run state the legacy column now holds, and an
    # inherited-sentinel key makes a side non-empty either way.
    await _refresh_metadata_row(
        session,
        run_id=run_id,
        organisation_id=organisation_id,
        outputs=outputs,
        telemetry=telemetry,
    )
    return {"outputs_dual_write_sentinel_filtered": len(filtered)}


def _split_inherited_sentinel_keys(
    incoming: dict[str, Any] | None,
    inherited: dict[str, Any] | None,
    *,
    kind: str,
) -> set[str]:
    """Validate the incoming dict's sentinel keys and return the inherited ones.

    A ``__``-prefixed key that was ALREADY STORED in the legacy dict
    (*inherited*) is returned for filtering; one that is NOT in *inherited*
    (or when *inherited* is None — nothing captured) raises
    :class:`OutputsSentinelViolation`. Non-sentinel keys are untouched.
    """
    if not incoming:
        return set()
    inherited_keys = set(inherited) if inherited else set()
    filtered: set[str] = set()
    for key in incoming:
        if not key.startswith("__"):
            continue
        if inherited_keys and key in inherited_keys:
            filtered.add(key)
            continue
        raise OutputsSentinelViolation(
            f"{kind} key {key!r} squats the reserved '__' sentinel namespace "
            "(run_node_outputs metadata/unknown sentinels)"
        )
    return filtered


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
    _validate_sentinel_keys(markers, kind="marker attempt key", attempt_keys=True)
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
            flags = r.outputs_json
            if not isinstance(flags, dict) or "empty_outputs" not in flags or "empty_telemetry" not in flags:
                # qa M21 — READ paths FAIL OPEN: a malformed metadata row is
                # treated as ABSENT (the run's node rows / legacy fallback
                # still serve); raising here would brick every reader for the
                # run. The WRITE side keeps validating strictly
                # (_validate_sentinel_keys + the writer's own flag payload),
                # so a malformed row can only be corruption — logged, never
                # fatal on read.
                _log.warning(
                    "run_node_outputs malformed metadata row treated as absent "
                    "(expected empty_outputs/empty_telemetry booleans, got %r)",
                    flags,
                )
                continue
            info.meta_exists = True
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


def _direction_aware_side(
    new_value: dict[str, Any] | None,
    legacy_value: dict[str, Any] | None,
    *,
    side: str,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """DIRECTION-AWARE legacy fallback decision for one blob side (qa M2/M3).

    Compares the LEGACY key set with the REASSEMBLED key set:

    * legacy ⊆ new (incl. equal, incl. legacy empty) → serve NEW — the legacy
      column is a stale subset (kill-switch-OFF write to an already-
      represented run is NOT shadowed; legacy is only ever behind);
    * new ⊂ legacy (proper) → serve LEGACY — the truncation guard: the new
      table is missing rows the legacy column has (partial sweep / partial
      write), legacy is the more complete store;
    * divergent (neither subset) → serve NEW + a warning log (neither store
      is a superset; the new table is the forward-looking authority and the
      divergence is reported for remediation);
    * legacy empty + new non-empty → serve NEW (the existing truncation-hole
      guard: an empty legacy column carries no truth to fall back to).

    Empty-legacy + new empty → equal sets → serve NEW (both carry nothing).
    """
    legacy_keys = set(legacy_value) if legacy_value else set()
    new_keys = set(new_value) if new_value is not None else set()
    if not legacy_keys:
        return new_value
    if legacy_keys <= new_keys:
        return new_value
    if new_keys < legacy_keys:
        _log.info(
            "run_node_outputs fallback: %s served from the legacy column (new table truncated) run=%s",
            side,
            run_id,
        )
        return legacy_value
    _log.warning(
        "run_node_outputs fallback: %s key sets diverge between the legacy column and "
        "the new table (serving the new table) run=%s legacy_only=%s new_only=%s",
        side,
        run_id,
        sorted(legacy_keys - new_keys),
        sorted(new_keys - legacy_keys),
    )
    return new_value


async def read_run_blobs_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> RunBlobs:
    """Reassemble with the DIRECTION-AWARE legacy fallback applied (qa M2/M3).

    Per side: when the new table holds NO representation for the side at all
    (no node rows AND no metadata row) the legacy column is served verbatim
    (pre-sweep stragglers / kill-switch-off mode). When the new table DOES
    represent the side, the subset-direction rule
    (:func:`_direction_aware_side`) decides between the reassembled dict and
    the legacy column by key-set containment — legacy ⊆ new serves NEW (a
    kill-switch-OFF legacy-only write to an already-represented run is NOT
    shadowed forever), new ⊂ legacy serves LEGACY (truncation guard), a
    divergent key set serves NEW with a warning.

    (When the metadata row exists, the backfill/writer invariants guarantee
    the reassembled value already matches the legacy column, including the
    ``{}``-vs-``None`` distinction.) The legacy read is one extra
    parameterised SELECT per call; B2b removes it together with the columns.

    B1 NOTE: this reader (and the fenced markers reader) reads the legacy
    columns via ORM attributes — B1 MUST convert these to raw parameterised
    SQL BEFORE dropping the model columns (design-doc addendum).
    """
    rows = await _fetch_output_rows(session, run_id)
    session_org = await read_rls_org(session)
    _assert_read_org(session_org, rows[0].organisation_id if rows else organisation_id, run_id)
    blobs, info = _assemble(rows)
    legacy = await _read_legacy_run_blobs(session, run_id)

    # No new-table representation for a side -> serve the legacy column
    # verbatim (pre-sweep stragglers / kill-switch-off mode); a represented
    # side goes through the subset-direction rule.
    outputs = legacy.outputs
    telemetry = legacy.telemetry
    if info.out_present or info.meta_exists:
        outputs = _direction_aware_side(blobs.outputs, legacy.outputs, side="outputs", run_id=run_id)
    if info.telemetry_present or info.meta_exists:
        telemetry = _direction_aware_side(blobs.telemetry, legacy.telemetry, side="telemetry", run_id=run_id)

    if not info.markers_present:
        markers = legacy.markers
    else:
        markers = _direction_aware_side(blobs.markers, legacy.markers, side="markers", run_id=run_id)

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


_FENCED_RUNNING_STATUS = "running"


async def read_run_markers_fenced(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    claim_token: str | None,
    for_update: bool = False,
    fence_status: bool = True,
) -> dict[str, Any] | None:
    """Fenced, single-statement markers read (qa M4/M5).

    ONE statement: the ``runs`` row is fetched under the fence predicates
    (``id``, ``organisation_id``, ``claim_token`` when given, and — by
    default — ``status = 'running'``) and LEFT-JOINed to the run's marker
    rows (``attempt_key != '__final__'`` AND ``raw_output_markers IS NOT
    NULL``); the markers reassemble flat in the same Python pass. A fence
    miss (wrong claim token / wrong status / missing run) yields ZERO rows —
    the caller gets ``None``, byte-for-byte the same visibility the fenced
    single-column gate read had (FAR-228 predicate-fenced gate).

    ``for_update=True`` adds ``FOR UPDATE OF runs`` (Postgres) so the
    connector-caller's gate decision serialises on the run row exactly like
    its previous raw ``SELECT ... FOR UPDATE``; SQLite renders no FOR UPDATE
    (no-op — matching every other generic-backend read).

    ``fence_status=False`` (qa Minor 4) drops the ``status = 'running'``
    predicate — ONLY the connector-caller path (``for_update=True``) uses it,
    preserving the OLD connector rewrite read's semantics exactly: that read
    had NO status predicate, so during a concurrent cancel it still served
    the suppression evidence (a ``delivery_done`` marker) instead of a
    fence-miss ``None`` — a ``None`` there would suppress nothing and risk a
    DUPLICATE connector write. The FAR-228 dispatch gate keeps the default
    ``fence_status=True`` (its predicate-fenced read always included the
    status check — a cancelled run must gate-serve nothing). The id + org
    (+ claim-token when given) predicates are ALWAYS applied on both paths.

    The DIRECTION-AWARE legacy fallback (qa M2/M3) is part of the contract:
    the legacy ``runs.raw_output_markers`` column is selected FROM THE SAME
    FENCED JOINED ROW — the fence predicates apply to the runs row, and the
    fallback read therefore re-checks the fence by construction (a fence-miss
    can never serve legacy markers either). Reassembly uses the same
    subset-direction rule as :func:`read_run_blobs_with_fallback`.

    ``claim_token=None`` skips the token predicate (the ``:tok IS NULL OR
    claim_token = :tok`` fence idiom) — a caller that fences by other means
    (e.g. the connector gate's plain run-row FOR UPDATE) passes None.
    """
    fence: list[Any] = [
        Run.id == run_id,
        Run.organisation_id == organisation_id,
    ]
    if fence_status:
        fence.append(Run.status == _FENCED_RUNNING_STATUS)
    if claim_token is not None:
        fence.append(Run.claim_token == claim_token)

    stmt = (
        select(Run.raw_output_markers, RunNodeOutput.attempt_key, RunNodeOutput.raw_output_markers)
        .join(
            RunNodeOutput,
            and_(
                RunNodeOutput.run_id == Run.id,
                RunNodeOutput.attempt_key != FINAL_ATTEMPT_KEY,
                RunNodeOutput.raw_output_markers.is_not(None),
            ),
            isouter=True,
        )
        .where(and_(*fence))
    )
    if for_update:
        # FOR UPDATE OF runs — Postgres-only; SQLite ignores FOR UPDATE.
        stmt = stmt.with_for_update(of=Run)

    session_org = await read_rls_org(session)
    _assert_read_org(session_org, organisation_id, run_id)
    joined = (await session.execute(stmt)).all()
    if not joined:
        return None  # fence miss — nothing is visible, legacy included

    legacy = _ensure_dict(joined[0][0])
    marker_items = [(row[1], row[2]) for row in joined if row[1] is not None]
    if not marker_items:
        return legacy
    reassembled: dict[str, Any] | None = dict(sorted(marker_items, key=lambda item: _jsonb_canonical_key(item[0])))
    return _direction_aware_side(reassembled, legacy, side="markers", run_id=run_id)


async def read_node_output_blob_bytes(
    session: AsyncSession,
    run_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Per-run summed blob bytes for retention accounting — EXCLUDES metadata
    rows (the flags payload overhead would skew the accounting). One batched
    query; Python-side ``len(json.dumps(value, default=str))`` per present
    side (the shared :func:`modulo.db.models.run_node_outputs.json_bytes`
    formula, also used by ``crud.run_retention``)."""
    if not run_ids:
        return {}
    rows = (await session.execute(select(*_ROW_COLUMNS).where(RunNodeOutput.run_id.in_(list(run_ids))))).all()
    totals: dict[uuid.UUID, int] = {}
    for r in rows:
        if r.attempt_key == FINAL_ATTEMPT_KEY and r.node_id == META_NODE_ID:
            continue
        run_key = uuid.UUID(str(r.run_id))
        total = totals.get(run_key, 0)
        total += 0 if r.outputs_absent else json_bytes(r.outputs_json)
        total += 0 if r.telemetry_absent else json_bytes(r.node_telemetry_json)
        total += 0 if r.markers_absent else json_bytes(r.raw_output_markers)
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
    permanently miss runs whose terminalizing transaction STARTED before
    the mark advanced (``completed_at`` is transaction-start ``now()``) —
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
    markers leg lives in migration 0176 only: the sweep heals TERMINAL
    runs, whose markers are complete.
    """
    await _assert_write_org(session, organisation_id)
    dialect = _resolve_dialect(session)

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
    quarantined_exists = exists().where(QUARANTINE_TABLE.c.run_id == Run.id)

    run_stmt = (
        select(Run.id, Run.outputs_json, Run.node_telemetry_json, Run.raw_output_markers, Run.completed_at)
        .where(
            Run.organisation_id == organisation_id,
            Run.status.in_(sorted(TERMINAL_STATUSES)),
        )
        .order_by(Run.completed_at.asc())
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
            _pg_side_object(Run.outputs_json),
            _pg_side_object(Run.node_telemetry_json),
            _pg_markers_object_nonempty(Run.raw_output_markers),
        )
        markers_meaningful = _pg_markers_object_nonempty(Run.raw_output_markers)
    else:
        side_meaningful = or_(
            _side_present(Run.outputs_json),
            _side_present(Run.node_telemetry_json),
            _markers_present(Run.raw_output_markers),
        )
        markers_meaningful = _markers_present(Run.raw_output_markers)
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
            await _upsert_rows(
                session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=True
            )
            counts["runs_backfilled"] += 1
            counts["rows_written"] += len(rows)

    counts["new_high_water"] = max_completed
    return counts
