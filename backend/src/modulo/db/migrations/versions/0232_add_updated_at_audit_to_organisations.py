"""Add updated_at, deleted_by, updated_by to organisations.

Revision ID: 0232_add_updated_at_audit_to_organisations
Revises: 0231_token_families_reuse_replay_count
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0232_add_updated_at_audit_to_organisations"
down_revision = "0231_token_families_reuse_replay_count"


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
    )
    op.add_column(
        "organisations",
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "organisations",
        sa.Column("updated_by", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organisations", "updated_by")
    op.drop_column("organisations", "deleted_by")
    op.drop_column("organisations", "updated_at")
