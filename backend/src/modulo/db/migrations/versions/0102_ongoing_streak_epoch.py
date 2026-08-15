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

* ``triggers.streak_epoch`` (nullable ``timestamptz``, ``DEFAULT CURRENT_TIMESTAMP``
  backfills every existing row to the migration instant). The backfill = the
  post-deploy grace anchor: pre-existing no-delivery history can never
  mass-deactivate on tick 1 because the boundary is ``GREATEST(last_delivery_at,
  streak_epoch)`` and every old run's ``completed_at`` precedes the anchored epoch.
  The column is re-anchored at creation and on every ``active=True`` transition by
  the shared ``cron_helpers.anchor_trigger_streak_epoch`` helper. ``COALESCE(
  triggers.streak_epoch, now())`` in the engine fails SAFE on a NULL epoch
  (rolling-deploy skew): the boundary becomes "now", so no run counts and the
  trigger stays active until the row is re-anchored.

* ``ix_runs_streak_engine`` — a partial index on ``runs (trigger_id,
  completed_at DESC) WHERE run_classification IS NOT NULL``. The every-60s
  dispatcher_reconcile sweep's per-trigger correlated subqueries filter exactly
  this (a trigger's classified terminal runs by recency). Without it each
  evaluated trigger seq-scans its ``runs`` rows every minute — the same
  every-N-minute-cron-needs-an-index rule as migration 0100.

Multi-backend notes (M7): the column DDL and the index use generic SQLAlchemy
constructs; ``postgresql_where`` is consumed only by the Postgres DDL compiler
and ignored on the other backends (the migration 0100 precedent). Downgrade
drops the index and the column (additive, nullable — safe).
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
    op.add_column(
        "triggers",
        sa.Column(
            "streak_epoch",
            sa.DateTime(timezone=True),
            nullable=True,
            # Backfills every existing row to the migration instant on every
            # backend (SQLite allows CURRENT_TIMESTAMP defaults in ADD COLUMN).
            server_default=sa.func.current_timestamp(),
        ),
    )
    # FAR-190 streak engine: per-trigger correlated subqueries scan a trigger's
    # classified terminal runs by recency (trigger_id, completed_at DESC) where
    # run_classification IS NOT NULL — the exact predicate of the 60s sweep.
    op.create_index(
        "ix_runs_streak_engine",
        "runs",
        ["trigger_id", sa.text("completed_at DESC")],
        unique=False,
        postgresql_where=sa.text("run_classification IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_streak_engine", table_name="runs")
    op.drop_column("triggers", "streak_epoch")
