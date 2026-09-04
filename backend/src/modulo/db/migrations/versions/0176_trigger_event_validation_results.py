"""Widen ck_trigger_events_validation_result for FAR-604 admission healing.

Revision ID: 0176_trigger_event_validation_results
Revises: 0174_per_org_last_admin_guard
Create Date: 2026-09-04

FAR-604 adds two trigger-event outcomes for the admission-healing work:

* ``coalesced`` — a webhook delivery was folded (latest-wins) into the
  pipeline's already-pending run for the same work item instead of minting a
  new row; ``run_id`` points at the coalesced run.
* ``backpressure_skipped`` — trigger dispatch refused run creation because the
  pipeline's pending queue exceeded the depth/age backpressure limits;
  ``error_detail`` carries the observed depths.

Both values enter the ORM ``VALIDATION_RESULT_VALUES`` vocabulary, so the DB
CHECK constraint must be widened to match (the constraint is re-created here
with the FULL 23-value vocabulary — the model is the single source of truth).
Same guarded-DDL idempotency pattern as 0110_schema_pipeline_runtime: drop the
constraint only when its definition differs, add only when absent, so the
reconciliation chain stays re-runnable. The DDL is hardcoded literal SQL (the
house convention — migrations never import app constants), with model parity
pinned by backend/tests/unit/db/test_trigger_event_vocabulary.py.
"""

from __future__ import annotations

from alembic import op

revision: str = "0176_trigger_event_validation_results"
down_revision: str | None = "0174_per_org_last_admin_guard"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Drop only when the live definition differs from the expected widened 23-value
# definition (single-quotes doubled inside the DO $$ literal), then add only
# when absent — both guarded so re-running this revision is a no-op. The
# definition shape matches 0110's exactly with 'coalesced' and
# 'backpressure_skipped' appended.
_DROP_IF_DIFFERENT = "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((validation_result)::text=ANY((ARRAY[''accepted''::character varying,''passed''::character varying,''hmac_failed''::character varying,''schema_validation_failed''::character varying,''deduplicated''::character varying,''concurrency_limit_reached''::character varying,''flood_rejected''::character varying,''timestamp_expired''::character varying,''validation_failed''::character varying,''rate_limited''::character varying,''no_match''::character varying,''condition_met''::character varying,''poll_error''::character varying,''signal_fired''::character varying,''event_type_not_accepted''::character varying,''spend_limit_reached''::character varying,''no_pipeline''::character varying,''test''::character varying,''paused''::character varying,''auto_deactivated''::character varying,''guardrail_blocked''::character varying,''coalesced''::character varying,''backpressure_skipped''::character varying])::text[])))') THEN ALTER TABLE public.trigger_events DROP CONSTRAINT IF EXISTS ck_trigger_events_validation_result; END IF; END $$;"

_ADD_IF_ABSENT = "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT ck_trigger_events_validation_result CHECK(((validation_result)::text=ANY((ARRAY['accepted'::character varying,'passed'::character varying,'hmac_failed'::character varying,'schema_validation_failed'::character varying,'deduplicated'::character varying,'concurrency_limit_reached'::character varying,'flood_rejected'::character varying,'timestamp_expired'::character varying,'validation_failed'::character varying,'rate_limited'::character varying,'no_match'::character varying,'condition_met'::character varying,'poll_error'::character varying,'signal_fired'::character varying,'event_type_not_accepted'::character varying,'spend_limit_reached'::character varying,'no_pipeline'::character varying,'test'::character varying,'paused'::character varying,'auto_deactivated'::character varying,'guardrail_blocked'::character varying,'coalesced'::character varying,'backpressure_skipped'::character varying])::text[]))); END IF; END $$;"


def upgrade() -> None:
    op.execute(_DROP_IF_DIFFERENT)
    op.execute(_ADD_IF_ABSENT)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops. The
    # pre-0176 constraint is recoverable by re-running the 0110 definition.
    pass
