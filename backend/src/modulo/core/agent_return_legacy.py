"""Agent-return legacy-shape straggler detection (FAR-132).

The Agent Return split (FAR-124 / FAR-125) writes a node's PURE return to
``run_node_outputs.outputs_json`` and its exhaustive telemetry to
``run_node_outputs.node_telemetry_json``. Rows written BEFORE the P1 write flip
carry the MIXED legacy envelope in ``outputs_json`` with no telemetry side, and
the read accessors (``core.node_output_split``) stay legacy-tolerant for exactly
those rows.

This module is the PERIODIC invariant behind the eventual FAR-132 strip: it
counts ``__final__`` rows that still reach the legacy fallback
(:func:`modulo.core.node_output_split.stored_row_is_legacy` — outputs side
present, telemetry side absent) so a post-flip writer that re-introduced the
legacy shape cannot go unnoticed. Zero is the expected, healthy result once the
P5 verify-backfill has run; a non-zero count blocks the strip.

Read-only: it NEVER mutates a row. The count is over the whole
``run_node_outputs`` table (cross-org, system role) — the predicate is
selective (telemetry-absent rows only), so it stays cheap once the backfill is
complete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.node_output_split import stored_row_is_legacy
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, RunNodeOutput

__all__ = [
    "LegacyAgentReturnReport",
    "count_legacy_agent_return_rows",
]

#: Rows echoed back as evidence with a report. Bounded — the count is exact,
#: the sample is illustrative.
_DEFAULT_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class LegacyAgentReturnReport:
    """Outcome of one legacy-shape straggler scan."""

    #: Exact number of ``__final__`` node rows that still reach the legacy
    #: fallback (``stored_row_is_legacy``).
    legacy_count: int
    #: ``(run_id, node_id)`` pairs, oldest-first by (run_id, node_id), capped at
    #: the scan's sample limit.
    sample: tuple[tuple[str, str], ...] = ()
    #: True when *legacy_count* exceeds the sampled rows.
    sample_truncated: bool = False


def _legacy_predicate() -> Any:
    """The set-based twin of :func:`stored_row_is_legacy`.

    Selects stored ``__final__`` node rows (not the run-metadata row) whose
    outputs side is PRESENT (``outputs_json IS NOT NULL`` — SQL NULL means
    absent; a stored jsonb ``null`` is present) and whose telemetry side is
    ABSENT (``node_telemetry_json IS NULL``). A unit test pins this expression
    against ``stored_row_is_legacy`` on seeded rows so the two forms cannot
    drift.
    """
    return and_(
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
        RunNodeOutput.node_id != META_NODE_ID,
        RunNodeOutput.outputs_json.is_not(None),
        RunNodeOutput.node_telemetry_json.is_(None),
    )


async def count_legacy_agent_return_rows(
    session: AsyncSession,
    *,
    sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
) -> LegacyAgentReturnReport:
    """Count ``run_node_outputs`` rows still carrying the legacy shape.

    Returns the exact count plus a bounded ``(run_id, node_id)`` sample. Each
    sampled row's legs are re-derived and confirmed through the shared
    :func:`stored_row_is_legacy` predicate — the per-row authority — so a looser
    SQL filter can never inflate the report with a non-legacy row.

    Raises on any database error: a scan that cannot run must be loud (the
    caller re-raises into SAQ's retry loop), never reported as a clean zero.
    """
    predicate = _legacy_predicate()
    legacy_count = int(
        (await session.execute(select(func.count()).select_from(RunNodeOutput).where(predicate))).scalar_one()
    )

    sample: list[tuple[str, str]] = []
    if legacy_count:
        rows = (
            await session.execute(
                select(
                    RunNodeOutput.run_id,
                    RunNodeOutput.node_id,
                    RunNodeOutput.outputs_json.is_(None).label("outputs_absent"),
                    RunNodeOutput.node_telemetry_json.is_(None).label("telemetry_absent"),
                )
                .where(predicate)
                .order_by(RunNodeOutput.run_id, RunNodeOutput.node_id)
                .limit(sample_limit)
            )
        ).all()
        for row in rows:
            run_id, node_id = row[0], row[1]
            # Re-confirm through the shared per-row predicate (the SQL filter is
            # its documented twin). A row that fails the check is a filter-drift
            # bug — surface it by excluding it from the sample rather than
            # silently trusting the SQL.
            if not stored_row_is_legacy(
                outputs_present=not row.outputs_absent,
                telemetry_present=not row.telemetry_absent,
            ):
                continue
            sample.append((_as_str(run_id), str(node_id)))

    return LegacyAgentReturnReport(
        legacy_count=legacy_count,
        sample=tuple(sample),
        sample_truncated=legacy_count > len(sample),
    )


def _as_str(value: Any) -> str:
    """Render a run id (``uuid.UUID`` on Postgres, ``str`` on SQLite) as text."""
    return value if isinstance(value, str) else str(uuid.UUID(str(value)))
