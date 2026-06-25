"""Add pass_threshold and suite_id columns to eval_definitions.

Revision ID: 0023_eval_suite_threshold
Revises: 0022_cost_export_anomalies
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_eval_suite_threshold"
down_revision: str | Sequence[str] | None = "0022_cost_export_anomalies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eval_definitions",
        sa.Column("pass_threshold", sa.Float(), nullable=True),
    )
    op.add_column(
        "eval_definitions",
        sa.Column("suite_id", sa.String(255), nullable=True),
    )
    op.create_index(
        op.f("ix_eval_definitions_suite_id"),
        "eval_definitions",
        ["suite_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_eval_definitions_suite_id"), table_name="eval_definitions")
    op.drop_column("eval_definitions", "suite_id")
    op.drop_column("eval_definitions", "pass_threshold")
