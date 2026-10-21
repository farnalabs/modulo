"""Eval judge verdict contract: ``outcome``, error state, judge envelope (ADR 066 S1).

Revision ID: 0325_eval_verdict_contract
Revises: 0324_purge_eval_definitions
Create Date: 2026-10-21

Three changes, one cutover:

1. **``llm_judge`` -> ``model_judge`` (ADR 066 D2).** The stored eval-type name
   was a false claim for a non-LLM judge. The data is renamed and the CHECK
   vocabulary swapped in the same migration. No alias: a wire alias would
   recreate the dual vocabulary the ADR removes. This is the one cutover that
   resets suite baselines (the definition checksum contains ``eval_type``), and
   it resets them exactly once.
2. **``eval_results.outcome`` (D5/D6).** A third state — ``pass`` | ``fail`` |
   ``error`` — with ``error`` meaning *no verdict was produced*. Backfilled from
   ``passed`` and then set NOT NULL **with no server default**, so a write that
   forgets the outcome fails instead of recording a failure.
3. **Verdict provenance (D5).** ``judge_result_json`` (schema-versioned
   envelope, physical ``jsonb``), ``judge_confidence_kind`` (closed
   vocabulary) and ``error_code`` (the coded reason, NULL on pass/fail).

Online-safety (FAR-403 class — a previous hot-table rewrite rolled back a
deploy here). ``eval_results`` and ``evals`` are live tables, so this migration
never holds ACCESS EXCLUSIVE across a scan or a data write:

* every ``ADD COLUMN`` and the vocabulary CHECK swap run on the dedicated
  AUTOCOMMIT connection (metadata-only DDL; the lock is released at once).
  ``alembic``'s ``autocommit_block()`` is NOT usable here: ``env.py`` wraps the
  whole upgrade in one transaction, and the repo's own migrations document the
  same constraint (``0193``/``0200``);
* both data rewrites are bounded, re-runnable ``ctid`` batches on a DEDICATED
  autocommit connection (never Alembic's connection — committing that would
  close the migration transaction and the next statement would raise
  ``Can't operate on closed transaction``);
* ``outcome`` is set NOT NULL only after a temporary
  ``CHECK (outcome IS NOT NULL)`` has been added ``NOT VALID`` and validated,
  so PostgreSQL proves the invariant from the check and skips the full scan;
* the final ``outcome`` and ``judge_confidence_kind`` CHECKs, and the
  vocabulary CHECK, are added ``NOT VALID`` and then validated under SHARE
  UPDATE EXCLUSIVE.

Non-Postgres backends are a no-op, matching ``0323`` and
``0324_purge_eval_definitions``: their schemas come from the ORM
``create_all``, which already models the new columns and CHECKs.

``passed`` is retained as the compatibility projection of ``outcome``; existing
readers keep working and the policy gate vocabulary is unchanged (D6).

Downgrade reverses the three changes and restores the historical vocabulary.
**After a downgrade an ``outcome='error'`` row is indistinguishable from a
genuine failure** — the previous schema has nowhere to record "no verdict was
produced", so the distinction is lost. That loss is the point of the ticket;
the backfill itself is not reversible per-row, but ``passed`` remains and is
re-derived by the previous code path.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0325_eval_verdict_contract"
down_revision: str | None = "0324_purge_eval_definitions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Bounded batch size for every data rewrite. Small enough that each batch is a
#: short row-level lock and the loop is safely re-runnable after an interruption.
_BATCH_SIZE = 1000

_OUTCOME_CHECK = "ck_eval_results_outcome"
_OUTCOME_NOT_NULL_CHECK = "ck_eval_results_outcome_not_null"
_CONFIDENCE_CHECK = "ck_eval_results_judge_confidence_kind"
_EVAL_TYPE_CHECK = "ck_evals_type"

# --- column DDL (metadata-only; each commits on its own) --------------------
_ADD_OUTCOME = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS outcome VARCHAR(16)"
_ADD_JUDGE_RESULT = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS judge_result_json JSONB"
_ADD_CONFIDENCE = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS judge_confidence_kind VARCHAR(32)"
_ADD_ERROR_CODE = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS error_code VARCHAR(64)"

# --- bounded data rewrites (executed on a dedicated autocommit connection) ---
_BACKFILL_OUTCOME = (
    "WITH batch AS (SELECT ctid FROM eval_results WHERE outcome IS NULL LIMIT 1000) "
    "UPDATE eval_results AS e "
    "SET outcome = CASE WHEN e.passed THEN 'pass' ELSE 'fail' END "
    "FROM batch WHERE e.ctid = batch.ctid "
    "RETURNING 1"
)
_RENAME_EVAL_TYPES = (
    "WITH batch AS (SELECT ctid FROM evals WHERE eval_type = 'llm_judge' LIMIT 1000) "
    "UPDATE evals AS e SET eval_type = 'model_judge' "
    "FROM batch WHERE e.ctid = batch.ctid "
    "RETURNING 1"
)
_RESTORE_EVAL_TYPES = (
    "WITH batch AS (SELECT ctid FROM evals WHERE eval_type = 'model_judge' LIMIT 1000) "
    "UPDATE evals AS e SET eval_type = 'llm_judge' "
    "FROM batch WHERE e.ctid = batch.ctid "
    "RETURNING 1"
)

# --- CHECK swaps (existence-gated, table-qualified) -------------------------
_DROP_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_type; END IF; END $$;"
)
_ADD_EVAL_TYPE_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['model_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying, "
    "'human_set'::character varying]::text[])) NOT VALID; END IF; END $$;"
)
_ADD_OLD_EVAL_TYPE_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['llm_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying, "
    "'human_set'::character varying]::text[])) NOT VALID; END IF; END $$;"
)
_VALIDATE_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass "
    "AND NOT convalidated) THEN "
    "ALTER TABLE public.evals VALIDATE CONSTRAINT ck_evals_type; END IF; END $$;"
)
_ADD_OUTCOME_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid = 'public.eval_results'::regclass) THEN "
    "ALTER TABLE public.eval_results ADD CONSTRAINT "
    "ck_eval_results_outcome_not_null CHECK (outcome IS NOT NULL) NOT VALID; END IF; END $$;"
)
_VALIDATE_OUTCOME_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid = 'public.eval_results'::regclass AND NOT convalidated) THEN "
    "ALTER TABLE public.eval_results VALIDATE CONSTRAINT ck_eval_results_outcome_not_null; "
    "END IF; END $$;"
)
_DROP_OUTCOME_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid = 'public.eval_results'::regclass) THEN "
    "ALTER TABLE public.eval_results DROP CONSTRAINT ck_eval_results_outcome_not_null; END IF; END $$;"
)
_SET_OUTCOME_NOT_NULL = "ALTER TABLE eval_results ALTER COLUMN outcome SET NOT NULL"
_DROP_OUTCOME_NOT_NULL = "ALTER TABLE eval_results ALTER COLUMN outcome DROP NOT NULL"
_ADD_OUTCOME_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome' "
    "AND conrelid = 'public.eval_results'::regclass) THEN "
    "ALTER TABLE public.eval_results ADD CONSTRAINT "
    "ck_eval_results_outcome CHECK (outcome IN ('pass', 'fail', 'error')) NOT VALID; END IF; END $$;"
)
_VALIDATE_OUTCOME_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome' "
    "AND conrelid = 'public.eval_results'::regclass AND NOT convalidated) THEN "
    "ALTER TABLE public.eval_results VALIDATE CONSTRAINT ck_eval_results_outcome; END IF; END $$;"
)
_ADD_CONFIDENCE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_judge_confidence_kind' "
    "AND conrelid = 'public.eval_results'::regclass) THEN "
    "ALTER TABLE public.eval_results ADD CONSTRAINT "
    "ck_eval_results_judge_confidence_kind CHECK ("
    "judge_confidence_kind IS NULL OR judge_confidence_kind IN "
    "('vendor_claimed_calibrated', 'model_self_reported', 'unavailable')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CONFIDENCE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_judge_confidence_kind' "
    "AND conrelid = 'public.eval_results'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.eval_results VALIDATE CONSTRAINT "
    "ck_eval_results_judge_confidence_kind; END IF; END $$;"
)
_DROP_OUTCOME_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome' "
    "AND conrelid = 'public.eval_results'::regclass) "
    "THEN ALTER TABLE public.eval_results DROP CONSTRAINT ck_eval_results_outcome; END IF; END $$;"
)
_DROP_CONFIDENCE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_judge_confidence_kind' "
    "AND conrelid = 'public.eval_results'::regclass) "
    "THEN ALTER TABLE public.eval_results DROP CONSTRAINT ck_eval_results_judge_confidence_kind; END IF; END $$;"
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _execute(sql: str) -> None:
    op.execute(sa.text(sql))


def _autocommit_connection() -> Any:
    """A dedicated AUTOCOMMIT connection for the bounded data rewrites.

    Deliberately NOT Alembic's ``op.get_bind()`` connection: committing that
    closes the migration's transaction and the next statement raises
    ``Can't operate on closed transaction``. A separate autocommit connection
    makes each batch durable and the loop resumable.
    """
    bind = op.get_bind()
    return bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _relation_visible(conn: Any) -> bool:
    """True when the AUTOCOMMIT connection can see ``eval_results``.

    On a FRESH chain the table was created by an earlier migration whose DDL is
    still UNCOMMITTED inside Alembic's single transaction, so a separate
    connection cannot see it. That only happens on a database with no traffic
    and no rows, so the caller falls back to Alembic's own connection.
    """
    return conn.execute(sa.text("SELECT to_regclass('public.eval_results')")).scalar() is not None


def _run_batches(sql: str) -> int:
    """Run a bounded ``ctid`` rewrite until it touches zero rows."""
    total = 0
    with _autocommit_connection() as conn:
        if not _relation_visible(conn):
            # Fresh chain: the table is brand new and empty (no historical rows
            # to backfill), and its DDL is not visible to this connection yet.
            logger.info("0325_eval_verdict_contract: fresh chain, no rows to backfill")
            return 0
        while True:
            touched = len(conn.execute(sa.text(sql)).fetchall())
            total += touched
            if touched == 0:
                break
    logger.info("0325_eval_verdict_contract: batch rewrite touched %d row(s)", total)
    return total


def _execute_autocommit(statements: tuple[str, ...]) -> None:
    """Run metadata-only DDL on the dedicated AUTOCOMMIT connection.

    Each statement commits immediately, so an ACCESS EXCLUSIVE lock is held for
    that statement only — never across the data backfill that follows it.
    """
    with _autocommit_connection() as conn:
        if not _relation_visible(conn):
            # Fresh chain (see _relation_visible): run the metadata DDL on the
            # migration's own connection. There is no live traffic or data yet,
            # so the AEL lock cannot block anyone.
            for statement in statements:
                op.execute(sa.text(statement))
            return
        for statement in statements:
            conn.execute(sa.text(statement))


def _add_columns_and_swap_vocabulary_check() -> None:
    """Metadata-only DDL, each committed immediately to release the AEL lock."""
    _execute_autocommit(
        (
            _ADD_OUTCOME,
            _ADD_JUDGE_RESULT,
            _ADD_CONFIDENCE,
            _ADD_ERROR_CODE,
            # Drop the old vocabulary before the rename (PostgreSQL validates
            # the old CHECK on UPDATE), then add the new one NOT VALID: existing
            # ``llm_judge`` rows are not checked until VALIDATE, but every
            # renamed row must satisfy the new vocabulary when it is written.
            _DROP_EVAL_TYPE_CHECK,
            _ADD_EVAL_TYPE_CHECK_NOT_VALID,
        )
    )


def _finalise_outcome() -> None:
    """Prove NOT NULL from a validated CHECK, then enforce it without a scan."""
    _execute(_ADD_OUTCOME_NOT_NULL_CHECK)
    _execute(_VALIDATE_OUTCOME_NOT_NULL_CHECK)
    _execute(_SET_OUTCOME_NOT_NULL)
    _execute(_DROP_OUTCOME_NOT_NULL_CHECK)
    _execute(_ADD_OUTCOME_CHECK)
    _execute(_VALIDATE_OUTCOME_CHECK)


def upgrade() -> None:
    if not _is_postgres():
        # Matching 0323 / 0324_purge_eval_definitions: the non-Postgres schema
        # comes from the ORM ``create_all``, which already models the columns
        # and CHECKs. No third behaviour is invented here.
        return

    _add_columns_and_swap_vocabulary_check()
    _run_batches(_BACKFILL_OUTCOME)
    _run_batches(_RENAME_EVAL_TYPES)
    _finalise_outcome()
    _execute(_VALIDATE_EVAL_TYPE_CHECK)
    _execute(_ADD_CONFIDENCE_CHECK)
    _execute(_VALIDATE_CONFIDENCE_CHECK)


def downgrade() -> None:
    if not _is_postgres():
        return

    _execute_autocommit((_DROP_EVAL_TYPE_CHECK, _ADD_OLD_EVAL_TYPE_CHECK_NOT_VALID))
    _run_batches(_RESTORE_EVAL_TYPES)
    _execute(_VALIDATE_EVAL_TYPE_CHECK)
    _execute_autocommit(
        (
            _DROP_CONFIDENCE_CHECK,
            _DROP_OUTCOME_CHECK,
            _DROP_OUTCOME_NOT_NULL,
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS error_code",
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS judge_confidence_kind",
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS judge_result_json",
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS outcome",
        )
    )
