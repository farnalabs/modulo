"""Create feedback_records table.

Revision ID: 0012_feedback_records
Revises: 0011_eval_tables
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_feedback_records"
down_revision: str | None = "0011_eval_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("gate_id", sa.String(255), nullable=False),
        sa.Column("rejected_by", sa.UUID(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=False),
        sa.Column(
            "rejected_output",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("producing_node_id", sa.String(255), nullable=False),
        sa.Column("producing_agent_id", sa.UUID(), nullable=True),
        sa.Column(
            "feedback_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "feedback_handler_type",
            sa.String(40),
            nullable=False,
            server_default=sa.text("'human'"),
        ),
        sa.Column("correction_run_id", sa.UUID(), nullable=True),
        sa.Column("eval_gap", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rejected_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["producing_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["correction_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_feedback_records_organisation_id"),
        "feedback_records",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_feedback_records_run_id"),
        "feedback_records",
        ["run_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_feedback_records_status",
        "feedback_records",
        "feedback_status IN ('pending', 'routing', 'correcting', 'resolved', 'escalated')",
    )
    op.create_check_constraint(
        "ck_feedback_records_handler_type",
        "feedback_records",
        "feedback_handler_type IN ('human', 'ai_correction', 'ai_correction_with_human_review')",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_records_run_id"), table_name="feedback_records")
    op.drop_index(op.f("ix_feedback_records_organisation_id"), table_name="feedback_records")
    op.drop_table("feedback_records")
