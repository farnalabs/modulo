"""Add primitive_ratings table for community rating system.

Revision ID: 0006_primitive_ratings
Revises: 0005_library_community_visibility
Create Date: 2026-06-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_primitive_ratings"
down_revision: str | None = "0005_library_community_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "primitive_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("primitive_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("thumbs_up", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["primitive_id"], ["library_primitives.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_primitive_ratings_primitive_id",
        "primitive_ratings",
        ["primitive_id"],
    )
    op.create_check_constraint(
        "ck_primitive_ratings_thumbs",
        "primitive_ratings",
        "thumbs_up IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_table("primitive_ratings")
