"""FAR-147: Legacy run-group classification backfill + legacy agent verdict exposure.

Revision ID: 0348_far147_legacy_run_group_backfill
Revises: 0347_system_maps_version_aware
Create Date: 2026-11-08

Part 1: Backfill run_group_id and run_group_kind for existing runs.

Only GENUINE retry/resume children are classified. ``correction`` and
``agent_signal`` children carry ``parent_run_id`` for lineage but deliberately
keep ``run_group_id`` / ``run_group_kind`` NULL (§15.1: those children keep
their existing rollup behaviour), so they are excluded unconditionally.

For each remaining child run with ``parent_run_id`` set and a NULL
``run_group_id``:

* the group root is the chain's EXISTING group root when any ancestor already
  carries a ``run_group_id`` (a child of a grouped attempt joins that group),
  otherwise the topmost run of the ``parent_run_id`` chain (``parent_run_id IS
  NULL``);
* ``run_group_kind`` is ``'resume'`` when the child shares its IMMEDIATE
  parent's ``langgraph_thread_id`` (it continues that parent's checkpoint) and
  ``'retry'`` when it runs on a fresh thread.

The traversal is bounded (100 levels) and idempotent: only rows whose
``run_group_id`` is still NULL are considered, and the UPDATE re-checks the
predicate, so a second run makes no changes.

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

# Children created for feedback correction / agent signals keep NULL grouping
# (see ``modulo.db.models.run`` — NULL is the group kind for signal /
# correction / work-item children). These are literals, not model imports:
# migrations pin the vocabulary they ran against (the 0302 precedent).
_LEGACY_EXCLUDED_TRIGGER_TYPES: tuple[str, ...] = ("correction", "agent_signal")

# The ``parent_run_id`` chain always has a finite length (a root terminates it),
# but a corrupted cycle must not hang the migration.
_MAX_CHAIN_DEPTH = 100

_POSTGRES_BACKFILL_SQL: str = """
    WITH RECURSIVE run_chain AS (
        -- Base case: candidate children (a genuine retry/resume attempt).
        SELECT
            r.id AS child_id,
            r.id AS current_id,
            r.parent_run_id,
            r.langgraph_thread_id AS thread_id,
            r.run_group_id AS ancestor_group_id,
            0 AS depth
        FROM runs r
        WHERE r.parent_run_id IS NOT NULL
          AND r.run_group_id IS NULL
          AND r.trigger_type NOT IN :excluded_trigger_types

        UNION ALL

        -- Recursive step: walk up the parent chain, stopping at the first
        -- ancestor that already belongs to a group (that group's root is the
        -- child's group) or at the topmost run.
        SELECT
            rc.child_id,
            p.id AS current_id,
            p.parent_run_id,
            p.langgraph_thread_id AS thread_id,
            p.run_group_id AS ancestor_group_id,
            rc.depth + 1
        FROM run_chain rc
        JOIN runs p ON p.id = rc.parent_run_id
        WHERE rc.depth < :max_chain_depth
          AND rc.ancestor_group_id IS NULL
    ),
    parent_info AS (
        -- The immediate parent's thread decides resume vs retry.
        SELECT rc.child_id, rc.thread_id AS parent_thread_id
        FROM run_chain rc
        WHERE rc.depth = 1
    ),
    group_info AS (
        -- Deepest qualifying row wins: an ancestor's existing group root beats
        -- the topmost root of the parent chain.
        SELECT DISTINCT ON (rc.child_id)
            rc.child_id,
            COALESCE(rc.ancestor_group_id, rc.current_id) AS group_root_id
        FROM run_chain rc
        WHERE rc.ancestor_group_id IS NOT NULL
           OR rc.parent_run_id IS NULL
        ORDER BY rc.child_id, rc.depth DESC
    )
    UPDATE runs
    SET
        run_group_id = group_info.group_root_id,
        run_group_kind = CASE
            WHEN parent_info.parent_thread_id IS NOT NULL
             AND runs.langgraph_thread_id = parent_info.parent_thread_id THEN 'resume'
            ELSE 'retry'
        END
    FROM group_info
    JOIN parent_info ON parent_info.child_id = group_info.child_id
    WHERE runs.id = group_info.child_id
      AND runs.run_group_id IS NULL;
"""


def _postgres_backfill_statement() -> sa.TextClause:
    """The recursive-CTE backfill statement with its expanding bind params.

    Exposed (with :func:`_postgres_backfill_params`) so the integration test
    can re-execute the PRODUCTION SQL to prove idempotence, rather than
    reimplementing the traversal.
    """
    return sa.text(_POSTGRES_BACKFILL_SQL).bindparams(
        sa.bindparam("excluded_trigger_types", expanding=True),
        sa.bindparam("max_chain_depth"),
    )


def _postgres_backfill_params() -> dict[str, object]:
    """Bind values for :func:`_postgres_backfill_statement`."""
    return {
        "excluded_trigger_types": list(_LEGACY_EXCLUDED_TRIGGER_TYPES),
        "max_chain_depth": _MAX_CHAIN_DEPTH,
    }


def classify_legacy_child(*, child_thread_id: str | None, parent_thread_id: str | None) -> str:
    """Classify one legacy child: ``resume`` when it shares its parent's thread.

    Shared by the portable fallback and pinned by tests, so both backfill
    paths apply the same model contract (§15.1): a child continuing its
    immediate parent's ``langgraph_thread_id`` is a resume, anything else is a
    retry on a fresh thread.
    """
    if parent_thread_id is not None and child_thread_id == parent_thread_id:
        return "resume"
    return "retry"


def _walk_parent_chain(bind: sa.Connection, parent_run_id: str | None) -> tuple[str | None, str | None]:
    """Return ``(group_root_id, parent_thread_id)`` for one child.

    ``group_root_id`` is the FIRST ancestor's existing ``run_group_id`` when
    present, else the topmost run id of the chain. ``parent_thread_id`` is the
    immediate parent's thread (``None`` when the parent row is missing).
    """
    current_id = parent_run_id
    parent_thread_id: str | None = None
    depth = 0
    while current_id is not None and depth < _MAX_CHAIN_DEPTH:
        row = bind.execute(
            sa.text(
                """
                SELECT id, parent_run_id, langgraph_thread_id, run_group_id
                FROM runs WHERE id = :id
                """
            ),
            {"id": current_id},
        ).fetchone()
        if row is None:
            return None, parent_thread_id
        row_id, row_parent, row_thread, row_group_id = row
        if depth == 0:
            parent_thread_id = row_thread
        if row_group_id is not None:
            return row_group_id, parent_thread_id
        if row_parent is None:
            return row_id, parent_thread_id
        current_id = row_parent
        depth += 1
    return None, parent_thread_id


def _backfill_non_postgres(bind: sa.Connection) -> None:
    """Backfill legacy children using portable row-by-row traversal.

    Mirrors :data:`_POSTGRES_BACKFILL_SQL` exactly: same candidate predicate
    (NULL group, has a parent, not a correction/agent_signal child), same
    resume/retry rule, same existing-group-root rule, and the same idempotent
    ``run_group_id IS NULL`` UPDATE guard.
    """
    child_runs = bind.execute(
        sa.text(
            """
            SELECT id, parent_run_id, langgraph_thread_id
            FROM runs
            WHERE parent_run_id IS NOT NULL
              AND run_group_id IS NULL
              AND trigger_type NOT IN :excluded_trigger_types
            """
        ).bindparams(sa.bindparam("excluded_trigger_types", expanding=True)),
        {"excluded_trigger_types": list(_LEGACY_EXCLUDED_TRIGGER_TYPES)},
    ).fetchall()

    for child_id, parent_run_id, child_thread_id in child_runs:
        group_root_id, parent_thread_id = _walk_parent_chain(bind, parent_run_id)
        if group_root_id is None:
            continue
        kind = classify_legacy_child(child_thread_id=child_thread_id, parent_thread_id=parent_thread_id)
        bind.execute(
            sa.text(
                """
                UPDATE runs SET run_group_id = :root_id, run_group_kind = :kind
                WHERE id = :child_id AND run_group_id IS NULL
                """
            ),
            {"root_id": group_root_id, "kind": kind, "child_id": child_id},
        )


def upgrade() -> None:
    """Backfill run_group_id and run_group_kind for existing runs with parent_run_id."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(_postgres_backfill_statement(), _postgres_backfill_params())
        return
    # SQLite/MariaDB: portable Python traversal (no recursive CTE support in
    # older SQLite).
    _backfill_non_postgres(bind)


def downgrade() -> None:
    """Refuse to downgrade because the backfill does not record ownership.

    Clearing the grouping columns would destroy classifications written by the
    application after this migration, and the migration has no durable marker
    that distinguishes its own writes from later writes. Refusing is safer than
    a destructive, best-effort rollback.
    """
    raise RuntimeError("0348_far147_legacy_run_group_backfill cannot be downgraded safely: backfilled run-group values are not distinguishable from later application writes")
