"""No-op reconciliation of organisations audit columns / created_by FK.

Migration 0233_add_updated_at_audit_to_organisations added the
``updated_at``/``updated_by``/``deleted_by`` columns and 0236_add_organisations_constraints
added the ``fk_organisations_created_by`` FK. Migration 0239_revert_organisations_audit_drift
was originally written to DROP those columns/FK, but it was corrected to a no-op that
*retains* them (the Organisation ORM model deliberately does not declare these DB-owned
audit columns — ``created_by`` stays non-FK so the first org can exist before its first
user; the columns/FK are migration-owned divergence, reconciled by the schema-parity
ignore list in ``test_initial_migration.py``).

This migration was previously written to RE-ADD the columns/FK on the (false) premise
that 0239 had dropped them. Because 0239 is a no-op, the columns/FK already exist, so the
re-add raised ``psycopg.errors.DuplicateColumn`` and broke every ``alembic upgrade heads``
run. The columns/FK are already present on every deployed database, so this migration is
now a no-op: it documents the real schema state rather than attempting a duplicate add.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    # No-op: the organisations updated_at/updated_by/deleted_by columns (0233) and
    # the fk_organisations_created_by FK (0236) are DB-owned / migration-owned and
    # are retained by the no-op 0239. They already exist on every migrated database,
    # so re-adding them would raise DuplicateColumn. The Organisation ORM model
    # deliberately does not declare these (created_by stays non-FK), and the
    # schema-parity test benign-ignores them via _AUDIT_CHAIN_COLUMNS /
    # _AUDIT_CHAIN_FK_COLUMNS — matching the sibling audit-chain tables.
    pass


def downgrade() -> None:
    # No-op: nothing was added in upgrade, so there is nothing to revert.
    pass
