"""Add pipelines/pipeline_snapshots.max_autonomy_level (FAR-1163 S0).

Revision ID: 0256_pipeline_max_autonomy_level
Revises: 0255_trigger_event_value_filter_label
Create Date: 2026-09-23

Closes the context-setter autonomy-escalation hole (ADR 043 slice S0): a
``max_autonomy_level`` hard ceiling on every HITL-gate resolution. The column
is NULLABLE with NO server default — NULL means "effective ceiling =
default_autonomy_level", so after this ships NO existing pipeline can be
escalated by a ``autonomy_recommendation`` written by a context-setter node.

* ``pipelines.max_autonomy_level`` — the configured ceiling (owner-set).
* ``pipeline_snapshots.max_autonomy_level`` — the ceiling frozen into the
  run snapshot (runs read the SNAPSHOT, not the live row).

Both columns are existence-gated ``ADD COLUMN IF NOT EXISTS`` and the CHECK
constraint is existence-gated, so re-running this revision is a no-op
(idempotent style, same pattern as 0110/0176/0255).
"""

from __future__ import annotations

from alembic import op

revision: str = "0256_pipeline_max_autonomy_level"
down_revision: str | None = "0255_trigger_event_value_filter_label"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ADD_PIPELINES_COLUMN = (
    'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "max_autonomy_level" character varying(30);'
)
_ADD_SNAPSHOTS_COLUMN = (
    'ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "max_autonomy_level" character varying(30);'
)

# Existence-gated CHECK, added NOT VALID (instant, brief ACCESS EXCLUSIVE) then
# VALIDATEd in a separate guarded step (SHARE UPDATE EXCLUSIVE — non-blocking
# for INSERTs), mirroring the lock-safety pattern introduced in 0176/0255.
_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_max_autonomy_level') "
    "THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_max_autonomy_level CHECK ("
    "max_autonomy_level IS NULL OR "
    "max_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_max_autonomy_level' "
    "AND NOT convalidated) THEN ALTER TABLE public.pipelines VALIDATE CONSTRAINT "
    "ck_pipelines_max_autonomy_level; END IF; END $$;"
)


def upgrade() -> None:
    op.execute(_ADD_PIPELINES_COLUMN)
    op.execute(_ADD_SNAPSHOTS_COLUMN)
    op.execute(_ADD_CHECK_NOT_VALID)
    op.execute(_VALIDATE_CHECK)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops.
    pass
