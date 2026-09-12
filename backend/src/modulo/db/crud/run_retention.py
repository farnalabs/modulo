"""CRUD for run data retention (FAR-427) — candidate listing, export, purge.

The backend ``runs`` table is small, but each run's LangGraph state lives in the
``langgraph.*`` checkpoint tables (``checkpoints``, ``checkpoint_blobs``,
``checkpoint_writes``) keyed by ``runs.langgraph_thread_id`` → ``thread_id``.
Those checkpoint rows are never cleaned up by the existing age-based purge
(``crud.run.purge_runs`` / ``batch_delete_old_terminal_runs``), which only deletes
``runs`` rows. Over a month that leaves the DB volume dominated by orphaned
graph checkpoints (observed 2026-08-24: 8.6GB, ~7.8GB of which is checkpoints +
checkpoint_writes), eventually filling the volume.

This module adds the FAR-427 operations:

* ``list_retention_candidates`` — list runs matching a filter set, with an
  ``estimated_bytes`` per run (its own JSON payload columns + its checkpoint
  rows) and a whole-set ``total_estimated_bytes``. FAR-660: the whole-set
  totals are estimated by bounded SQL aggregates (one grouped scan per source
  table under a per-request deadline), never by walking every matching run —
  the retired Python walk hydrated the full dataset twice and 503'd the
  endpoint on the multi-GB production DB.
* ``iter_run_export`` — an async generator of JSONL lines (one per run) that
  streams run metadata + full outputs + telemetry + a checkpoint summary. Runs
  in pages so memory stays bounded regardless of how much data the run holds.
* ``purge_terminal_runs`` — delete terminal runs matching a filter set together
  with their checkpoint rows and the related per-run rows. Batched (default 500
  runs per SAVEPOINT), transactional, idempotent, and never touches a
  non-terminal run.

RLS / org scoping: an org-admin passes its ``organisation_id`` so every query and
delete is scoped to that org (the caller also sets ``set_rls_org``). A
system-admin passes ``organisation_id=None`` to operate across all orgs — it
must scope itself manually when it targets a single org. The checkpoint
``thread_id`` encodes ``{org_id}:{run_id}`` (see ``crud.run.create_run``), so it
is globally unique and cross-org deletes never leak.

DELIBERATE NON-DELETION — ``run_daily_facts`` and ``modulo_journey_facts``:
both carry a ``run_id`` that is deliberately NOT a foreign key and must SURVIVE
the run purge (ADR 020; the model comments literally say a future "fix" into an
FK breaks retention). Purging a run therefore leaves those fact rows in place.
Similarly ``cost_components``, ``error_events``, ``token_families`` and
``org_api_keys`` are NOT per-run tables (no ``run_id`` FK) and are untouched.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    bindparam,
    cast,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run_node_outputs import (
    # KEPT through the drop (FAR-694): migration 0215 dropped the runs blob
    # columns and these rows are the pre-B1 quarantined blobs' ONLY
    # surviving copy, so the purge's delete stays live.
    QUARANTINE_TABLE,
    RunBlobs,
    read_node_output_blob_bytes,
    read_run_blobs,
)
from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.run import TERMINAL_STATUSES, Run

# qa rider (FAR-583): the byte-size estimator was duplicated here and in the
# run_node_outputs repo module — the single shared copy now lives on the leaf
# model module; imported under the historical private name so existing
# callers/tests are untouched.
from modulo.db.models.run_node_outputs import META_NODE_ID
from modulo.db.models.run_node_outputs import json_bytes as _json_bytes
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.sqlstates import sqlstate_of

_log = logging.getLogger(__name__)

# Default batch size for the purge loop (runs per SAVEPOINT) and the export page
# size (runs fetched per page). Matches crud.run's 500 default.
BATCH_SIZE_DEFAULT = 500
PAGE_SIZE_DEFAULT = 500

# Default checkpoint-retention window used by the Housekeeping-driven
# ``purge_terminal_checkpoints``. Checkpoints are unread post-terminal and are
# the bulk of DB volume, so they age out much sooner than the ``runs`` rows
# (30 days). Kept in sync with ``org_deletion.CHECKPOINT_RETENTION_DAYS``.
CHECKPOINT_RETENTION_DAYS = 3

# The langgraph checkpoint tables created by ModuloPostgresSaver.setup(). Each
# carries organisation_id + thread_id and has no FK to ``runs``, so they must be
# deleted explicitly when purging a run. The size expression is Postgres-only
# (octet_length) — the checkpoint tables are Postgres-only (JSONB / BYTEA DDL).
_CHECKPOINT_TABLES: tuple[tuple[str, str], ...] = (
    ("checkpoints", "octet_length(checkpoint::text) + octet_length(metadata::text)"),
    ("checkpoint_blobs", "octet_length(blob)"),
    ("checkpoint_writes", "octet_length(blob)"),
)

# Hard-coded per-table SQL templates (NO f-string, NO string interpolation — the
# table name and size expression are baked into each literal). A table name is
# only ever used after membership validation against _CHECKPOINT_TABLES (the
# allowlist) / these dict keys, so an arbitrary `table` value can never reach the
# SQL string. thread_ids and organisation_id are always bound parameters
# (:tids via an expanding bind, :org via a plain bind) — never concatenated —
# so there is no SQL-injection surface. Pure string literals also mean the
# bandit `# nosec B608` suppression is no longer required.
#
# Each size template is split into (head, tail) so the optional _ORG_CLAUSE can
# be spliced into the WHERE clause. Appending it to a single-string template put
# it AFTER `GROUP BY thread_id`, producing
# `GROUP BY thread_id AND organisation_id = :org` — Postgres rejects that with
# "argument of AND must be type boolean, not type text", so every org-scoped
# size probe errored, the error aborted the enclosing transaction, and the next
# statement in the same session died with InFailedSQLTransactionError. The org
# filter must be a WHERE predicate, never a GROUP BY expression.
_CHECKPOINT_SIZE_SQL: dict[str, tuple[str, str]] = {
    "checkpoints": (
        (
            "SELECT thread_id, COALESCE(SUM(octet_length(checkpoint::text) + "
            "octet_length(metadata::text)), 0) AS bytes, COUNT(*) AS cnt "
            "FROM checkpoints WHERE thread_id IN :tids"
        ),
        " GROUP BY thread_id",
    ),
    "checkpoint_blobs": (
        (
            "SELECT thread_id, COALESCE(SUM(octet_length(blob)), 0) AS bytes, "
            "COUNT(*) AS cnt FROM checkpoint_blobs WHERE thread_id IN :tids"
        ),
        " GROUP BY thread_id",
    ),
    "checkpoint_writes": (
        (
            "SELECT thread_id, COALESCE(SUM(octet_length(blob)), 0) AS bytes, "
            "COUNT(*) AS cnt FROM checkpoint_writes WHERE thread_id IN :tids"
        ),
        " GROUP BY thread_id",
    ),
}
_CHECKPOINT_DELETE_SQL: dict[str, str] = {
    "checkpoints": "DELETE FROM checkpoints WHERE thread_id IN :tids",
    "checkpoint_blobs": "DELETE FROM checkpoint_blobs WHERE thread_id IN :tids",
    "checkpoint_writes": "DELETE FROM checkpoint_writes WHERE thread_id IN :tids",
}
# Appended to a template ONLY when org_id is in scope; the org value is a bound
# parameter (:org), never string-interpolated.
_ORG_CLAUSE = " AND organisation_id = :org"

# FAR-660: request budget for the candidates size estimate. The estimate runs
# as bounded SQL aggregates (one grouped scan per source table) instead of the
# retired O(N/500 x 4)-query Python walk, but a pathological filter still has
# to scan its matching rows once. Each scan is bounded TWICE by this monotonic
# deadline: it is skipped entirely when no budget remains, and while it runs a
# transaction-local ``statement_timeout`` (see _SQL_SET_STATEMENT_TIMEOUT)
# bounds the statement itself to the remaining budget — a started scan that
# would detoast gigabytes of checkpoints is cancelled by Postgres instead of
# running for minutes. Components that are skipped, fail, or time out
# contribute 0 and the response degrades to a partial approximation (surfaced
# as ``estimate_degraded``) rather than a 503. The admin candidates route
# stamps the deadline at handler start and threads it down; direct callers get
# the same default.
ESTIMATE_DEADLINE_SECONDS = 20.0

# Per-scan statement timeout (qa: a deadline checked only BEFORE a scan does
# not bound the scan itself — one checkpoint aggregate can detoast ~7.8GB).
# Mirrors the shared precedent (core/analytics/service.py, core/
# cost_controller/probe.py): the timeout is a transaction-local GUC
# (``set_config(..., true)``), so a savepoint rollback reverts it when a scan
# times out. On the success path the scan clears the GUC back to 0 BEFORE the
# savepoint releases — a released SET LOCAL survives to the end of the
# enclosing transaction, and a leftover small budget would abort the cheap
# statements that follow (the terminal count, the audit write).
_SQL_SET_STATEMENT_TIMEOUT = "SELECT set_config('statement_timeout', :ms, true)"

# Postgres SQLSTATE 57014 (query_canceled) — the statement_timeout
# cancellation, extracted dialect-tolerantly via the shared ``sqlstate_of``
# walker so it is caught explicitly and degrades to 0 + warning exactly like
# the other best-effort failure paths, never surfacing as a generic failure.
_STATEMENT_TIMEOUT_SQLSTATE = "57014"

# SELECT-only Core definitions (the QUARANTINE_TABLE precedent) of the tables
# the SQL-side estimate aggregates. The ``langgraph.*`` checkpoint tables are
# created at runtime by ModuloPostgresSaver.setup() and are deliberately NOT
# ORM models; ``run_node_outputs`` IS an ORM model but its legacy-blob columns
# must not be referenced as ORM attributes here (FAR-583 read-switch lens —
# the size aggregates are a sanctioned accounting read, not a payload read),
# so the Core subset keeps the reference to the Column objects only. Only the
# columns the aggregates reference are declared (SQLAlchemy renders just
# those; nothing here writes). Types mirror the runtime DDL in
# modulo_saver._MIGRATION_SQL / migration 0192.
_CHECKPOINT_AGG_METADATA = MetaData()

_CHECKPOINTS_AGG = Table(
    "checkpoints",
    _CHECKPOINT_AGG_METADATA,
    Column("organisation_id", Uuid()),
    Column("thread_id", String(512)),
    Column("checkpoint", JSON().with_variant(JSONB(), "postgresql")),
    Column("metadata", JSON().with_variant(JSONB(), "postgresql")),
)

_CHECKPOINT_BLOBS_AGG = Table(
    "checkpoint_blobs",
    _CHECKPOINT_AGG_METADATA,
    Column("organisation_id", Uuid()),
    Column("thread_id", String(512)),
    Column("blob", LargeBinary()),
)

_CHECKPOINT_WRITES_AGG = Table(
    "checkpoint_writes",
    _CHECKPOINT_AGG_METADATA,
    Column("organisation_id", Uuid()),
    Column("thread_id", String(512)),
    Column("blob", LargeBinary()),
)

_RUN_NODE_OUTPUTS_AGG = Table(
    "run_node_outputs",
    _CHECKPOINT_AGG_METADATA,
    Column("run_id", Uuid()),
    Column("node_id", String()),
    Column("outputs_json", JSON().with_variant(JSONB(), "postgresql")),
    Column("node_telemetry_json", JSON().with_variant(JSONB(), "postgresql")),
    Column("raw_output_markers", JSON().with_variant(JSONB(), "postgresql")),
)


def _json_col_bytes(col: Any) -> Any:
    """Character count of a JSON column's text rendering, JSON ``null`` = 0.

    Mirrors the page-level estimator ``models.run_node_outputs.json_bytes``,
    which returns 0 for a ``None`` payload. Two DIFFERENT absences must both
    count 0 here:

    * SQL NULL (the column was omitted on INSERT) — handled by the COALESCE;
    * JSON ``null`` (the column was written as an explicit Python ``None``) —
      handled by the NULLIF.

    The second case is why this helper exists. SQLAlchemy's ``JSON`` type binds
    an explicit Python ``None`` as the JSON value ``null`` (``none_as_null`` is
    False by default), NOT as SQL NULL, so ``cast(col AS TEXT)`` renders the
    4-character string ``null``. The ORM loads that straight back as ``None``,
    so the page estimator scored it 0 while this aggregate scored it 4 — the
    whole-set totals drifted +4 bytes per absent payload column against the
    page sum (3 runs x absent columns = the 12-byte delta that failed
    test_candidates_shape_and_estimates). NULLIF on the rendered text collapses
    JSON ``null`` to SQL NULL so both metrics agree, keeping the documented
    "coincide for simple ASCII payloads" contract true.

    A JSON *string* ``"null"`` renders WITH quotes (6 chars), so it is never
    mistaken for the JSON ``null`` literal.
    """
    return func.coalesce(func.length(func.nullif(cast(col, Text), "null")), 0)


# Per-table byte-size expressions, mirroring the raw _CHECKPOINT_SIZE_SQL
# formulas exactly (octet_length(checkpoint::text) + octet_length(metadata::text)
# for the checkpoints row, octet_length(blob) for the BYTEA blob columns) so the
# checkpoint accounting cannot drift between the page-level reader
# (_checkpoint_detail) and the whole-set aggregates. JSON payload columns use
# _json_col_bytes — a CHARACTER count of Postgres' jsonb text rendering. That is
# NOT byte-identical to the page-level Python estimator's
# len(json.dumps(...)) (models.run_node_outputs.json_bytes): json.dumps
# ensure_ascii-escapes non-ASCII to \uXXXX and renders numbers with Python
# repr, while jsonb text keeps raw unicode and re-renders numbers as
# normalized numeric text. The two renderings COINCIDE only for simple ASCII
# payloads; for non-ASCII / exponent-format payloads the page-level per-run
# estimate and the whole-set SQL aggregate measure different (both approximate)
# character counts, so the page sum MAY differ from total_estimated_bytes.
# That asymmetry is a documented response-contract property (qa FAR-660):
# the totals are the authoritative whole-set figure and the per-run values are
# indicative — the SQL aggregate is NOT claimed to equal the page sum. The
# per-column COALESCE keeps an SQL-NULL (absent) column at 0 so a single NULL
# never voids the row's whole contribution, and the NULLIF keeps an explicit
# JSON null at 0 to match the Python estimator (see _json_col_bytes).
_RUN_PAYLOAD_BYTES = (
    _json_col_bytes(Run.cost_breakdown) + _json_col_bytes(Run.input_payload) + _json_col_bytes(Run.run_classification)
)

_NODE_OUTPUT_BYTES = (
    _json_col_bytes(_RUN_NODE_OUTPUTS_AGG.c["outputs_json"])
    + _json_col_bytes(_RUN_NODE_OUTPUTS_AGG.c["node_telemetry_json"])
    + _json_col_bytes(_RUN_NODE_OUTPUTS_AGG.c["raw_output_markers"])
)

# (table, size expression) pairs for the three checkpoint aggregates.
_CHECKPOINT_AGG_SOURCES: tuple[tuple[Table, Any], ...] = (
    (
        _CHECKPOINTS_AGG,
        func.octet_length(cast(_CHECKPOINTS_AGG.c["checkpoint"], Text))
        + func.octet_length(cast(_CHECKPOINTS_AGG.c["metadata"], Text)),
    ),
    (_CHECKPOINT_BLOBS_AGG, func.octet_length(_CHECKPOINT_BLOBS_AGG.c.blob)),
    (_CHECKPOINT_WRITES_AGG, func.octet_length(_CHECKPOINT_WRITES_AGG.c.blob)),
)


def _run_row_bytes(run: Run, node_output_bytes: int = 0) -> int:
    """Estimated bytes a single ``runs`` row contributes to the DB.

    FAR-583: the per-node blobs (outputs / telemetry / markers) are counted
    from the ``run_node_outputs`` store (metadata rows EXCLUDED — the flags
    payload overhead would skew the accounting), passed in as
    *node_output_bytes* by the caller (batched via
    ``read_node_output_blob_bytes``); the legacy ``runs`` blob columns are no
    longer summed here. The remaining columns are still run-row payloads.

    Metric note (qa FAR-660): this is the Python-side rendering
    (``len(json.dumps(...))``), NOT the same rendering the whole-set SQL
    aggregates measure (``length(cast(jsonb AS text))``) — see the
    ``_RUN_PAYLOAD_BYTES`` comment for the documented asymmetry; the values
    agree only for simple ASCII payloads.
    """

    return (
        node_output_bytes
        + _json_bytes(run.cost_breakdown)
        + _json_bytes(run.input_payload)
        + _json_bytes(run.run_classification)
    )


def _retention_conditions(
    *,
    org_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    pipeline_id: uuid.UUID | None,
    status: str | None,
    statuses: frozenset[str] | None,
) -> list[Any]:
    """Build the SQLAlchemy WHERE conditions shared by list / export / purge.

    ``org_id`` scopes to one org; ``None`` leaves the org scope to the caller
    (system admin operating across all orgs). ``status`` is an exact status
    match, while ``statuses`` (when given) is a whitelist to intersect — the
    purge passes ``TERMINAL_STATUSES`` so a request can never purge a live run.
    """

    conditions: list[Any] = []
    if org_id is not None:
        conditions.append(Run.organisation_id == org_id)
    if date_from is not None:
        conditions.append(Run.created_at >= date_from)
    if date_to is not None:
        conditions.append(Run.created_at <= date_to)
    if pipeline_id is not None:
        conditions.append(Run.pipeline_id == pipeline_id)
    if statuses is not None:
        # Purge never sweeps outside the terminal set, even if asked for more.
        if status is not None:
            statuses = statuses.intersection({status})
            if not statuses:
                # Requested a status that is not purgable — match nothing.
                conditions.append(Run.id.is_(None))
        conditions.append(Run.status.in_(statuses))
    elif status is not None:
        conditions.append(Run.status == status)
    return conditions


def _serialize_run(run: Run, *, checkpoint_count: int, checkpoint_bytes: int, blobs: RunBlobs) -> dict[str, Any]:
    """Serialise a run row for the export stream.

    FAR-583: the three blob fields are the REASSEMBLED legacy-dict shapes
    from the ``run_node_outputs`` store (repo reader, new-table-only since
    B2c; the legacy runs columns died with migration 0215), so the export
    keeps serving the exact pre-FAR-583 payload shape.
    DISPOSITION (post-0215, the drop has landed): 0192-quarantined
    runs' blobs live in the quarantine table (ops SQL), NOT in
    ``run_node_outputs``, so these readers serve an
    absent-side shape for them — accepted, evidence is preserved elsewhere.
    """

    return {
        "id": str(run.id),
        "run_number": run.run_number,
        "organisation_id": str(run.organisation_id),
        "pipeline_id": str(run.pipeline_id),
        "snapshot_id": str(run.snapshot_id),
        "thread_id": run.langgraph_thread_id,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "total_tokens": run.total_tokens,
        "total_cost_usd": str(run.total_cost_usd) if run.total_cost_usd is not None else None,
        "cost_breakdown": run.cost_breakdown,
        "input_payload": run.input_payload,
        "outputs_json": blobs.outputs,
        "node_telemetry_json": blobs.telemetry,
        "raw_output_markers": blobs.markers,
        "run_classification": run.run_classification,
        "error_code": run.error_code,
        "error_detail": run.error_detail,
        "checkpoint_summary": {
            "rows": checkpoint_count,
            "estimated_bytes": checkpoint_bytes,
        },
    }


async def _checkpoint_detail(
    session: AsyncSession,
    thread_ids: list[str],
    org_id: uuid.UUID | None,
) -> tuple[dict[str, int], dict[str, int]]:
    """Return ``({thread_id: checkpoint_bytes}, {thread_id: checkpoint_rows})``.

    Aggregates the three checkpoint tables in one pass per table. Best-effort:
    if a table is missing or the dialect is not Postgres the aggregate is
    skipped (returns 0) rather than failing the whole listing. ``org_id`` is
    applied when given; the thread_id itself already encodes the org, so a
    cross-org system-admin delete stays correct without it.

    Each probe runs inside its OWN ``session.begin_nested()`` SAVEPOINT, mirroring
    :func:`_run_grouped_estimate`. Without one, "skipped rather than failing the
    whole listing" was not actually true: a failed statement aborts the enclosing
    Postgres transaction, so swallowing the error left the session poisoned and
    the NEXT caller statement died with InFailedSQLTransactionError. The savepoint
    rollback is what makes this contract real. Failures are logged with
    ``_log.exception`` — a bare warning hid a malformed-SQL bug here (the org
    clause landing in GROUP BY, see _CHECKPOINT_SIZE_SQL) behind a benign
    "table missing" message.
    """

    if not thread_ids:
        return {}, {}
    bytes_by_thread: dict[str, int] = {}
    count_by_thread: dict[str, int] = {}
    params: dict[str, Any] = {"tids": thread_ids}
    org_clause = ""
    if org_id is not None:
        org_clause = _ORG_CLAUSE
        params["org"] = str(org_id)
    for table, _ in _CHECKPOINT_TABLES:
        parts = _CHECKPOINT_SIZE_SQL.get(table)
        if parts is None:
            # Not in the hard-coded allowlist — never interpolate an unknown
            # table name into SQL; skip this table for the size estimate.
            _log.warning("run_retention.checkpoint_size_unavailable", extra={"table": table})
            continue
        head, tail = parts
        try:
            stmt = text(head + org_clause + tail).bindparams(bindparam("tids", expanding=True))
            async with session.begin_nested():
                rows = (await session.execute(stmt, params)).all()
        except Exception:
            # The `langgraph.*` tables may not exist yet (pre-checkpointer) or
            # the dialect may not support octet_length — treat as zero bytes.
            # The savepoint rollback keeps the enclosing transaction usable.
            _log.exception("run_retention.checkpoint_size_unavailable", extra={"table": table})
            continue
        for row in rows:
            bytes_by_thread[row[0]] = bytes_by_thread.get(row[0], 0) + int(row[1] or 0)
            count_by_thread[row[0]] = count_by_thread.get(row[0], 0) + int(row[2] or 0)
    return bytes_by_thread, count_by_thread


async def _select_run_page(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    pipeline_id: uuid.UUID | None,
    status: str | None,
    statuses: frozenset[str] | None,
    limit: int,
    offset: int,
) -> list[Run]:
    """Fetch one page of runs matching the retention filters."""

    stmt = (
        select(Run)
        .where(
            *_retention_conditions(
                org_id=org_id,
                date_from=date_from,
                date_to=date_to,
                pipeline_id=pipeline_id,
                status=status,
                statuses=statuses,
            )
        )
        .order_by(Run.created_at, Run.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_retention_candidates(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = PAGE_SIZE_DEFAULT,
    offset: int = 0,
    deadline: float | None = None,
) -> dict[str, Any]:
    """List runs matching the filter set with an estimated per-run byte size.

    ``limit``/``offset`` bound the returned ``runs`` page. ``total_count`` counts
    every matching run and ``total_estimated_bytes`` sums the estimate across
    ALL matches (not just the page), so the UI can show a meaningful
    "reclaimable" figure. Runs of every status are listed (including live ones);
    only the purge refuses non-terminal runs — the UI shows terminal-only as
    purge-able.

    FAR-660: the whole-set figures come from bounded SQL aggregates (one
    grouped scan per source table — see :func:`_estimate_bytes_by_status`),
    never from walking every matching run. ``deadline`` is a
    ``time.monotonic()`` timestamp bounding those scans; ``None`` applies the
    module default (``ESTIMATE_DEADLINE_SECONDS``). The per-run
    ``estimated_bytes`` values on the page stay per-row Python estimates —
    bounded to the page. Two contract notes (qa FAR-660):

    * the page estimates and the whole-set totals measure the same columns
      through DIFFERENT renderings (Python ``json.dumps`` vs Postgres jsonb
      text) — they coincide only for simple ASCII payloads; see the
      ``_RUN_PAYLOAD_BYTES`` comment;
    * ``estimate_degraded`` is True when any whole-set scan was skipped past
      the deadline, failed, or timed out — the totals are then a partial
      approximation (every failed component contributes 0), and the caller
      must annotate them rather than present an authoritative 0.
    """

    conditions = _retention_conditions(
        org_id=org_id,
        date_from=date_from,
        date_to=date_to,
        pipeline_id=pipeline_id,
        status=status,
        statuses=None,
    )
    total_count = (await session.execute(select(func.count()).select_from(Run).where(*conditions))).scalar_one() or 0

    page = await _select_run_page(
        session,
        org_id=org_id,
        date_from=date_from,
        date_to=date_to,
        pipeline_id=pipeline_id,
        status=status,
        statuses=None,
        limit=limit,
        offset=offset,
    )

    bytes_by_thread, _count_by_thread = await _checkpoint_detail(session, [r.langgraph_thread_id for r in page], org_id)
    node_bytes_by_run = await read_node_output_blob_bytes(session, [r.id for r in page])

    runs_out: list[dict[str, Any]] = []
    for run in page:
        est = _run_row_bytes(run, node_bytes_by_run.get(run.id, 0)) + int(
            bytes_by_thread.get(run.langgraph_thread_id, 0)
        )
        runs_out.append(
            {
                "id": str(run.id),
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "status": run.status,
                "pipeline_id": str(run.pipeline_id),
                "thread_id": run.langgraph_thread_id,
                "estimated_bytes": est,
            }
        )

    # FAR-660: ONE grouped estimate pass covers both whole-set figures —
    # total = every status group, terminal = the TERMINAL_STATUSES groups —
    # replacing the two full-dataset Python walks (which hydrated every
    # matching Run with all payload columns and 503'd the endpoint on the
    # multi-GB production DB).
    est_by_status, estimate_degraded = await _estimate_bytes_by_status(
        session,
        org_id=org_id,
        date_from=date_from,
        date_to=date_to,
        pipeline_id=pipeline_id,
        status=status,
        deadline=deadline,
    )
    total_estimated_bytes = sum(est_by_status.values())
    # Terminal-only totals drive the purge UI. The purge deletes every matching
    # TERMINAL run (unbounded — it pages the whole set), so the confirm dialog
    # and reclaimable figure must come from a server-side terminal count, never
    # from the page-capped candidate list the client happens to hold.
    terminal_estimated_bytes = sum(est for est_status, est in est_by_status.items() if est_status in TERMINAL_STATUSES)
    terminal_total = (
        await session.execute(
            select(func.count())
            .select_from(Run)
            .where(
                *_retention_conditions(
                    org_id=org_id,
                    date_from=date_from,
                    date_to=date_to,
                    pipeline_id=pipeline_id,
                    status=status,
                    statuses=TERMINAL_STATUSES,
                )
            )
        )
    ).scalar_one() or 0

    return {
        "runs": runs_out,
        "total_count": total_count,
        "total_estimated_bytes": total_estimated_bytes,
        "terminal_total": terminal_total,
        "terminal_estimated_bytes": terminal_estimated_bytes,
        "estimate_degraded": estimate_degraded,
    }


def _runs_payload_stmt(conditions: list[Any]) -> Any:
    """Grouped scan: per-status retained-payload bytes over the matching runs."""
    return select(Run.status, func.coalesce(func.sum(_RUN_PAYLOAD_BYTES), 0)).where(*conditions).group_by(Run.status)


def _node_output_stmt(conditions: list[Any]) -> Any:
    """Grouped scan: per-status blob bytes from ``run_node_outputs`` for the
    matching runs. Metadata rows (``__run_meta__``) are excluded — parity with
    ``read_node_output_blob_bytes`` (the flags payload overhead would skew the
    accounting)."""
    return (
        select(Run.status, func.coalesce(func.sum(_NODE_OUTPUT_BYTES), 0))
        .select_from(_RUN_NODE_OUTPUTS_AGG)
        .join(Run, Run.id == _RUN_NODE_OUTPUTS_AGG.c.run_id)
        .where(*conditions, _RUN_NODE_OUTPUTS_AGG.c["node_id"] != META_NODE_ID)
        .group_by(Run.status)
    )


def _checkpoint_agg_stmt(cp_table: Table, size_expr: Any, conditions: list[Any], org_id: uuid.UUID | None) -> Any:
    """Grouped scan: per-status checkpoint bytes for the matching runs' threads.

    Joins the checkpoint table to the filtered runs on ``thread_id`` (globally
    unique, encodes the org — see the module docstring); the checkpoint-side
    ``organisation_id`` filter is applied when in scope as defense in depth,
    mirroring the raw size/delete SQL above. An inner join also excludes
    orphaned checkpoint rows no run references — the purge could never
    reclaim those via the run set, so they are not "reclaimable" here.
    """
    wheres = list(conditions)
    if org_id is not None:
        wheres.append(cp_table.c.organisation_id == org_id)
    return (
        select(Run.status, func.coalesce(func.sum(size_expr), 0))
        .select_from(cp_table)
        .join(Run, Run.langgraph_thread_id == cp_table.c.thread_id)
        .where(*wheres)
        .group_by(Run.status)
    )


async def _run_grouped_estimate(
    session: AsyncSession,
    stmt: Any,
    *,
    label: str,
    deadline: float,
    by_status: dict[str, int],
) -> bool:
    """Execute one grouped estimate scan, accumulating per-status bytes.

    Returns True when the scan contributed its rows; False when it was
    skipped or failed (the caller surfaces that as a degraded estimate — see
    :func:`_estimate_bytes_by_status`).

    Each scan is best-effort and bounded BOTH before and during execution:

    * skipped (contributing 0) when the request deadline has passed — the
      response degrades to a partial approximation rather than a minutes-long
      walk (FAR-660);
    * executed inside its own SAVEPOINT so a failure (missing checkpoint
      tables on a pre-checkpointer DB, a dialect without the size functions)
      rolls back only the failed scan and contributes 0 — never a 503 for the
      whole listing;
    * bound DURING execution by a transaction-local ``statement_timeout`` set
      to the remaining budget (qa: checking the deadline only before the scan
      left a started scan unbounded — one checkpoint aggregate can detoast
      ~7.8GB and run for minutes). The GUC is set inside the savepoint and
      cleared before the savepoint releases, so a released value never leaks
      into the enclosing transaction (a savepoint rollback reverts it).
    """
    budget_ms = int((deadline - time.monotonic()) * 1000)
    if budget_ms <= 0:
        _log.warning("run_retention.estimate_deadline_skipped", extra={"component": label})
        return False
    try:
        async with session.begin_nested():
            await session.execute(text(_SQL_SET_STATEMENT_TIMEOUT), {"ms": str(budget_ms)})
            rows = (await session.execute(stmt)).all()
            await session.execute(text(_SQL_SET_STATEMENT_TIMEOUT), {"ms": "0"})
    except SQLAlchemyError as exc:
        if sqlstate_of(exc) == _STATEMENT_TIMEOUT_SQLSTATE:
            # The statement itself exceeded the remaining budget — degraded,
            # not fatal (same contract as the other best-effort paths).
            _log.warning("run_retention.estimate_statement_timeout", extra={"component": label})
        else:
            _log.exception("run_retention.estimate_component_unavailable", extra={"component": label})
        return False
    for row_status, row_bytes in rows:
        if row_status is None:
            continue
        by_status[row_status] = by_status.get(row_status, 0) + int(row_bytes or 0)
    return True


async def _estimate_bytes_by_status(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = None,
    deadline: float | None = None,
) -> tuple[dict[str, int], bool]:
    """Per-status estimated byte totals for ALL runs matching the filter set.

    Returns ``(by_status, degraded)``: ``by_status`` maps each matching run
    status to its estimated retained bytes; ``degraded`` is True when ANY
    scan was skipped past the deadline, failed, or timed out — every such
    component contributes 0, so ``by_status`` is then a PARTIAL
    approximation (a lower bound), never an authoritative accounting. The
    caller surfaces ``degraded`` as ``estimate_degraded`` on the response so
    an operator cannot mistake a degraded 0 for "nothing reclaimable".

    FAR-660: replaces the retired ``_estimate_total_bytes`` Python walk (which
    hydrated every matching Run — full payload columns — twice, once for the
    whole-set total and once for the terminal-only figure, with three
    checkpoint aggregates and per-run ``json.dumps`` per 500-row batch) with
    ONE grouped SQL scan per source table:

    * ``runs`` — the three retained JSON payload columns
      (``_RUN_PAYLOAD_BYTES``);
    * ``run_node_outputs`` — the per-node blob store joined to the matching
      runs, metadata rows excluded (``_node_output_stmt``);
    * the three ``langgraph.*`` checkpoint tables joined on ``thread_id``,
      using the exact ``octet_length`` expressions of the raw size SQL
      (``_CHECKPOINT_AGG_SOURCES``).

    Both response figures derive from this single result set:
    ``total_estimated_bytes`` sums every status group and
    ``terminal_estimated_bytes`` sums only the ``TERMINAL_STATUSES`` groups —
    no second walk. The byte sizes are on-disk approximations (text-cast
    rendering of the stored payloads), the same "estimated" contract as
    before. Every scan is best-effort and deadline-guarded — skipped when no
    budget remains, and bound WHILE it runs by a transaction-local
    ``statement_timeout`` (see :func:`_run_grouped_estimate`); a failed,
    late, or timed-out component contributes 0 (degraded) and the listing
    still renders.

    ``deadline`` is a ``time.monotonic()`` timestamp; ``None`` applies the
    module default budget (``ESTIMATE_DEADLINE_SECONDS``).
    """
    if deadline is None:
        deadline = time.monotonic() + ESTIMATE_DEADLINE_SECONDS
    conditions = _retention_conditions(
        org_id=org_id,
        date_from=date_from,
        date_to=date_to,
        pipeline_id=pipeline_id,
        status=status,
        statuses=None,
    )
    by_status: dict[str, int] = {}
    degraded = False
    components: list[tuple[Any, str]] = [
        (_runs_payload_stmt(conditions), "runs_payload"),
        (_node_output_stmt(conditions), "run_node_outputs"),
    ]
    components.extend(
        (_checkpoint_agg_stmt(cp_table, size_expr, conditions, org_id), f"checkpoint:{cp_table.name}")
        for cp_table, size_expr in _CHECKPOINT_AGG_SOURCES
    )
    for stmt, label in components:
        contributed = await _run_grouped_estimate(
            session,
            stmt,
            label=label,
            deadline=deadline,
            by_status=by_status,
        )
        degraded = degraded or not contributed
    return by_status, degraded


async def iter_run_export(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = None,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> AsyncIterator[str]:
    """Yield one JSONL line per matching run (memory-safe streaming export).

    Runs are fetched page-by-page; each page's checkpoint rows are aggregated in
    a second pass, so nothing accumulates in memory. ``date_from``/``date_to``
    are applied to ``runs.created_at``. Any status may be exported; it is the
    operator's decision whether an exported run was later purged.

    DISPOSITION (post-0215, the columns are gone): the blob fields reassemble
    from ``run_node_outputs`` only (B2c readers); 0192-quarantined runs' blobs
    live in the quarantine table (ops SQL), so they export with absent blob
    sides — accepted, see :func:`_serialize_run`.
    """

    offset = 0
    while True:
        page = await _select_run_page(
            session,
            org_id=org_id,
            date_from=date_from,
            date_to=date_to,
            pipeline_id=pipeline_id,
            status=status,
            statuses=None,
            limit=page_size,
            offset=offset,
        )
        if not page:
            break
        bytes_by_thread, count_by_thread = await _checkpoint_detail(
            session, [r.langgraph_thread_id for r in page], org_id
        )
        for run in page:
            blobs = await read_run_blobs(session, run_id=run.id, organisation_id=org_id)
            yield (
                json.dumps(
                    _serialize_run(
                        run,
                        checkpoint_count=int(count_by_thread.get(run.langgraph_thread_id, 0)),
                        checkpoint_bytes=int(bytes_by_thread.get(run.langgraph_thread_id, 0)),
                        blobs=blobs,
                    ),
                    default=str,
                )
                + "\n"
            )
        offset += len(page)
        if len(page) < page_size:
            break


async def purge_terminal_runs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    pipeline_id: uuid.UUID | None = None,
    _status: str | None = None,
    batch_size: int = BATCH_SIZE_DEFAULT,
) -> dict[str, int]:
    """Delete terminal runs matching the filter set, cascading to checkpoints.

    Behaviour:
    * Only runs whose ``status`` is in :data:`TERMINAL_STATUSES` are ever
      deleted — never a ``pending``/``running``/``awaiting_human``/``claimed``
      run (the whitelist is the filter, so even a request that names a live
      status could not delete one).
    * Runs are processed in ``batch_size``-sized batches. Each batch runs inside
      its own ``session.begin_nested()`` SAVEPOINT, so a failure in one batch
      rolls back only that batch; the remaining batches are aborted and the
      partially-purged counts are returned.
    * Per batch, the checkpoint rows (``checkpoints``, ``checkpoint_blobs``,
      ``checkpoint_writes``) are deleted by ``thread_id`` and the SET NULL /
      RESTRICT ``run_id`` rows (``trigger_events``,
      ``notification_delivery_log``) are deleted before the runs themselves.
      FK CASCADE tables (``eval_results``, ``hitl_claims``,
      ``node_observations``, ``feedback_records``, ``run_evidence``) are
      cleaned up by the database. The ``run_node_outputs_quarantine`` rows
      for the batch's run ids are deleted too (qa Major 3a — best-effort, see
      :func:`_delete_quarantine_rows`: the table deliberately has no FK, so
      without an explicit delete every purged run would leave a permanent
      orphaned blob copy).
    * Idempotent: re-running produces zero because the runs no longer match.

    Returns ``{purged_runs, purged_checkpoints, freed_estimated_bytes}``.
    ``purged_checkpoints`` counts checkpoint rows removed; ``freed_estimated_bytes``
    is the estimated reclaimable byte total of the deleted runs + checkpoints.
    """

    purged_runs = 0
    purged_checkpoints = 0
    freed_estimated_bytes = 0

    while True:
        batch = await _select_run_page(
            session,
            org_id=org_id,
            date_from=date_from,
            date_to=date_to,
            pipeline_id=pipeline_id,
            status=None,
            statuses=TERMINAL_STATUSES,
            limit=batch_size,
            offset=0,
        )
        # ``offset=0`` on every iteration is intentional: the previous batch was
        # deleted, so the remaining matching runs shift forward and the next
        # ``limit``-sized slice starts at the new head.
        if not batch:
            break

        ids = [r.id for r in batch]
        thread_ids = [r.langgraph_thread_id for r in batch]
        checkpoint_bytes, checkpoint_counts = await _checkpoint_detail(session, thread_ids, org_id)
        node_bytes_by_run = await read_node_output_blob_bytes(session, ids)
        batch_freed = sum(_run_row_bytes(r, node_bytes_by_run.get(r.id, 0)) for r in batch) + sum(
            checkpoint_bytes.values()
        )

        try:
            async with session.begin_nested():
                await _delete_checkpoints(session, thread_ids, org_id)
                await _delete_quarantine_rows(session, ids)
                await _delete_run_id_rows(session, ids)
                await session.execute(
                    text("DELETE FROM runs WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
                    {"ids": ids},
                )
                await session.flush()
        except Exception:
            _log.exception(
                "run_retention.purge_batch_failed",
                extra={"batch_runs": len(ids), "org_id": str(org_id) if org_id else None},
            )
            # A SAVEPOINT rollback already undid this batch; stop rather than
            # repeat a systemic failure (e.g. an unexpected RESTRICT FK) on every
            # remaining batch.
            break

        purged_runs += len(ids)
        purged_checkpoints += sum(checkpoint_counts.values())
        freed_estimated_bytes += batch_freed

        if len(ids) < batch_size:
            break

    return {
        "purged_runs": purged_runs,
        "purged_checkpoints": purged_checkpoints,
        "freed_estimated_bytes": freed_estimated_bytes,
    }


async def purge_terminal_checkpoints(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    max_age_days: int = CHECKPOINT_RETENTION_DAYS,
    batch_size: int = BATCH_SIZE_DEFAULT,
) -> dict[str, int]:
    """Purge LangGraph checkpoint rows for old TERMINAL runs, keeping the ``runs``.

    FAR-432: the ``runs`` table carries every feature (outputs, telemetry,
    classification), but each run's graph state lives in the ``langgraph.*``
    checkpoint tables, which are unread post-terminal and dominate DB volume.
    This deletes the checkpoint rows (``checkpoints``, ``checkpoint_blobs``,
    ``checkpoint_writes``) for TERMINAL runs older than ``max_age_days`` —
    NEVER the runs themselves, and NEVER a non-terminal (pending/running/
    awaiting_human/claimed) run, so a HITL-paused run keeps its interrupt
    checkpoint (``resume_run`` re-reads it on later approval).

    ``org_id`` scopes the select to one org (the caller applies RLS). Unlike
    ``purge_terminal_runs`` this does NOT touch ``trigger_events`` /
    ``notification_delivery_log`` or the ``runs`` rows —
    the runs stay for audit + analytics (ADR 020); only the reclaimable
    checkpoint bytes are freed.

    Age is keyed on ``runs`` (``COALESCE(completed_at, created_at)``), never on
    a checkpoint ``created_at`` column (absent in some deployed schemas —
    FAR-432). Deletes happen per-thread via the allowlisted checkpoint-table
    delete templates, so a missing saver table is tolerated (logged, not fatal).

    Returns ``{checkpoints_purged, threads_purged, bytes_freed}``:
    ``checkpoints_purged`` is the checkpoint-row count removed, ``threads_purged``
    the number of terminal runs whose threads were swept, ``bytes_freed`` the
    pre-delete estimated byte total of those checkpoint rows.
    """

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)

    checkpoints_purged = 0
    threads_purged = 0
    bytes_freed = 0

    while True:
        run_stmt = (
            select(Run.langgraph_thread_id)
            .where(
                Run.status.in_(sorted(TERMINAL_STATUSES)),
                func.coalesce(Run.completed_at, Run.created_at) < cutoff,
            )
            .order_by(Run.created_at, Run.id)
        )
        if org_id is not None:
            run_stmt = run_stmt.where(Run.organisation_id == org_id)
        thread_ids = list((await session.execute(run_stmt.limit(batch_size))).scalars().all())
        if not thread_ids:
            break

        threads_purged += len(thread_ids)
        bytes_by_thread, counts_by_thread = await _checkpoint_detail(session, thread_ids, org_id)
        bytes_freed += sum(bytes_by_thread.values())
        checkpoints_purged += sum(counts_by_thread.values())

        await _delete_checkpoints(session, thread_ids, org_id)
        await session.flush()

        if len(thread_ids) < batch_size:
            break

    return {
        "checkpoints_purged": checkpoints_purged,
        "threads_purged": threads_purged,
        "bytes_freed": bytes_freed,
    }


async def _delete_checkpoints(
    session: AsyncSession,
    thread_ids: list[str],
    org_id: uuid.UUID | None,
) -> None:
    """Delete checkpoint rows for a set of run thread-ids.

    Best-effort per table: the ``langgraph.*`` tables are created by
    :class:`ModuloPostgresSaver` at startup, so a deployed DB normally has them;
    on a DB where the checkpointer was never initialised a delete would fail. A
    missing table must not block the run purge itself — the runs are still
    reclaimed, and a log records that checkpoints could not be swept.
    """

    if not thread_ids:
        return
    params: dict[str, Any] = {"tids": thread_ids}
    org_clause = ""
    if org_id is not None:
        org_clause = _ORG_CLAUSE
        params["org"] = str(org_id)
    for table, _ in _CHECKPOINT_TABLES:
        base_sql = _CHECKPOINT_DELETE_SQL.get(table)
        if base_sql is None:
            # Not in the hard-coded allowlist — never interpolate an unknown
            # table name into SQL.
            _log.warning(
                "run_retention.checkpoint_delete_unavailable",
                extra={"table": table, "org_id": str(org_id) if org_id else None},
            )
            continue
        stmt = text(base_sql + org_clause).bindparams(bindparam("tids", expanding=True))
        try:
            await session.execute(stmt, params)
        except Exception:
            _log.warning(
                "run_retention.checkpoint_delete_unavailable",
                extra={"table": table, "org_id": str(org_id) if org_id else None},
            )


async def _delete_run_id_rows(session: AsyncSession, run_ids: list[Any]) -> None:
    """Delete the SET NULL / RESTRICT per-run rows that reference ``run_ids``.

    ``trigger_events`` and ``notification_delivery_log`` would otherwise be
    left with a dangling ``run_id``. Tables with ON DELETE CASCADE are handled
    by the database.
    """

    if not run_ids:
        return
    # These are ORM-mapped, so RLS (Postgres) and the generic tenant filter
    # (SQLite/MariaDB) stay in play — an org-admin can never match another org's
    # rows even when the run_id list accidentally overlaps.
    await session.execute(delete(NotificationDeliveryLog).where(NotificationDeliveryLog.run_id.in_(run_ids)))
    await session.execute(delete(TriggerEvent).where(TriggerEvent.run_id.in_(run_ids)))


async def _delete_quarantine_rows(session: AsyncSession, run_ids: list[Any]) -> None:
    """Best-effort DELETE of the batch's ``run_node_outputs_quarantine`` rows
    (qa Major 3a).

    The quarantine table deliberately has NO foreign key to ``runs``
    (migration 0192), so purged runs leave their quarantined blob copies
    orphaned forever unless the purge deletes them explicitly. Its privileges
    are explicit on Postgres (qa iteration 2, Major 5 — migration 0192 grants
    ``SELECT, DELETE`` to ``modulo_app``, the role this purge runs on via the
    admin run-retention route; ``SELECT, INSERT, DELETE`` to the system role
    the B2b-era sweep ran on) and it has no ORM mapping, so the delete
    still mirrors the :func:`_delete_checkpoints` best-effort pattern rather
    than the hard ``_delete_run_id_rows`` one: it runs in its OWN savepoint,
    and a missing-table / ungranted-env failure is logged and swallowed —
    the run purge must never abort because the evidence copy could not be
    reclaimed. The run_ids come from the org-scoped terminal batch, so the
    delete cannot leak across orgs regardless of the table's missing RLS
    policy.

    NOTE (FAR-694): the quarantine table is KEPT - migration 0215 dropped
    the runs blob columns, so these rows are the ONLY surviving copy of the
    0192-quarantined legacy blobs; the delete stays live and the retention
    purge keeps reclaiming them.
    """

    if not run_ids:
        return
    try:
        async with session.begin_nested():
            await session.execute(delete(QUARANTINE_TABLE).where(QUARANTINE_TABLE.c.run_id.in_(run_ids)))
    except Exception:
        _log.warning(
            "run_retention.quarantine_delete_unavailable",
            exc_info=True,
            extra={"batch_runs": len(run_ids)},
        )
