"""Add journey/work-item tracking columns to runs (FAR-142)

Revision ID: 0083_journey_work_item_columns
Revises: 0082_lifecycle_map_stages
Create Date: 2026-08-12

Additive, nullable columns that anchor a run to a work-item journey:

* ``work_item_id`` — uuid chain anchor, written ONCE at create (floor id or
  adopted from the parent run), never mutated afterwards.
* ``work_item_refs`` — jsonb array of ``{kind, ref, source, status?}`` entries
  (source in {derived, reported}; status optional in {done, attempted}).
  JSONB (not JSON) so the partial GIN index works; the ORM model maps it as
  generic JSON for SQLite/MariaDB parity (the hitl_claims.decision_payload
  precedent).
* ``is_replay`` — boolean set by replay_event.
* ``variant_group_id`` — uuid set by run_variant_weighted.

No backfill and no NOT NULL in v1: the columns are minted from create-time
stamping going forward; pre-existing rows stay NULL.

Indexes:
* partial GIN ``ix_runs_work_item_refs_gin`` on ``work_item_refs``
  ``WHERE jsonb_array_length(work_item_refs) > 0`` — ``jsonb_array_length``,
  NOT ``array_length`` (``array_length(jsonb)`` is invalid SQL).
* btree ``ix_runs_org_work_item_id`` on (organisation_id, work_item_id).
  ``parent_run_id`` is already indexed (the runs.parent_run_id FK index).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0083_journey_work_item_columns"
down_revision: str | None = "0082_lifecycle_map_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("work_item_id", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("work_item_refs", JSONB(), nullable=True))
    op.add_column("runs", sa.Column("is_replay", sa.Boolean(), nullable=True))
    op.add_column("runs", sa.Column("variant_group_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_runs_work_item_refs_gin",
        "runs",
        ["work_item_refs"],
        postgresql_using="gin",
        postgresql_where=sa.text("jsonb_array_length(work_item_refs) > 0"),
    )
    op.create_index("ix_runs_org_work_item_id", "runs", ["organisation_id", "work_item_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_org_work_item_id", table_name="runs")
    op.drop_index("ix_runs_work_item_refs_gin", table_name="runs")
    op.drop_column("runs", "variant_group_id")
    op.drop_column("runs", "is_replay")
    op.drop_column("runs", "work_item_refs")
    op.drop_column("runs", "work_item_id")
