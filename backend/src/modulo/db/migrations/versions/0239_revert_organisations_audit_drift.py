"""No-op retain of organisations columns (drift NOT actually present here).

PR #530 added updated_at/updated_by/deleted_by columns (0233) and a
fk_organisations_created_by FK (0236) to the organisations table. This
migration was originally written to DROP those columns/FK because the
Organisation ORM model does NOT declare them and the pre-deploy integration
test (test_migrated_schema_matches_orm_metadata) flagged the drift.

The subsequent claim that PR #553 ("align Organisation ORM with migrations
0233/0236") made the Organisation model declare these columns is FALSE: the
Organisation model (src/modulo/db/models/organisation.py) still does NOT
declare updated_at/updated_by/deleted_by, and created_by is deliberately NOT
a FK. The columns/FK added by 0233/0236 are therefore genuine schema drift vs
the ORM, not part of the intended schema.

This migration is therefore a no-op: it neither drops nor retains the columns
intentionally. Migration 0240_reinstate_organisations_audit_columns was then
written on the false premise that this migration dropped them, and tried to
re-add updated_at -> DuplicateColumn on a fresh database. 0240 has since been
corrected to DROP the drift columns/FK so the migrated schema matches the ORM.
The 0236 CHECK constraints were always kept (they are legitimate,
migration-owned quality guards).

Revision ID: 0239_revert_organisations_audit_drift
Revises: 0238_workspace_input_drift_and_audit
Create Date: 2026-09-14
"""

revision = "0239_revert_organisations_audit_drift"
down_revision = "0238_workspace_input_drift_and_audit"


def upgrade() -> None:
    # No-op: the organisations updated_at/updated_by/deleted_by columns and the
    # fk_organisations_created_by FK (added by 0233/0236) are now declared on
    # the Organisation ORM model (PR #553, commit b341e13a1), so they must be
    # retained.
    pass


def downgrade() -> None:
    # No-op: nothing was dropped in upgrade, so there is nothing to revert.
    pass
