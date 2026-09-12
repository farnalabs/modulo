"""Add ``artifacts_json`` to ``run_node_outputs`` for FAR-582 side-car artifact pointers.

Revision ID: 0220_run_node_artifacts
Revises: 0219_eval_cluster_check_constraints
Create Date: 2026-09-12

FAR-582: full sandbox stdout/stderr side-car files.  Each node attempt's
``_artifact_pointers`` list (a ``list[pointer]`` of zstd-compressed
on-disk file references) is persisted into a new JSONB column
``artifacts_json`` on the ``run_node_outputs`` table.  The column is
JSON on the ORM model (repo parity convention: JSONB in migrations,
generic JSON in the model for SQLite/MariaDB portability).
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

_log = logging.getLogger(__name__)

revision = "0220_run_node_artifacts"
down_revision = "0219_eval_cluster_check_constraints"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``artifacts_json`` (JSONB on Postgres, JSON on SQLite)."""
    dialect = op.get_bind().dialect.name
    col_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    op.add_column(
        "run_node_outputs",
        sa.Column("artifacts_json", col_type, nullable=True),
    )
    _log.info(
        "FAR-582: added artifacts_json column to run_node_outputs (dialect=%s)",
        dialect,
    )


def downgrade() -> None:
    """Drop ``artifacts_json``."""
    op.drop_column("run_node_outputs", "artifacts_json")
    _log.info("FAR-582: dropped artifacts_json column from run_node_outputs")
