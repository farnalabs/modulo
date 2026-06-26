"""Add version_group_id and update_available_version_id to library_primitives.

Revision ID: 0030_contribution_versions
Revises: 0029_eval_failed_status
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_contribution_versions"
down_revision: str | Sequence[str] | None = "0029_eval_failed_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "library_primitives",
        sa.Column("version_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "library_primitives",
        sa.Column(
            "update_available_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_library_primitives_update_available_version",
        "library_primitives",
        "library_primitives",
        ["update_available_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_library_primitives_update_available_version",
        "library_primitives",
        type_="foreignkey",
    )
    op.drop_column("library_primitives", "update_available_version_id")
    op.drop_column("library_primitives", "version_group_id")
