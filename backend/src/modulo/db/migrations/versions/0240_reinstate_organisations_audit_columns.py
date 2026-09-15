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

On a fresh database the columns/FK are already present (0233 adds the three
audit columns, 0236 adds the FK), so the upgrade is idempotent: each object is
only created when it is currently absent. On a live database where an earlier
DROP variant of 0239 was applied, the missing columns/FK are reinstated. The
downgrade only drops what this migration actually added.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("organisations")}
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("organisations")}

    if "updated_at" not in existing_columns:
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
    if "updated_by" not in existing_columns:
        op.add_column("organisations", sa.Column("updated_by", sa.Uuid(), nullable=True))
    if "deleted_by" not in existing_columns:
        op.add_column("organisations", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    if "fk_organisations_created_by" not in existing_fks:
        op.create_foreign_key(
            "fk_organisations_created_by",
            "organisations",
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("organisations")}
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("organisations")}

    if "fk_organisations_created_by" in existing_fks:
        op.drop_constraint("fk_organisations_created_by", "organisations", type_="foreignkey")
    if "deleted_by" in existing_columns:
        op.drop_column("organisations", "deleted_by")
    if "updated_by" in existing_columns:
        op.drop_column("organisations", "updated_by")
    if "updated_at" in existing_columns:
        op.drop_column("organisations", "updated_at")
