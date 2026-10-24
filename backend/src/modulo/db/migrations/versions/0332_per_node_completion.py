"""Per-node completion record: add completed_at to run_node_outputs (FAR-2050).

Revision ID: 0332_per_node_completion
Revises: 0330_eval_verdict_contract
Create Date: 2026-10-24

Adds a durable per-node-completion record so the node-deadline watchdog can
re-dispatch ONLY when this attempt has at least one completed node. Without a
completion record the kill terminal-fails with node_deadline_exceeded.

Column contract:
- ``completed_at`` (timestamptz, nullable) on ``run_node_outputs`` — written
  once when a node actually finishes work (the FINAL_ATTEMPT_KEY row for that
  node). NULL means the node has not yet completed in this attempt.

Locking / online safety (FAR-403 class, 0330 pattern):

- The table is created by an EARLIER migration (0192). Its DDL is committed
  when this migration runs, so a side connection CAN see it.
- ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS completed_at ...`` commits
  immediately on the AUTOCOMMIT side connection (each ALTER is its own
  transaction, so the ACCESS EXCLUSIVE is released before the VALIDATE scan).
- No backfill needed — the column is NULL for historical rows. The application
  writes it going forward.
- No ALTER COLUMN TYPE rewrite (the non-blocking pattern: add column, done).

Non-Postgres backends are a no-op (schema comes from ORM create_all).
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision: str = "0332_per_node_completion"
down_revision: str | None = "0330_eval_verdict_contract"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_ADD_COMPLETED_AT = "ALTER TABLE run_node_outputs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE"

_SET_SIDE_LOCK_TIMEOUT = "SET lock_timeout = '15s'"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _autocommit_connection():
    """Dedicated AUTOCOMMIT connection for run_node_outputs DDL.

    Each ALTER commits immediately so its ACCESS EXCLUSIVE lock is not held
    across a following VALIDATE scan (which runs under SHARE UPDATE EXCLUSIVE).
    """
    bind = op.get_bind()
    conn = bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(sa.text(_SET_SIDE_LOCK_TIMEOUT))
    return conn


def upgrade() -> None:
    if not _is_postgres():
        # Non-Postgres schema comes from ORM create_all
        return

    with _autocommit_connection() as conn:
        conn.execute(sa.text(_ADD_COMPLETED_AT))
        logger.info("0331_per_node_completion: added completed_at column to run_node_outputs")


def downgrade() -> None:
    if not _is_postgres():
        return

    with _autocommit_connection() as conn:
        conn.execute(sa.text("ALTER TABLE run_node_outputs DROP COLUMN IF EXISTS completed_at"))
        logger.info("0331_per_node_completion: dropped completed_at column from run_node_outputs")
