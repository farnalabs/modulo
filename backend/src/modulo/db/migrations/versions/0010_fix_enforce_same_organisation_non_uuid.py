"""Fix enforce_same_organisation to skip non-UUID child columns.

The trigger on agents.input_schema_version and agents.output_schema_version
called enforce_same_organisation() which blindly casts the column value to
UUID — but these columns are VARCHAR(50). When the value is "latest" (the
default for unversioned schema references), the cast raises:

    invalid input syntax for type uuid: "latest"

This caused every POST /api/v1/agents to return 503.

Fix: check the child column's data_type from information_schema.columns;
if it isn't "uuid", skip the cross-org check (the composite FK constraint
already enforces organisation_id consistency across the three columns).

Revision ID: 0010_fix_enforce_same_organisation_non_uuid
Revises: 0009_fix_enforce_same_organisation
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_fix_enforce_same_organisation_non_uuid"
down_revision: str | None = "0009_fix_enforce_same_organisation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_same_organisation() RETURNS trigger AS $$
DECLARE
    referenced_id uuid;
    referenced_organisation_id uuid;
    child_organisation_id uuid;
    col_exists boolean;
    col_type text;
BEGIN
    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_name = TG_TABLE_NAME AND column_name = TG_ARGV[1];
    IF col_type IS DISTINCT FROM 'uuid' THEN
        RETURN NEW;
    END IF;
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
        """)
    )
