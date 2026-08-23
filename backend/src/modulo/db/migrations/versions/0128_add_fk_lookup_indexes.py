"""Add missing foreign-key lookup indexes on high-traffic join columns.

Revision ID: 0128_add_fk_lookup_indexes
Revises: 0127_soft_delete_partial_unique
Create Date: 2026-08-23

Several foreign-key columns that are heavily used in JOINs and filter
predicates (runs-for-pipeline, runs-for-account, nodes-for-pipeline,
eval/feedback/trigger/lease-for-run) had no secondary index, forcing
sequential scans on what are core read paths. The ``OrgScoped`` mixin only
indexes ``organisation_id``; these indexes cover the join/lookup keys.

The migration runs inside Alembic's single ``engine.begin()`` transaction
(``env.py`` wraps every revision in one transaction), so the referenced
tables are still uncommitted when this revision executes. A separate
autocommit connection (required by ``CREATE INDEX CONCURRENTLY``) therefore
cannot see those tables and fails with ``relation "<table>" does not exist``.
``CREATE INDEX CONCURRENTLY`` also cannot run inside a transaction block at
all. We therefore build plain (blocking) ``CREATE INDEX IF NOT EXISTS``
statements on the migration's own connection, which is both visible to the
uncommitted schema and transaction-safe. ``IF NOT EXISTS`` keeps the
migration idempotent and re-runnable.

NOTE (prod-size impact): ``runs`` and ``trigger_events`` can be large, and a
blocking ``CREATE INDEX`` takes a lock on the table for the duration of the
build, which can stall concurrent writes on busy production deployments. Because
the revision runs inside Alembic's single transaction, ``CONCURRENTLY`` (which
would avoid the long-held lock) is not available here. Schedule this migration
in a low-traffic window; on very large tables, consider a post-deploy
``CREATE INDEX CONCURRENTLY`` and a follow-up migration that only asserts the
index exists.
"""

from alembic import op
from sqlalchemy import text

revision = "0128_add_fk_lookup_indexes"
down_revision = "0127_soft_delete_partial_unique"
branch_labels = None
depends_on = None

# Only the five indexes below are genuinely new: the corresponding model
# columns now declare them via ``index=True`` (see MAJOR-1 fix). The other
# FK columns (nodes.pipeline_id, eval_results.run_id, feedback_records.run_id,
# workspace_leases.run_id) already carry ``index=True`` in their models, so
# SQLAlchemy creates those indexes itself under the same default names -- listing
# them here would be a redundant no-op.
_INDEXES = (
    ("ix_trigger_events_run_id", "trigger_events", ["run_id"]),
    ("ix_pipelines_account_id", "pipelines", ["account_id"]),
    ("ix_pipelines_owner_team_id", "pipelines", ["owner_team_id"]),
    ("ix_runs_owner_team_id", "runs", ["owner_team_id"]),
    ("ix_runs_account_id", "runs", ["account_id"]),
)


def upgrade():
    bind = op.get_bind()
    for index_name, table, columns in _INDEXES:
        cols = ", ".join(columns)
        bind.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({cols})"))


def downgrade():
    bind = op.get_bind()
    for index_name, _table, _columns in _INDEXES:
        bind.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
