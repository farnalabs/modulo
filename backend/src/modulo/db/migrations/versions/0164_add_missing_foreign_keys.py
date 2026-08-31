"""Add missing foreign-key constraints on internal reference columns.

Revision ID: 0164_add_missing_foreign_keys
Revises: 0163_rls_strict_oauth_auth_codes_token_families
Create Date: 2026-08-29

These columns are clearly foreign keys (named ``*_id`` and referencing another
table's primary key) but were never given a DB-level foreign key. Without the
constraint, orphaned rows can be written and referential integrity is not
enforced. All six columns are already ``Uuid`` typed in the models, so only the
constraint is added (no type change). ON DELETE semantics follow the existing
convention in the schema: ``SET NULL`` for nullable refs, ``RESTRICT`` for the
mandatory ``pipeline_edges`` endpoints.

Deploy-safety: every FK is added ``NOT VALID`` then ``VALIDATE``-d, mirroring
``0151_fix_constraints``. ``ADD CONSTRAINT ... FOREIGN KEY NOT VALID`` takes only
a brief ``AccessExclusive`` lock and does NOT scan existing rows, so a populated
table never aborts the upgrade on pre-existing orphans; the ``VALIDATE`` is
online. ``pipeline_edges`` uses ``RESTRICT``, and a historical orphan there would
otherwise hard-fail a naive ``ADD CONSTRAINT``.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0164_add_missing_foreign_keys"
down_revision: str | None = "0163_rls_strict_oauth_auth_codes_token_families"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# (constraint, table, columns, ref_table, ref_columns, ondelete)
_FKS: tuple[tuple[str, str, list[str], str, list[str], str], ...] = (
    ("org_api_keys_run_id_fkey", "org_api_keys", ["run_id"], "runs", ["id"], "SET NULL"),
    ("eval_definitions_node_id_fkey", "eval_definitions", ["node_id"], "nodes", ["id"], "SET NULL"),
    ("eval_results_node_id_fkey", "eval_results", ["node_id"], "nodes", ["id"], "SET NULL"),
    ("pipeline_edges_source_node_id_fkey", "pipeline_edges", ["source_node_id"], "nodes", ["id"], "RESTRICT"),
    ("pipeline_edges_target_node_id_fkey", "pipeline_edges", ["target_node_id"], "nodes", ["id"], "RESTRICT"),
    ("runs_variant_group_id_fkey", "runs", ["variant_group_id"], "variant_groups", ["id"], "SET NULL"),
)


def upgrade() -> None:
    for name, table, cols, ref_table, ref_cols, ondelete in _FKS:
        col_list = ", ".join(cols)
        ref_list = ", ".join(ref_cols)
        op.execute(
            text(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"FOREIGN KEY ({col_list}) REFERENCES {ref_table} ({ref_list}) "
                f"ON DELETE {ondelete} NOT VALID"
            )
        )
        op.execute(text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))


def downgrade() -> None:
    for name, table, _cols, _ref_table, _ref_cols, _ondelete in reversed(_FKS):
        op.drop_constraint(name, table, type_="foreignkey")
