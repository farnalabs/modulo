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
  rows) and a whole-set ``total_estimated_bytes``.
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
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.workspace_lease import WorkspaceLease

_log = logging.getLogger(__name__)

# Default batch size for the purge loop (runs per SAVEPOINT) and the export page
# size (runs fetched per page). Matches crud.run's 500 default.
BATCH_SIZE_DEFAULT = 500
PAGE_SIZE_DEFAULT = 500

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
_CHECKPOINT_SIZE_SQL: dict[str, str] = {
    "checkpoints": (
        "SELECT thread_id, COALESCE(SUM(octet_length(checkpoint::text) + "
        "octet_length(metadata::text)), 0) AS bytes, COUNT(*) AS cnt "
        "FROM checkpoints WHERE thread_id IN :tids GROUP BY thread_id"
    ),
    "checkpoint_blobs": (
        "SELECT thread_id, COALESCE(SUM(octet_length(blob)), 0) AS bytes, "
        "COUNT(*) AS cnt FROM checkpoint_blobs WHERE thread_id IN :tids "
        "GROUP BY thread_id"
    ),
    "checkpoint_writes": (
        "SELECT thread_id, COALESCE(SUM(octet_length(blob)), 0) AS bytes, "
        "COUNT(*) AS cnt FROM checkpoint_writes WHERE thread_id IN :tids "
        "GROUP BY thread_id"
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


def _json_bytes(value: Any) -> int:
    """Approximate byte size of a JSON-serialisable column value."""

    if value is None:
        return 0
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 0


def _run_row_bytes(run: Run) -> int:
    """Estimated bytes a single ``runs`` row contributes to the DB."""

    return (
        _json_bytes(run.outputs_json)
        + _json_bytes(run.node_telemetry_json)
        + _json_bytes(run.cost_breakdown)
        + _json_bytes(run.input_payload)
        + _json_bytes(run.raw_output_markers)
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


def _serialize_run(run: Run, *, checkpoint_count: int, checkpoint_bytes: int) -> dict[str, Any]:
    """Serialise a run row for the export stream."""

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
        "outputs_json": run.outputs_json,
        "node_telemetry_json": run.node_telemetry_json,
        "raw_output_markers": run.raw_output_markers,
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
        base_sql = _CHECKPOINT_SIZE_SQL.get(table)
        if base_sql is None:
            # Not in the hard-coded allowlist — never interpolate an unknown
            # table name into SQL; skip this table for the size estimate.
            _log.warning("run_retention.checkpoint_size_unavailable", extra={"table": table})
            continue
        try:
            stmt = text(base_sql + org_clause).bindparams(bindparam("tids", expanding=True))
            result = await session.execute(stmt, params)
        except Exception:
            # The `langgraph.*` tables may not exist yet (pre-checkpointer) or
            # the dialect may not support octet_length — treat as zero bytes.
            _log.warning("run_retention.checkpoint_size_unavailable", extra={"table": table})
            continue
        for row in result:
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
) -> dict[str, Any]:
    """List runs matching the filter set with an estimated per-run byte size.

    ``limit``/``offset`` bound the returned ``runs`` page. ``total_count`` counts
    every matching run and ``total_estimated_bytes`` sums the estimate across
    ALL matches (not just the page), so the UI can show a meaningful
    "reclaimable" figure. Runs of every status are listed (including live ones);
    only the purge refuses non-terminal runs — the UI shows terminal-only as
    purge-able.
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

    runs_out: list[dict[str, Any]] = []
    for run in page:
        est = _run_row_bytes(run) + int(bytes_by_thread.get(run.langgraph_thread_id, 0))
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

    total_estimated_bytes = await _estimate_total_bytes(session, conditions, org_id)

    return {
        "runs": runs_out,
        "total_count": total_count,
        "total_estimated_bytes": total_estimated_bytes,
    }


async def _estimate_total_bytes(
    session: AsyncSession,
    conditions: list[Any],
    org_id: uuid.UUID | None,
) -> int:
    """Estimate the total reclaimable bytes across ALL matching runs.

    Computed as the sum of run-row JSON payload bytes plus the sum of the
    checkpoint rows attributed to every matching run's ``thread_id``. Runs only;
    the run-daily-fact / journey-fact tables are intentionally left in place
    (ADR 020), so they are never counted as reclaimable here. The matching runs
    are streamed in pages so the estimate stays bounded in memory.
    """

    total = 0
    page_size = BATCH_SIZE_DEFAULT
    offset = 0
    while True:
        page = list(
            (
                await session.execute(
                    select(Run).where(*conditions).order_by(Run.created_at, Run.id).limit(page_size).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        if not page:
            break
        threads = [r.langgraph_thread_id for r in page]
        bytes_by_thread, _counts = await _checkpoint_detail(session, threads, org_id)
        total += sum(_run_row_bytes(r) for r in page) + sum(bytes_by_thread.values())
        offset += len(page)
        if len(page) < page_size:
            break

    return total


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
            yield (
                json.dumps(
                    _serialize_run(
                        run,
                        checkpoint_count=int(count_by_thread.get(run.langgraph_thread_id, 0)),
                        checkpoint_bytes=int(bytes_by_thread.get(run.langgraph_thread_id, 0)),
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
    status: str | None = None,
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
      RESTRICT ``run_id`` rows (``trigger_events``, ``notification_delivery_log``,
      ``workspace_leases``) are deleted before the runs themselves. FK CASCADE
      tables (``eval_results``, ``hitl_claims``, ``node_observations``,
      ``feedback_records``, ``run_evidence``) are cleaned up by the database.
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
        batch_freed = sum(_run_row_bytes(r) for r in batch) + sum(checkpoint_bytes.values())

        try:
            async with session.begin_nested():
                await _delete_checkpoints(session, thread_ids, org_id)
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

    ``trigger_events`` and ``notification_delivery_log`` would otherwise be left
    with a dangling ``run_id``; ``workspace_leases`` (ON DELETE RESTRICT) would
    block the run delete outright. Tables with ON DELETE CASCADE are handled by
    the database.
    """

    if not run_ids:
        return
    # These are ORM-mapped, so RLS (Postgres) and the generic tenant filter
    # (SQLite/MariaDB) stay in play — an org-admin can never match another org's
    # rows even when the run_id list accidentally overlaps.
    await session.execute(delete(NotificationDeliveryLog).where(NotificationDeliveryLog.run_id.in_(run_ids)))
    await session.execute(delete(TriggerEvent).where(TriggerEvent.run_id.in_(run_ids)))
    await session.execute(delete(WorkspaceLease).where(WorkspaceLease.run_id.in_(run_ids)))
