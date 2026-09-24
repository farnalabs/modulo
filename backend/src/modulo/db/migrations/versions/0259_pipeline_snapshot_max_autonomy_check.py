"""Add pipeline_snapshots.max_autonomy_level CHECK (FAR-1223).

Revision ID: 0259_pipeline_snapshot_max_autonomy_check
Revises: 0258_pipeline_accountability_owners
Create Date: 2026-09-24

Migration 0256 added ``max_autonomy_level`` to BOTH ``pipelines`` and
``pipeline_snapshots`` but only guarded the ``pipelines`` column with the
vocabulary CHECK — a snapshot row could store an arbitrary ceiling string that
the run-time resolution path would then treat as unparseable (falling back to
the snapshot default). This closes the gap with the identical constraint
``ck_pipeline_snapshots_max_autonomy_level``:

    max_autonomy_level IS NULL OR
    max_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')

Idempotent/existence-gated (same pattern as 0110/0176/0255/0256): added NOT
VALID first (instant, brief ACCESS EXCLUSIVE) then VALIDATEd in a separate
guarded step (SHARE UPDATE EXCLUSIVE — non-blocking for INSERTs), so
re-running this revision is a no-op.
"""

from __future__ import annotations

from alembic import op

revision: str = "0259_pipeline_snapshot_max_autonomy_check"
down_revision: str | None = "0258_pipeline_accountability_owners"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Existence-gated CHECK, added NOT VALID (instant, brief ACCESS EXCLUSIVE) then
# VALIDATEd in a separate guarded step (SHARE UPDATE EXCLUSIVE — non-blocking
# for INSERTs), mirroring the lock-safety pattern introduced in 0176/0255/0256.
_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipeline_snapshots_max_autonomy_level') "
    "THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT ck_pipeline_snapshots_max_autonomy_level CHECK ("
    "max_autonomy_level IS NULL OR "
    "max_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipeline_snapshots_max_autonomy_level' "
    "AND NOT convalidated) THEN ALTER TABLE public.pipeline_snapshots VALIDATE CONSTRAINT "
    "ck_pipeline_snapshots_max_autonomy_level; END IF; END $$;"
)


def upgrade() -> None:
    op.execute(_ADD_CHECK_NOT_VALID)
    op.execute(_VALIDATE_CHECK)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops.
    pass
