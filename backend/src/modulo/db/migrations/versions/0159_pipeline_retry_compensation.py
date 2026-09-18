"""Register ``compensation_failed``/``unknown`` run statuses + P5 columns (FAR-402 P5).

Revision ID: 0159_pipeline_retry_compensation
Revises: 0158_sso_provider_id
Create Date: 2026-08-26

Renumber note: this migration was authored as ``0150_pipeline_retry_compensation``
and has been renumbered four times as main advanced underneath it. It first collided
with main's ``0150_add_router_no_match_status`` (FAR-402 P1 / FAR-415) and was
renumbered to ``0151``; that in turn collided with main's ``0151_fix_constraints``
(improve-database). Main's chain has since grown
``0151_fix_constraints`` -> ``0152_dismissed_by_user_id_index`` ->
``0153_add_numeric_check_constraints`` -> ``0154_add_web_vital_events_time_index`` ->
``0155_add_hot_query_indexes`` -> ``0156_add_soft_delete_partial_uniques`` ->
``0157_add_numeric_check_constraints`` -> ``0158_sso_provider_id``, so this migration is
now numbered ``0159`` and re-parented onto main's real head ``0158_sso_provider_id``,
keeping the graph a single linear chain with ``0160_run_idempotency_key`` as the sole head.

Implements the FAR-402 P5 (FAR-419) failure/retry + compensation data-model
deltas:

1. **``runs.idempotency_key``** (nullable String(128)) — the run-level
   idempotency identity of an operator re-run, derived deterministically from
   ``<pipeline_id>:<run_number>`` (FAR-410 primitive, column previously
   deferred). An ``UNKNOWN``-terminated run re-run by an operator reuses the
   SAME persisted key so a write that may have reached the upstream is not
   re-applied as a fresh operation.

2. **``pipeline_edges.retry`` / ``pipeline_edges.on_failure_target``** — a
   transition-edge retry block (re-executes the source node) and a compensation
   node id. Mutually exclusive per failure (design §4F); the graph validator
   enforces the exclusion at compile time.

3. **``ck_runs_status``** — extend the CHECK constraint to admit the two new
   statuses. ``compensation_failed`` is a NEW TERMINAL status (a watched node AND
   its compensation path both failed); ``unknown`` is adopted from FAR-410 as a
   NON-TERMINAL recovery status. Because main's ``0150_add_router_no_match_status``
   (FAR-415) also extends this same constraint (adding ``router_no_match``), the
   recreated list here is the UNION of both migrations' status additions:
   ``router_no_match`` (from main P1) plus ``unknown`` and ``compensation_failed``
   (from this P5). The drop-if-differs / add-if-absent pattern
   (introduced in 0146, mirroring 0110) keeps the migration idempotent and
   rolling-deploy safe — the constraint is rebuilt with a backward-compatible
   SUPERSET so the overlap window where new app code emits a new status while the
   constraint is still the OLD list stays WRITE-SAFE (design §10 O1).
"""

import sqlalchemy as sa
from alembic import op

revision = "0159_pipeline_retry_compensation"
down_revision = "0158_sso_provider_id"
branch_labels = None
depends_on = None

# DROP (idempotent): only drop the existing constraint if its definition is NOT
# already the target list (whitespace-stripped comparison against the def that
# Postgres would emit for the NEW status set). String literals are hardcoded
# (no f-string interpolation) so the raw-SQL injection detector does not fire —
# these are immutable constants, mirroring migration 0146.
#
# The NEW target list is the UNION of main's 0150 (router_no_match) and this
# migration's additions (unknown, compensation_failed) — 14 statuses total.
_DROP_NEW = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''unknown''::charactervarying,"
    "''complete''::charactervarying,''failed''::charactervarying,''cancelled''::charactervarying,"
    "''eval_failed''::charactervarying,''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying,''router_no_match''::charactervarying,"
    "''compensation_failed''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
# ADD (idempotent): re-add the constraint with the NEW list if it is now absent.
_ADD_NEW = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'unknown'::character varying, 'complete'::character varying, "
    "'failed'::character varying, 'cancelled'::character varying, 'eval_failed'::character varying, "
    "'stalled'::character varying, 'budget_exceeded'::character varying, "
    "'cost_ceiling_exceeded'::character varying, 'router_no_match'::character varying, "
    "'compensation_failed'::character varying])::text[]))); "
    "END IF; END $$;"
)
# Revert: DROP if the def is not already the pre-0151 list ...
# The pre-0151 list (main's 0150 state) already contains router_no_match but NOT
# unknown / compensation_failed, so the OLD target list is that 12-status set.
_DROP_OLD = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''complete''::charactervarying,"
    "''failed''::charactervarying,''cancelled''::charactervarying,''eval_failed''::charactervarying,"
    "''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying,''router_no_match''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
# ... and ADD the pre-0151 list if absent.
_ADD_OLD = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, 'failed'::character varying, "
    "'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying, 'cost_ceiling_exceeded'::character varying, "
    "'router_no_match'::character varying])::text[]))); "
    "END IF; END $$;"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_runs = {col["name"] for col in inspector.get_columns("runs")}
    # 1. run-level idempotency key (FAR-410 deferred column, landed by P5).
    # Idempotent: the parallel ``0160_run_idempotency_key`` migration (FAR-438)
    # also adds this column, so only add if it is not already present — otherwise
    # an upgrade applying 0159 before 0158 fails with "column already exists".
    if "idempotency_key" not in existing_runs:
        op.add_column("runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    # 2. edge retry + compensation columns (design §5).
    existing_edges = {col["name"] for col in inspector.get_columns("pipeline_edges")}
    if "retry" not in existing_edges:
        op.add_column("pipeline_edges", sa.Column("retry", sa.JSON(), nullable=True))
    if "on_failure_target" not in existing_edges:
        op.add_column("pipeline_edges", sa.Column("on_failure_target", sa.String(length=64), nullable=True))
    # 3. extend ck_runs_status to the new superset.
    op.execute(_DROP_NEW)
    op.execute(_ADD_NEW)


def downgrade() -> None:
    op.drop_column("pipeline_edges", "on_failure_target")
    op.drop_column("pipeline_edges", "retry")
    # Idempotent: 0160_run_idempotency_key may already have dropped the column on
    # its own downgrade, so only drop it if it is still present — otherwise the
    # downgrade chain 0160 -> 0159 fails with "column does not exist".
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_runs = {col["name"] for col in inspector.get_columns("runs")}
    if "idempotency_key" in existing_runs:
        op.drop_column("runs", "idempotency_key")
    op.execute(_DROP_OLD)
    op.execute(_ADD_OLD)
