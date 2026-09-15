"""Retain organisations audit columns + created_by FK (no-op fix of 0240).

PR #530 added updated_at/updated_by/deleted_by (0233) and the
fk_organisations_created_by FK (0236) to the organisations table. This
migration was originally written to RE-ADD those columns/FK because it was
assumed 0239_revert_organisations_audit_drift had dropped them.

That assumption was wrong on two counts:
* 0239 is itself a no-op (it retains the 0233/0236 columns/FK, which the
  Organisation ORM model declared after PR #553);
* the columns/FK are therefore already present after 0233/0236 run.

The original RE-ADD produced a DuplicateColumn failure on a fresh database
("column updated_at of relation organisations already exists") and broke every
CI run that boots the schema from scratch (break-glass deploy gate, schema
freshness). Because 0240 never completed successfully, no database has it
recorded in alembic_version, so turning it into a no-op is safe: 0233/0236
already provide the intended schema and the migration chain stays linear.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    # No-op: the organisations updated_at/updated_by/deleted_by columns (added
    # by 0233) and the fk_organisations_created_by FK (added by 0236) are already
    # part of the migrated schema. Re-adding them here caused a DuplicateColumn
    # failure on fresh databases; retaining them (and doing nothing) keeps the
    # schema correct without breaking the migration chain.
    pass


def downgrade() -> None:
    # No-op: nothing was added in upgrade, so there is nothing to revert.
    pass
