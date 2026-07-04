"""Add 'community' source to library_primitives CK constraints.

Introduces the "community database" concept from ADR 010 §2: opinionated,
narrower example pipelines contributed by users, kept separate from the
Modulo-maintained Native library (source='modulo'). Reuses the existing
`source` column with a new allowed value `'community'` rather than a new
table.

The actual community-database seed content is NOT inserted here — it
follows the same in-memory built-in primitive mechanism already used for
the Native library (see `_MODULO_PRIMITIVES` / `_COMMUNITY_PRIMITIVES` in
`modulo.core.library_service`), because those primitives must be visible
to every organisation and the org-scoped DB query path
(`list_library_primitives`) filters by the caller's own organisation_id
before RLS is even consulted. This migration only widens the CHECK
constraints so that a `source='community'` DB row (e.g. a future
user-published community contribution) is not rejected.

Revision ID: 0063_library_community_source
Revises: 0062_add_integration_tier
Create Date: 2026-07-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0063_library_community_source"
down_revision: str | Sequence[str] | None = "0062_add_integration_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source")
    op.execute(
        "ALTER TABLE library_primitives"
        " ADD CONSTRAINT ck_library_primitives_source"
        " CHECK (source IN ('local', 'registry', 'modulo', 'community'))"
    )

    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source_fields")
    op.execute("""ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitives_source_fields CHECK (
        (source = 'local' AND source_url IS NULL AND checksum IS NULL
         AND ed25519_signature IS NULL AND verified IS NULL
         AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL)
        OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL
         AND ed25519_signature IS NULL AND verified IS NULL
         AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL)
        OR (source = 'community' AND source_url IS NULL AND checksum IS NULL
         AND ed25519_signature IS NULL
         AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL)
        OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org'
         AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL
         AND ed25519_signature IS NOT NULL AND verified IS NOT NULL
         AND download_count IS NOT NULL AND average_rating IS NOT NULL
         AND review_count IS NOT NULL)
    )""")


def downgrade() -> None:
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source_fields")
    op.execute("UPDATE library_primitives SET source = 'local' WHERE source = 'community'")
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
    op.execute("ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitives_source")
    op.execute(
        "ALTER TABLE library_primitives"
        " ADD CONSTRAINT ck_library_primitives_source"
        " CHECK (source IN ('local', 'registry', 'modulo'))"
    )
