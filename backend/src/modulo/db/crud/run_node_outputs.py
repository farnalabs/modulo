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
* Reads are NEW-TABLE-ONLY: B2b (migration 0212) dropped the legacy
   ``runs`` blob columns; the EMPTY/MISMATCH fallback machinery died with
   them. The only cross-table reader left is the fenced markers gate, which
   fences on ``runs.status`` / ``claim_token`` / ``organisation_id`` (ordinary
   columns the ORM still maps) without touching any blob column.

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

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
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
    "assert_write_org",
    "dialect_insert",
    "parse_marker_node_id",
    "read_node_output_blob_bytes",
    "read_run_blobs",
    "read_run_markers",
    "read_run_markers_fenced",
    "read_run_node_outputs_raw",
    "read_run_outputs",
    "read_run_telemetry",
    "replace_run_node_outputs",
    "resolve_dialect",
    "upsert_rows",
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


# The 0192 quarantine side table as a CORE-ONLY Table - deliberately NOT an
# ORM model (ops/remediation surface). **KEPT at B2b** (migration 0212 does
# NOT drop it): its rows are the only surviving copy of the 0192-quarantined
# legacy blobs (sentinel ``__``-prefixed keys could never be represented),
# and after 0212 the runs columns are gone. The retention purge's delete
# stays live; ops SQL still reads it.

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
    # B2b: the new table is the ONLY store. Inherited sentinel-namespace
    # node ids (pre-0176 legacy junk the writer filters on every REPLACE)
    # must SURVIVE the blanking - there is no legacy column to preserve
    # them any more. ``__run_meta__`` is excluded separately: its row is
    # the flags payload, never a side value.
    stmt = update(RunNodeOutput).where(
        RunNodeOutput.run_id == run_id,
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
        RunNodeOutput.node_id != META_NODE_ID,
        RunNodeOutput.node_id.notlike("\\_\\_%", escape="\\"),
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
    ``__``-prefixed node ids INHERITED from the already-stored state (the
    B2b repair migration re-mapped pre-0176 sentinel keys into the new
    table verbatim) - without special handling every such REPLACE would
    raise and leave the run permanently un-finalizable. Keys present in
    *inherited_outputs* / *inherited_telemetry* (the caller-captured
    CURRENT new-table state, :func:`read_run_node_outputs_raw`) are
    FILTERED from the REPLACE upserts (their rows SURVIVE the blanking -
    there is no legacy column any more) and counted in the returned
    ``outputs_dual_write_sentinel_filtered``. A ``__``-prefixed key NOT
    already stored (newly introduced by the caller) still raises
    :class:`OutputsSentinelViolation`; when the caller captured no state
    (``None``), every ``__``-prefixed key raises (fail-closed - nothing
    proves it was inherited).

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
    # FILTERED maps; inherited sentinel-key rows are EXCLUDED from the
    # blanking (see _blank_absent_side) and so survive either way.
    if outputs is not None:
        await _blank_absent_side(session, run_id=run_id, side="outputs_json", keys=set(outputs_map))
    if telemetry is not None:
        await _blank_absent_side(session, run_id=run_id, side="node_telemetry_json", keys=set(telemetry_map))
    await _drop_dead_final_rows(session, run_id=run_id)
    # Metadata flags derive from the FULL incoming dicts (pre-filter): the
    # flags describe the run state the store now holds, and an
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

    A ``__``-prefixed key that was ALREADY STORED in the caller-captured
    state (*inherited* - the CURRENT new-table blobs since B2b) is returned
    for filtering; one that is NOT in *inherited* (or when *inherited* is
    None - nothing captured) raises :class:`OutputsSentinelViolation`.
    Non-sentinel keys are untouched.
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


def _assemble(rows: Sequence[Any]) -> RunBlobs:
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
            meta_flags = flags
            continue
        if r.attempt_key == FINAL_ATTEMPT_KEY:
            if not r.outputs_absent:
                out_items.append((r.node_id, r.outputs_json))
            if not r.telemetry_absent:
                tel_items.append((r.node_id, r.node_telemetry_json))
            continue
        if not r.markers_absent:
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

    return RunBlobs(outputs=outputs, telemetry=telemetry, markers=markers)


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
    return _assemble(rows)


_FENCED_RUNNING_STATUS = "running"


async def _read_run_markers_fenced_rows(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    claim_token: str | None,
    for_update: bool,
    fence_status: bool,
) -> list[tuple[str | None, Any | None]]:
    """ONE statement: the ``runs`` row is fetched under the fence predicates
    (``id``, ``organisation_id``, ``claim_token`` when given, and — by
    default — ``status = 'running'``) and LEFT-JOINed to the run's marker
    rows (``attempt_key != '__final__'`` AND ``raw_output_markers IS NOT
    NULL``). A fence miss yields ZERO rows.

    ``for_update=True`` adds ``FOR UPDATE OF runs`` (Postgres; SQLite
    ignores FOR UPDATE). ``fence_status=False`` is ONLY the
    connector-caller path (preserving the old connector rewrite read's
    no-status-predicate semantics — a concurrent cancel still serves the
    suppression evidence instead of a fence-miss None). The id + org
    predicates are ALWAYS applied. B2b: the legacy joined leg is gone —
    the runs row is fetched through the ORM (:class:`modulo.db.models.run.Run`),
    whose status/claim_token/organisation_id columns are ordinary
    non-blob columns.
    """
    from modulo.db.models.run import Run

    fence: list[Any] = [
        Run.id == run_id,
        Run.organisation_id == organisation_id,
    ]
    if fence_status:
        fence.append(Run.status == _FENCED_RUNNING_STATUS)
    if claim_token is not None:
        fence.append(Run.claim_token == claim_token)

    stmt = (
        select(RunNodeOutput.attempt_key, RunNodeOutput.raw_output_markers)
        .select_from(Run)
        .outerjoin(
            RunNodeOutput,
            and_(
                RunNodeOutput.run_id == Run.id,
                RunNodeOutput.attempt_key != FINAL_ATTEMPT_KEY,
                RunNodeOutput.raw_output_markers.is_not(None),
            ),
        )
        .where(and_(*fence))
    )
    if for_update:
        stmt = stmt.with_for_update(of=Run)
    joined = (await session.execute(stmt)).all()
    if not joined:
        return []
    return [(row[0], row[1]) for row in joined]


async def read_run_blobs(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> RunBlobs:
    """The reassembled legacy dict shapes ? new-table-only (B2b: the runs
    blob columns are gone; the EMPTY/MISMATCH fallback machinery died with
    them)."""
    return await read_run_node_outputs_raw(session, run_id=run_id, organisation_id=organisation_id)


async def read_run_outputs(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled outputs dict (legacy shape), new-table-only."""
    return (await read_run_blobs(session, run_id=run_id, organisation_id=organisation_id)).outputs


async def read_run_telemetry(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled telemetry dict (legacy shape), new-table-only."""
    return (await read_run_blobs(session, run_id=run_id, organisation_id=organisation_id)).telemetry


async def read_run_markers(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The reassembled flat markers dict, new-table-only."""
    return (await read_run_blobs(session, run_id=run_id, organisation_id=organisation_id)).markers


async def read_run_markers_fenced(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    claim_token: str | None,
    for_update: bool = False,
    fence_status: bool = True,
) -> dict[str, Any] | None:
    """Fenced, single-statement markers read (B2b: new-table-only).

    A fence miss (wrong claim token / wrong status / missing run) yields
    ZERO rows ? the caller gets ``None``, byte-for-byte the same visibility
    the predicate-fenced gate read always had (FAR-228 gate). A fence HIT
    with no marker rows is a run with no markers: ``None`` markers too.
    Reassembly is jsonb-canonical (:func:`_reassemble_markers`).

    ``claim_token=None`` skips the token predicate; ``for_update=True``
    adds ``FOR UPDATE OF runs`` (the connector-caller's gate decision must
    serialise on the run row exactly like its previous raw
    ``SELECT ... FOR UPDATE``); ``fence_status=False`` (connector-caller
    path only) drops the ``status = 'running'`` predicate.
    """
    marker_items = await _read_run_markers_fenced_rows(
        session,
        run_id=run_id,
        organisation_id=organisation_id,
        claim_token=claim_token,
        for_update=for_update,
        fence_status=fence_status,
    )
    marker_pairs = [(key, value) for key, value in marker_items if key is not None]
    if not marker_pairs:
        return None
    return _reassemble_markers(marker_pairs)


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
