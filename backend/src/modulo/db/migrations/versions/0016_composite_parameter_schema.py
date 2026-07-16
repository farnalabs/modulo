"""Add parameter_schema_id to composite_templates.

Implements RFC Â§10 Phase 4: CompositeTemplate Parameter Schema.

Changes:
  - Add parameter_schema_id column to composite_templates table (nullable)
  - FK to parameter_schemas.id ON DELETE RESTRICT
  - Index on the new column

Revision ID: 0016_composite_parameter_schema
Revises: 0015_parameter_bindings
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_composite_parameter_schema"
down_revision: str | None = "0015_parameter_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "composite_templates",
        sa.Column(
            "parameter_schema_id",
            sa.Uuid(),
            sa.ForeignKey("parameter_schemas.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_composite_templates_parameter_schema_id"),
        "composite_templates",
        ["parameter_schema_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_composite_templates_parameter_schema_id"),
        table_name="composite_templates",
    )
    op.drop_constraint(
        op.f("fk_composite_templates_parameter_schema_id"),
        "composite_templates",
        type_="foreignkey",
    )
    op.drop_column("composite_templates", "parameter_schema_id")
