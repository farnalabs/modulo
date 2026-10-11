"""Decision-record corruption detection / reconciliation (FAR-1108 chunk 8b).

The decision-record write path (FAR-1102 chunk 4) is **fail-open**: a failure
to persist a ``PolicyGateDecision`` row is logged but never blocks, delays or
reverses the decision (the decision has already been made). That is deliberate
— but it means the durable audit trail can silently diverge from the decisions
that were actually taken. This module is the detection surface for that
divergence.

It is **read-only**: it never deletes, repairs or mutates a decision record.
Decision records are audit evidence; deleting them is a governance action owned
by the retention/purge chunk, not by a reconciler. The job reports anomalies
through structured logs, an OTel counter and a liveness stats key; it is wired
as the ``decision_record_reconcile`` SAQ system cron.

Anomaly classes (the three the chunk spec names):

1. **Duplicate rows** — more than one row for the same governance event.
   * ``duplicate_event_rows``: a non-NULL ``eval_result_id`` appearing on more
     than one row. The permanent partial unique index
     ``uq_policy_gate_decisions_eval_result_id`` makes this impossible; a hit
     is a tripwire for a dropped/disabled index or restored data.
   * ``duplicate_unbounded_event_rows``: NULL-result rows sharing the same
     ``(run_id, policy_gate_id)``. These rows are exempt from the unique index
     by design, so this is the class the index cannot catch.
2. **Rows with no evaluation-result id where one was expected** —
   ``missing_eval_result_id``: any row with ``eval_result_id IS NULL``. The
   write path only reaches the decision-record step after a successful
   ``EvalResult`` persist, so every row it writes carries a result id; a NULL
   row came from another path (backfill/legacy) and is itself an anomaly.
3. **Rows whose referenced entities no longer exist** —
   ``orphaned_eval_result`` (``eval_result_id`` points at no ``EvalResult``)
   and ``orphaned_run`` (``run_id`` points at no ``Run``). Both columns are
   plain non-FK columns (chunk 4 §2.4), so referential drift is possible.
   ``policy_gate_id``/``eval_id`` are FK-enforced (``ON DELETE RESTRICT``) and
   cannot orphan.

The scan is cross-org when called with the system session (``org_id=None``),
or scoped to one org when ``org_id`` is supplied. Each class returns at most
``sample_limit`` representative entries — the report is a detection signal,
not an exhaustive dump.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval_result import EvalResult
from modulo.db.models.policy_gate_decision import PolicyGateDecision
from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

#: Default cap on representative entries per anomaly class.
DEFAULT_SAMPLE_LIMIT = 50

ANOMALY_DUPLICATE_EVENT = "duplicate_event_rows"
ANOMALY_DUPLICATE_UNBOUNDED_EVENT = "duplicate_unbounded_event_rows"
ANOMALY_MISSING_EVAL_RESULT_ID = "missing_eval_result_id"
ANOMALY_ORPHANED_EVAL_RESULT = "orphaned_eval_result"
ANOMALY_ORPHANED_RUN = "orphaned_run"


@dataclass
class DecisionRecordAnomaly:
    """One detected decision-record anomaly."""

    kind: str
    detail: dict[str, Any]


@dataclass
class DecisionReconcileReport:
    """Outcome of one decision-record reconciliation scan."""

    scanned: int = 0
    anomalies: list[DecisionRecordAnomaly] = field(default_factory=list)
    sample_limit: int = DEFAULT_SAMPLE_LIMIT

    @property
    def total(self) -> int:
        return len(self.anomalies)

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for anomaly in self.anomalies:
            counts[anomaly.kind] = counts.get(anomaly.kind, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "total_anomalies": self.total,
            "counts_by_kind": self.counts_by_kind(),
            "sample_limit": self.sample_limit,
            "samples": [{"kind": a.kind, **a.detail} for a in self.anomalies],
        }


def _scoped_decision_query(stmt: Any, org_id: uuid.UUID | None) -> Any:
    """Add the optional per-org scope to a ``policy_gate_decisions`` query."""
    if org_id is not None:
        return stmt.where(PolicyGateDecision.organisation_id == org_id)
    return stmt


async def reconcile_decision_records(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> DecisionReconcileReport:
    """Scan ``policy_gate_decisions`` for corruption/anomalies (read-only).

    Never mutates a row. Returns a :class:`DecisionReconcileReport` carrying the
    scanned row count and bounded samples of each anomaly class.
    """
    report = DecisionReconcileReport(sample_limit=sample_limit)

    count_stmt = _scoped_decision_query(
        select(func.count()).select_from(PolicyGateDecision),
        org_id,
    )
    report.scanned = int((await session.execute(count_stmt)).scalar() or 0)

    # 1a. Duplicate event rows — a non-NULL eval_result_id on >1 row.
    dup_stmt = _scoped_decision_query(
        select(PolicyGateDecision.eval_result_id, func.count().label("row_count"))
        .where(PolicyGateDecision.eval_result_id.is_not(None))
        .group_by(PolicyGateDecision.eval_result_id)
        .having(func.count() > 1)
        .order_by(func.count().desc())
        .limit(sample_limit),
        org_id,
    )
    for eval_result_id, row_count in (await session.execute(dup_stmt)).all():
        report.anomalies.append(
            DecisionRecordAnomaly(
                kind=ANOMALY_DUPLICATE_EVENT,
                detail={"eval_result_id": str(eval_result_id), "row_count": int(row_count)},
            )
        )

    # 1b. Duplicate unbounded events — NULL-result rows sharing (run, gate).
    dup_null_stmt = _scoped_decision_query(
        select(
            PolicyGateDecision.run_id,
            PolicyGateDecision.policy_gate_id,
            func.count().label("row_count"),
        )
        .where(PolicyGateDecision.eval_result_id.is_(None))
        .group_by(PolicyGateDecision.run_id, PolicyGateDecision.policy_gate_id)
        .having(func.count() > 1)
        .order_by(func.count().desc())
        .limit(sample_limit),
        org_id,
    )
    for run_id, gate_id, row_count in (await session.execute(dup_null_stmt)).all():
        report.anomalies.append(
            DecisionRecordAnomaly(
                kind=ANOMALY_DUPLICATE_UNBOUNDED_EVENT,
                detail={
                    "run_id": str(run_id) if run_id is not None else None,
                    "policy_gate_id": str(gate_id),
                    "row_count": int(row_count),
                },
            )
        )

    # 2. Missing evaluation-result id — every written row should carry one.
    missing_stmt = _scoped_decision_query(
        select(
            PolicyGateDecision.id,
            PolicyGateDecision.run_id,
            PolicyGateDecision.policy_gate_id,
            PolicyGateDecision.eval_id,
            PolicyGateDecision.resolved_action,
        )
        .where(PolicyGateDecision.eval_result_id.is_(None))
        .order_by(PolicyGateDecision.id)
        .limit(sample_limit),
        org_id,
    )
    for row in (await session.execute(missing_stmt)).all():
        report.anomalies.append(
            DecisionRecordAnomaly(
                kind=ANOMALY_MISSING_EVAL_RESULT_ID,
                detail={
                    "decision_id": str(row[0]),
                    "run_id": str(row[1]) if row[1] is not None else None,
                    "policy_gate_id": str(row[2]),
                    "eval_id": str(row[3]),
                    "resolved_action": row[4],
                },
            )
        )

    # 3a. Orphaned evaluation result — eval_result_id with no EvalResult row.
    eval_result_exists = select(EvalResult.id).where(EvalResult.id == PolicyGateDecision.eval_result_id).exists()
    orphan_er_stmt = _scoped_decision_query(
        select(PolicyGateDecision.id, PolicyGateDecision.eval_result_id)
        .where(PolicyGateDecision.eval_result_id.is_not(None), ~eval_result_exists)
        .order_by(PolicyGateDecision.id)
        .limit(sample_limit),
        org_id,
    )
    for decision_id, eval_result_id in (await session.execute(orphan_er_stmt)).all():
        report.anomalies.append(
            DecisionRecordAnomaly(
                kind=ANOMALY_ORPHANED_EVAL_RESULT,
                detail={"decision_id": str(decision_id), "eval_result_id": str(eval_result_id)},
            )
        )

    # 3b. Orphaned run — run_id with no Run row.
    run_exists = select(Run.id).where(Run.id == PolicyGateDecision.run_id).exists()
    orphan_run_stmt = _scoped_decision_query(
        select(PolicyGateDecision.id, PolicyGateDecision.run_id)
        .where(PolicyGateDecision.run_id.is_not(None), ~run_exists)
        .order_by(PolicyGateDecision.id)
        .limit(sample_limit),
        org_id,
    )
    for decision_id, run_id in (await session.execute(orphan_run_stmt)).all():
        report.anomalies.append(
            DecisionRecordAnomaly(
                kind=ANOMALY_ORPHANED_RUN,
                detail={"decision_id": str(decision_id), "run_id": str(run_id)},
            )
        )

    return report
