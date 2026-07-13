"""Fix enforce_same_organisation trigger to skip parent tables without organisation_id.

The trigger on org_memberships and token_families called
enforce_same_organisation() which blindly queries
``SELECT organisation_id FROM accounts WHERE id = $1`` — but ``accounts``
has no ``organisation_id`` column (it is not org-scoped). The missing column
caused ``ProgrammingError`` on every INSERT into those tables, blocking both
user seeding (lifespan) and login (via create_family).

Fix: check information_schema.columns first; if the referenced table
lacks ``organisation_id``, skip the cross-org check.

Revision ID: 0009_fix_enforce_same_organisation
Revises: 0008_rls_pipeline_folders
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_fix_enforce_same_organisation"
down_revision: str | None = "0008_rls_pipeline_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_same_organisation() RETURNS trigger AS $$
DECLARE
    referenced_id uuid;
    referenced_organisation_id uuid;
    child_organisation_id uuid;
    col_exists boolean;
BEGIN
    referenced_id := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid;
    IF referenced_id IS NULL THEN
        RETURN NEW;
    END IF;
    child_organisation_id := (to_jsonb(NEW) ->> 'organisation_id')::uuid;
    SELECT COUNT(*) > 0 INTO col_exists
    FROM information_schema.columns
    WHERE table_name = TG_ARGV[0] AND column_name = 'organisation_id';
    IF NOT col_exists THEN
        RETURN NEW;
    END IF;
    EXECUTE format('SELECT organisation_id FROM %I WHERE id = $1', TG_ARGV[0])
        INTO referenced_organisation_id USING referenced_id;
    IF referenced_organisation_id IS NULL OR referenced_organisation_id <> child_organisation_id
    THEN
        RAISE EXCEPTION 'cross-organisation reference from %.% to %',
            TG_TABLE_NAME, TG_ARGV[1], TG_ARGV[0]
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(sa.text(_FIXED_FUNCTION))


def downgrade() -> None:
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION enforce_same_organisation() RETURNS trigger AS $$
        DECLARE
            referenced_id uuid;
            referenced_organisation_id uuid;
            child_organisation_id uuid;
        BEGIN
            referenced_id := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid;
            IF referenced_id IS NULL THEN
                RETURN NEW;
            END IF;
            child_organisation_id := (to_jsonb(NEW) ->> 'organisation_id')::uuid;
            EXECUTE format('SELECT organisation_id FROM %I WHERE id = $1', TG_ARGV[0])
                INTO referenced_organisation_id USING referenced_id;
            IF referenced_organisation_id IS NULL OR referenced_organisation_id <> child_organisation_id
            THEN
                RAISE EXCEPTION 'cross-organisation reference from %.% to %',
                    TG_TABLE_NAME, TG_ARGV[1], TG_ARGV[0]
                    USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    )
