"""FAR-147: Legacy run-group classification backfill + legacy agent verdict exposure.

Revision ID: 0348_far147_legacy_run_group_backfill
Revises: 0347_system_maps_version_aware
Create Date: 2026-11-08

Part 1: Backfill run_group_id and run_group_kind for existing runs.

For each child run with parent_run_id set:
- Walk parent_run_id chain to find the root run (where parent_run_id IS NULL)
- Set run_group_id to the root run's ID
- Set run_group_kind to 'resume' if child.langgraph_thread_id == parent.langgraph_thread_id, else 'retry'

This is idempotent and safe to run on existing data.

Part 2: No schema change needed - the legacy agent verdict is exposed via API
by reading the existing outputs_json column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0348_far147_legacy_run_group_backfill"
down_revision: str | None = "0347_system_maps_version_aware"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _backfill_non_postgres(bind: sa.Connection) -> None:
    """Backfill legacy children using portable row-by-row traversal."""
    child_runs = bind.execute(
        sa.text("""
        SELECT id, parent_run_id, langgraph_thread_id
        FROM runs
        WHERE parent_run_id IS NOT NULL
          AND run_group_id IS NULL
        """)
    ).fetchall()

    for child_id, parent_run_id, child_thread_id in child_runs:
        current_id = parent_run_id
        root_id = None
        parent_thread_id = None
        depth = 0
        while current_id and depth < 100:
            row = bind.execute(
                sa.text("""
                SELECT id, parent_run_id, langgraph_thread_id
                FROM runs WHERE id = :id
                """),
                {"id": current_id},
            ).fetchone()
            if not row:
                break
            row_id, row_parent, row_thread = row
            if depth == 0:
                # Direct children compare against their immediate parent,
                # including when that parent is the root.
                parent_thread_id = row_thread
            if row_parent is None:
                root_id = row_id
                break
            current_id = row_parent
            depth += 1

        if root_id:
            kind = "resume" if child_thread_id == parent_thread_id else "retry"
            bind.execute(
                sa.text("""
                UPDATE runs SET run_group_id = :root_id, run_group_kind = :kind
                WHERE id = :child_id AND run_group_id IS NULL
                """),
                {"root_id": root_id, "kind": kind, "child_id": child_id},
            )


def upgrade() -> None:
    """Backfill run_group_id and run_group_kind for existing runs with parent_run_id."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # First, identify all runs that have a parent_run_id but NULL run_group_id
    # These are the legacy child runs that need backfilling
    if dialect == "postgresql":
        # Use a recursive CTE to find the root run for each child
        backfill_sql = sa.text("""
            WITH RECURSIVE run_chain AS (
                -- Base case: runs with parent_run_id (children)
                SELECT
                    id AS child_id,
                    parent_run_id,
                    langgraph_thread_id,
                    id AS current_id,
                    0 AS depth
                FROM runs
                WHERE parent_run_id IS NOT NULL
                  AND run_group_id IS NULL

                UNION ALL

                -- Recursive step: walk up the parent chain
                SELECT
                    rc.child_id,
                    r.parent_run_id,
                    r.langgraph_thread_id,
                    r.id AS current_id,
                    rc.depth + 1
                FROM run_chain rc
                JOIN runs r ON r.id = rc.parent_run_id
                WHERE rc.depth < 100  -- safety limit
            ),
            root_runs AS (
                -- Find the root (where parent_run_id IS NULL) for each child
                SELECT DISTINCT ON (child_id)
                    child_id,
                    current_id AS root_id,
                    langgraph_thread_id AS root_thread_id
                FROM run_chain
                WHERE parent_run_id IS NULL
            ),
            parent_info AS (
                -- Get the immediate parent's thread_id for resume detection
                SELECT
                    r.id AS child_id,
                    r.parent_run_id,
                    r.langgraph_thread_id AS child_thread_id,
                    p.langgraph_thread_id AS parent_thread_id
                FROM runs r
                JOIN runs p ON p.id = r.parent_run_id
                WHERE r.parent_run_id IS NOT NULL
                  AND r.run_group_id IS NULL
            )
            UPDATE runs
            SET
                run_group_id = rr.root_id,
                run_group_kind = CASE
                    WHEN pi.child_thread_id = pi.parent_thread_id THEN 'resume'
                    ELSE 'retry'
                END
            FROM root_runs rr
            JOIN parent_info pi ON pi.child_id = rr.child_id
            WHERE runs.id = rr.child_id
              AND runs.run_group_id IS NULL;
        """)
    else:
        # SQLite/MariaDB: use iterative approach (no recursive CTE support in older SQLite)
        # We'll do it in Python for simplicity and portability
        backfill_sql = None

    if backfill_sql:
        bind.execute(backfill_sql)
    else:
        _backfill_non_postgres(bind)


def downgrade() -> None:
    """Refuse to downgrade because the backfill does not record ownership.

    Clearing the grouping columns would destroy classifications written by the
    application after this migration, and the migration has no durable marker
    that distinguishes its own writes from later writes. Refusing is safer than
    a destructive, best-effort rollback.
    """
    raise RuntimeError(
        "0348_far147_legacy_run_group_backfill cannot be downgraded safely: "
        "backfilled run-group values are not distinguishable from later application writes"
    )
