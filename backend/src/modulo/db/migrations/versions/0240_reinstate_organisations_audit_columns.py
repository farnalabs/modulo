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

Idempotency: 0239 was later made a no-op (it retains the columns/FK added by
0233/0236), so on a fresh DB these columns/FK already exist when this
migration runs. Adding them unconditionally here therefore fails the whole
chain with "column organisations.updated_at already exists" during
``alembic upgrade head`` (seen on the pre-deploy integration-test run). The
existence checks below keep the migration correct for both a fresh DB (columns
present - skip the add) and a prod DB whose 0239 actually dropped them (columns
absent - add them).

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"

_FK_NAME = "fk_organisations_created_by"


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns("organisations")}
    existing_fks = {fk["name"] for fk in insp.get_foreign_keys("organisations")}

    if "updated_at" not in existing_cols:
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
    if "updated_by" not in existing_cols:
        op.add_column("organisations", sa.Column("updated_by", sa.Uuid(), nullable=True))
    if "deleted_by" not in existing_cols:
        op.add_column("organisations", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    if _FK_NAME not in existing_fks:
        op.create_foreign_key(
            _FK_NAME,
            "organisations",
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "organisations", type_="foreignkey")
    op.drop_column("organisations", "deleted_by")
    op.drop_column("organisations", "updated_by")
    op.drop_column("organisations", "updated_at")
