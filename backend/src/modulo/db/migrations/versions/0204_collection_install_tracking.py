"""Add collection install/uninstall tracking tables and provenance columns (FAR-761).

Revision ID: 0204_collection_install_tracking
Revises: 0203_library_collection_type
Create Date: 2026-09-10
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0204_collection_install_tracking"
down_revision = "0203_library_collection_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create collection_install table.
    op.execute(
        "CREATE TABLE collection_install ("
        "    install_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
        "    collection_id UUID NOT NULL REFERENCES library_primitives(id) ON DELETE RESTRICT,"
        "    collection_version VARCHAR(20),"
        "    org_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,"
        "    status VARCHAR(20) NOT NULL CHECK (status IN ('installed', 'failed')),"
        "    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        "    resolved_manifest JSONB,"
        "    connector_checklist JSONB,"
        "    installed_entities JSONB"
        ")"
    )
    op.execute("CREATE INDEX idx_collection_install_org ON collection_install(org_id)")
    op.execute("CREATE INDEX idx_collection_install_collection ON collection_install(collection_id)")
    op.execute("CREATE UNIQUE INDEX idx_collection_install_id ON collection_install(install_id)")

    # 2. Create collection_install_entity table.
    op.execute(
        "CREATE TABLE collection_install_entity ("
        "    install_id UUID NOT NULL REFERENCES collection_install(install_id) ON DELETE CASCADE,"
        "    entity_type VARCHAR(50) NOT NULL,"
        "    entity_id UUID NOT NULL,"
        "    PRIMARY KEY (install_id, entity_type, entity_id)"
        ")"
    )

    # 3. Add nullable provenance columns to entity tables.
    op.execute("ALTER TABLE schemas ADD COLUMN IF NOT EXISTS collection_install_id UUID")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS collection_install_id UUID")
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS collection_install_id UUID")


def downgrade() -> None:
    # 1. Drop provenance columns from entity tables.
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS collection_install_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS collection_install_id")
    op.execute("ALTER TABLE schemas DROP COLUMN IF EXISTS collection_install_id")

    # 2. Drop collection_install_entity table.
    op.execute("DROP TABLE IF EXISTS collection_install_entity")

    # 3. Drop collection_install table.
    op.execute("DROP TABLE IF EXISTS collection_install")
