"""Add updated_at, deleted_by, updated_by to organisations.

Revision ID: 0233_add_updated_at_audit_to_organisations
Revises: 0232_seed_modulo_sentinel_organisation
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0233_add_updated_at_audit_to_organisations"
down_revision = "0232_seed_modulo_sentinel_organisation"


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
