"""Drop organisations audit columns + created_by FK (PR #559 follow-up to 0240).

PR #530 added updated_at/updated_by/deleted_by columns (0233) and a
fk_organisations_created_by FK (0236) to the organisations table. Migration
0239 originally dropped them, but 0240_reinstate_organisations_audit_columns
reinstated them on the grounds that the ORM declares them — that premise is
wrong for the Organisation root entity: the Organisation model does NOT declare
these columns/FK (see src/modulo/db/models/organisation.py), organisations is
intentionally excluded from the trigger-maintained audit-chain (agents /
connector_instances / scheduled_reports only), and no application code reads or
writes them. Keeping them produces schema drift vs the ORM metadata, which the
pre-deploy integration test (test_migrated_schema_matches_orm_metadata) flags.

This migration chains on top of 0240 and drops the spurious columns/FK so the
migrated schema matches the Organisation ORM. The 0236 CHECK constraints are
kept (they are legitimate, migration-owned quality guards).

Revision ID: 0241_drop_organisations_audit_columns
Revises: 0240_reinstate_organisations_audit_columns
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0241_drop_organisations_audit_columns"
down_revision = "0240_reinstate_organisations_audit_columns"


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
            onupdate=sa.func.current_timestamp(),
            nullable=False,
        ),
    )
    op.add_column("organisations", sa.Column("updated_by", sa.Uuid(), nullable=True))
    op.add_column("organisations", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_organisations_created_by",
        "organisations",
        "accounts",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
