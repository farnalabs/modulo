"""Make token_family.organisation_id NOT NULL.

Revision ID: 0236_fix_token_family_org_nullable
Revises: 0235_add_organisations_constraints
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0236_fix_token_family_org_nullable"
down_revision = "0235_add_organisations_constraints"


def upgrade() -> None:
    op.execute(
        "UPDATE token_families SET organisation_id = '00000000-0000-0000-0000-000000000000' "
        "WHERE organisation_id IS NULL"
    )
    op.alter_column(
        "token_families",
        "organisation_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "token_families",
        "organisation_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
