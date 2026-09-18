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
online.

0164-round-2 correction (deploy-integration rot, 2026-09-02): the four
``*_node_id`` FKs this migration originally added referenced the ``nodes``
table — which is the DEPRECATED composite-template table (see
``db/models/node.py``). Pipeline graph nodes live ONLY in
``pipelines.graph_nodes_json`` / ``pipeline_snapshots.graph_nodes_json``; their
IDs are minted client-side and never materialised into ``nodes``. The four
constraints were therefore unsatisfiable by design: ``VALIDATE`` fails on any
populated DB, and every first-class pipeline-edge / eval-definition /
eval-result write violates the FK (caught by the integration suite —
``tests/integration/crud/test_pipeline.py``,
``tests/integration/test_guardrail_config_api.py``). They are dropped from this
migration entirely (edit-in-place is safe: no environment applied 0164 — the
deploy gate blocked every deploy after this merged, and CI migrates fresh
DBs). The two FKs that reference real, populated tables remain.
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
