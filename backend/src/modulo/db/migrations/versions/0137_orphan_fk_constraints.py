"""Add missing FK constraints on FK-like columns.

Revision ID: 0137_orphan_fk_constraints
Revises: 0136_rename_remy_user_id_to_account_id
Create Date: 2026-08-23

Several columns reference primary keys of other tables (``nodes``,
``accounts``, ``runs``, ``variant_groups``) but were created as plain
``Uuid()`` columns without a DB-level FOREIGN KEY. This adds the missing
constraints so referential integrity is enforced.

Each constraint is added only when:
  * it does not already exist (idempotent re-run safe), and
  * there are no orphaned rows that would violate it.

If orphans exist the constraint is skipped with a ``RAISE NOTICE`` rather
than forced, so the migration never fails on pre-existing dirty data. The
orphan case must be cleaned up manually (delete or re-point the orphans)
before re-running the migration to add that specific constraint.

ON DELETE semantics:
  * ``pipeline_edges.source_node_id`` / ``target_node_id`` -> ``nodes.id``
    CASCADE (an edge is meaningless without its endpoint node; node
    deletion already cascades from its pipeline, but an edge referencing a
    non-existent node must be removed).
  * ``eval_results.node_id`` -> ``nodes.id`` CASCADE (a result tied to a
    node dies with the node).
  * ``eval_definitions.node_id`` -> ``nodes.id`` SET NULL (the definition
    can outlive a specific node binding).
  * ``eval_definitions.deleted_by`` -> ``accounts.id`` SET NULL (audit col).
  * ``lifecycle_maps.updated_by`` -> ``accounts.id`` SET NULL (audit col).
  * ``org_api_keys.run_id`` -> ``runs.id`` SET NULL (a key survives the run).
  * ``runs.variant_group_id`` -> ``variant_groups.id`` SET NULL (a run
    survives a soft-deleted variant group).
"""

from alembic import op

# ruff: noqa: S608 -- every value interpolated into the SQL below is sourced
# exclusively from the hardcoded ``_FK_SPECS`` tuple in this module (internal
# table/column identifiers), never from user input.
revision = "0137_orphan_fk_constraints"
down_revision = "0136_rename_remy_user_id_to_account_id"
branch_labels = None
depends_on = None

# (constraint_name, table, column, ref_table, ref_column, on_delete)
_FK_SPECS = [
    ("fk_pipeline_edges_source_node_id", "pipeline_edges", "source_node_id", "nodes", "id", "CASCADE"),
    ("fk_pipeline_edges_target_node_id", "pipeline_edges", "target_node_id", "nodes", "id", "CASCADE"),
    ("fk_eval_definitions_node_id", "eval_definitions", "node_id", "nodes", "id", "SET NULL"),
    ("fk_eval_results_node_id", "eval_results", "node_id", "nodes", "id", "CASCADE"),
    ("fk_eval_definitions_deleted_by", "eval_definitions", "deleted_by", "accounts", "id", "SET NULL"),
    ("fk_lifecycle_maps_updated_by", "lifecycle_maps", "updated_by", "accounts", "id", "SET NULL"),
    ("fk_org_api_keys_run_id", "org_api_keys", "run_id", "runs", "id", "SET NULL"),
    ("fk_runs_variant_group_id", "runs", "variant_group_id", "variant_groups", "id", "SET NULL"),
]


def upgrade() -> None:
    for name, table, column, ref_table, ref_col, on_delete in _FK_SPECS:
        # Values interpolated below come exclusively from the hardcoded
        # ``_FK_SPECS`` tuple in this file (internal identifiers), never from
        # user input — B608 is a false positive.
        sql = f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM {table} t
                        LEFT JOIN {ref_table} r ON t.{column} = r.{ref_col}
                        WHERE t.{column} IS NOT NULL AND r.{ref_col} IS NULL
                    ) THEN
                        ALTER TABLE {table}
                            ADD CONSTRAINT {name}
                            FOREIGN KEY ({column})
                            REFERENCES {ref_table}({ref_col})
                            ON DELETE {on_delete};
                    ELSE
                        RAISE NOTICE 'Skipping FK %: orphaned rows exist in %.%',
                            '{name}', '{table}', '{column}';
                    END IF;
                END IF;
            END $$;
            """  # nosec B608
        op.execute(sql)


def downgrade() -> None:
    for name, table, *_ in _FK_SPECS:
        sql = f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE {table} DROP CONSTRAINT {name};
                END IF;
            END $$;
            """  # nosec B608
        op.execute(sql)
