"""Drop the 'owner' org role and add the authz_enforce kill-switch column.

Revision ID: 0030_drop_owner_role_add_authz_enforce
Revises: 0029_fix_expiry_fields_non_null
Create Date: 2026-07-31
"""

import logging

from alembic import op
from sqlalchemy import text

revision: str = "0030_drop_owner_role_add_authz_enforce"
down_revision: str | None = "0029_fix_expiry_fields_non_null"

_log = logging.getLogger(__name__)


def upgrade() -> None:
    conn = op.get_bind()

    # Pre-migration row-count audit (ADR 017 A1a): log how many memberships
    # still claim the owner role before they are migrated to admin.
    owner_count = conn.execute(text("SELECT COUNT(*) FROM org_memberships WHERE role = 'owner'")).scalar()
    _log.info("authz.owner_rows_pre_migration", extra={"count": owner_count})

    # 1. Owner -> admin. Idempotent: a re-run finds zero owner rows.
    op.execute("UPDATE org_memberships SET role = 'admin' WHERE role = 'owner'")

    # 2. Drop and re-add the role CHECK constraint without 'owner'. PG DDL is
    #    transactional, so this runs in the same transaction as the UPDATE.
    op.execute("ALTER TABLE org_memberships DROP CONSTRAINT IF EXISTS ck_org_memberships_role")
    op.execute(
        "ALTER TABLE org_memberships ADD CONSTRAINT ck_org_memberships_role "
        "CHECK (role IN ('admin', 'operator', 'runner', 'viewer'))"
    )

    # 3. Dedicated tenancy-bounded kill-switch column (ADR 017 DECISION 3).
    #    A dedicated boolean is atomic at statement level and multi-backend
    #    safe — jsonb_set cannot target the JSON settings_json column.
    op.execute("ALTER TABLE organisations ADD COLUMN authz_enforce BOOLEAN NOT NULL DEFAULT TRUE")


def downgrade() -> None:
    # ADR 017: the owner-drop is irreversible (owner rows no longer exist to
    # restore). Only the additive kill-switch column is removed.
    op.execute("ALTER TABLE organisations DROP COLUMN IF EXISTS authz_enforce")
