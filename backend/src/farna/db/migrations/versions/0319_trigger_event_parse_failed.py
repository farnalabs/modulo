"""Widen ck_trigger_events_validation_result for the Slack parse-failure label.

Revision ID: 0319_trigger_event_parse_failed
Revises: 0318_trigger_event_required_refs_missing
Create Date: 2026-10-17

The Slack ``app_mention`` engine audits a malformed payload with a
``parse_failed`` TriggerEvent (``core/trigger_engine/slack_app_mention.py``,
``_parse_mention``), but the value was never added to the
``ck_trigger_events_validation_result`` vocabulary. The CHECK is a HARD DB
gate, so on any real database the INSERT raised ``IntegrityError`` — the
parse-failure audit event could never persist and the path surfaced as a DB
error instead of the intended Slack error response. This migration adds the
value to the ORM vocabulary and widens the CHECK in the same change (the value
a write site emits must be in the vocabulary).

``parse_failed`` — an ``app_mention`` payload could not be parsed, so the
delivery was refused and audited; the sender must correct the payload.

Lock-safety (same pattern as 0176/0255/0318): the widened CHECK is added ``NOT
VALID`` (instant, brief ACCESS EXCLUSIVE) then ``VALIDATE CONSTRAINT`` runs in a
separate guarded step (SHARE UPDATE EXCLUSIVE — non-blocking for INSERTs).
"""

from __future__ import annotations

from alembic import op

revision: str = "0319_trigger_event_parse_failed"
down_revision: str | None = "0318_trigger_event_required_refs_missing"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Drop only when the live definition differs from the expected widened 26-value
# definition (single-quotes doubled inside the DO $$ literal), then add only
# when absent — both guarded so re-running this revision is a no-op.
_DROP_IF_DIFFERENT = "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((validation_result)::text=ANY((ARRAY[''accepted''::charactervarying,''passed''::charactervarying,''hmac_failed''::charactervarying,''schema_validation_failed''::charactervarying,''deduplicated''::charactervarying,''concurrency_limit_reached''::charactervarying,''flood_rejected''::charactervarying,''timestamp_expired''::charactervarying,''validation_failed''::charactervarying,''rate_limited''::charactervarying,''no_match''::charactervarying,''condition_met''::charactervarying,''poll_error''::charactervarying,''signal_fired''::charactervarying,''event_type_not_accepted''::charactervarying,''spend_limit_reached''::charactervarying,''no_pipeline''::charactervarying,''test''::charactervarying,''paused''::charactervarying,''auto_deactivated''::charactervarying,''guardrail_blocked''::charactervarying,''coalesced''::charactervarying,''backpressure_skipped''::charactervarying,''event_value_filter_not_accepted''::charactervarying,''required_refs_missing''::charactervarying,''parse_failed''::charactervarying])::text[])))') THEN ALTER TABLE public.trigger_events DROP CONSTRAINT IF EXISTS ck_trigger_events_validation_result; END IF; END $$;"

_ADD_IF_ABSENT_NOT_VALID = "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT ck_trigger_events_validation_result CHECK(((validation_result)::text=ANY((ARRAY['accepted'::character varying,'passed'::character varying,'hmac_failed'::character varying,'schema_validation_failed'::character varying,'deduplicated'::character varying,'concurrency_limit_reached'::character varying,'flood_rejected'::character varying,'timestamp_expired'::character varying,'validation_failed'::character varying,'rate_limited'::character varying,'no_match'::character varying,'condition_met'::character varying,'poll_error'::character varying,'signal_fired'::character varying,'event_type_not_accepted'::character varying,'spend_limit_reached'::character varying,'no_pipeline'::character varying,'test'::character varying,'paused'::character varying,'auto_deactivated'::character varying,'guardrail_blocked'::character varying,'coalesced'::character varying,'backpressure_skipped'::character varying,'event_value_filter_not_accepted'::character varying,'required_refs_missing'::character varying,'parse_failed'::character varying])::text[]))) NOT VALID; END IF; END $$;"

_VALIDATE_IF_NEEDED = "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result' AND NOT convalidated) THEN ALTER TABLE public.trigger_events VALIDATE CONSTRAINT ck_trigger_events_validation_result; END IF; END $$;"


def upgrade() -> None:
    op.execute(_DROP_IF_DIFFERENT)
    op.execute(_ADD_IF_ABSENT_NOT_VALID)
    op.execute(_VALIDATE_IF_NEEDED)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops.
    pass
