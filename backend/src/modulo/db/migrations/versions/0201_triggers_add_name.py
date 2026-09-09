"""Trigger name — declarative-apply identity column + uniqueness (FAR-681 slice 2).

Revision ID: 0201_triggers_add_name
Revises: 0200_runs_runner_marker_sweep_index
Create Date: 2026-09-09

``modulo apply`` (FAR-681) identifies triggers declaratively by the pair
``(pipeline, name)``; the ``name`` column is the declarative identity handle.
The column is nullable: existing (UI/MCP-created) rows carry NULL and remain
invisible to name-based apply (they stay managed through their existing
surfaces). Postgres creates it as ``varchar(255)`` via the portable
``op.add_column`` template (0190 style — plain ``ADD COLUMN``, no table-level
constraint, round-trips on SQLite and Postgres alike); the plain string type
is identical on every backend so no dialect branch is needed.

The same migration adds the identity's uniqueness as a PARTIAL unique index
``(organisation_id, pipeline_id, name) WHERE deleted_at IS NULL AND name IS
NOT NULL`` — portable via ``op.create_index``'s ``postgresql_where`` /
``sqlite_where`` (no raw DDL). Predicate notes:

* ``name IS NOT NULL`` — legacy unnamed rows never participate in (or get
  colliding NULL "keys" under) the identity;
* ``deleted_at IS NULL`` — soft-deleted rows release their name (the 0127
  soft-delete partial-unique pattern), so "delete then re-create the same
  name" keeps working.

Downgrade drops exactly what the upgrade added (index first, then the
column — additive, nullable, never backfilled — safe).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0201_triggers_add_name"
down_revision: str | None = "0200_runs_runner_marker_sweep_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TRIGGER_IDENTITY_INDEX = "uq_triggers_org_pipeline_name"


def upgrade() -> None:
    op.add_column("triggers", sa.Column("name", sa.String(255), nullable=True))
    op.create_index(
        _TRIGGER_IDENTITY_INDEX,
        "triggers",
        ["organisation_id", "pipeline_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND name IS NOT NULL"),
        sqlite_where=sa.text("deleted_at IS NULL AND name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_TRIGGER_IDENTITY_INDEX, table_name="triggers")
    op.drop_column("triggers", "name")
