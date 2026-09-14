"""Add status and created_at indexes to organisations.

Revision ID: 0233_add_organisations_indexes
Revises: 0232_add_updated_at_audit_to_organisations
Create Date: 2026-09-14
"""

from alembic import op

revision = "0233_add_organisations_indexes"
down_revision = "0232_add_updated_at_audit_to_organisations"


def upgrade() -> None:
    op.create_index("ix_organisations_status", "organisations", ["status"])
    op.create_index("ix_organisations_created_at", "organisations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_organisations_created_at", table_name="organisations")
    op.drop_index("ix_organisations_status", table_name="organisations")
