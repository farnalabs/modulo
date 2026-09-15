"""No-op: organisations audit columns + created_by FK are retained by 0239.

Migration 0233_add_updated_at_audit_to_organisations added the
updated_at/updated_by/deleted_by columns and 0236_add_organisations_constraints
added the fk_organisations_created_by FK to organisations. Migration
0239_revert_organisations_audit_drift was originally written to DROP those
columns/FK, but was reworked into a no-op that RETURNS them, because the
Organisation ORM model (PR #553, commit b341e13a1) declares them and dropping
them produces schema drift vs the ORM.

Because 0239 is a no-op that retains the columns/FK, this migration must also
be a no-op: attempting to re-add updated_at/updated_by/deleted_by or recreate
fk_organisations_created_by raises DuplicateColumn / duplicate-object errors on
a fresh (from-base) migration run, which breaks the BDD full suite, the Bundled
Runner harness, and the break-glass deploy gate. The columns/FK are already
present in the intended schema after 0233 + 0236 + 0239, so there is nothing to
reinstate.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"


def upgrade() -> None:
    # No-op: the updated_at/updated_by/deleted_by columns (added by 0233) and
    # the fk_organisations_created_by FK (added by 0236) are retained by the
    # no-op 0239, so they already exist in the migrated schema. Re-adding them
    # would raise DuplicateColumn / duplicate-object on a fresh migration run.
    pass


def downgrade() -> None:
    # No-op: nothing was added in upgrade, so there is nothing to revert.
    pass
