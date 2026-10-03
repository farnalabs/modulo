"""FAR-1421: ``run_daily_facts`` trigger + dispatch-phase provenance columns.

Revision ID: 0277_run_daily_facts_trigger_dispatch_phase
Revises: 0276_runs_autovacuum_enabled
Create Date: 2026-10-02

The claim→dispatch latency figure (FAR-1088's last Done-means item, tracked
as FAR-1421) must be bucketed per ``trigger_type`` / ``trigger_id`` on the
EXISTING analytics surface. The facts table (ADR 020) is deliberately
self-contained: reads never join ``runs``, because facts must outlive the
90-day run purge. It carried neither field, so the metric had no source.

This migration copies the two provenance columns onto the fact::

    trigger_id               uuid        NULL (NULL for manual runs)
    dispatch_phase           text        NULL
    dispatch_phase_entered_at timestamptz NULL

with a bounded one-shot backfill from the ``runs`` rows that still exist (the
90-day retention window — beyond it the run row is gone and there is nothing
to copy, which is exactly the NULL-phase fallback the metric already
documents). It is idempotent: re-running rewrites the same values, and the
``IS NULL`` guard skips rows the live writer has already stamped.

The metric these feed (in ``core/analytics/builder.py``)::

    avg_dispatch_latency_ms = dispatch_phase_entered_at - created_at
                              -- falling back to started_at - created_at
                                 when the phase timestamp is NULL

``first_node_dispatched`` is deliberately NOT part of the source: it is not in
``DURABLE_PHASES`` yet, so it never reaches ``runs`` and therefore cannot reach
this table. The narrower claim→first-node figure becomes derivable the moment
that phase is written durably — no further schema change required.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0277_run_daily_facts_trigger_dispatch_phase"
down_revision: str | None = "0276_runs_autovacuum_enabled"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Mirror the model's types: nullable everywhere (the source run may carry none
# of them, and facts written before this revision keep their NULLs).
_TRIGGER_ID = sa.Column(
    "trigger_id",
    sa.Uuid(),
    nullable=True,
    comment="NOT a FK — facts survive the run purge (ADR 020); NULL for manual runs and for pre-FAR-1421 facts",
)
_DISPATCH_PHASE = sa.Column("dispatch_phase", sa.Text(), nullable=True)
_DISPATCH_PHASE_ENTERED_AT = sa.Column(
    "dispatch_phase_entered_at",
    sa.DateTime(timezone=True),
    nullable=True,
    comment="when runs.dispatch_phase was last entered — from Run.dispatch_phase_entered_at",
)

# One-shot backfill over the run-retention window. Runs older than this have
# been purged, so there is nothing to copy — those facts keep NULL phase and
# the metric falls back to ``started_at`` (its documented behaviour). The
# ``IS NULL`` guards make a re-run a no-op for rows the live writer stamped.
# Plain single-statement SQL: it stays inside the migration transaction and
# opens no side connection (rehearses under ``ALEMBIC_REHEARSAL=1``).
_BACKFILL = """
    UPDATE public."run_daily_facts" f
       SET trigger_id = r.trigger_id,
           dispatch_phase = r.dispatch_phase,
           dispatch_phase_entered_at = r.dispatch_phase_entered_at
      FROM public."runs" r
     WHERE r.id = f.run_id
       AND f.run_date >= (CURRENT_DATE - INTERVAL '90 days')
       AND (f.trigger_id IS NULL
            OR f.dispatch_phase IS NULL
            OR f.dispatch_phase_entered_at IS NULL)
"""

# Data columns only — downgrade drops them and the copied values go with them.
_DROP_TRIGGER_ID = 'ALTER TABLE public."run_daily_facts" DROP COLUMN IF EXISTS "trigger_id";'
_DROP_DISPATCH_PHASE = 'ALTER TABLE public."run_daily_facts" DROP COLUMN IF EXISTS "dispatch_phase";'
_DROP_DISPATCH_PHASE_ENTERED_AT = (
    'ALTER TABLE public."run_daily_facts" DROP COLUMN IF EXISTS "dispatch_phase_entered_at";'
)


def upgrade() -> None:
    op.add_column("run_daily_facts", _TRIGGER_ID)
    op.add_column("run_daily_facts", _DISPATCH_PHASE)
    op.add_column("run_daily_facts", _DISPATCH_PHASE_ENTERED_AT)
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.execute(_DROP_DISPATCH_PHASE_ENTERED_AT)
    op.execute(_DROP_DISPATCH_PHASE)
    op.execute(_DROP_TRIGGER_ID)
