"""HITL reject-terminates (FAR-1487): add the terminal ``rejected`` run status.

Revision ID: 0284_add_rejected_run_status
Revises: 0283_runs_drop_unused_indexes
Create Date: 2026-10-05

A HITL rejection with no configured reject destination now ENDS the run with a
dedicated terminal status, ``rejected`` - not ``failed`` (nothing broke) and not
``cancelled`` (no operator stopped it). Coalesced-supersede system rejections
land here too (``error_code='hitl.superseded'``).

Schema changes (all in the closed run-status vocabulary):

1. ``ck_runs_status`` is widened to admit ``rejected`` (precedent:
   0150_add_router_no_match_status, 0180_hitl_parked_status). The recreated list
   is the UNION of the live 15-status list (as of 0180) plus ``rejected`` - 16
   total. Same idempotent drop-if-differs / staged ``ADD ... NOT VALID`` /
   guarded ``VALIDATE`` pattern as 0180; the widened list is a strict superset,
   so VALIDATE can never fail on pre-existing rows.
2. ``ck_run_daily_facts_status`` (run_daily_facts, added by 0165 and never
   widened since) is widened the same way - every terminal run writes a fact
   row, so a ``rejected`` run would otherwise fail its facts insert.
3. The ``ix_runs_workspace_drift_sweep`` partial index is re-created with
   ``rejected`` in its predicate (see below).

Model parity is pinned by backend/tests/unit/db/test_run_status_vocabulary.py.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0284_add_rejected_run_status"
down_revision: str | None = "0283_runs_drop_unused_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Whitespace-stripped pg_get_constraintdef form of the NEW list (0110 guard
# pattern); a trailing NOT VALID is normalised off so a partially-run upgrade
# is still recognised.
_DROP_NEW = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g'), 'NOTVALID$', '') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''unknown''::charactervarying,"
    "''hitl_parked''::charactervarying,"
    "''complete''::charactervarying,''failed''::charactervarying,''cancelled''::charactervarying,"
    "''eval_failed''::charactervarying,''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying,''router_no_match''::charactervarying,"
    "''compensation_failed''::charactervarying,''rejected''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
_ADD_NEW = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'unknown'::character varying, 'hitl_parked'::character varying, "
    "'complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, "
    "'cost_ceiling_exceeded'::character varying, 'router_no_match'::character varying, "
    "'compensation_failed'::character varying, 'rejected'::character varying])::text[]))) NOT VALID; "
    "END IF; END $$;"
)
_VALIDATE_NEW = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' "
    "AND pg_get_constraintdef(oid) LIKE '%NOT VALID') "
    "THEN ALTER TABLE public.runs VALIDATE CONSTRAINT ck_runs_status; END IF; END $$;"
)
# Downgrade: restore the 0180 15-status list.
_DROP_OLD = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g'), 'NOTVALID$', '') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''unknown''::charactervarying,"
    "''hitl_parked''::charactervarying,"
    "''complete''::charactervarying,''failed''::charactervarying,''cancelled''::charactervarying,"
    "''eval_failed''::charactervarying,''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying,''router_no_match''::charactervarying,"
    "''compensation_failed''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
_ADD_OLD = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'unknown'::character varying, 'hitl_parked'::character varying, "
    "'complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, "
    "'cost_ceiling_exceeded'::character varying, 'router_no_match'::character varying, "
    "'compensation_failed'::character varying])::text[]))); "
    "END IF; END $$;"
)

# The 0278 partial index ``ix_runs_workspace_drift_sweep`` carries the sweep's
# WHERE clause VERBATIM (``status IN (TERMINAL_STATUSES)``). Adding ``rejected``
# to TERMINAL_STATUSES changes that IN list, so a partial index with the old
# predicate would stop serving the sweep (the planner can only use a partial
# index whose predicate the query implies) - re-create it with the widened
# list. Must equal the model's ``Index("ix_runs_workspace_drift_sweep")``.
_DRIFT_INDEX = "ix_runs_workspace_drift_sweep"
_DRIFT_PREDICATE_NEW = (
    "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed', 'rejected') "
    "AND workspace_inputs_drift_detected IS NULL"
)
_DRIFT_PREDICATE_OLD = (
    "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed') "
    "AND workspace_inputs_drift_detected IS NULL"
)


# run_daily_facts.status CHECK (0165 ``_RUN_STATUS``: the 14 pre-hitl_parked
# statuses - facts only ever hold TERMINAL runs). Widened with ``rejected``.
_FACTS_STATUS_OLD = (
    "status IN ('pending','running','awaiting_human','claimed','unknown','complete',"
    "'failed','cancelled','eval_failed','stalled','budget_exceeded',"
    "'router_no_match','compensation_failed','cost_ceiling_exceeded')"
)
_FACTS_STATUS_NEW = (
    "status IN ('pending','running','awaiting_human','claimed','unknown','complete',"
    "'failed','cancelled','eval_failed','stalled','budget_exceeded',"
    "'router_no_match','compensation_failed','cost_ceiling_exceeded','rejected')"
)
# Whole-literal DDL (no interpolation): op.execute cannot bind parameters.
_FACTS_DROP = "ALTER TABLE run_daily_facts DROP CONSTRAINT IF EXISTS ck_run_daily_facts_status"
_FACTS_ADD_NEW = (
    "ALTER TABLE run_daily_facts ADD CONSTRAINT ck_run_daily_facts_status CHECK ("
    "status IN ('pending','running','awaiting_human','claimed','unknown','complete',"
    "'failed','cancelled','eval_failed','stalled','budget_exceeded',"
    "'router_no_match','compensation_failed','cost_ceiling_exceeded','rejected')) NOT VALID"
)
_FACTS_ADD_OLD = (
    "ALTER TABLE run_daily_facts ADD CONSTRAINT ck_run_daily_facts_status CHECK ("
    "status IN ('pending','running','awaiting_human','claimed','unknown','complete',"
    "'failed','cancelled','eval_failed','stalled','budget_exceeded',"
    "'router_no_match','compensation_failed','cost_ceiling_exceeded')) NOT VALID"
)
_FACTS_VALIDATE = "ALTER TABLE run_daily_facts VALIDATE CONSTRAINT ck_run_daily_facts_status"


def _set_facts_status_check(add_ddl: str) -> None:
    """Re-create ck_run_daily_facts_status (idempotent drop + NOT VALID add + VALIDATE)."""
    op.execute(_FACTS_DROP)
    op.execute(add_ddl)
    op.execute(_FACTS_VALIDATE)


def _recreate_drift_index(predicate: str) -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_workspace_drift_sweep")
    op.create_index(
        _DRIFT_INDEX,
        "runs",
        ["id"],
        postgresql_where=sa.text(predicate),
        sqlite_where=sa.text(predicate),
    )


def upgrade() -> None:
    op.execute(_DROP_NEW)
    op.execute(_ADD_NEW)
    op.execute(_VALIDATE_NEW)
    _set_facts_status_check(_FACTS_ADD_NEW)
    _recreate_drift_index(_DRIFT_PREDICATE_NEW)


def downgrade() -> None:
    # ``rejected`` rows cannot satisfy the restored constraint and the closest
    # pre-existing terminal-non-failure outcome is ``cancelled``; demote them
    # FIRST (the constraint recreation would otherwise fail on them).
    op.execute("UPDATE runs SET status = 'cancelled' WHERE status = 'rejected'")
    op.execute("UPDATE run_daily_facts SET status = 'cancelled' WHERE status = 'rejected'")
    _set_facts_status_check(_FACTS_ADD_OLD)
    op.execute(_DROP_OLD)
    op.execute(_ADD_OLD)
    _recreate_drift_index(_DRIFT_PREDICATE_OLD)
