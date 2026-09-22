"""FAR-902: Compensating sweep for schema enforcement aggregate counters.

Corrects terminal runs whose ``run_daily_facts`` enforcement columns are NULL
but whose ``run_node_outputs`` rows have ``schema_enforcement_json`` data.
This happens when the enforcement record was written AFTER
``record_run_facts`` (a race during terminalization) or when the facts write
failed for that run.

Runs as a system sweep from ``dispatcher_reconcile`` (every 60s) via
``_run_reconcile_sweeps`` in ``cron_helpers.py``.  Bounded to 200 rows per
tick (the sweep is idempotent -- re-runs are safe).

The query predicate matches the partial index
``ix_run_node_outputs_enforcement_pending`` exactly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select, text, update

_log = logging.getLogger(__name__)

# Maximum rows corrected per tick (bounded to prevent tick overrun).
_SWEEP_MAX_PER_TICK = 200


# The query predicate that matches the partial index exactly.
# ix_run_node_outputs_enforcement_pending:
#   CREATE INDEX ... ON run_node_outputs (run_id)
#   WHERE schema_enforcement_json IS NOT NULL AND attempt_key <> '__final__'
ENFORCEMENT_SWEEP_PREDICATE = "rno.schema_enforcement_json IS NOT NULL AND rno.attempt_key <> '__final__'"


async def sweep_schema_enforcement_facts(
    factory: Any,
) -> dict[str, Any]:
    """Correct terminal runs with missing enforcement aggregate counters.

    Accepts a session **factory** (``async_sessionmaker``), not a bare
    session — matching the contract expected by ``_open_system_factory()`` in
    ``cron_helpers.py``.  Opens and manages its own session internally.

    Scans ``run_node_outputs`` rows that have ``schema_enforcement_json`` data
    (matching the partial index predicate), joins to ``run_daily_facts`` where
    the enforcement columns are NULL, aggregates the per-attempt records, and
    updates the fact row.

    Returns a summary dict: ``{"scanned": int, "corrected": int}``.
    """
    from modulo.core.pipeline_engine.schema_enforcement import aggregate_run_enforcement
    from modulo.db.models.run_daily_facts import RunDailyFact
    from modulo.db.models.run_node_outputs import RunNodeOutput

    scanned = 0
    corrected = 0

    try:
        async with factory() as session, session.begin():
            # Find runs that have enforcement data but NULL fact columns.
            # The predicate matches the partial index exactly.
            # Uses raw text() for the scan (matches the partial index
            # predicate exactly) — the run_id returned is a proper UUID
            # object that ORM queries can use.
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT rno.run_id
                        FROM run_node_outputs rno
                        JOIN run_daily_facts rdf ON rdf.run_id = rno.run_id
                        WHERE rno.schema_enforcement_json IS NOT NULL
                          AND rno.attempt_key <> '__final__'
                          AND rdf.enforcement_native_count IS NULL
                        LIMIT :limit
                        """
                    ),
                    {"limit": _SWEEP_MAX_PER_TICK},
                )
            ).fetchall()

            scanned = len(rows)

            for row in rows:
                raw_id = row[0]
                # Ensure run_id is a UUID object (the raw text() scan may
                # return a string on SQLite or a UUID on Postgres).
                run_id = raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(str(raw_id))
                # Fetch all enforcement records for this run via ORM
                # (handles UUID type portably across Postgres and SQLite).
                records = (
                    (
                        await session.execute(
                            select(RunNodeOutput.schema_enforcement_json).where(
                                RunNodeOutput.run_id == run_id,
                                RunNodeOutput.schema_enforcement_json.isnot(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                payloads = [r for r in records if isinstance(r, dict)]
                if not payloads:
                    continue

                agg = aggregate_run_enforcement(payloads)

                # Update the fact row via ORM (handles UUID type portably).
                result = await session.execute(
                    update(RunDailyFact)
                    .where(
                        RunDailyFact.run_id == run_id,
                        RunDailyFact.enforcement_native_count.is_(None),
                    )
                    .values(
                        enforcement_native_count=agg.native_count,
                        enforcement_verbatim_count=agg.verbatim_count,
                        enforcement_repair_count=agg.repair_count,
                        enforcement_wasted_count=agg.wasted_count,
                    )
                )
                if result.rowcount and result.rowcount > 0:
                    corrected += 1
    except asyncio.CancelledError:
        raise
    except Exception:
        # _log.exception logs at ERROR level with full traceback — a
        # structural failure (e.g. wrong interface) must never be silently
        # swallowed at WARNING.
        _log.exception(
            "analytics.enforcement_sweep_failed",
            extra={"scanned": scanned, "corrected": corrected},
        )

    return {"scanned": scanned, "corrected": corrected}
