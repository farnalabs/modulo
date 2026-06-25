"""Create variant_groups table.

Revision ID: 0012_variant_groups
Revises: 0011_eval_tables
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_variant_groups"
down_revision: str | None = "0011_eval_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "variant_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("pipeline_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("variants", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column(
            "selection_strategy", sa.String(20), nullable=False,
            server_default=sa.text("'weighted'"),
        ),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("degraded_evals", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_variant_groups_organisation_id"),
        "variant_groups",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_variant_groups_pipeline_id"),
        "variant_groups",
        ["pipeline_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_variant_groups_selection_strategy",
        "variant_groups",
        "selection_strategy IN ('weighted', 'single')",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_variant_groups_pipeline_id"), table_name="variant_groups")
    op.drop_index(op.f("ix_variant_groups_organisation_id"), table_name="variant_groups")
    op.drop_table("variant_groups")
