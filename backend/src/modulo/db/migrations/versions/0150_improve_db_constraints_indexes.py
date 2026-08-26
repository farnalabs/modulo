"""Add missing FK constraints, status lookup indexes, and a sort-order CHECK.

Revision ID: 0150_improve_db_constraints_indexes
Revises: 0149_suite_run_trigger_kind
Create Date: 2026-08-26

Schema-quality fixes from the improve-database pass over
``backend/src/modulo/db/models/``:

Foreign keys that were modelled as bare ``Uuid`` columns but never got a
DB-level FK (so deletes could orphan rows):

* ``pipeline_edges.source_node_id`` / ``target_node_id`` -> ``nodes(id)``
  ``ON DELETE CASCADE``
* ``organisations.plan_id`` -> ``tier_catalog(tier_id)`` ``ON DELETE SET NULL``
* ``runs.variant_group_id`` -> ``variant_groups(id)`` ``ON DELETE SET NULL``
* ``eval_definitions.node_id`` -> ``nodes(id)`` ``ON DELETE SET NULL``

Composite lookup indexes on the org-scoped ``status`` column, which is the
primary dashboard / triage filter (the table is already filtered by
``organisation_id`` under RLS):

* ``runs(organisation_id, status)``
* ``error_events(organisation_id, status)``
* ``error_groups(organisation_id, status)``

A CHECK constraint pinning ``saved_views.sort_order`` to ``('asc', 'desc')``.

All operations are additive and idempotent (``IF NOT EXISTS`` / ``IF EXISTS``),
so the migration is safe to re-run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0150_improve_db_constraints_indexes"
down_revision: str | None = "0149_suite_run_trigger_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Foreign keys -----------------------------------------------------
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_edges "
            "ADD CONSTRAINT fk_pipeline_edges_source_node "
            "FOREIGN KEY (source_node_id) REFERENCES nodes(id) ON DELETE CASCADE"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_edges "
            "ADD CONSTRAINT fk_pipeline_edges_target_node "
            "FOREIGN KEY (target_node_id) REFERENCES nodes(id) ON DELETE CASCADE"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE organisations "
            "ADD CONSTRAINT fk_organisations_plan "
            "FOREIGN KEY (plan_id) REFERENCES tier_catalog(tier_id) ON DELETE SET NULL"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE runs "
            "ADD CONSTRAINT fk_runs_variant_group "
            "FOREIGN KEY (variant_group_id) REFERENCES variant_groups(id) ON DELETE SET NULL"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE eval_definitions "
            "ADD CONSTRAINT fk_eval_definitions_node "
            "FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE SET NULL"
        )
    )

    # --- Status lookup indexes -------------------------------------------
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_runs_organisation_id_status ON runs (organisation_id, status)"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_error_events_organisation_id_status "
            "ON error_events (organisation_id, status)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_error_groups_organisation_id_status "
            "ON error_groups (organisation_id, status)"
        )
    )

    # --- CHECK constraint -------------------------------------------------
    op.execute(
        sa.text(
            "ALTER TABLE saved_views ADD CONSTRAINT ck_saved_views_sort_order CHECK (sort_order IN ('asc', 'desc'))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE saved_views DROP CONSTRAINT IF EXISTS ck_saved_views_sort_order"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_error_groups_organisation_id_status"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_error_events_organisation_id_status"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_runs_organisation_id_status"))
    op.execute(sa.text("ALTER TABLE eval_definitions DROP CONSTRAINT IF EXISTS fk_eval_definitions_node"))
    op.execute(sa.text("ALTER TABLE runs DROP CONSTRAINT IF EXISTS fk_runs_variant_group"))
    op.execute(sa.text("ALTER TABLE organisations DROP CONSTRAINT IF EXISTS fk_organisations_plan"))
    op.execute(sa.text("ALTER TABLE pipeline_edges DROP CONSTRAINT IF EXISTS fk_pipeline_edges_target_node"))
    op.execute(sa.text("ALTER TABLE pipeline_edges DROP CONSTRAINT IF EXISTS fk_pipeline_edges_source_node"))
