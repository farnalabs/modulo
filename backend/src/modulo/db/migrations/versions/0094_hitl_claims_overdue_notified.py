"""hitl_claims.overdue_notified_at — idempotent hitl_overdue notification dispatch

Revision ID: 0094_hitl_claims_overdue_notified
Revises: 0093_run_number_sequence
Create Date: 2026-08-13

Adds ``hitl_claims.overdue_notified_at`` (timestamptz, nullable). The
``hitl_overdue`` notification job (SAQ system cron) stamps this column when it
dispatches a ``hitl_overdue`` event for a claim, so a claim that stays waiting
past the overdue threshold is alerted exactly once instead of re-alerting on
every cron tick. ``NULL`` means "not yet alerted"; the job selects only rows
with ``overdue_notified_at IS NULL`` and sets the stamp after a successful
dispatch. Existing undecided claims keep ``NULL`` and will be picked up by the
next job tick — no backfill needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094_hitl_claims_overdue_notified"
down_revision: str | None = "0093_run_number_sequence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "hitl_claims"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("overdue_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "overdue_notified_at")
