"""Fix Remy context-source and skill uniqueness / constraint gaps.

Revision ID: 0127_remy_constraint_fixes
Revises: 0126_remy_lookup_indexes
Create Date: 2026-08-23

Two correctness gaps in the Remy model family:

1. ``remy_context_sources`` carried a single
   ``UNIQUE (organisation_id, user_id, source_key)`` constraint. Because the
   owner columns are nullable (exactly one of ``organisation_id`` /
   ``user_id`` is set, enforced by ``ck_remy_context_sources_owner``),
   Postgres treats NULLs as not-equal in unique constraints. Two org-scoped
   rows and two user-scoped rows with the same ``source_key`` were both
   permitted, so the "natural key" was never actually enforced.

   This drops the broken constraint and adds two partial unique indexes that
   enforce uniqueness per owner type:
     * ``uq_remy_context_sources_org_source_key``  UNIQUE (organisation_id, source_key) WHERE organisation_id IS NOT NULL
     * ``uq_remy_context_sources_user_source_key`` UNIQUE (user_id, source_key)         WHERE user_id IS NOT NULL
   The leftmost prefix of each also serves as the per-owner lookup index
   used by context_source_service.py.

2. ``remy_skills`` had no natural-key protection, allowing duplicate skill
   names per owner. This adds the same pattern of partial unique indexes on
   ``(organisation_id, name)`` / ``(user_id, name)``.

3. ``remy_skills.source_mode`` had no CHECK constraint while its sibling
   ``remy_context_sources.source_mode`` did. This adds a matching
   ``ck_remy_skills_source_mode`` so the two Remy models stay consistent.

Postgres-only concern: ``postgresql_where`` is ignored by the deprecated
SQLite / MariaDB backends, where the indexes are created without the
predicate.
"""

from alembic import op
from sqlalchemy import text

revision = "0127_remy_constraint_fixes"
down_revision = "0126_remy_lookup_indexes"
branch_labels = None
depends_on = None

_ORG_NOT_NULL = text("organisation_id IS NOT NULL")
_USER_NOT_NULL = text("user_id IS NOT NULL")
_SOURCE_MODE_VALUES = text("source_mode IS NULL OR source_mode IN ('always_on', 'tool', 'off')")


def upgrade() -> None:
    # 1. Fix the broken context-source natural key.
    op.drop_constraint("uq_remy_context_sources_key", "remy_context_sources", type_="unique")
    op.create_index(
        "uq_remy_context_sources_org_source_key",
        "remy_context_sources",
        ["organisation_id", "source_key"],
        unique=True,
        postgresql_where=_ORG_NOT_NULL,
    )
    op.create_index(
        "uq_remy_context_sources_user_source_key",
        "remy_context_sources",
        ["user_id", "source_key"],
        unique=True,
        postgresql_where=_USER_NOT_NULL,
    )

    # 2. Enforce unique skill names per owner.
    op.create_index(
        "uq_remy_skills_org_name",
        "remy_skills",
        ["organisation_id", "name"],
        unique=True,
        postgresql_where=_ORG_NOT_NULL,
    )
    op.create_index(
        "uq_remy_skills_user_name",
        "remy_skills",
        ["user_id", "name"],
        unique=True,
        postgresql_where=_USER_NOT_NULL,
    )

    # 3. Consistent source_mode CHECK on skills.
    op.create_check_constraint("ck_remy_skills_source_mode", "remy_skills", _SOURCE_MODE_VALUES)


def downgrade() -> None:
    op.drop_constraint("ck_remy_skills_source_mode", "remy_skills", type_="check")
    op.drop_index("uq_remy_skills_user_name", table_name="remy_skills")
    op.drop_index("uq_remy_skills_org_name", table_name="remy_skills")
    op.drop_index("uq_remy_context_sources_user_source_key", table_name="remy_context_sources")
    op.drop_index("uq_remy_context_sources_org_source_key", table_name="remy_context_sources")
    op.create_unique_constraint(
        "uq_remy_context_sources_key",
        "remy_context_sources",
        ["organisation_id", "user_id", "source_key"],
    )
