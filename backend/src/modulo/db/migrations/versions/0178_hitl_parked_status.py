"""HITL capacity (FAR-604 D2): ``hitl_parked`` run status.

Revision ID: 0178_hitl_parked_status
Revises: 0177_invitations
Create Date: 2026-09-05

Renumber note: originally ``0177_hitl_parked_status`` chained off
``0176_trigger_event_validation_results``; main's ``0177_invitations`` took
the same number, so this is renumbered to ``0178`` and re-parented onto
``0177_invitations`` to keep the chain linear (the standard collision
renumber flow).

FAR-604 D2 (HITL capacity design) introduces the park-on-expiry sweep: a run
whose HITL gate expired unanswered past the grace window (settings
``HITL_PARK_GRACE_SECONDS``, default 24h) is transitioned out of
``awaiting_human`` into the dedicated non-terminal ``hitl_parked`` status so it
stops occupying review state while the gate stays OPEN AND CLAIMABLE.

One schema change (qa F13 dropped the originally-proposed
``hitl_claims.parked_at`` column: the ``hitl_parked`` STATUS carries the
parked signal — the column was unread by any consumer, went stale on a
re-park, and added schema + phantom-count surface):

1. **``ck_runs_status``** - extend the CHECK constraint to admit
   ``hitl_parked``. The recreated list is the UNION of the live 14-status list
   (as of 0159/0176) plus ``hitl_parked`` - 15 statuses total. The
   drop-if-differs / add-if-absent guard pattern (0110 lineage) keeps the
   migration idempotent and rolling-deploy safe: the widened constraint is a
   backward-compatible SUPERSET, so the overlap window where new app code
   emits ``hitl_parked`` while the constraint is still the OLD list is
   write-safe only after the migration lands (pre-existing rows never carry
   the new status).

   The ADD is staged ``NOT VALID`` + ``VALIDATE CONSTRAINT`` (qa F8):
   ``ADD CONSTRAINT ... NOT VALID`` takes only a SHARE UPDATE EXCLUSIVE lock
   (online — it does not block reads/writes the way a validated ADD's full
   table scan under ACCESS EXCLUSIVE would on a large ``runs`` table), and
   ``VALIDATE CONSTRAINT`` then proves the pre-existing rows against the
   widened list under the same weak lock. The widened list is a strict
   SUPERSET of the live list, so pre-existing rows (all carrying
   pre-widening statuses) cannot violate it — VALIDATE can never fail; it
   just performs the online proof.

Model parity is pinned by backend/tests/unit/db/test_run_status_vocabulary.py
(the model ``ck_runs_status`` string is the single source of truth).
"""

from __future__ import annotations

from alembic import op

revision: str = "0178_hitl_parked_status"
down_revision: str | None = "0177_invitations"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The NEW target list: the live 14-status list (0159 union state, unchanged by
# 0160-0176) plus ``hitl_parked`` - 15 statuses total.
#
# Guard comparison (qa F8): the live definition may or may not carry the
# ``NOT VALID`` suffix (a partially-run upgrade leaves ADD..NOT VALID
# committed with VALIDATE pending). The whitespace-stripped definition is
# additionally stripped of a trailing ``NOT VALID`` so BOTH forms compare
# equal to the expected literal — the drop-if-differs guard cannot miss a
# NOT VALID-constrained constraint (which would then collide with the re-ADD).
_DROP_NEW = (
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
# ADD (idempotent, staged): re-add the constraint with the NEW list if it is
# now absent — staged NOT VALID so the append takes only a SHARE UPDATE
# EXCLUSIVE lock (online). VALIDATE below performs the online proof.
_ADD_NEW = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'unknown'::character varying, 'hitl_parked'::character varying, "
    "'complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, "
    "'cost_ceiling_exceeded'::character varying, 'router_no_match'::character varying, "
    "'compensation_failed'::character varying])::text[]))) NOT VALID; "
    "END IF; END $$;"
)
# VALIDATE (idempotent): prove the pre-existing rows against the widened
# list under SHARE UPDATE EXCLUSIVE. The new list is a strict superset of
# any prior list, so pre-existing rows can never violate it. Guarded on the
# NOT VALID marker so an already-validated constraint is not re-scanned.
_VALIDATE_NEW = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' "
    "AND pg_get_constraintdef(oid) LIKE '%NOT VALID') "
    "THEN ALTER TABLE public.runs VALIDATE CONSTRAINT ck_runs_status; END IF; END $$;"
)
# The pre-0177 list (the live 14-status set) for the downgrade path. The same
# NOT-VALID-tolerant normalisation (qa F8): the live constraint at downgrade
# time may still carry ``NOT VALID`` (upgrade validated but a residual marker
# form) and must still be matched + dropped.
_DROP_OLD = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g'), 'NOTVALID$', '') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''unknown''::charactervarying,"
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
    "'claimed'::character varying, 'unknown'::character varying, "
    "'complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, "
    "'cost_ceiling_exceeded'::character varying, 'router_no_match'::character varying, "
    "'compensation_failed'::character varying])::text[]))); "
    "END IF; END $$;"
)


def upgrade() -> None:
    # Widen ck_runs_status to the superset (idempotent drop-if-differs /
    # staged add-if-absent + guarded online validate).
    op.execute(_DROP_NEW)
    op.execute(_ADD_NEW)
    op.execute(_VALIDATE_NEW)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): the downgrade restores the
    # pre-0177 schema. Parked rows cannot be downgraded to a legal status
    # value without losing information, so any residual ``hitl_parked`` rows
    # are moved back to ``awaiting_human`` FIRST (the constraint recreation
    # would otherwise fail on them).
    op.execute("UPDATE runs SET status = 'awaiting_human' WHERE status = 'hitl_parked'")
    op.execute(_DROP_OLD)
    op.execute(_ADD_OLD)
