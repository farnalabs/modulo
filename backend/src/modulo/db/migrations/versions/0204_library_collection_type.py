"""Add library_collection primitive type and collection authoring fields (FAR-760).

Revision ID: 0204_library_collection_type
Revises: 0203_triggers_add_name
Create Date: 2026-09-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0204_library_collection_type"
down_revision = "0203_triggers_add_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the old CHECK constraint and re-create with library_collection.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_type') THEN "
        "ALTER TABLE library_primitives DROP CONSTRAINT ck_library_primitives_type; "
        "END IF; END $$;"
    )
    op.execute(
        "ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitives_type "
        "CHECK (primitive_type IN ("
        "'schema', 'workflow', 'agent', 'integration', "
        "'test_fixture', 'pipeline_template', 'composite', 'lifecycle_map', "
        "'library_collection'"
        "))"
    )

    # 2. Add collection authoring lifecycle columns (nullable for backward compat).
    op.execute("ALTER TABLE library_primitives ADD COLUMN IF NOT EXISTS status VARCHAR(20)")
    op.execute("ALTER TABLE library_primitives ADD COLUMN IF NOT EXISTS manifest_pins JSONB")
    op.execute("ALTER TABLE library_primitives ADD COLUMN IF NOT EXISTS trust_header JSONB")

    # 3. Enforce valid status values via a CHECK constraint.
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_status') THEN "
        "ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitives_status "
        "CHECK (status IS NULL OR status IN ('draft', 'published')); "
        "END IF; END $$;"
    )


def downgrade() -> None:
    # Drop the new CHECK constraint.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_status') THEN "
        "ALTER TABLE library_primitives DROP CONSTRAINT ck_library_primitives_status; "
        "END IF; END $$;"
    )

    # Drop the new columns.
    op.execute("ALTER TABLE library_primitives DROP COLUMN IF EXISTS trust_header")
    op.execute("ALTER TABLE library_primitives DROP COLUMN IF EXISTS manifest_pins")
    op.execute("ALTER TABLE library_primitives DROP COLUMN IF EXISTS status")

    # Restore the original CHECK constraint without library_collection.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_type') THEN "
        "ALTER TABLE library_primitives DROP CONSTRAINT ck_library_primitives_type; "
        "END IF; END $$;"
    )
    op.execute(
        "ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitives_type "
        "CHECK (primitive_type IN ("
        "'schema', 'workflow', 'agent', 'integration', "
        "'test_fixture', 'pipeline_template', 'composite', 'lifecycle_map'"
        "))"
    )
