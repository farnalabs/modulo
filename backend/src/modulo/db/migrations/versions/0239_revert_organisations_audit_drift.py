"""Retain organisations audit columns and created_by FK (no-op revert of 0233/0236).

PR #530 added updated_at/updated_by/deleted_by columns (0233) and a
fk_organisations_created_by FK (0236) to the organisations table. This
migration was originally written to DROP those columns/FK because, at the time,
the Organisation ORM model did not declare them and the pre-deploy integration
test (test_migrated_schema_matches_orm_metadata) flagged the drift.

Commit #553 ("align Organisation ORM with migrations 0233/0236") then aligned
the ORM to those migrations: the Organisation model now declares
updated_at/updated_by/deleted_by and created_by as a FK to accounts. The
columns/FK are therefore part of the INTENDED schema, not drift, so dropping
them reintroduces drift (and breaks every query that loads an Organisation,
e.g. GET /api/v1/runners/status -> 501 UndefinedColumnError).

This migration is now a no-op: it retains the columns/FK added by 0233/0236 so
the migrated schema matches the ORM. The 0236 CHECK constraints were always
kept (they are legitimate, migration-owned quality guards).

Revision ID: 0239_revert_organisations_audit_drift
Revises: 0238_workspace_input_drift_and_audit
Create Date: 2026-09-14
"""

revision = "0239_revert_organisations_audit_drift"
down_revision = "0238_workspace_input_drift_and_audit"


def upgrade() -> None:
    # No-op: the organisations updated_at/updated_by/deleted_by columns and the
    # fk_organisations_created_by FK (added by 0233/0236) are now declared on
    # the Organisation ORM model (commit #553), so they must be retained.
    pass


def downgrade() -> None:
    # No-op: nothing was dropped in upgrade, so there is nothing to revert.
    pass
