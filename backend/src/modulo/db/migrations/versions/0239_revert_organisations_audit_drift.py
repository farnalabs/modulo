"""Revert erroneous organisations audit columns and created_by FK.

PR #530 added updated_at/updated_by/deleted_by columns (0233) and a
fk_organisations_created_by FK (0236) to the organisations table, but the
Organisation ORM model never declared them and no application code uses them.
The model comment is explicit that created_by is "deliberately not an FK"
(the first organisation must exist before its first user), and organisations
is intentionally excluded from the trigger-maintained audit-chain (agents /
connector_instances / scheduled_reports only). Those migrations therefore
produce schema drift vs the ORM metadata, which the pre-deploy integration
test (test_migrated_schema_matches_orm_metadata) flags. This migration drops
the spurious columns/FK so the migrated schema matches the ORM. The 0236 CHECK
constraints are kept (they are legitimate, migration-owned quality guards).

Revision ID: 0239_revert_organisations_audit_drift
Revises: 0238_workspace_input_drift_and_audit
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0239_revert_organisations_audit_drift"
down_revision = "0238_workspace_input_drift_and_audit"


def upgrade() -> None:
    op.drop_constraint("fk_organisations_created_by", "organisations", type_="foreignkey")
    op.drop_column("organisations", "updated_by")
    op.drop_column("organisations", "deleted_by")
    op.drop_column("organisations", "updated_at")


def downgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
    )
    op.add_column("organisations", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    op.add_column("organisations", sa.Column("updated_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_organisations_created_by",
        "organisations",
        "accounts",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
