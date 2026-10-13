"""Add ``policy_gates.evaluation_mode`` (FAR-939 chunk 12).

Revision ID: 0308_policy_gate_evaluation_mode
Revises: 0307_runs_guardrail_override_resume_pending
Create Date: 2026-10-13

Chunk 12 makes human review a NATIVE evaluation mode for a Policy Gate: a
gate whose ``evaluation_mode`` is ``'human'`` takes its evaluation result
from a person at the existing HITL pause instead of from the automated eval
loop. This migration adds the decision-layer column that carries the mode::

    policy_gates.evaluation_mode TEXT NOT NULL DEFAULT 'automatic'
    CHECK ck_policy_gates_evaluation_mode
        (evaluation_mode IN ('automatic', 'human'))

* ``'automatic'`` — current behaviour: the gate evaluates via automated
  evals (``resolve_policy_gate`` over an ``EvalResult``).
* ``'human'`` — the gate's evaluation result is the human's decision at the
  HITL review (resolved by ``resolve_human_evaluation``); the automated
  evals governed by the gate are skipped entirely.

``NOT NULL DEFAULT 'automatic'`` makes every pre-existing row (and every
insert that omits the column — including OLD containers during a rolling
deploy, mirroring the 0272 ``enabled`` precedent) automatic, i.e. today's
behaviour. The CHECK is added AFTER the column so every backfilled row
already satisfies the predicate.

This migration ADDS A COLUMN ONLY. It never re-creates ``resolved_action``
or any payload column — those shipped in ``0261_decision_record_payload``
(chunk 4 / FAR-1102); re-creating them would fail.

Dialect-guarded (0274/0305 pattern): the CHECK is added NOT VALID then
VALIDATEd on Postgres (existence-gated, table-qualified); SQLite uses batch
mode (it has no ``ALTER TABLE ... ADD CONSTRAINT``). The column follows the
0265 pattern: raw ``ADD COLUMN IF NOT EXISTS`` under ``_is_postgres()``,
because SQLite has no ``ADD COLUMN IF NOT EXISTS`` either — SQLite gets the
column through ``batch_alter_table`` (table recreate from the reflected
schema). The downgrade drops the CHECK then the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0308_policy_gate_evaluation_mode"
down_revision: str | None = "0307_runs_guardrail_override_resume_pending"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "policy_gates"
_COLUMN = "evaluation_mode"
_CHECK_NAME = "ck_policy_gates_evaluation_mode"
#: Literal DDL only (no f-string/format-built SQL — migration-fstring-sql
#: semgrep rule). Existence-gated + table-qualified so a same-named CHECK on
#: another table cannot make the gate silently skip.
_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_policy_gates_evaluation_mode' "
    "AND conrelid = 'public.policy_gates'::regclass) "
    "THEN ALTER TABLE public.policy_gates ADD CONSTRAINT "
    "ck_policy_gates_evaluation_mode CHECK ("
    "evaluation_mode IN ('automatic', 'human')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_policy_gates_evaluation_mode' "
    "AND conrelid = 'public.policy_gates'::regclass "
    "AND NOT convalidated) "
    "THEN ALTER TABLE public.policy_gates "
    "VALIDATE CONSTRAINT ck_policy_gates_evaluation_mode; END IF; END $$;"
)
_DROP_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_policy_gates_evaluation_mode' "
    "AND conrelid = 'public.policy_gates'::regclass) "
    "THEN ALTER TABLE public.policy_gates "
    "DROP CONSTRAINT ck_policy_gates_evaluation_mode; END IF; END $$;"
)

#: SQLite batch-mode predicate — must match the model CheckConstraint
#: EXACTLY so metadata.create_all() / SQLite-mirror CTAS parity holds.
_CHECK_PREDICATE = "evaluation_mode IN ('automatic', 'human')"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # 1. Column. Postgres: existence-gated raw DDL. NOT NULL DEFAULT
    #    'automatic': every pre-existing row reads 'automatic' (today's
    #    behaviour), and an OLD container INSERT that omits the column
    #    during a rolling deploy stores 'automatic' rather than failing
    #    NOT NULL. SQLite has NO ``ADD COLUMN IF NOT EXISTS`` (it would
    #    raise a syntax error), so the SQLite path adds the column through
    #    batch mode — table recreate from the reflected schema, the 0265
    #    pattern; the server default backfills existing rows.
    if _is_postgres():
        op.execute(
            sa.text(
                "ALTER TABLE policy_gates ADD COLUMN IF NOT EXISTS evaluation_mode TEXT NOT NULL DEFAULT 'automatic'"
            )
        )
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(sa.Column(_COLUMN, sa.Text(), nullable=False, server_default="automatic"))

    # 2. CHECK — Postgres: NOT VALID + VALIDATE (0274 pattern); SQLite: batch.
    if _is_postgres():
        op.execute(sa.text(_ADD_CHECK_NOT_VALID))
        op.execute(sa.text(_VALIDATE_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(_CHECK_NAME, _CHECK_PREDICATE)


def downgrade() -> None:
    if _is_postgres():
        op.execute(sa.text(_DROP_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(_CHECK_NAME, type_="check")

    if _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
    else:
        op.execute(sa.text("ALTER TABLE policy_gates DROP COLUMN IF EXISTS evaluation_mode"))
