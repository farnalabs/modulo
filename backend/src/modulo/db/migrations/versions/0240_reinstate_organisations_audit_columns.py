"""Reinstate organisations audit columns + created_by FK if missing (idempotent).

Migration 0239_revert_organisations_audit_drift was originally written to DROP
the updated_at/updated_by/deleted_by columns (added by 0233) and the
fk_organisations_created_by FK (added by 0236), on the grounds that the
Organisation ORM model did not declare them. That premise was wrong: the
Organisation model DOES declare these columns/FK (see
src/modulo/db/models/organisation.py), so 0239 was reworked into a no-op that
*retains* them.

This migration guarantees the columns/FK end up present on every database the
chain runs against. On a fresh DB the columns/FK already exist (0233/0236 added
them and 0239 retained them), so re-adding them would fail the whole chain with
``column organisations.updated_at already exists`` / a duplicate FK — which is
exactly the BDD pre-deploy failure this migration caused. On a database whose
earlier 0239 actually dropped them, this migration restores them. Existence
checks keep the migration correct for both a fresh and an already-applied DB.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"

_TABLE = "organisations"
_FK_NAME = "fk_organisations_created_by"


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns(_TABLE)}
    existing_fks = {fk["name"] for fk in insp.get_foreign_keys(_TABLE)}

    if "updated_at" not in existing_cols:
        op.add_column(
            _TABLE,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                onupdate=sa.func.current_timestamp(),
                nullable=False,
            ),
        )
    if "updated_by" not in existing_cols:
        op.add_column(_TABLE, sa.Column("updated_by", sa.Uuid(), nullable=True))
    if "deleted_by" not in existing_cols:
        op.add_column(_TABLE, sa.Column("deleted_by", sa.Uuid(), nullable=True))
    if _FK_NAME not in existing_fks:
        op.create_foreign_key(
            _FK_NAME,
            _TABLE,
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_fks = {fk["name"] for fk in insp.get_foreign_keys(_TABLE)}
    existing_cols = {c["name"] for c in insp.get_columns(_TABLE)}

    if _FK_NAME in existing_fks:
        op.drop_constraint(_FK_NAME, _TABLE, type_="foreignkey")
    if "deleted_by" in existing_cols:
        op.drop_column(_TABLE, "deleted_by")
    if "updated_by" in existing_cols:
        op.drop_column(_TABLE, "updated_by")
    if "updated_at" in existing_cols:
        op.drop_column(_TABLE, "updated_at")
