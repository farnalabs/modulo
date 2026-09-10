"""run_node_outputs — the single chokepoint for the per-node blob store (FAR-583).

Every read and write of the ``run_node_outputs`` table goes through this
module. It provides:

* dialect-guarded upserts (Postgres/SQLite ``ON CONFLICT``; MariaDB explicitly
  guarded out — no ``on_conflict`` support and deprecated) with an explicit
  ``updated_at = current_timestamp()`` in ``set_`` (the ``system_config``
  precedent: ``TimestampMixin.onupdate`` does not fire for Core upserts);
* the PRIMARY-write helpers the store chokepoints call
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
   the columns. B1 (design-doc addendum) cut the legacy columns' ORM mapping
   first — every legacy-column access here (and the fenced markers reader's
   joined legacy leg, and the marker dual-write's legacy leg) runs through
   the raw Core table :data:`RUNS_LEGACY_TABLE`.

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
    func,
    null,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
    # Dialect/organisation write primitives shared by the module's own write
    # paths (upserts + the legacy-table writers). Normal public names of the
    # storage module.
    "assert_write_org",
    "dialect_insert",
    "parse_marker_node_id",
    "read_legacy_raw_output_markers",
    "read_legacy_run_blobs",
    "read_node_output_blob_bytes",
    "read_run_blobs_with_fallback",
    "read_run_markers_fenced",
    "read_run_markers_with_fallback",
    "read_run_node_outputs_raw",
    "read_run_outputs_with_fallback",
    "read_run_telemetry_with_fallback",
    "replace_run_node_outputs",
    "resolve_dialect",
    "upsert_rows",
    "write_legacy_raw_output_markers",
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
# or 'connector'. MUST stay byte-identical to migration 0192's bound regex
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


def _reassemble_markers(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """The flat markers dict from (attempt_key, value) items in jsonb-canonical
    key order — the ONE reassembly site used by both the ``__final__``-row
    assembler (:func:`_assemble`) and the fenced reader
    (:func:`read_run_markers_fenced`), so the two can never drift apart."""
    return dict(sorted(items, key=lambda item: _jsonb_canonical_key(item[0])))


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


def resolve_dialect(session: AsyncSession) -> str:
    """The bind's dialect name (SQLAlchemy 2.x ``AsyncSession.get_bind`` is
    sync — the defensive iscoroutine dance the 1.x habits suggested is dead
    code and was removed)."""
    return session.get_bind().dialect.name


def dialect_insert(dialect: str) -> Any:
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


# The quarantine side table (migration 0192) as a CORE-ONLY Table —
# deliberately NOT an ORM model (ops/remediation surface: written by the
# migration, read by ops SQL only). Column set matches migration 0192's DDL
# exactly (JSONB on Postgres via the variant, generic JSON elsewhere).
# NOTE: remove this Core table (and the retention purge's delete) when the
# quarantine table itself drops (B2b+).
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


# The legacy ``runs`` blob columns as a CORE-ONLY Table (B1 — FAR-583): the
# ORM mapping of ``outputs_json`` / ``node_telemetry_json`` /
# ``raw_output_markers`` was CUT from :class:`modulo.db.models.run.Run` in B1,
# but the columns EXIST IN THE DATABASE until migration 0194 (B2b), so the
# EMPTY/MISMATCH fallback readers, the fenced markers read, and the marker
# dual-write's legacy read-merge-write leg
# select/patch them through this table. Statement-shaped parameterised SQL
# with the same ``Uuid``/``JSON``-variant types the ORM mapping carried —
# SQLAlchemy's type-aware binding keeps every dialect's UUID/JSON
# bind-and-result round-trip identical to the ORM's (SQLite stores ``Uuid`` in
# the non-text form a plain string bind parameter could not match). Postgres'
# runs RLS policy is a statement-level property (the ``app.organisation_id``
# session setting), so raw-core SELECTs against ``runs`` are RLS-filtered
# exactly like ORM ones; read paths additionally bound the session's org
# explicitly (the compensation for the ORM-only generic-backend tenant filter
# this Core statement bypasses — see :func:`_read_legacy_run_blobs`).
# Dies with the columns at B2b together with every reader of these columns.
_RUNS_LEGACY_METADATA = MetaData()
RUNS_LEGACY_TABLE = Table(
    "runs",
    _RUNS_LEGACY_METADATA,
    Column("id", Uuid(), primary_key=True),
    Column("organisation_id", Uuid(), nullable=False),
    Column("status", String(30), nullable=False),
    Column("claim_token", String(128), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("outputs_json", JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    Column("node_telemetry_json", JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    Column("raw_output_markers", JSON().with_variant(JSONB(), "postgresql"), nullable=True),
)


def _validate_sentinel_keys(
    mapping: dict[str, Any] | None,
    *,
    kind: str,
    attempt_keys: bool,
) -> None:
    """Reject sentinel-namespace squatting in an incoming write.

    * Every key starting with ``__`` is rejected for both kinds.
    * ``attempt_keys=True`` (markers): a PARSEABLE key whose DERIVED node id
      is ``__``-prefixed is also rejected (qa M19b — sentinel squatting via
      the grammar: ``run:<uuid>:node:__sneaky__:1`` would land on the
      reserved node-id namespace). Unparseable keys map to ``__unknown__``,
      which is the ALLOWED sentinel row (evidence preserved, FAR-188).

    *attempt_keys* is a REQUIRED keyword: the derived-node-id check only has
    meaning for marker attempt keys (outputs/telemetry keys ARE node ids), so
    a call that forgets it is a bug, not a default.
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


async def assert_write_org(session: AsyncSession, organisation_id: uuid.UUID | None) -> None:
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


async def upsert_rows(
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
    insert_factory = dialect_insert(resolve_dialect(session))
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
    await assert_write_org(session, organisation_id)

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
        await upsert_rows(session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=False)

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
        await upsert_rows(
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
    await assert_write_org(session, organisation_id)

    rows = [
        NodeOutputWrite(node_id=parse_marker_node_id(key), attempt_key=key, markers=value)
        for key, value in markers.items()
    ]
    if rows:
        await upsert_rows(session, run_id=run_id, organisation_id=organisation_id, rows=rows, ignore_conflicts=False)

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
        markers = _reassemble_markers(marker_items)

    return RunBlobs(outputs=outputs, telemetry=telemetry, markers=markers), info


async def read_run_node_outputs_raw(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> RunBlobs:
    """Reassemble the legacy dict shapes from the new table — NO fallback.

    Used by callers that must see exactly what the new table holds (parity
    checks). Raises :class:`OutputsRlsMismatch` when the session org
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


async def _read_legacy_run_blobs(
    session: AsyncSession, run_id: uuid.UUID, organisation_id: uuid.UUID | None
) -> RunBlobs:
    """Single parameterised SELECT of the legacy ``runs`` blob columns.

    The one sanctioned legacy read in this module (the EMPTY/MISMATCH
    fallback; B2b removes it with the columns). B1 note fulfilled: the read is
    COLUMN-LEVEL SQL against :data:`RUNS_LEGACY_TABLE` (bound ``run_id``/
    ``organisation_id`` — never f-stringed, never an ORM attribute reference
    since the ORM mapping was cut). It stays a single statement on every
    dialect and picks up the runs RLS policy on Postgres (a session-level
    property); *organisation_id*, when the session has an org context, is
    included in the WHERE as the compensation for the ORM-only generic-backend
    tenant filter this Core statement bypasses (a mismatched legacy read
    degrades to an absent side, which the caller resolves toward the NEW
    table — never toward another tenant's data).
    """
    stmt = select(
        RUNS_LEGACY_TABLE.c.outputs_json,
        RUNS_LEGACY_TABLE.c.node_telemetry_json,
        RUNS_LEGACY_TABLE.c.raw_output_markers,
    ).where(RUNS_LEGACY_TABLE.c.id == run_id)
    if organisation_id is not None:
        stmt = stmt.where(RUNS_LEGACY_TABLE.c.organisation_id == organisation_id)
    row = (await session.execute(stmt)).first()
    if row is None:
        return RunBlobs(outputs=None, telemetry=None, markers=None)
    return RunBlobs(
        outputs=_ensure_dict(row.outputs_json),
        telemetry=_ensure_dict(row.node_telemetry_json),
        markers=_ensure_dict(row.raw_output_markers),
    )


async def read_legacy_run_blobs(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> RunBlobs:
    """Public read of the legacy ``runs`` blob columns (raw parameterised SQL).

    The chokepoints' PRE-WRITE capture source (crud.run's ORM + fenced
    branches capture the legacy dicts for the M19 inherited-sentinel filter —
    the caller's write NEVER shadows a pre-existing sentinel key). B2b removes
    this together with the columns.
    """
    return await _read_legacy_run_blobs(session, run_id, organisation_id)


async def read_legacy_raw_output_markers(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The legacy ``runs.raw_output_markers`` dict, raw parameterised SQL.

    Serves the marker dual-write's legacy read-merge-write leg (node_runner's
    ``_write_raw_output_marker``) after the ORM mapping was cut at B1; the
    *organisation_id* predicate applies when given (the caller's RLS org).
    """
    stmt = select(RUNS_LEGACY_TABLE.c.raw_output_markers).where(RUNS_LEGACY_TABLE.c.id == run_id)
    if organisation_id is not None:
        stmt = stmt.where(RUNS_LEGACY_TABLE.c.organisation_id == organisation_id)
    return _ensure_dict((await session.execute(stmt)).scalar_one_or_none())


async def write_legacy_raw_output_markers(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    markers: dict[str, Any],
    organisation_id: uuid.UUID | None = None,
) -> None:
    """Parameterised UPDATE of the legacy ``runs.raw_output_markers`` column.

    The marker dual-write's legacy leg after the ORM mapping was cut at B1
    (the column exists in the database until B2b; the caller holds the run row
    FOR UPDATE, so this serialises exactly like the former ORM assignment —
    including the ``updated_at`` stamp the former ORM leg fired via
    ``TimestampMixin.onupdate``, written explicitly here as
    ``current_timestamp()``). The *organisation_id* predicate applies when
    given (mirrors :func:`read_legacy_raw_output_markers`'s tenant
    compensation). A ``*markers* of ``{}`` writes SQL NULL (the legacy
    no-markers state) — the JSON null VALUE is not used for the markers
    column.
    """
    stmt = (
        update(RUNS_LEGACY_TABLE)
        .where(RUNS_LEGACY_TABLE.c.id == run_id)
        .values(raw_output_markers=markers or _sql_null(), updated_at=func.current_timestamp())
    )
    if organisation_id is not None:
        stmt = stmt.where(RUNS_LEGACY_TABLE.c.organisation_id == organisation_id)
    await session.execute(stmt)


def _direction_aware_side(
    new_value: dict[str, Any] | None,
    legacy_value: dict[str, Any] | None,
    *,
    side: str,
    run_id: uuid.UUID,
    meta_row_exists: bool | None = None,
) -> dict[str, Any] | None:
    """DIRECTION-AWARE legacy fallback decision for one blob side (qa M2/M3).

    Compares the LEGACY key set with the REASSEMBLED key set:

    * legacy ⊆ new (incl. equal, incl. legacy empty) → serve NEW — the legacy
      column is a stale subset (legacy is only ever behind, since the legacy
      writes stopped at B1), so an already-represented run is NOT shadowed;
    * new ⊂ legacy (proper) → serve LEGACY — the truncation guard: the new
      table is missing rows the legacy column has (pre-B1 straggler rows),
      legacy is the more complete store;
    * divergent (neither subset) → serve NEW + a warning log (neither store
      is a superset; the new table is the forward-looking authority and the
      divergence is reported for remediation);
    * legacy empty + new non-empty → serve NEW (the existing truncation-hole
      guard: an empty legacy column carries no truth to fall back to) —
      EXCEPT the shrink case below.

    Shrink case (qa iteration 2, Major 1 — *meta_row_exists* is not None):
    when the metadata row is ABSENT (``False``) and the LEGACY side is exactly
    ``{}`` while the new side is non-empty → serve LEGACY. ``meta-absent +
    legacy-'{}'`` can only arise from a POST-representation legacy-only write:
    a backfill over legacy ``{}`` would have written the metadata row, so a
    represented run whose legacy side reads ``{}`` with no metadata row had
    its legacy column rewritten afterwards — legacy is fresher. Only ever
    passed for the outputs/telemetry sides (markers have no metadata row).

    Empty-legacy + new empty → equal sets → serve NEW (both carry nothing).
    """
    legacy_keys = set(legacy_value) if legacy_value else set()
    new_keys = set(new_value) if new_value is not None else set()
    if not legacy_keys:
        if meta_row_exists is False and legacy_value == {} and new_keys:
            _log.info(
                "run_node_outputs fallback: %s served from the legacy column (metadata row absent "
                "with a legacy-'{}' rewrite — post-representation legacy-only write) run=%s",
                side,
                run_id,
            )
            return legacy_value
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
    (pre-B1 straggler rows). When the new table DOES represent the side, the
    subset-direction rule (:func:`_direction_aware_side`) decides between the
    reassembled dict and the legacy column by key-set containment —
    legacy ⊆ new serves NEW (legacy is only ever behind post-B1), new ⊂
    legacy serves LEGACY (truncation guard), a divergent key set serves NEW
    with a warning.

    The shrink blind spot (metadata row absent + legacy ``{}`` + new
    non-empty → LEGACY) is applied UNCONDITIONALLY for the outputs/telemetry
    sides — it is deterministic (qa iteration 2, Major 1).

    (When the metadata row exists, the writer invariants guarantee the
    reassembled value already matches the legacy column, including the
    ``{}``-vs-``None`` distinction.) The legacy read is one extra
    parameterised SELECT per call; B2b removes it together with the columns.
    """
    rows = await _fetch_output_rows(session, run_id)
    session_org = await read_rls_org(session)
    blobs, info = _assemble(rows)
    legacy = await _read_legacy_run_blobs(session, run_id, session_org)
    if rows:
        _assert_read_org(session_org, rows[0].organisation_id, run_id)
    else:
        _assert_read_org(session_org, organisation_id, run_id)

    # No new-table representation for a side -> serve the legacy column
    # verbatim (pre-B1 straggler rows); a represented side goes through the
    # subset-direction rule. meta_exists drives the shrink blind spot
    # (outputs/telemetry only — markers have no metadata row, so the marker
    # call passes None).
    outputs = legacy.outputs
    telemetry = legacy.telemetry
    if info.out_present or info.meta_exists:
        outputs = _direction_aware_side(
            blobs.outputs,
            legacy.outputs,
            side="outputs",
            run_id=run_id,
            meta_row_exists=info.meta_exists,
        )
    if info.telemetry_present or info.meta_exists:
        telemetry = _direction_aware_side(
            blobs.telemetry,
            legacy.telemetry,
            side="telemetry",
            run_id=run_id,
            meta_row_exists=info.meta_exists,
        )

    if not info.markers_present:
        markers = legacy.markers
    else:
        markers = _direction_aware_side(
            blobs.markers,
            legacy.markers,
            side="markers",
            run_id=run_id,
        )

    return RunBlobs(outputs=outputs, telemetry=telemetry, markers=markers)


async def read_run_outputs_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled outputs dict (legacy shape), direction-aware legacy fallback."""
    return (await read_run_blobs_with_fallback(session, run_id=run_id, organisation_id=organisation_id)).outputs


async def read_run_telemetry_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled telemetry dict (legacy shape), direction-aware legacy fallback."""
    return (await read_run_blobs_with_fallback(session, run_id=run_id, organisation_id=organisation_id)).telemetry


async def read_run_markers_with_fallback(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled flat markers dict, direction-aware legacy fallback."""
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
    FENCED JOINED ROW — the fence predicates apply to the runs row (bound
    through the raw Core legacy table since B1's ORM cut), and the fallback
    read therefore re-checks the fence by construction (a fence-miss can never
    serve legacy markers either). Reassembly uses the same
    subset-direction rule as :func:`read_run_blobs_with_fallback`.

    ``claim_token=None`` skips the token predicate (the ``:tok IS NULL OR
    claim_token = :tok`` fence idiom) — a caller that fences by other means
    (e.g. the connector gate's plain run-row FOR UPDATE) passes None.
    """
    fence: list[Any] = [
        RUNS_LEGACY_TABLE.c.id == run_id,
        RUNS_LEGACY_TABLE.c.organisation_id == organisation_id,
    ]
    if fence_status:
        fence.append(RUNS_LEGACY_TABLE.c.status == _FENCED_RUNNING_STATUS)
    if claim_token is not None:
        fence.append(RUNS_LEGACY_TABLE.c.claim_token == claim_token)

    stmt = (
        select(
            RUNS_LEGACY_TABLE.c.raw_output_markers,
            RunNodeOutput.attempt_key,
            RunNodeOutput.raw_output_markers,
        )
        .join(
            RunNodeOutput,
            and_(
                RunNodeOutput.run_id == RUNS_LEGACY_TABLE.c.id,
                RunNodeOutput.attempt_key != FINAL_ATTEMPT_KEY,
                RunNodeOutput.raw_output_markers.is_not(None),
            ),
            isouter=True,
        )
        .where(and_(*fence))
    )
    if for_update:
        # FOR UPDATE OF runs — Postgres-only; SQLite ignores FOR UPDATE.
        stmt = stmt.with_for_update(of=RUNS_LEGACY_TABLE)

    session_org = await read_rls_org(session)
    _assert_read_org(session_org, organisation_id, run_id)
    joined = (await session.execute(stmt)).all()
    if not joined:
        return None  # fence miss — nothing is visible, legacy included

    legacy = _ensure_dict(joined[0][0])
    marker_items = [(row[1], row[2]) for row in joined if row[1] is not None]
    if not marker_items:
        return legacy
    reassembled: dict[str, Any] | None = _reassemble_markers(marker_items)
    return _direction_aware_side(
        reassembled,
        legacy,
        side="markers",
        run_id=run_id,
    )


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
