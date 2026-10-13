"""Add the ``escalate`` guardrail action to the policy-gate vocabularies (FAR-1694 S1).

Revision ID: 0305_guardrail_escalate_action
Revises: 0304_metrics_staging_recorded_at_index
Create Date: 2026-10-13

FAR-1694 introduces an in-run "escalate to human" guardrail outcome. Slice S1
is the vocabulary half and is behaviour-neutral: the action is accepted
everywhere, but nothing yet suspends a run (the bridge protocol, mid-loop
suspension and resume semantics are later slices).

Two DB CHECK-constrained action vocabularies are widened in this ONE migration:

* ``policy_gates.action`` (``ck_policy_gates_action``, created by 0250):
  ``('warn', 'block')`` -> ``('warn', 'block', 'escalate')``.
* ``policy_gate_decisions.resolved_action``
  (``ck_policy_gate_decisions_resolved_action``, created by 0261):
  ``('continue', 'warn', 'block')`` -> ``('continue', 'warn', 'block',
  'escalate')``.

Both are strict WIDENINGS, so every existing row satisfies the new predicate
and re-validation passes. A CHECK cannot be ALTERed in place on Postgres, so
each is DROP + ADD: the DROP is existence-gated and the ADD is ``NOT VALID``
then ``VALIDATEd`` so the ``VALIDATE`` takes only ``SHARE UPDATE EXCLUSIVE``
rather than blocking writes (the 0265/0274 idiom). SQLite has no
``ALTER TABLE ... ADD CONSTRAINT``, so it goes through batch mode (the
0262/0265 pattern).

The DOWN path narrows the vocabularies back. It FAILS LOUDLY if a row already
holds ``'escalate'`` — the correct behaviour: a downgrade must never silently
strand a value the code may have written. S1 writes no such row, so the down
path is clean in practice.

The shipped migrations 0250/0261 are NEVER edited — this is a new revision on
the current head (DB-vocabulary rule: the constant and the widening land in the
same change).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0305_guardrail_escalate_action"
down_revision: str | None = "0304_metrics_staging_recorded_at_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_POLICY_GATES = "policy_gates"
_POLICY_GATE_DECISIONS = "policy_gate_decisions"
_GATES_CHECK = "ck_policy_gates_action"
_DECISIONS_CHECK = "ck_policy_gate_decisions_resolved_action"

# Widened predicates (upgrade) and the original 0250/0261 predicates (downgrade).
# Literal strings only — an f-string/format-built SQL statement is blocked by the
# migration-fstring-sql semgrep rule in this directory.
_GATES_ACTION_WIDENED = "action IN ('warn', 'block', 'escalate')"
_GATES_ACTION_ORIGINAL = "action IN ('warn', 'block')"
_DECISIONS_ACTION_WIDENED = "resolved_action IN ('continue', 'warn', 'block', 'escalate')"
_DECISIONS_ACTION_ORIGINAL = "resolved_action IN ('continue', 'warn', 'block')"

# Postgres DDL — one statement per element (a multi-statement execute string
# cannot be sent as a single prepared statement through asyncpg). Each pair of
# DROP/ADD is existence-gated + NOT VALID then VALIDATEd.
_PG_WIDEN_UP: tuple[str, ...] = (
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_policy_gates_action' "
        "AND conrelid='public.policy_gates'::regclass) "
        "THEN ALTER TABLE public.policy_gates DROP CONSTRAINT ck_policy_gates_action; END IF; END $$;"
    ),
    (
        "ALTER TABLE public.policy_gates ADD CONSTRAINT ck_policy_gates_action "
        "CHECK (action IN ('warn', 'block', 'escalate')) NOT VALID;"
    ),
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_policy_gates_action' "
        "AND conrelid='public.policy_gates'::regclass AND NOT convalidated) "
        "THEN ALTER TABLE public.policy_gates VALIDATE CONSTRAINT ck_policy_gates_action; END IF; END $$;"
    ),
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='ck_policy_gate_decisions_resolved_action' "
        "AND conrelid='public.policy_gate_decisions'::regclass) "
        "THEN ALTER TABLE public.policy_gate_decisions "
        "DROP CONSTRAINT ck_policy_gate_decisions_resolved_action; END IF; END $$;"
    ),
    (
        "ALTER TABLE public.policy_gate_decisions "
        "ADD CONSTRAINT ck_policy_gate_decisions_resolved_action "
        "CHECK (resolved_action IN ('continue', 'warn', 'block', 'escalate')) NOT VALID;"
    ),
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='ck_policy_gate_decisions_resolved_action' "
        "AND conrelid='public.policy_gate_decisions'::regclass AND NOT convalidated) "
        "THEN ALTER TABLE public.policy_gate_decisions "
        "VALIDATE CONSTRAINT ck_policy_gate_decisions_resolved_action; END IF; END $$;"
    ),
)

_PG_NARROW_DOWN: tuple[str, ...] = (
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_policy_gates_action' "
        "AND conrelid='public.policy_gates'::regclass) "
        "THEN ALTER TABLE public.policy_gates DROP CONSTRAINT ck_policy_gates_action; END IF; END $$;"
    ),
    (
        "ALTER TABLE public.policy_gates ADD CONSTRAINT ck_policy_gates_action "
        "CHECK (action IN ('warn', 'block')) NOT VALID;"
    ),
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_policy_gates_action' "
        "AND conrelid='public.policy_gates'::regclass AND NOT convalidated) "
        "THEN ALTER TABLE public.policy_gates VALIDATE CONSTRAINT ck_policy_gates_action; END IF; END $$;"
    ),
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='ck_policy_gate_decisions_resolved_action' "
        "AND conrelid='public.policy_gate_decisions'::regclass) "
        "THEN ALTER TABLE public.policy_gate_decisions "
        "DROP CONSTRAINT ck_policy_gate_decisions_resolved_action; END IF; END $$;"
    ),
    (
        "ALTER TABLE public.policy_gate_decisions "
        "ADD CONSTRAINT ck_policy_gate_decisions_resolved_action "
        "CHECK (resolved_action IN ('continue', 'warn', 'block')) NOT VALID;"
    ),
    (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='ck_policy_gate_decisions_resolved_action' "
        "AND conrelid='public.policy_gate_decisions'::regclass AND NOT convalidated) "
        "THEN ALTER TABLE public.policy_gate_decisions "
        "VALIDATE CONSTRAINT ck_policy_gate_decisions_resolved_action; END IF; END $$;"
    ),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _recreate_checks(
    *,
    pg_statements: tuple[str, ...],
    gates_predicate: str,
    decisions_predicate: str,
) -> None:
    """Drop + recreate both action CHECKs with the given predicates.

    Postgres runs the literal DDL statements; SQLite uses batch mode (SQLite
    has no ``ALTER TABLE ... ADD/DROP CONSTRAINT``).
    """
    if _is_postgres():
        for statement in pg_statements:
            op.execute(sa.text(statement))
        return
    if _is_sqlite():
        with op.batch_alter_table(_POLICY_GATES) as batch_op:
            batch_op.drop_constraint(_GATES_CHECK, type_="check")
            batch_op.create_check_constraint(_GATES_CHECK, gates_predicate)
        with op.batch_alter_table(_POLICY_GATE_DECISIONS) as batch_op:
            batch_op.drop_constraint(_DECISIONS_CHECK, type_="check")
            batch_op.create_check_constraint(_DECISIONS_CHECK, decisions_predicate)


def upgrade() -> None:
    _recreate_checks(
        pg_statements=_PG_WIDEN_UP,
        gates_predicate=_GATES_ACTION_WIDENED,
        decisions_predicate=_DECISIONS_ACTION_WIDENED,
    )


def downgrade() -> None:
    # Narrowing FAILS LOUDLY if a row holds 'escalate' — see the module docstring.
    _recreate_checks(
        pg_statements=_PG_NARROW_DOWN,
        gates_predicate=_GATES_ACTION_ORIGINAL,
        decisions_predicate=_DECISIONS_ACTION_ORIGINAL,
    )
