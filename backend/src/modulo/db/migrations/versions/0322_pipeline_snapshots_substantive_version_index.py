"""FAR-1895: partial index for the substantive-version lookup.

The pipeline editor's version chip resolves "the current version" with

    SELECT ... FROM pipeline_snapshots
    WHERE pipeline_id = ?
      AND created_kind <> 'run'
    ORDER BY snapshot_version DESC
    LIMIT 1

Run-start snapshots (``created_kind='run'``) are created unconditionally per run
and run retention never prunes them, so a long-lived scheduled pipeline's
history can be an UNBOUNDED streak of run rows. The only pre-existing index on
the ordering is ``uq_pipeline_snapshot_version`` — a unique btree on
``(pipeline_id, snapshot_version)``. Postgres can satisfy the ORDER BY from
that index, but the ``created_kind <> 'run'`` predicate is not part of it, so
the scan walks the entire run streak in version order until it finds the first
non-run row. That is exactly the unbounded quantity the FAR-1895 filter exists
to remove, and it runs against a per-call deadline — the request most likely to
time out.

This migration adds a partial composite index whose predicate mirrors the
filter, so the DESC scan touches only substantive rows::

    CREATE INDEX ... ON pipeline_snapshots (pipeline_id, snapshot_version DESC)
    WHERE created_kind <> 'run'

The predicate mirrors the filter the query carries. The frontend/CRUD emits
``created_kind NOT IN ('run')`` (a one-element exclusion list), which reduces
to ``created_kind <> 'run'`` for the single value, so PostgreSQL's predicate
proof matches the query against this index predicate and uses the index
directly — verified on PostgreSQL 16 with ``EXPLAIN``: the filtered ``ORDER BY
snapshot_version DESC LIMIT 2`` plan is an ``Index Scan using
ix_pipeline_snapshots_version_substantive`` (it takes the partial index rather
than walking the run streak on ``uq_pipeline_snapshot_version``). The query's
optional ``draft IS false`` predicate (added for the same lookup) is not part of
this index: a draft is an editor auto-save and is bounded by edit activity, not
by run volume, so the run streak is the starvation source this index removes.
Adding ``AND draft IS false`` would not let the index serve the historical
no-draft-filter form, and the draft rows it would skip are negligible next to
the run rows it already skips.

Uses plain ``CREATE INDEX IF NOT EXISTS`` (idempotent, consistent with
0187/0171/0182). Cannot use ``CONCURRENTLY`` inside Alembic's transaction block.
Deploy-safety: index creation takes a SHARE lock (blocking writes) proportional
to table size; schedule outside peaks.

Revision ID: 0322_pipeline_snapshots_substantive_version_index
Revises: 0321_organisation_cost_state
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op

revision = "0322_pipeline_snapshots_substantive_version_index"
down_revision = "0321_organisation_cost_state"
branch_labels = None
depends_on = None

_CREATE_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_version_substantive "
    "ON pipeline_snapshots (pipeline_id, snapshot_version DESC) "
    "WHERE created_kind <> 'run'"
)

# Literal (not interpolated) so no f-string DDL trips the migration-fstring-sql
# / raw-sql-fstring semgrep rules; the name is a compile-time constant, so the
# repo convention for >0247 migrations is a literal statement (0283/0303/0310).
_DROP_SQL = "DROP INDEX IF EXISTS ix_pipeline_snapshots_version_substantive"


def upgrade() -> None:
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return
    op.execute(_CREATE_SQL)


def downgrade() -> None:
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return
    op.execute(_DROP_SQL)
