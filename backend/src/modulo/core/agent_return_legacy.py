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

Only GENUINE node-return rows are counted. The reserved ``__``-prefixed
sentinel namespace (``__run_meta__``, ``__unknown__``, ``__mwi_audit__`` and
future sentinels — see :data:`~modulo.db.models.run_node_outputs.
SENTINEL_NODE_ID_PREFIX`) is excluded: those rows are bookkeeping and are
written with an outputs side and NO telemetry side by construction, so counting
them would pin the healthy zero permanently above zero and make the strip gate
unreachable.

Query cost
----------

The sweep is a FULL-TABLE sequential scan with NO supporting index today:
``run_node_outputs``'s primary key is ``(run_id, node_id, attempt_key)`` and
``attempt_key`` is its trailing column, so a predicate on ``attempt_key`` alone
cannot use that index. The predicate is therefore NOT selective in the sense of
touching few pages — it reads the whole table every run. That is acceptable at
current table size, but a PARTIAL INDEX matching the sweep predicate
(``ON run_node_outputs (attempt_key) WHERE attempt_key = '__final__' AND
substr(node_id, 1, 2) <> '__' AND outputs_json IS NOT NULL AND
node_telemetry_json IS NULL``, mirroring the ``0251``
``ix_run_node_outputs_enforcement_pending`` precedent) is REQUIRED before the
table grows. It is deliberately NOT added here — a second concurrent migration
would collide with another in-flight branch's ``0311`` — and is tracked as a
follow-up.

Read-only: it NEVER mutates a row.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.node_output_split import stored_row_is_legacy
from modulo.db.models.run_node_outputs import (
    FINAL_ATTEMPT_KEY,
    SENTINEL_NODE_ID_PREFIX,
    RunNodeOutput,
)

_log = logging.getLogger(__name__)

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
    #: ``(run_id, node_id)`` pairs, oldest-first by
    #: ``(created_at, run_id, node_id)``, capped at the scan's sample limit.
    sample: tuple[tuple[str, str], ...] = ()
    #: True when *legacy_count* exceeds the sampled rows.
    sample_truncated: bool = False


def _legacy_predicate() -> Any:
    """The set-based twin of :func:`stored_row_is_legacy`.

    Selects stored ``__final__`` node rows whose node id is NOT in the reserved
    ``__``-prefixed sentinel namespace, whose outputs side is PRESENT
    (``outputs_json IS NOT NULL`` — SQL NULL means absent; a stored jsonb
    ``null`` is present) and whose telemetry side is ABSENT
    (``node_telemetry_json IS NULL``). The sentinel exclusion uses the shared
    :data:`SENTINEL_NODE_ID_PREFIX` so ``__run_meta__`` (and every future
    sentinel, e.g. ``__mwi_audit__``) is covered from one definition. A unit
    test pins this expression against ``stored_row_is_legacy`` on seeded rows so
    the two forms cannot drift.
    """
    return and_(
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
        # Reserved sentinel namespace: run metadata / audit rows carry an
        # outputs side and no telemetry side by construction, so they must never
        # be counted as stragglers. ``substr`` (portable across Postgres and
        # SQLite) avoids LIKE's ``_`` wildcard, which would match any 2 chars.
        func.substr(RunNodeOutput.node_id, 1, len(SENTINEL_NODE_ID_PREFIX)) != SENTINEL_NODE_ID_PREFIX,
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
                .order_by(RunNodeOutput.created_at, RunNodeOutput.run_id, RunNodeOutput.node_id)
                .limit(sample_limit)
            )
        ).all()
        dropped = 0
        for row in rows:
            run_id, node_id = row[0], row[1]
            # Re-confirm through the shared per-row predicate (the SQL filter is
            # its documented twin). A row that fails the check means the SQL
            # filter and the shared predicate have drifted; count it and log so
            # the drift is surfaced rather than silently dropped.
            if not stored_row_is_legacy(
                outputs_present=not row.outputs_absent,
                telemetry_present=not row.telemetry_absent,
            ):
                dropped += 1
                continue
            sample.append((_as_str(run_id), str(node_id)))
        if dropped:
            _log.error(
                "agent_return.sample_predicate_drift",
                extra={"dropped_rows": dropped, "legacy_count": legacy_count},
            )

    return LegacyAgentReturnReport(
        legacy_count=legacy_count,
        sample=tuple(sample),
        sample_truncated=legacy_count > len(sample),
    )


def _as_str(value: Any) -> str:
    """Render a run id (``uuid.UUID`` on Postgres, ``str`` on SQLite) as text."""
    return value if isinstance(value, str) else str(uuid.UUID(str(value)))
