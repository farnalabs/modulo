"""FAR-1530: per-pipeline Paused execution state (columns + breaker backfill).

Revision ID: 0286_pipeline_run_state
Revises: 0285_system_audit_events
Create Date: 2026-10-07

Adds the unified per-pipeline run state to ``pipelines`` (ratified by Hopper
(mechanism) and Roddy (product), 2026-10-06):

* ``run_enabled``          BOOLEAN NOT NULL DEFAULT true — when false the
  pipeline is PRESENT and VISIBLE but NON-EXECUTING: every run origin is
  refused at the ``create_run`` pipeline-state gate (FAR-1528's choke point,
  extended here) with ``PipelineNotRunnableError(state="paused")``.
* ``run_disabled_reason``  VARCHAR(20) NULL — closed vocabulary
  ``('operator', 'circuit_breaker')``; the CAUSE owns the reason (first cause
  wins), so an admin's circuit-breaker reset can never clear an operator pause
  and a trip can never overwrite one.
* ``run_disabled_at``      TIMESTAMPTZ NULL — when the cause was set.

CHECK constraints (model parity: ``ck_pipelines_run_enabled`` /
``ck_pipelines_run_disabled_reason`` in ``db/models/pipeline.py``, mirroring
``ck_organisations_triggers_paused_at``):

* ``run_enabled OR (run_disabled_reason IS NOT NULL AND run_disabled_at IS
  NOT NULL)`` — a disabled row always carries its cause and timestamp;
* ``run_disabled_reason IS NULL OR run_disabled_reason IN ('operator',
  'circuit_breaker')`` — the reason vocabulary is closed.

Data migration in the SAME revision: every LIVE row whose
``circuit_breaker_tripped`` witness is true is folded into the unified state
(``run_enabled=false, run_disabled_reason='circuit_breaker',
run_disabled_at=circuit_breaker_tripped_at`` — COALESCE guards a legacy
``tripped_at IS NULL`` witness against the new NOT-NULL tie CHECK). The
witness columns THEMSELVES are untouched: they stay the breaker's witness, so
the admin reset path (which clears the witness and re-activates triggers)
keeps working unchanged, and the reset only additionally clears the unified
state when the reason is ``'circuit_breaker'``.

All DDL is existence-gated (``ADD COLUMN IF NOT EXISTS`` / ``DO $$ IF NOT
EXISTS (pg_constraint)``) so a partially-applied upgrade no-ops, matching the
idempotence discipline of 0132/0284/0285.
"""

from __future__ import annotations

from alembic import op

revision: str = "0286_pipeline_run_state"
down_revision: str | None = "0285_system_audit_events"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Guarded ADD CONSTRAINT blocks (0284/0285 pattern): no-op when the named
# constraint already exists, so a re-run or a partially-applied upgrade is safe.
_ADD_TIE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_run_enabled') "
    "THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_run_enabled CHECK "
    "(run_enabled OR (run_disabled_reason IS NOT NULL AND run_disabled_at IS NOT NULL)); "
    "END IF; END $$;"
)
_ADD_REASON_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_run_disabled_reason') "
    "THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_run_disabled_reason CHECK "
    "(run_disabled_reason IS NULL OR run_disabled_reason IN ('operator', 'circuit_breaker')); "
    "END IF; END $$;"
)

# Fold every tripped witness into the unified state. Idempotent by both keys:
# after the first run the matched rows have run_enabled=false, so a re-run
# matches nothing. COALESCE guards a legacy tripped row whose
# circuit_breaker_tripped_at is NULL against the tie CHECK (the cause still
# gets a real timestamp). A row somehow already disabled keeps its existing
# cause (first cause owns the reason).
_BACKFILL = (
    "UPDATE pipelines SET run_enabled = false, "
    "run_disabled_reason = 'circuit_breaker', "
    "run_disabled_at = COALESCE(circuit_breaker_tripped_at, CURRENT_TIMESTAMP) "
    "WHERE circuit_breaker_tripped = true AND run_enabled = true"
)


def upgrade() -> None:
    op.execute("ALTER TABLE public.pipelines ADD COLUMN IF NOT EXISTS run_enabled BOOLEAN NOT NULL DEFAULT true")
    op.execute("ALTER TABLE public.pipelines ADD COLUMN IF NOT EXISTS run_disabled_reason VARCHAR(20)")
    op.execute("ALTER TABLE public.pipelines ADD COLUMN IF NOT EXISTS run_disabled_at TIMESTAMP WITH TIME ZONE")
    # Data first, then constraints: the CHECKs then validate the final rows
    # (every pre-existing row starts run_enabled=true, so the backfilled rows
    # satisfy the tie CHECK by construction).
    op.execute(_BACKFILL)
    op.execute(_ADD_TIE_CHECK)
    op.execute(_ADD_REASON_CHECK)


def downgrade() -> None:
    op.execute("ALTER TABLE public.pipelines DROP CONSTRAINT IF EXISTS ck_pipelines_run_disabled_reason")
    op.execute("ALTER TABLE public.pipelines DROP CONSTRAINT IF EXISTS ck_pipelines_run_enabled")
    op.execute("ALTER TABLE public.pipelines DROP COLUMN IF EXISTS run_disabled_at")
    op.execute("ALTER TABLE public.pipelines DROP COLUMN IF EXISTS run_disabled_reason")
    op.execute("ALTER TABLE public.pipelines DROP COLUMN IF EXISTS run_enabled")
