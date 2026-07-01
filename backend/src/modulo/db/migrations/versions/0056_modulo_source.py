"""Add 'modulo' source to library_primitives CK constraint

Revision ID: 0056_modulo_source
Revises: 0055_error_forwarder_configs
Create Date: 2026-07-01
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0056_modulo_source"
down_revision: str | Sequence[str] | None = "0055_error_forwarder_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source")
    op.execute(
        "ALTER TABLE library_primitives"
        " ADD CONSTRAINT ck_library_primitives_source"
        " CHECK (source IN ('local', 'registry', 'modulo'))"
    )
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source_fields")
    op.execute("""ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitives_source_fields CHECK (
        (source = 'local' AND source_url IS NULL AND checksum IS NULL
         AND ed25519_signature IS NULL AND verified IS NULL
         AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL)
        OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL
         AND ed25519_signature IS NULL AND verified IS NULL
         AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL)
        OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org'
         AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL
         AND ed25519_signature IS NOT NULL AND verified IS NOT NULL
         AND download_count IS NOT NULL AND average_rating IS NOT NULL
         AND review_count IS NOT NULL)
    )""")


def downgrade() -> None:
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source_fields")
    op.execute("UPDATE library_primitives SET source = 'local' WHERE source = 'modulo'")
    op.execute("""ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitives_source_fields CHECK (
        (source = 'local' AND source_url IS NULL AND checksum IS NULL
         AND ed25519_signature IS NULL AND verified IS NULL
         AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL)
        OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org'
         AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL
         AND ed25519_signature IS NOT NULL AND verified IS NOT NULL
         AND download_count IS NOT NULL AND average_rating IS NOT NULL
         AND review_count IS NOT NULL)
    )""")
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source")
    op.execute(
        "ALTER TABLE library_primitives"
        " ADD CONSTRAINT ck_library_primitives_source"
        " CHECK (source IN ('local', 'registry'))"
    )
