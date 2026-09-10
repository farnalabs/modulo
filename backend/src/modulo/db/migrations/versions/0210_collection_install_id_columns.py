"""Add denormalised collection_install_id pointer to entity tables (FAR-765).

Revision ID: 0210_collection_install_id_columns
Revises: 0209_community_gate
Create Date: 2026-09-10

Adds a nullable, indexed ``collection_install_id`` column to ``schemas`` /
``agents`` / ``pipelines``. ``install.py`` / ``uninstall.py`` stamp and clear
this pointer directly on the entity rows so each materialised schema/agent/
pipeline knows which collection install produced it.

This is intentionally a *new* chained revision rather than an amend of
``0207_collection_install_tracking``: 0207 is already merged and applied on
main, so deployed databases at 0207+ would never re-run an amended ``upgrade()``
and the columns would stay missing (``UndefinedColumnError`` at runtime). Adding
a fresh child migration guarantees the columns are created on every database
that has not yet run this revision.

Additive only: nullable columns + supporting indexes on existing tables, no
table/relationship changes and no RLS ceremony (the underlying tables already
carry FORCE RLS + the appropriate DML grants). No FK — the install row is
provenance audit history and may outlive the entity per ON DELETE behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0210_collection_install_id_columns"
down_revision = "0209_community_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Denormalised provenance pointer on the entity tables. ``install.py`` stamps
    # ``collection_install_id`` on every schema/agent/pipeline it materialises
    # (and ``uninstall.py`` clears it), so the column must exist on the live
    # entity rows — not only in ``collection_install_entity``. Nullable + indexed
    # (mirrors the model declarations in schema.py / agent.py / pipeline.py).
    for _table in ("schemas", "agents", "pipelines"):
        op.add_column(
            _table,
            sa.Column("collection_install_id", sa.Uuid(), nullable=True),
        )
        op.create_index(
            f"ix_{_table}_collection_install_id",
            _table,
            ["collection_install_id"],
        )


def downgrade() -> None:
    for _table in ("pipelines", "agents", "schemas"):
        op.drop_index(f"ix_{_table}_collection_install_id", table_name=_table)
        op.drop_column(_table, "collection_install_id")
