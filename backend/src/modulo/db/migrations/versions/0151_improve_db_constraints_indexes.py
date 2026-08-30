"""Add missing FK constraints, status lookup indexes, and a sort-order CHECK.

Revision ID: 0151_improve_db_constraints_indexes
Revises: 0150_add_router_no_match_status
Create Date: 2026-08-26

Schema-quality fixes from the improve-database pass over
``backend/src/modulo/db/models/``.

Foreign keys added
------------------
Two of the original draft's FKs targeted the deprecated ``nodes`` table
(``pipeline_edges.source_node_id`` / ``target_node_id`` and
``eval_definitions.node_id``). Pipeline/eval node ids are pipeline-graph UUIDs
stored in ``pipelines.graph_nodes_json`` (and validated against that JSON, never
inserted into ``nodes``), so those FKs would abort on any DB that already holds
edges/eval defs and would make every fresh-DB graph save fail with an FK
violation. They are **removed** here.

The two remaining FKs are valid and reference real parent tables:

* ``runs.variant_group_id`` -> ``variant_groups(id)`` ``ON DELETE SET NULL``
* ``organisations.plan_id`` -> ``tier_catalog(tier_id)`` ``ON DELETE SET NULL``

Composite lookup indexes on the org-scoped ``status`` column (the primary
dashboard / triage filter, already RLS-filtered by ``organisation_id``):

* ``runs(organisation_id, status)``
* ``error_events(organisation_id, status)``
* ``error_groups(organisation_id, status)``

A CHECK constraint pinning ``saved_views.sort_order`` to ``('asc', 'desc')``.

Idempotency / safety
--------------------
The three indexes use ``CREATE INDEX IF NOT EXISTS`` (standard, idempotent). The
two FK adds and the CHECK add run only on Postgres and are reconciled the same
way as ``0120_org_fk_hardening``:

* each is skipped if the constraint already exists (``pg_constraint`` guard), so
  re-running ``upgrade()`` after a prior ``upgrade()`` is a no-op;
* each is skipped if adding it would orphan existing rows (a ``NOT EXISTS``
  orphan scan returns > 0), so a live dataset with stale references never makes
  the migration ERROR and blocks the deploy — those rows are left for triage.

This replaces the original draft, whose raw ``ADD CONSTRAINT`` statements had no
``IF NOT EXISTS`` guard and re-validated every row, aborting on any orphan.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0151_improve_db_constraints_indexes"
down_revision: str | None = "0150_add_router_no_match_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Status lookup indexes (standard SQL, idempotent) -----------------
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

    # --- Constraint adds (Postgres-only, reconciled & idempotent) ---------
    # The two node-id FKs from the original draft were removed (see module
    # docstring); the remaining FKs + CHECK are guarded so the migration never
    # fails on existing data and is safe to re-run. Mirrors 0120_org_fk_hardening.
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                DO $$
                DECLARE
                    con_exists BOOLEAN;
                    orphan_count BIGINT;
                BEGIN
                    -- runs.variant_group_id -> variant_groups(id) ON DELETE SET NULL
                    SELECT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'fk_runs_variant_group'
                    ) INTO con_exists;
                    IF NOT con_exists THEN
                        SELECT count(*) INTO orphan_count
                        FROM runs
                        WHERE variant_group_id IS NOT NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM variant_groups v WHERE v.id = runs.variant_group_id
                          );
                        IF orphan_count = 0 THEN
                            EXECUTE
                                'ALTER TABLE runs ADD CONSTRAINT fk_runs_variant_group '
                                'FOREIGN KEY (variant_group_id) '
                                'REFERENCES variant_groups(id) ON DELETE SET NULL';
                        END IF;
                    END IF;

                    -- organisations.plan_id -> tier_catalog(tier_id) ON DELETE SET NULL
                    SELECT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'fk_organisations_plan'
                    ) INTO con_exists;
                    IF NOT con_exists THEN
                        SELECT count(*) INTO orphan_count
                        FROM organisations
                        WHERE plan_id IS NOT NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM tier_catalog t WHERE t.tier_id = organisations.plan_id
                          );
                        IF orphan_count = 0 THEN
                            EXECUTE
                                'ALTER TABLE organisations ADD CONSTRAINT fk_organisations_plan '
                                'FOREIGN KEY (plan_id) '
                                'REFERENCES tier_catalog(tier_id) ON DELETE SET NULL';
                        END IF;
                    END IF;

                    -- ck_saved_views_sort_order CHECK (skip if rows would violate)
                    SELECT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'ck_saved_views_sort_order'
                    ) INTO con_exists;
                    IF NOT con_exists THEN
                        SELECT count(*) INTO orphan_count
                        FROM saved_views
                        WHERE sort_order NOT IN ('asc', 'desc');
                        IF orphan_count = 0 THEN
                            EXECUTE
                                'ALTER TABLE saved_views ADD CONSTRAINT ck_saved_views_sort_order '
                                'CHECK (sort_order IN (''asc'', ''desc''))';
                        END IF;
                    END IF;
                END $$;
                """
            )
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE saved_views DROP CONSTRAINT IF EXISTS ck_saved_views_sort_order"))
        op.execute(sa.text("ALTER TABLE organisations DROP CONSTRAINT IF EXISTS fk_organisations_plan"))
        op.execute(sa.text("ALTER TABLE runs DROP CONSTRAINT IF EXISTS fk_runs_variant_group"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_error_groups_organisation_id_status"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_error_events_organisation_id_status"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_runs_organisation_id_status"))
