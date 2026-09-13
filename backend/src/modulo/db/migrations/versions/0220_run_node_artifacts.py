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


def _artifacts_column_exists(bind: sa.Connection) -> bool:
    """True when ``run_node_outputs.artifacts_json`` already exists.

    Needed so re-running this migration (e.g. idempotency tests that rewind
    ``alembic_version`` to the previous revision and re-apply the chain) is a
    no-op instead of raising ``DuplicateColumn``.
    """
    return bool(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'run_node_outputs' "
                "AND column_name = 'artifacts_json'"
            )
        ).scalar()
    )


def upgrade() -> None:
    """Add ``artifacts_json`` (JSONB on Postgres, JSON on SQLite).

    Existence-guarded so re-applies are no-ops (see ``_artifacts_column_exists``).
    """
    bind = op.get_bind()
    dialect = bind.dialect.name
    col_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    if _artifacts_column_exists(bind):
        _log.info("FAR-582: artifacts_json column already present on run_node_outputs; skipping add")
        return
    op.add_column(
        "run_node_outputs",
        sa.Column("artifacts_json", col_type, nullable=True),
    )
    _log.info(
        "FAR-582: added artifacts_json column to run_node_outputs (dialect=%s)",
        dialect,
    )


def downgrade() -> None:
    """Drop ``artifacts_json`` (idempotent)."""
    if _artifacts_column_exists(op.get_bind()):
        op.drop_column("run_node_outputs", "artifacts_json")
        _log.info("FAR-582: dropped artifacts_json column from run_node_outputs")
