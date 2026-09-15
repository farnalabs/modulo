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

Idempotency: the reinstatement uses server-side ``ADD COLUMN IF NOT EXISTS`` and
a guarded ``ADD CONSTRAINT`` (DO-block over ``information_schema``), so applying
the chain from an empty database — where 0233 already created these columns and
0239 is a no-op — is a no-op rather than raising ``DuplicateColumn``. On a live
DB that actually ran a dropping 0239 the columns/FK are absent and get
reinstated here.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    """Reinstate the organisations audit columns + created_by FK if missing.

    Server-side idempotent DDL: Postgres itself decides whether each object
    already exists, so the migration is safe to re-apply on top of 0233's
    columns (fresh DB) and still reinstates them on a DB where 0239 dropped them.
    """
    op.execute(
        sa.text(
            "ALTER TABLE organisations "
            "ADD COLUMN IF NOT EXISTS updated_at "
            "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL"
        )
    )
    op.execute(sa.text("ALTER TABLE organisations ADD COLUMN IF NOT EXISTS updated_by UUID"))
    op.execute(sa.text("ALTER TABLE organisations ADD COLUMN IF NOT EXISTS deleted_by UUID"))
    op.execute(
        sa.text(
            "DO $$ "
            "BEGIN "
            "IF NOT EXISTS ("
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = 'public' AND table_name = 'organisations' "
            "AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = 'fk_organisations_created_by'"
            ") THEN "
            "ALTER TABLE organisations "
            "ADD CONSTRAINT fk_organisations_created_by "
            "FOREIGN KEY (created_by) REFERENCES accounts(id) ON DELETE SET NULL; "
            "END IF; "
            "END $$"
        )
    )


def downgrade() -> None:
    """Drop the reinstated columns/FK (idempotent)."""
    op.execute(
        sa.text(
            "DO $$ "
            "BEGIN "
            "IF EXISTS ("
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = 'public' AND table_name = 'organisations' "
            "AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = 'fk_organisations_created_by'"
            ") THEN "
            "ALTER TABLE organisations DROP CONSTRAINT fk_organisations_created_by; "
            "END IF; "
            "END $$"
        )
    )
    for col in ("deleted_by", "updated_by", "updated_at"):
        # Column names are a fixed literal whitelist (no user input), so inline
        # them directly — DDL identifiers cannot be passed as bind parameters.
        op.execute(sa.text(f"ALTER TABLE organisations DROP COLUMN IF EXISTS {col}"))
