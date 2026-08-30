"""Add partial unique indexes for soft-delete natural keys (improve-database).

Revision ID: 0156_add_soft_delete_partial_uniques
Revises: 0155_add_hot_query_indexes
Create Date: 2026-08-30

``SoftDeleteMixin`` tables that carry a human-facing ``name`` still allow two
*active* (non-deleted) rows to share ``(organisation_id, name)``, and a
deleted row permanently blocks reuse of that name. Migration 0127 established
the partial-unique pattern for node_categories, parameter_schemas,
parameter_sets, error_forwarder_configs and library_primitives; these tables
were missed.

A partial UNIQUE index ``(organisation_id, name) WHERE deleted_at IS NULL``
permits a soft-deleted name to be reused while still enforcing uniqueness
among live rows, matching the existing convention.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0156_add_soft_delete_partial_uniques"
down_revision: str | None = "0155_add_hot_query_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_PARTIAL_UNIQUES = [
    ("uq_pipelines_org_name", "pipelines"),
    ("uq_variant_groups_org_name", "variant_groups"),
    ("uq_saved_views_org_name", "saved_views"),
    ("uq_environment_profiles_org_name", "environment_profiles"),
    ("uq_composite_templates_org_name", "composite_templates"),
    ("uq_lifecycle_maps_org_name", "lifecycle_maps"),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, table in _PARTIAL_UNIQUES:
        bind.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                f'ON public."{table}" (organisation_id, name) '
                f"WHERE deleted_at IS NULL;"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for name, _table in _PARTIAL_UNIQUES:
        bind.execute(text(f"DROP INDEX IF EXISTS {name};"))
