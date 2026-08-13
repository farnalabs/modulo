"""hitl_claims.overdue_notified_at — idempotent hitl_overdue notification dispatch

Revision ID: 0096_hitl_claims_overdue_notified
Revises: 0095_ongoing_trigger_flag
Create Date: 2026-08-13

Adds ``hitl_claims.overdue_notified_at`` (timestamptz, nullable). The
``hitl_overdue`` notification job (SAQ system cron) stamps this column when it
dispatches a ``hitl_overdue`` event for a claim, so a claim that stays waiting
past the overdue threshold is alerted exactly once instead of re-alerting on
every cron tick. ``NULL`` means "not yet alerted"; the job selects only rows
with ``overdue_notified_at IS NULL`` and sets the stamp after a successful
dispatch. Existing undecided claims keep ``NULL`` and will be picked up by the
next job tick — no backfill needed.

Migration tree: ``0093_run_number_sequence`` -> ``0094_ongoing_trigger_type``
-> ``0095_ongoing_trigger_flag`` -> ``0096_hitl_claims_overdue_notified``
(sole head).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0096_hitl_claims_overdue_notified"
down_revision: str | None = "0095_ongoing_trigger_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "hitl_claims"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("overdue_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "overdue_notified_at")
