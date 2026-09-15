"""No-op: the organisations audit columns / created_by FK are intentional.

PR #530 added updated_at/updated_by/deleted_by (0233) and a
fk_organisations_created_by FK plus CHECK guards (0236) to ``organisations``.
#552 originally added THIS migration to drop them because, at that time, the
Organisation ORM model did not declare them. #553 then aligned the ORM with
migrations 0233/0236 (fix(deploy): align Organisation ORM with migrations
0233/0236) — the model now deliberately declares created_by (FK, ondelete
SET NULL), updated_at, updated_by and deleted_by, and the 0236 CHECK guards
are legitimate migration-owned quality constraints. So the columns/FK are no
longer drift: they are the intended, ORM-modelled surface.

This migration therefore does nothing. The columns and FK created by 0233/0236
stay, matching the ORM metadata, so
test_migrated_schema_matches_orm_metadata stays green. The revision id is kept
unchanged (already recorded in alembic_version on deployed DBs) so re-running
``alembic upgrade heads`` is a safe no-op.

Revision ID: 0239_revert_organisations_audit_drift
Revises: 0238_workspace_input_drift_and_audit
Create Date: 2026-09-14
"""

revision = "0239_revert_organisations_audit_drift"
down_revision = "0238_workspace_input_drift_and_audit"


def upgrade() -> None:
    # Intentionally a no-op. The organisations audit columns (updated_at,
    # updated_by, deleted_by) and the fk_organisations_created_by FK are now
    # part of the ORM model (see #553), so dropping them would create schema
    # drift vs the ORM metadata. They remain owned by 0233/0236.
    pass


def downgrade() -> None:
    # Symmetric no-op: the columns/FK are owned (created/dropped) by 0233/0236,
    # not by this migration.
    pass
