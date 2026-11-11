"""Widen ``ck_runs_cancel_reason`` to the current HITL vocabulary (FAR-1406).

Revision ID: 0275_run_cancel_reason_vocabulary
Revises: 0274_policy_gate_pin_fingerprint_operator_control
Create Date: 2026-10-02

FAR-1104 (PR #1025) renamed the HITL cancel-reason vocabulary from
``hitl_gate_expired`` / ``hitl_gate_missing`` to ``hitl_review_expired`` /
``hitl_review_missing`` by editing the SHIPPED migration 0260 in place. Any
database that had already run 0260 kept the PRE-rename constraint text, while
a fresh checkout (every CI/integration database) replays the renamed text —
the two worlds silently diverged. On the deployed side the code now writes a
value the CHECK forbids: ``_terminalize_expired_hitl_reviews`` binds
``cancel_reason='hitl_review_expired'`` and every sweep tick dies on
``violates check constraint "ck_runs_cancel_reason"`` — an expired HITL gate
can never be terminalized (confirmed live on staging, FAR-1406).

This migration reconciles and repairs, in one transaction:

1. DROP the stale constraint — definition-gated: only dropped when its
   definition still lacks the current vocabulary, so a database whose 0260
   already ships the renamed text (a fresh CI/integration DB) is left
   untouched and re-running this revision stays a no-op;
2. RECONCILE legacy rows (``hitl_gate_expired`` -> ``hitl_review_expired``,
   ``hitl_gate_missing`` -> ``hitl_review_missing``) — necessarily AFTER the
   drop, because the new values are exactly what the stale CHECK forbids;
3. re-ADD ``ck_runs_cancel_reason`` over the CURRENT vocabulary
   (``user_requested``, ``agent_requested``, ``hitl_review_expired``,
   ``hitl_review_missing``; NULL still allowed — pre-0260 runs keep their
   "reason not recorded" NULL by design) as NOT VALID: existence-gated
   (``pg_constraint`` guard, the 0260 idempotent style) and instant (no scan);
4. VALIDATE in a separate guarded step (SHARE UPDATE EXCLUSIVE — non-blocking
   for INSERTs), the lock-safety pattern from 0176/0255/0256/0260.

The CHECK stays migration-owned: the ORM does not duplicate it (repo parity
rule — see ``tests/integration/test_initial_migration.py::_MIGRATION_OWNED_CHECKS``).

Downgrade is a no-op (reconciliation-chain convention, 0108+): narrowing the
CHECK back would re-break the shipped code and strand the reconciled rows.
"""

from __future__ import annotations

from alembic import op

revision: str = "0275_run_cancel_reason_vocabulary"
down_revision: str | None = "0274_policy_gate_pin_fingerprint_operator_control"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# 1. Drop the stale definition ONLY when it is actually stale — a database
#    whose ck_runs_cancel_reason already carries the current vocabulary (every
#    fresh DB: 0260's file text was renamed by PR #1025) keeps its constraint,
#    so the revision is idempotent end-to-end.
_DROP_STALE_CHECK = (
    "DO $$ DECLARE def text; BEGIN "
    "SELECT pg_get_constraintdef(oid) INTO def FROM pg_constraint "
    "WHERE conname='ck_runs_cancel_reason' AND conrelid='public.runs'::regclass; "
    "IF def IS NOT NULL AND (def NOT LIKE '%hitl_review_expired%' "
    "OR def NOT LIKE '%hitl_review_missing%') THEN "
    "ALTER TABLE public.runs DROP CONSTRAINT ck_runs_cancel_reason; "
    "END IF; END $$;"
)

# 2. Reconcile the deployed pre-rename rows. Runs BEFORE the drop would be
#    rejected by the stale CHECK (the new value is exactly what it forbids),
#    and a re-added new-vocabulary CHECK would reject the old values — hence
#    sandwiched between steps 1 and 3.
_RECONCILE_EXPIRED = (
    "UPDATE public.runs SET cancel_reason='hitl_review_expired' WHERE cancel_reason='hitl_gate_expired';"
)
_RECONCILE_MISSING = (
    "UPDATE public.runs SET cancel_reason='hitl_review_missing' WHERE cancel_reason='hitl_gate_missing';"
)

# 3. Existence-gated CHECK over the current vocabulary (NULL allowed), added
#    NOT VALID (instant) then VALIDATEd in a separate guarded step — the same
#    lock-safety pattern 0260 introduced, so a big `runs` table never takes a
#    long ACCESS EXCLUSIVE lock scan.
_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_cancel_reason' "
    "AND conrelid='public.runs'::regclass) "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_cancel_reason CHECK ("
    "cancel_reason IS NULL OR "
    "cancel_reason IN ('user_requested', 'agent_requested', 'hitl_review_expired', 'hitl_review_missing')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_cancel_reason' "
    "AND conrelid='public.runs'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.runs VALIDATE CONSTRAINT "
    "ck_runs_cancel_reason; END IF; END $$;"
)


def upgrade() -> None:
    op.execute(_DROP_STALE_CHECK)
    op.execute(_RECONCILE_EXPIRED)
    op.execute(_RECONCILE_MISSING)
    op.execute(_ADD_CHECK_NOT_VALID)
    op.execute(_VALIDATE_CHECK)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops.
    pass
