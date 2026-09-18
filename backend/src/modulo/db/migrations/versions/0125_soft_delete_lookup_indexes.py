"""Add (organisation_id, deleted_at) lookup indexes on soft-delete tables.

Revision ID: 0125_soft_delete_lookup_indexes
Revises: 0124_fix_team_rls_policies
Create Date: 2026-08-23

Every read against a soft-deleted (SoftDeleteMixin) table filters on
``deleted_at IS NULL`` under RLS (``rls_org_isolation`` already gates on
``organisation_id``). The base ``OrgScoped`` mixin only indexes
``organisation_id`` on its own, so the common active-row scan
(``WHERE organisation_id = $1 AND deleted_at IS NULL``) falls back to a
per-organisation index scan + heap filter on ``deleted_at``.

This adds a partial composite index ``(organisation_id, deleted_at)
WHERE deleted_at IS NULL`` on each OrgScoped soft-delete table that was
missing one, and a partial index on ``organisations.deleted_at`` (the org
table is soft-deleted manually via ``status='deleted'`` + ``deleted_at`` and
has no org column of its own). Partial indexes keep the index small since
only live rows are covered.

Index-only scans now satisfy the active-row filter without touching the heap
for these config / entity tables.

Postgres-only concern: ``postgresql_where`` is ignored by the deprecated
SQLite / MariaDB backends, where the index is created without the predicate.
"""

from alembic import op
from sqlalchemy import text

revision = "0125_soft_delete_lookup_indexes"
down_revision = "0124_fix_team_rls_policies"
branch_labels = None
depends_on = None

# OrgScoped + SoftDeleteMixin tables lacking a (organisation_id, deleted_at)
# index. ``organisation_id`` is already indexed by the base mixin; the
# composite partial index additionally covers the ``deleted_at IS NULL``
# predicate used by every active-row query.
_ORG_SOFT_DELETE_TABLES = (
    "composite_templates",
    "environment_profiles",
    "error_forwarder_configs",
    "eval_definitions",
    "pipelines",
    "lifecycle_maps",
    "saved_views",
    "node_categories",
    "parameter_sets",
    "library_primitives",
    "variant_groups",
)

_DELETED_AT_WHERE = text("deleted_at IS NULL")


def _index_name(table: str) -> str:
    return f"ix_{table}_organisation_id_deleted_at"


def upgrade() -> None:
    for table in _ORG_SOFT_DELETE_TABLES:
        op.create_index(
            _index_name(table),
            table,
            ["organisation_id", "deleted_at"],
            postgresql_where=_DELETED_AT_WHERE,
        )
    # The org table is soft-deleted manually and has no organisation_id column.
    op.create_index(
        "ix_organisations_deleted_at",
        "organisations",
        ["deleted_at"],
        postgresql_where=_DELETED_AT_WHERE,
    )


def downgrade() -> None:
    op.drop_index("ix_organisations_deleted_at", table_name="organisations")
    for table in reversed(_ORG_SOFT_DELETE_TABLES):
        op.drop_index(_index_name(table), table_name=table)
