"""No-op chain link — organisations audit columns are already present.

The organisations ``updated_at``/``updated_by``/``deleted_by`` columns (added by
0233) and the ``fk_organisations_created_by`` FK (added by 0236) are part of the
intended schema: they are retained by 0239_revert_organisations_audit_drift,
whose ``upgrade()`` is itself a no-op that deliberately keeps them (the
Organisation ORM model in PR #553 aligns to these columns, so dropping them
would reintroduce schema drift vs the ORM).

An earlier version of this migration re-added those columns/FK on the mistaken
belief that 0239 had dropped them. That produced ``DuplicateColumn: column
"updated_at" of relation "organisations" already exists`` on a fresh database,
because 0233 had already created them. Since the columns/FK already exist after
0233 + 0236 + 0239, this migration has nothing to reinstate and is now a no-op.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    # No-op: the organisations updated_at/updated_by/deleted_by columns and the
    # fk_organisations_created_by FK already exist after 0233 + 0236 + 0239, so
    # there is nothing to reinstate. Re-adding them raised DuplicateColumn on a
    # fresh database.
    pass


def downgrade() -> None:
    # No-op: nothing was added in upgrade, so there is nothing to revert.
    pass
