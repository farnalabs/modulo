"""Add composite indexes for hot pipeline queries.

Revision ID: 0184_pipeline_performance_indexes
Revises: 0183_pipeline_check_constraints_deleted_by
Create Date: 2026-09-07

Findings from improve-database lens analysis:

1. ``pipeline_edges`` — ``get_pipeline_graph``, ``_load_pipeline_and_edges``,
   ``_read_clone_source_snapshot``, ``list_snapshots`` all filter
   ``WHERE pipeline_id = ? ORDER BY created_at, id``.  The existing
   ``ix_pipeline_edges_pipeline_id`` satisfies the WHERE but forces a sort.
   Composite ``(pipeline_id, created_at, id)`` eliminates the sort.

2. ``pipeline_snapshots`` — ``resolve_snapshot_for_channel`` filters
    ``WHERE pipeline_id = ? AND channel = ? AND draft IS false
    ORDER BY snapshot_version DESC LIMIT 1``.  A partial composite index
    ``(pipeline_id, channel, snapshot_version) WHERE draft IS false`` turns
    this into a direct index scan (the ``IS false`` predicate mirrors the
    query's ``draft.is_(False)`` BooleanTest exactly, which PostgreSQL's
    ``predtest.c`` can prove implies the index predicate).

3. ``pipeline_folders`` — ``list_folders`` sorts ``ORDER BY sort_order, name``
   across all folders in an org.  Composite
   ``(organisation_id, sort_order, name)`` eliminates the filesort.

4. ``nodes`` — ``get_child_nodes`` queries
   ``WHERE parent_node_id = ? ORDER BY created_at``.  Composite
   ``(parent_node_id, created_at)`` serves both filter and order.

5. ``pipelines`` — ``list_pipelines`` sorts ``ORDER BY created_at DESC``
   after filtering ``WHERE deleted_at IS NULL`` with RLS
   ``organisation_id``.  Partial composite
   ``(organisation_id, deleted_at, created_at) WHERE deleted_at IS NULL``
   allows index-only descending scans.

All indexes use plain ``CREATE INDEX IF NOT EXISTS`` (idempotent,
consistent with 0171/0182).  Cannot use ``CONCURRENTLY`` inside Alembic
transaction blocks.  Deploy-safety: index creation acquires SHARE lock
(blocking writes) proportional to table size; schedule outside peaks.
"""

from __future__ import annotations

from alembic import op

revision: str = "0184_pipeline_performance_indexes"
down_revision: str | None = "0183_pipeline_check_constraints_deleted_by"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES: list[tuple[str, str, str | None]] = [
    (
        "ix_pipeline_edges_pipeline_created",
        "pipeline_edges",
        (
            "CREATE INDEX IF NOT EXISTS ix_pipeline_edges_pipeline_created "
            "ON pipeline_edges (pipeline_id, created_at, id)"
        ),
    ),
    (
        "ix_pipeline_snapshots_channel_version",
        "pipeline_snapshots",
        (
            "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_channel_version "
            "ON pipeline_snapshots (pipeline_id, channel, snapshot_version) "
            "WHERE draft IS false"
        ),
    ),
    (
        "ix_pipeline_folders_org_sort",
        "pipeline_folders",
        (
            "CREATE INDEX IF NOT EXISTS ix_pipeline_folders_org_sort "
            "ON pipeline_folders (organisation_id, sort_order, name)"
        ),
    ),
    (
        "ix_nodes_parent_created",
        "nodes",
        ("CREATE INDEX IF NOT EXISTS ix_nodes_parent_created ON nodes (parent_node_id, created_at)"),
    ),
    (
        "ix_pipelines_org_deleted_created",
        "pipelines",
        (
            "CREATE INDEX IF NOT EXISTS ix_pipelines_org_deleted_created "
            "ON pipelines (organisation_id, deleted_at, created_at) "
            "WHERE deleted_at IS NULL"
        ),
    ),
]


def upgrade() -> None:
    for _name, _table, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _table, _ddl in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
