"""Create composite_templates table.

Revision ID: 0050_composite_templates
Revises: 0049_remy_tables
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_composite_templates"
down_revision: str | Sequence[str] | None = "0049_remy_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "composite_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sub_pipeline_graph_json", sa.JSON(), nullable=False),
        sa.Column("parameter_ports_json", sa.JSON(), nullable=False),
        sa.Column("input_schema_id", sa.Uuid(), nullable=True),
        sa.Column("output_schema_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.String(50), nullable=False, server_default="1.0.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
    )

    op.create_index("ix_composite_templates_organisation_id", "composite_templates", ["organisation_id"])
    op.create_index("ix_composite_templates_account_id", "composite_templates", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_composite_templates_account_id", table_name="composite_templates")
    op.drop_index("ix_composite_templates_organisation_id", table_name="composite_templates")
    op.drop_table("composite_templates")
