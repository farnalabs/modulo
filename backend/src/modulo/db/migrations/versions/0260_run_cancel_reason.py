"""Add runs.cancel_reason / runs.cancelled_by (FAR-1233 cancellation transparency).

Revision ID: 0260_run_cancel_reason
Revises: 0259_pipeline_snapshot_max_autonomy_check
Create Date: 2026-09-25

Two nullable columns recording WHY and WHO cancelled a run:

* ``cancel_reason`` — one of the closed vocabulary in
  ``modulo.db.models.run.CANCEL_REASON_VALUES`` (``user_requested``,
  ``agent_requested``, ``hitl_gate_expired``, ``hitl_gate_missing``), guarded
  by the ``ck_runs_cancel_reason`` CHECK. NULL means "reason not recorded":
  every run cancelled BEFORE this migration keeps NULL by design and the run
  detail page renders a neutral fallback (no backfill — a guessed reason would
  be a lie about history).
* ``cancelled_by`` — the acting account id, or the ``system`` sentinel for a
  watchdog/terminalizer-owned cancellation.

Both columns are existence-gated ``ADD COLUMN IF NOT EXISTS`` and the CHECK is
existence-gated too, so re-running this revision is a no-op (idempotent style,
same pattern as 0110/0176/0255/0256). The CHECK is migration-owned: the ORM
does not duplicate it (repo parity rule — see
``tests/integration/test_initial_migration.py::_MIGRATION_OWNED_CHECKS``).
"""

from __future__ import annotations

from alembic import op

revision: str = "0260_run_cancel_reason"
down_revision: str | None = "0259_pipeline_snapshot_max_autonomy_check"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ADD_CANCEL_REASON_COLUMN = 'ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "cancel_reason" character varying(64);'
_ADD_CANCELLED_BY_COLUMN = 'ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "cancelled_by" character varying(64);'

# Existence-gated CHECK over the closed vocabulary (NULL allowed — pre-0259
# runs have no recorded reason), added NOT VALID (instant) then VALIDATEd in a
# separate guarded step (SHARE UPDATE EXCLUSIVE — non-blocking for INSERTs),
# mirroring the lock-safety pattern introduced in 0176/0255/0256.
_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_cancel_reason') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_cancel_reason CHECK ("
    "cancel_reason IS NULL OR "
    "cancel_reason IN ('user_requested', 'agent_requested', 'hitl_gate_expired', 'hitl_gate_missing')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_cancel_reason' "
    "AND NOT convalidated) THEN ALTER TABLE public.runs VALIDATE CONSTRAINT "
    "ck_runs_cancel_reason; END IF; END $$;"
)


def upgrade() -> None:
    op.execute(_ADD_CANCEL_REASON_COLUMN)
    op.execute(_ADD_CANCELLED_BY_COLUMN)
    op.execute(_ADD_CHECK_NOT_VALID)
    op.execute(_VALIDATE_CHECK)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops.
    pass
