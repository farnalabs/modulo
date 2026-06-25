"""Add eval_failed run status to the ck_runs_status CHECK constraint.

Revision ID: 0029_eval_failed_status
Revises: 0028_notification_endpoint_team
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_eval_failed_status"
down_revision: str | Sequence[str] | None = "0028_notification_endpoint_team"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('pending', 'running', 'awaiting_human', 'claimed', "
        "'waiting_for_lock', 'complete', 'failed', 'cancelled', 'eval_failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('pending', 'running', 'awaiting_human', 'claimed', "
        "'waiting_for_lock', 'complete', 'failed', 'cancelled')",
    )
