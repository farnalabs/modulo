"""Add server_default to pipelines.run_context_defaults.

Revision ID: 0188_pipeline_run_context_defaults_default
Revises: 0187_pipeline_performance_indexes
Create Date: 2026-09-07

``run_context_defaults`` is a NOT NULL JSON column (added nullable in 0110 then
SET NOT NULL when no rows were null) but it has no server_default. The ORM path
hides this via the Python-side ``default=dict``, but any raw INSERT that does
not explicitly supply the column — including integration tests that insert a
minimal row to exercise the CHECK constraints — fails with a NOT NULL violation
before the intended constraint can fire. Give the column a server_default of
'{}' so direct inserts succeed, matching the other JSON columns (``retry_policy``,
``graph_nodes_json``).
"""

from __future__ import annotations

from alembic import op

revision: str = "0188_pipeline_run_context_defaults_default"
down_revision: str | None = "0187_pipeline_performance_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute('ALTER TABLE public."pipelines" ALTER COLUMN "run_context_defaults" SET DEFAULT \'{}\'::json')


def downgrade() -> None:
    op.execute('ALTER TABLE public."pipelines" ALTER COLUMN "run_context_defaults" DROP DEFAULT')
