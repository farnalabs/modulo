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

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import logging

import sqlalchemy as sa
from alembic import op

_log = logging.getLogger(__name__)

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def _column_exists(bind: sa.Connection, column_name: str) -> bool:
    """True when ``organisations.<column_name>`` already exists."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'organisations' "
                "AND column_name = :col"
            ).bindparams(col=column_name)
        ).scalar()
    )


def _fk_exists(bind: sa.Connection, constraint_name: str) -> bool:
    """True when the ``<constraint_name>`` FK already exists on ``organisations``."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_schema = 'public' AND table_name = 'organisations' "
                "AND constraint_type = 'FOREIGN KEY' AND constraint_name = :cname"
            ).bindparams(cname=constraint_name)
        ).scalar()
    )


def upgrade() -> None:
    """Reinstate the organisations audit columns + created_by FK if missing.

    Existence-guarded so re-applying the chain from an empty database (where
    0239 is now a no-op and 0233 already created these columns) is a no-op
    rather than raising ``DuplicateColumn``. On a live DB that already ran the
    original dropping 0239, the columns/FK are absent and get reinstated here.
    """
    bind = op.get_bind()
    for col in ("updated_at", "updated_by", "deleted_by"):
        if _column_exists(bind, col):
            _log.info("0240: organisations.%s already present; skipping add", col)
            continue
        if col == "updated_at":
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
        else:
            op.add_column("organisations", sa.Column(col, sa.Uuid(), nullable=True))
        _log.info("0240: added organisations.%s", col)
    if _fk_exists(bind, "fk_organisations_created_by"):
        _log.info("0240: fk_organisations_created_by already present; skipping add")
    else:
        op.create_foreign_key(
            "fk_organisations_created_by",
            "organisations",
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )
        _log.info("0240: added fk_organisations_created_by FK")


def downgrade() -> None:
    """Drop the reinstated columns/FK (idempotent)."""
    bind = op.get_bind()
    if _fk_exists(bind, "fk_organisations_created_by"):
        op.drop_constraint("fk_organisations_created_by", "organisations", type_="foreignkey")
    for col in ("deleted_by", "updated_by", "updated_at"):
        if _column_exists(bind, col):
            op.drop_column("organisations", col)
