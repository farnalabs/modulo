"""Partial index for the workspace-input drift sweep predicate (FAR-1438).

Revision ID: 0278_runs_workspace_drift_sweep_index
Revises: 0276_runs_autovacuum_enabled
Create Date: 2026-10-03

``_sweep_workspace_input_drift_flags`` (``core/cron_helpers.py``) is a
compensating sweep of the reconcile tick's ``compensating_sweeps`` stage. It
runs, every 60 seconds::

    SELECT runs.id, runs.organisation_id
    FROM runs
    WHERE runs.status IN (<TERMINAL_STATUSES>)
      AND runs.workspace_inputs_drift_detected IS NULL
    ORDER BY runs.id
    LIMIT 200

Migration 0238 added ``runs.workspace_inputs_drift_detected`` but no index on
it. Once the sweep has backfilled the historical NULL set, the planner must
scan the whole ``runs`` table (7.5 GB on prod, FAR-1419) every tick just to
prove fewer than 200 rows still match — a sequential scan repeated once a
minute forever.

This migration adds the supporting partial index::

    CREATE INDEX ix_runs_workspace_drift_sweep ON runs (id)
    WHERE status IN ('complete', ...) AND workspace_inputs_drift_detected IS NULL

Design notes:

* The ``postgresql_where`` predicate is the sweep's WHERE clause **exactly**
  (both conjuncts), so every index entry already satisfies the filter and the
  only remaining access requirement is ``ORDER BY id`` — which the ``(id)``
  key serves as a plain ordered index scan that stops at ``LIMIT 200``. A
  ``(status, id)`` key would instead force a 9-way MergeAppend or a sort of
  the whole match set before the LIMIT.
* The predicate pins ``TERMINAL_STATUSES`` as of this migration. Adding a
  terminal status to ``modulo.db.models.run.TERMINAL_STATUSES`` without
  widening this predicate strands the new status outside the index (the sweep
  would seq-scan for it) — guarded by
  ``tests/unit/db/test_migration_0278_runs_workspace_drift_sweep_index.py``,
  which compiles the sweep's real SELECT and compares it against this
  predicate, the ``Run`` model declaration, and the status vocabulary.
* Additive index only — no column/table changes, no behavior change to the
  sweep or its LIMIT-200 budget. Plain (non-CONCURRENT) create follows the
  0128/0155/0267/0271/0272 convention: Alembic wraps each revision in a
  transaction so ``CONCURRENTLY`` is unavailable, and the autocommit escape
  hatch is refused under the rehearsal runner (``ALEMBIC_REHEARSAL=1``). The
  build holds a SHARE lock on ``runs`` for its duration (blocks writers, not
  readers) — same trade-off every prior ``runs`` index migration accepted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0278_runs_workspace_drift_sweep_index"
down_revision: str | None = "0277_run_daily_facts_trigger_dispatch_phase"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: Index name — asserted against the ``Run`` model declaration by the
#: migration's unit test.
INDEX_NAME: str = "ix_runs_workspace_drift_sweep"

#: The sweep's WHERE clause, verbatim. ``modulo.db.models.run.TERMINAL_STATUSES``
#: rendered as a literal IN list plus the NULL-flag conjunct. Keep byte-stable
#: with the sweep predicate (the unit test compares them canonically, sorting
#: the IN list on both sides so frozenset iteration order cannot flake).
PREDICATE: str = (
    "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed') "
    "AND workspace_inputs_drift_detected IS NULL"
)


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "runs",
        ["id"],
        postgresql_where=sa.text(PREDICATE),
        sqlite_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="runs")
