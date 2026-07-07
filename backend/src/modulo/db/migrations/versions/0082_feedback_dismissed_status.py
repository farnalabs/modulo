"""Add 'dismissed' to feedback_status CHECK constraint.

The dismiss review action (POST /feedback/inbox/{record_id}/review with
action: dismiss) sets feedback_status to 'dismissed', but the CHECK constraint
only allowed 'pending', 'routing', 'correcting', 'resolved', 'escalated'.

Revision ID: 0082_feedback_dismissed_status
Revises: 0081_add_error_group_resolved_at
Create Date: 2026-07-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0082_feedback_dismissed_status"
down_revision: str | Sequence[str] | None = "0081_add_error_group_resolved_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_feedback_records_status", "feedback_records", type_="check")
    op.create_check_constraint(
        "ck_feedback_records_status",
        "feedback_records",
        "feedback_status IN ('pending', 'routing', 'correcting', 'resolved', 'escalated', 'dismissed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_feedback_records_status", "feedback_records", type_="check")
    op.create_check_constraint(
        "ck_feedback_records_status",
        "feedback_records",
        "feedback_status IN ('pending', 'routing', 'correcting', 'resolved', 'escalated')",
    )
