"""Add 'composite' to library_primitives type constraint.

Revision ID: 0051_composite_library_type
Revises: 0050_composite_templates
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_composite_library_type"
down_revision: str | Sequence[str] | None = "0050_composite_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_library_primitives_type", "library_primitives")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        sa.text(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', "
            "'test_fixture', 'pipeline_template', 'composite')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_library_primitives_type", "library_primitives")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        sa.text(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', "
            "'test_fixture', 'pipeline_template')"
        ),
    )
