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
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# Maximum rows corrected per tick (bounded to prevent tick overrun).
_SWEEP_MAX_PER_TICK = 200


# The query predicate that matches the partial index exactly.
# ix_run_node_outputs_enforcement_pending:
#   CREATE INDEX ... ON run_node_outputs (run_id)
#   WHERE schema_enforcement_json IS NOT NULL AND attempt_key <> '__final__'
ENFORCEMENT_SWEEP_PREDICATE = "rno.schema_enforcement_json IS NOT NULL AND rno.attempt_key <> '__final__'"


async def sweep_schema_enforcement_facts(
    session: AsyncSession,
) -> dict[str, Any]:
    """Correct terminal runs with missing enforcement aggregate counters.

    Scans ``run_node_outputs`` rows that have ``schema_enforcement_json`` data
    (matching the partial index predicate), joins to ``run_daily_facts`` where
    the enforcement columns are NULL, aggregates the per-attempt records, and
    updates the fact row.

    Returns a summary dict: ``{"scanned": int, "corrected": int}``.
    """
    from modulo.core.pipeline_engine.schema_enforcement import aggregate_run_enforcement

    scanned = 0
    corrected = 0

    try:
        # Find runs that have enforcement data but NULL fact columns.
        # The predicate matches the partial index exactly.
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
            run_id = row[0]
            # Fetch all enforcement records for this run.
            records = (
                await session.execute(
                    text(
                        """
                        SELECT rno.schema_enforcement_json
                        FROM run_node_outputs rno
                        WHERE rno.run_id = :run_id
                          AND rno.schema_enforcement_json IS NOT NULL
                          AND rno.attempt_key <> '__final__'
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).fetchall()

            payloads = [r[0] for r in records if isinstance(r[0], dict)]
            if not payloads:
                continue

            agg = aggregate_run_enforcement(payloads)

            # Update the fact row.
            result = await session.execute(
                text(
                    """
                    UPDATE run_daily_facts
                    SET enforcement_native_count = :native,
                        enforcement_verbatim_count = :verbatim,
                        enforcement_repair_count = :repair,
                        enforcement_wasted_count = :wasted
                    WHERE run_id = :run_id
                      AND enforcement_native_count IS NULL
                    """
                ),
                {
                    "run_id": str(run_id),
                    "native": agg.native_count,
                    "verbatim": agg.verbatim_count,
                    "repair": agg.repair_count,
                    "wasted": agg.wasted_count,
                },
            )
            if result.rowcount and result.rowcount > 0:  # type: ignore[attr-defined]
                corrected += 1

        await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "analytics.enforcement_sweep_failed",
            exc_info=True,
        )

    return {"scanned": scanned, "corrected": corrected}
