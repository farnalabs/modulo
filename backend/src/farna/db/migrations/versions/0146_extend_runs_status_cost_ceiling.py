"""Register ``cost_ceiling_exceeded`` as a valid terminal run status (FAR-391).

Revision ID: 0146_extend_runs_status_cost_ceiling
Revises: 0145_spend_ceiling
Create Date: 2026-08-23

The FAR-391 spend-ceiling enforcement writes ``status = 'cost_ceiling_exceeded'``
in two places (the executor pre-gate in ``executor.py`` and the finalize ledger
gate in ``finalize.py``). That status was never registered in the
``ck_runs_status`` CHECK constraint, so the direct ``Run.status`` write raised an
``IntegrityError`` against a real Postgres database and the enforced terminal
state never persisted. It was also absent from ``RUN_STATUS_WHITELIST`` (so
``update_run_status`` raised ``ValueError`` and the executor pre-gate failed
open) and from ``TERMINAL_STATUSES`` (so analytics / recovery misclassified it).

This migration extends the ``ck_runs_status`` CHECK constraint to include the new
terminal status, mirroring the idempotent drop-if-differs / add-if-absent pattern
introduced in migration 0110 (which added ``budget_exceeded``).

The status vocabulary is the single source of truth in
``modulo.db.models.run.TERMINAL_STATUSES`` and
``modulo.db.crud.run.RUN_STATUS_WHITELIST``; this migration keeps the DB
constraint in lock-step with those sets.
"""

from alembic import op

revision = "0146_extend_runs_status_cost_ceiling"
down_revision = "0145_spend_ceiling"
branch_labels = None
depends_on = None

# DROP (idempotent): only drop the existing constraint if its definition is NOT
# already the target list (whitespace-stripped comparison against the def that
# Postgres would emit for the NEW status set).
_DROP_NEW = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''complete''::charactervarying,"
    "''failed''::charactervarying,''cancelled''::charactervarying,''eval_failed''::charactervarying,"
    "''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
# ADD (idempotent): re-add the constraint with the NEW list if it is now absent.
_ADD_NEW = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, 'failed'::character varying, "
    "'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying, 'cost_ceiling_exceeded'::character varying])::text[]))); "
    "END IF; END $$;"
)
# Revert: DROP if the def is not already the OLD (pre-0128) list ...
_DROP_OLD = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''complete''::charactervarying,"
    "''failed''::charactervarying,''cancelled''::charactervarying,''eval_failed''::charactervarying,"
    "''stalled''::charactervarying,''budget_exceeded''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
# ... and ADD the OLD list if absent.
_ADD_OLD = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, 'failed'::character varying, "
    "'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying])::text[]))); END IF; END $$;"
)


def upgrade() -> None:
    op.execute(_DROP_NEW)
    op.execute(_ADD_NEW)


def downgrade() -> None:
    op.execute(_DROP_OLD)
    op.execute(_ADD_OLD)
