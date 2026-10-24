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

- The table is created by an EARLIER migration (0192). On an existing install
  it is committed and a side connection can see it. On a fresh upgrade, 0192's
  DDL is still uncommitted inside Alembic's transaction, so a side connection
  cannot see it; that path must use the migration connection.
- ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS completed_at ...`` commits
  immediately on the AUTOCOMMIT side connection for existing installs. The
  fresh-chain fallback stays in Alembic's transaction so the new table is
  visible and the version row remains atomic.
- No backfill needed — the column is NULL for historical rows. The application
  writes it going forward.
- No ALTER COLUMN TYPE rewrite (the non-blocking pattern: add column, done).

Non-Postgres backends are a no-op (schema comes from ORM create_all).
"""

from __future__ import annotations

import logging
import os

import sqlalchemy as sa
from alembic import op

revision: str = "0332_per_node_completion"
down_revision: str | None = "0330_eval_verdict_contract"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_ADD_COMPLETED_AT = "ALTER TABLE run_node_outputs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE"
_DROP_COMPLETED_AT = "ALTER TABLE run_node_outputs DROP COLUMN IF EXISTS completed_at"

_SET_SIDE_LOCK_TIMEOUT = "SET lock_timeout = '15s'"
_REHEARSAL_ENV_VAR = "ALEMBIC_REHEARSAL"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _rehearsal_mode() -> bool:
    """Return whether the migration is running in the rollback rehearsal."""
    return os.environ.get(_REHEARSAL_ENV_VAR, "") == "1"


def _autocommit_connection() -> sa.Connection:
    """Dedicated AUTOCOMMIT connection for existing-install DDL."""
    bind = op.get_bind()
    conn = bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(sa.text(_SET_SIDE_LOCK_TIMEOUT))
    return conn


def _relation_visible(conn: sa.Connection) -> bool:
    """Return whether the side connection can see the table.

    A table created earlier in the same fresh Alembic transaction is invisible
    to this connection. The migration connection must handle that case.
    """
    return conn.execute(sa.text("SELECT to_regclass('public.run_node_outputs')")).scalar() is not None


def _migration_transaction_locks_table() -> bool:
    """Return whether this transaction already holds a lock on the table."""
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM pg_locks "
                "WHERE pid = pg_backend_pid() AND relation = to_regclass('public.run_node_outputs')"
            )
        )
        .scalar()
    )
    return bool(count)


def _execute_on_migration_connection() -> None:
    op.execute(sa.text(_ADD_COMPLETED_AT))


def _execute_on_side_or_migration_connection() -> None:
    if _rehearsal_mode():
        _execute_on_migration_connection()
        return

    with _autocommit_connection() as conn:
        if not _relation_visible(conn) or _migration_transaction_locks_table():
            _execute_on_migration_connection()
            return
        conn.execute(sa.text(_ADD_COMPLETED_AT))


def upgrade() -> None:
    if not _is_postgres():
        # Non-Postgres schema comes from ORM create_all
        return

    _execute_on_side_or_migration_connection()
    logger.info("0332_per_node_completion: added completed_at column to run_node_outputs")


def downgrade() -> None:
    if not _is_postgres():
        return

    if _rehearsal_mode():
        op.execute(sa.text(_DROP_COMPLETED_AT))
    else:
        with _autocommit_connection() as conn:
            if not _relation_visible(conn) or _migration_transaction_locks_table():
                op.execute(sa.text(_DROP_COMPLETED_AT))
            else:
                conn.execute(sa.text(_DROP_COMPLETED_AT))
    logger.info("0332_per_node_completion: dropped completed_at column from run_node_outputs")
