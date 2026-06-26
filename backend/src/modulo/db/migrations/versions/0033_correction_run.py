"""Add parent_run_id to runs, add correction to trigger_type constraint.

Revision ID: 0033_correction_run
Revises: 0032_publishers
Create Date: 2026-06-26
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_correction_run"
down_revision: str | Sequence[str] | None = "0032_publishers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    op.drop_constraint("ck_runs_trigger_type", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_trigger_type",
        "runs",
        "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_trigger_type", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_trigger_type",
        "runs",
        "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal')",
    )

    op.drop_column("runs", "parent_run_id")
