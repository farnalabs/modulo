"""Trigger name — declarative-apply identity column (FAR-681 slice 2).

Revision ID: 0200_triggers_add_name
Revises: 0199_runs_json_to_jsonb
Create Date: 2026-09-09

``modulo apply`` (FAR-681) identifies triggers declaratively by the pair
``(pipeline, name)``; the ``name`` column is the declarative identity handle.
The column is nullable: existing (UI/MCP-created) rows carry NULL and remain
invisible to name-based apply (they stay managed through their existing
surfaces). Postgres creates it as ``varchar(255)`` via the portable
``op.add_column`` template (0190 style — plain ``ADD COLUMN``, no table-level
constraint, round-trips on SQLite and Postgres alike); the plain string type
is identical on every backend so no dialect branch is needed.

Downgrade drops exactly the column the upgrade added (additive, nullable,
never backfilled — safe).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0200_triggers_add_name"
down_revision: str | None = "0199_runs_json_to_jsonb"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("triggers", sa.Column("name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("triggers", "name")
