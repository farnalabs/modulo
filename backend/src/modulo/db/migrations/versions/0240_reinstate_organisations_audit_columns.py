"""Reinstate organisations audit columns + created_by FK dropped by 0239.

Migration 0239_revert_organisations_audit_drift erroneously dropped the
updated_at/updated_by/deleted_by columns (added by 0233) and the
fk_organisations_created_by FK (added by 0236) on the grounds that the
Organisation ORM model did not declare them. That premise was wrong: the
Organisation model DOES declare these columns/FK (see
src/modulo/db/models/organisation.py), so dropping them produced schema drift
vs the ORM and broke every query that touches organisations at runtime
(UndefinedColumnError: column organisations.updated_at does not exist).

This migration restores the columns/FK so the migrated schema matches the ORM
metadata again. It chains on top of 0239 (the erroneous revert is retained as
history, not undone, because it has already been applied to live databases).

The add operations are idempotent (guarded by information_schema checks): on a
fresh database the columns/FK already exist because migration 0233_add_updated_at
and 0236_add_organisations_constraints create them, and 0239 is a no-op, so this
migration must not re-create them (that raised DuplicateColumn on a clean DB).
On a live database where the originally-applied 0239 physically dropped them,
this migration adds them back.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM information_schema.columns WHERE table_name = :table AND column_name = :column"),
            {"table": table, "column": column},
        ).scalar()
        is not None
    )


def _fk_exists(conn, table: str, fk_name: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE table_name = :table "
                "AND constraint_name = :fk AND constraint_type = 'FOREIGN KEY'"
            ),
            {"table": table, "fk": fk_name},
        ).scalar()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, "organisations", "updated_at"):
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
    if not _column_exists(conn, "organisations", "updated_by"):
        op.add_column("organisations", sa.Column("updated_by", sa.Uuid(), nullable=True))
    if not _column_exists(conn, "organisations", "deleted_by"):
        op.add_column("organisations", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    if not _fk_exists(conn, "organisations", "fk_organisations_created_by"):
        op.create_foreign_key(
            "fk_organisations_created_by",
            "organisations",
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _fk_exists(conn, "organisations", "fk_organisations_created_by"):
        op.drop_constraint("fk_organisations_created_by", "organisations", type_="foreignkey")
    if _column_exists(conn, "organisations", "deleted_by"):
        op.drop_column("organisations", "deleted_by")
    if _column_exists(conn, "organisations", "updated_by"):
        op.drop_column("organisations", "updated_by")
    if _column_exists(conn, "organisations", "updated_at"):
        op.drop_column("organisations", "updated_at")
