"""Add category to library_primitives, allow pipeline_template type.

Revision ID: 0019_pipeline_templates
Revises: 0018_api_key_expires_at
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_pipeline_templates"
down_revision: str | Sequence[str] | None = "0018_api_key_expires_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "library_primitives",
        sa.Column("category", sa.String(50), nullable=True),
    )
    op.drop_constraint("ck_library_primitives_type", "library_primitives")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        sa.text(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', 'test_fixture', 'pipeline_template')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_library_primitives_type", "library_primitives")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        sa.text("primitive_type IN ('schema', 'workflow', 'agent', 'integration', 'test_fixture')"),
    )
    op.drop_column("library_primitives", "category")
