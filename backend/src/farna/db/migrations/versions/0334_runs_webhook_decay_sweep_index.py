"""Partial index for the pipeline-decay alerting sweep predicate (FAR-2116).

``core/pipeline_decay_alerts.py`` runs from ``dispatcher_reconcile`` — every 60
seconds — and reads a rolling 2-hour window of **webhook-triggered terminal**
runs, grouped per pipeline::

    SELECT r.organisation_id, r.pipeline_id, p.name,
           count(*),
           count(*) FILTER (WHERE r.status = 'complete'),
           coalesce(sum(r.node_deadline_watchdog_fired_count), 0)
    FROM runs r JOIN pipelines p ON p.id = r.pipeline_id
    WHERE r.trigger_type = 'webhook'
      AND r.status IN (<TERMINAL_STATUSES>)
      AND r.completed_at IS NOT NULL
      AND r.completed_at >= <now - 2h>
    GROUP BY r.organisation_id, r.pipeline_id, p.name
    ORDER BY count(*) DESC
    LIMIT 200

Nothing on ``runs`` served that shape: ``completed_at`` carried no index at
all, so each tick would sequentially scan ``runs`` — the largest table on a
mature instance (7.5 GB on prod, FAR-1419) — to aggregate two hours of rows.

This migration adds the supporting partial index::

    CREATE INDEX ix_runs_webhook_decay_sweep ON runs (completed_at)
    INCLUDE (organisation_id, pipeline_id, status,
             node_deadline_watchdog_fired_count)
    WHERE trigger_type = 'webhook' AND status IN ('budget_exceeded', ...)

Design notes:

* The partial predicate is the sweep's OWN **static** filter verbatim
  (``trigger_type`` plus the terminal-status list). ``completed_at >= now-2h``
  is the range condition on the index key, not part of the predicate: the
  query's WHERE trivially implies the predicate, which is what the planner
  needs to choose the partial index.
* Only webhook runs that reached a terminal state ever enter the index. A row
  is inserted into it when the run reaches a terminal status (the same UPDATE
  that stamps ``completed_at``) and never updated afterwards — one narrow entry
  per webhook run, not one per status transition.
* The INCLUDE columns are exactly the ones the aggregate projects, so the
  scan is index-only.
* The predicate pins ``TERMINAL_STATUSES`` as of this migration. Adding a
  terminal status to ``modulo.db.models.run.TERMINAL_STATUSES`` without
  widening this predicate strands the new status outside the index (the sweep
  would seq-scan for it) — guarded by
  ``tests/unit/db/test_migration_0334_runs_webhook_decay_sweep_index.py``,
  which compiles the sweep's real SELECT and compares it against this
  predicate, the ``Run`` model declaration and the status vocabulary.
* Additive index only — no column/table changes, no behaviour change. Plain
  (non-CONCURRENT) ``CREATE INDEX IF NOT EXISTS`` follows the
  0128/0155/0267/0271/0272/0278/0328 convention: Alembic wraps each revision in
  a transaction, so ``CONCURRENTLY`` is unavailable. The build holds a SHARE
  lock on ``runs`` (blocks writers, not readers) for its duration — the same
  trade-off every prior ``runs`` index migration accepted; schedule outside
  peaks.
* Postgres only; SQLite parity comes from the ``Run`` model's ``__table_args__``
  via ``create_all`` (the 0309/0328 precedent).

Revision ID: 0334_runs_webhook_decay_sweep_index
Revises: 0333_trigger_event_findings
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0334_runs_webhook_decay_sweep_index"
down_revision: str | None = "0333_trigger_event_findings"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_RUNS_TABLE = "runs"
_INDEX_NAME = "ix_runs_webhook_decay_sweep"

# The sweep's static filter, verbatim: webhook-triggered terminal runs.
# Pinned in three places (this predicate, the model's Index, and the sweep's
# own WHERE) by the structural unit test.
_DECAY_WHERE = sa.text(
    "trigger_type = 'webhook' AND status IN ('budget_exceeded', 'cancelled', "
    "'compensation_failed', 'complete', 'cost_ceiling_exceeded', 'eval_failed', "
    "'failed', 'rejected', 'router_no_match', 'stalled')"
)

_INCLUDE_COLUMNS = ["organisation_id", "pipeline_id", "status", "node_deadline_watchdog_fired_count"]


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    op.create_index(
        _INDEX_NAME,
        _RUNS_TABLE,
        ["completed_at"],
        postgresql_include=_INCLUDE_COLUMNS,
        postgresql_where=_DECAY_WHERE,
        if_not_exists=True,
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.drop_index(_INDEX_NAME, table_name=_RUNS_TABLE, if_exists=True)
