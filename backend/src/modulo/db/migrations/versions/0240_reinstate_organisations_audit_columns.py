"""No-op retain of organisations audit columns + created_by FK.

PR #530 added updated_at/updated_by/deleted_by to organisations (0233) and the
fk_organisations_created_by FK (0236). Migration 0239_revert_organisations_audit_drift
was originally written to DROP those columns/FK, but is now itself a no-op retainer:
the Organisation ORM model declares these columns/FK, so dropping them would reintroduce
schema drift vs the ORM (UndefinedColumnError: column organisations.updated_at does not
exist) and break every query that loads an Organisation.

Because 0233 already adds the columns/FK and 0239 (the immediate down_revision) does NOT
drop them, re-adding them here would raise DuplicateColumn on a fresh database. This
migration is therefore an explicit no-op that simply documents that the columns/FK are
retained as part of the intended schema. It chains on top of 0239 so the migration graph
stays linear.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    # No-op: the organisations updated_at/updated_by/deleted_by columns and the
    # fk_organisations_created_by FK are already added by 0233/0236 and retained
    # by the no-op 0239, so there is nothing to reinstate here. Re-adding them
    # would raise DuplicateColumn on a fresh database.
    pass


def downgrade() -> None:
    # No-op: nothing was added in upgrade, so there is nothing to revert.
    pass
