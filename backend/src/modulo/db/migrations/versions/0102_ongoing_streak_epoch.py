"""Add triggers.streak_epoch + the FAR-190 streak-engine index.

Revision ID: 0102_ongoing_streak_epoch
Revises: 0101_guardrails
Create Date: 2026-08-15

FAR-190 adds the ongoing-trigger no-delivery streak engine: a sweep that walks
a trigger's terminal run-classification records (FAR-189) and auto-deactivates
the trigger after N consecutive no-delivery runs, then notifies. The streak
boundary is ``GREATEST(last_delivery_at, streak_epoch)`` where ``last_delivery_at``
is derived from the classification log (``MAX(completed_at)`` of runs classified
``delivered``) — a single source of truth.

This migration adds:

* ``triggers.streak_epoch`` (nullable ``timestamptz``). The backfill is a
  separate ``UPDATE`` after the ``ADD COLUMN`` — NOT a ``server_default`` on the
  ADD COLUMN — because SQLite forbids a non-constant default on ``ADD COLUMN``
  for a table with existing rows ("Cannot add a column with non-constant
  default"), and the ``CURRENT_TIMESTAMP`` function default can never be baked
  into the column DDL portably. Every existing row is backfilled to the
  migration instant: the backfill = the post-deploy grace anchor, so
  pre-existing no-delivery history can never mass-deactivate on tick 1 because
  the boundary is ``GREATEST(last_delivery_at, streak_epoch)`` and every old
  run's ``completed_at`` precedes the anchored epoch. A ``CURRENT_TIMESTAMP``
  server default is then applied for NEW rows (``alter_column``), which batch
  mode rebuilds safely on SQLite and issues as ``SET DEFAULT`` on Postgres. The
  column is re-anchored at creation and on every ``active=True`` transition by
  the shared ``trigger_streak.anchor_trigger_streak_epoch`` helper.
  ``COALESCE((SELECT tr.streak_epoch FROM triggers tr WHERE tr.id = :tid ...),
  now())`` in the engine fails SAFE on a NULL epoch (rolling-deploy skew): the
  boundary becomes "now", so no run counts and the trigger stays active until
  the row is re-anchored.

* ``ix_runs_streak_engine`` — an index on ``runs (trigger_id, completed_at
  DESC)`` with NO partial predicate. The every-60s dispatcher_reconcile sweep's
  per-trigger correlated subqueries filter ``trigger_id`` + ``status IN (...)``
  + ``completed_at`` ranges and order by recency; the original partial predicate
  ``WHERE run_classification IS NOT NULL`` NEVER matched those queries (predicate
  implication cannot prove ``IS NOT NULL`` from ``run_classification ->> 'value'
  = '...'``), so the partial index was dead weight. A plain ``(trigger_id,
  completed_at DESC)`` index keys the actual scan. Without it each evaluated
  trigger seq-scans its ``runs`` rows every minute — the same
  every-N-minute-cron-needs-an-index rule as migration 0100.

Multi-backend notes (M7): the column DDL, the backfill UPDATE, and the index use
generic SQLAlchemy constructs; ``postgresql_where`` is deliberately NOT used
(the index is non-partial). Downgrade drops the index and the column (additive,
nullable — safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102_ongoing_streak_epoch"
down_revision: str | None = "0101_guardrails"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # (a) Add the column nullable with NO default — a non-constant default on
    # ADD COLUMN breaks SQLite on a populated triggers table. The backfill is a
    # separate UPDATE so the column DDL stays portable.
    op.add_column(
        "triggers",
        sa.Column("streak_epoch", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill every existing row to the migration instant (= the post-deploy
    # grace anchor). CURRENT_TIMESTAMP is valid on both Postgres and SQLite.
    op.execute("UPDATE triggers SET streak_epoch = CURRENT_TIMESTAMP WHERE streak_epoch IS NULL")
    # (b) Apply the default for NEW rows via alter_column: Postgres issues
    # ALTER COLUMN SET DEFAULT; SQLite batch mode rebuilds the table (render_as_batch)
    # with the default baked into the new CREATE TABLE.
    op.alter_column(
        "triggers",
        "streak_epoch",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    # FAR-190 streak engine: per-trigger correlated subqueries scan a trigger's
    # classified terminal runs by recency (trigger_id, completed_at DESC) with
    # status/classification filters — the exact keyset of the 60s sweep. A
    # NON-partial index so the planner can use it for the sweep's
    # ``run_classification ->> 'value'`` filters (predicate implication cannot
    # prove ``run_classification IS NOT NULL`` from a ``->> 'value'`` lookup).
    op.create_index(
        "ix_runs_streak_engine",
        "runs",
        ["trigger_id", sa.text("completed_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runs_streak_engine", table_name="runs")
    op.drop_column("triggers", "streak_epoch")
