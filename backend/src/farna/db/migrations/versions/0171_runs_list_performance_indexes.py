"""Add runs-list total COUNT covering index for GET /api/v1/runs.

Revision ID: 0171_runs_list_performance_indexes
Revises: 0170_add_residual_foreign_keys
Create Date: 2026-09-01

The Runs page timed out on populated orgs. Three structural costs in the list
request path, one of which this migration addresses:

1. ``count_active_runs_for_org`` (every GET /api/v1/runs request AND every
   dispatch admission gate) filters ``organisation_id = ?`` AND
   ``status IN (...)``. Migration 0155 (already in the applied chain) already
   provides ``ix_runs_organisation_status (organisation_id, status)`` for it,
   and ``ix_runs_pipeline_status (pipeline_id, status)`` for
   ``count_active_runs_for_pipeline`` — so no new status index is created
   here. A second index whose leading columns duplicate 0155's would double
   the write amplification of ``status``, a column updated on EVERY run
   transition, on the biggest table.
2. The list's ``total`` COUNT joins pipelines for the soft-delete filter and
   needs ``pipeline_id`` for every org run; the existing
   ``ix_runs_refusal (organisation_id, created_at)`` (0066) cannot cover it,
   forcing a heap fetch per run over the whole org.
   ``ix_runs_org_created_pipeline`` keeps the COUNT index-only via a non-key
   INCLUDE column.
3. The page SELECT loaded every heavy JSONB payload column
   (outputs_json / node_telemetry_json / ...). Fixed in CRUD
   (``crud.run._RUNS_LIST_DEFERRED_COLUMNS``) — no schema change needed.

Deploy-safety: plain ``CREATE INDEX`` (non-concurrent) is used because Alembic
runs inside a transaction and ``CREATE INDEX CONCURRENTLY`` cannot. Expect a
table-size-proportional upgrade with a brief write lock on ``runs``; deploy
outside traffic peaks. The table can be large — schedule accordingly.

Indexed with the same ``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0154/0155
to keep the migration idempotent and re-runnable.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0171_runs_list_performance_indexes"
down_revision: str | None = "0170_add_residual_foreign_keys"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# List total COUNT: (organisation_id, created_at) key (superset of the
# ordering path) + pipeline_id as a non-key INCLUDE column so the count can
# be served index-only. Active-run counts (org/pipeline + status IN (...))
# are already served by 0155's hot-query indexes — see the module docstring.
_CREATE_LIST_COUNT_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_runs_org_created_pipeline "
    'ON public."runs" (organisation_id, created_at) INCLUDE (pipeline_id);'
)


def upgrade() -> None:
    op.get_bind().execute(text(_CREATE_LIST_COUNT_INDEX))


def downgrade() -> None:
    op.get_bind().execute(text("DROP INDEX IF EXISTS ix_runs_org_created_pipeline;"))
