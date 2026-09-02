"""Add residual missing foreign-key constraints on internal reference columns.

Revision ID: 0170_add_residual_foreign_keys
Revises: 0169_connector_instance_degraded
Create Date: 2026-09-01

These columns are clearly foreign keys (named ``*_id`` and referencing another
table's primary key) but were never given a DB-level foreign key. Without the
constraint, orphaned rows can be written and referential integrity is not
enforced. All five columns are already ``Uuid`` typed in the models, so only
the constraint is added (no type change). ON DELETE semantics follow the
existing convention in the schema (see 0164_add_missing_foreign_keys):
``SET NULL`` for nullable refs, ``RESTRICT`` for the mandatory node references.

Deploy-safety: every FK is added ``NOT VALID`` then ``VALIDATE``-d, mirroring
0164_add_missing_foreign_keys and 0151_fix_constraints.
``ADD CONSTRAINT ... FOREIGN KEY NOT VALID`` takes only a brief
``AccessExclusive`` lock and does NOT scan existing rows, so a populated table
never aborts the upgrade on pre-existing orphans; the ``VALIDATE`` is online.

0170-round-2 correction (deploy-integration rot, 2026-09-02): the three
``*_node_id`` FKs this migration originally added referenced the ``nodes``
table — the DEPRECATED composite-template table (see ``db/models/node.py``).
Pipeline graph nodes live ONLY in ``pipelines.graph_nodes_json`` /
``pipeline_snapshots.graph_nodes_json``; the node IDs carried by
``run_evidence``, ``node_observations`` and ``snapshot_schema_pins`` are
JSON-embedded graph node IDs and never materialise into ``nodes``, so those
constraints were unsatisfiable by design (any run-evidence / observation /
schema-pin write would violate the FK). They are dropped from this migration
entirely — same rationale and same edit-in-place safety argument as the 0164
correction. The two FKs that reference real, populated tables remain.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0170_add_residual_foreign_keys"
down_revision: str | None = "0169_connector_instance_degraded"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# (constraint, table, columns, ref_table, ref_columns, ondelete)
_FKS: tuple[tuple[str, str, list[str], str, list[str], str], ...] = (
    ("journeys_map_id_fkey", "journeys", ["map_id"], "lifecycle_maps", ["id"], "SET NULL"),
    ("lifecycle_maps_updated_by_fkey", "lifecycle_maps", ["updated_by"], "accounts", ["id"], "SET NULL"),
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
