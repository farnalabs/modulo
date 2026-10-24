"""Stage 2: ``eval_results.outcome`` NOT NULL + narrow ``ck_evals_type`` (ADR 066 S1).

Revision ID: 0331_eval_outcome_not_null_narrow_vocab
Revises: 0330_eval_verdict_contract
Create Date: 2026-10-24

This migration follows the EXPAND release (0330_eval_verdict_contract) and runs
AFTER every machine has rolled to the new image. It implements:

1. **Readiness gate** — assert ``SELECT count(*) FROM eval_results WHERE outcome IS NULL`` = 0
   and ``SELECT count(*) FROM evals WHERE eval_type = 'llm_judge'`` = 0 before
   proceeding. Older-image inserts during the rollout window can leave NULL
   outcomes and ``llm_judge`` rows behind.

2. **``eval_results.outcome`` NOT NULL via validated CHECK** — add
   ``CHECK (outcome IS NOT NULL)``, ``VALIDATE`` it (scans under SHARE UPDATE
   EXCLUSIVE, no DML block), then ``ALTER COLUMN ... SET NOT NULL`` (metadata
   only, skips the full scan because the validated CHECK proves every row
   satisfies it), then drop the CHECK. Avoids the full-table scan that a
   bare ``SET NOT NULL`` would require.

3. **Narrow ``evals`` vocabulary** — drop the TRANSITIONAL CHECK that accepted
   both ``llm_judge`` and ``model_judge``, add the FINAL CHECK with the
   narrowed vocabulary (no ``llm_judge``), ``VALIDATE`` it. Runs on the
   migration connection with ``lock_timeout``.

Locking / online safety (FAR-403 class):

* ``evals`` DDL stays on the **migration connection** (same transaction as
  ``0328_evidence_index_aggregate_indexes`` and ``0324_purge_eval_definitions``).
  A side connection taking ACCESS EXCLUSIVE there would wait on a lock this
  transaction holds — a self-deadlock.
* ``eval_results`` DDL runs on a dedicated AUTOCOMMIT connection: every
  ``ALTER`` commits immediately so its ACCESS EXCLUSIVE is held for that
  statement only, never across the following ``VALIDATE`` scan.
* Both connections set an explicit ``lock_timeout`` so a future conflict fails
  loudly instead of hanging.

Non-Postgres backends are a no-op, matching ``0323`` and ``0324_purge``:
their schemas come from the ORM ``create_all``.

Downgrade reverses the column NOT NULL and restores the TRANSITIONAL vocabulary.
**After a downgrade an ``outcome='error'`` row is indistinguishable from a
genuine failure** — the previous schema has nowhere to record "no verdict was
produced", so the distinction is lost. That loss is the point of the ticket;
the backfill is not reversible per-row, but ``passed`` remains and is re-derived
by the previous code path.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0331_eval_outcome_not_null_narrow_vocab"
down_revision: str | None = "0330_eval_verdict_contract"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Fail loudly rather than wait forever on a lock the migration cannot get.
_SET_MIGRATION_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '15s'"
_SET_SIDE_LOCK_TIMEOUT = "SET lock_timeout = '15s'"

_OUTCOME_NOT_NULL_CHECK = "ck_eval_results_outcome_not_null"
_EVAL_TYPE_CHECK = "ck_evals_type"

# --- Final evals vocabulary (no llm_judge) -----------------------------------
_FINAL_EVAL_TYPES = (
    "'model_judge'::character varying, "
    "'regex'::character varying, "
    "'json_schema'::character varying, "
    "'custom_function'::character varying, "
    "'guardrail'::character varying, "
    "'human_set'::character varying"
)

# --- evals vocabulary narrowing (on migration connection) --------------------
_DROP_TRANSITIONAL_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_type' "
    "AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_type; END IF; END $$;"
)
_ADD_FINAL_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['model_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying, "
    "'human_set'::character varying]::text[])) NOT VALID; END IF; END $$;"
)
_VALIDATE_FINAL_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass "
    "AND NOT convalidated) THEN "
    "ALTER TABLE public.evals VALIDATE CONSTRAINT ck_evals_type; END IF; END $$;"
)

# --- eval_results outcome NOT NULL (on AUTOCOMMIT side connection) -----------
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
    "ALTER TABLE public.eval_results VALIDATE CONSTRAINT ck_eval_results_outcome_not_null; END IF; END $$;"
)
_DROP_OUTCOME_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid = 'public.eval_results'::regclass) "
    "THEN ALTER TABLE public.eval_results DROP CONSTRAINT ck_eval_results_outcome_not_null; END IF; END $$;"
)
_SET_OUTCOME_NOT_NULL = "ALTER TABLE eval_results ALTER COLUMN outcome SET NOT NULL"

# --- Restore TRANSITIONAL evals vocabulary (for downgrade) -------------------
_ADD_TRANSITIONAL_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['llm_judge'::character varying, "
    "'model_judge'::character varying, 'regex'::character varying, "
    "'json_schema'::character varying, 'custom_function'::character varying, "
    "'guardrail'::character varying, 'human_set'::character varying]::text[])) "
    "NOT VALID; END IF; END $$;"
)
_VALIDATE_TRANSITIONAL_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass "
    "AND NOT convalidated) THEN "
    "ALTER TABLE public.evals VALIDATE CONSTRAINT ck_evals_type; END IF; END $$;"
)

# --- Restore eval_results outcome nullable (for downgrade) -------------------
_DROP_OUTCOME_NOT_NULL = "ALTER TABLE eval_results ALTER COLUMN outcome DROP NOT NULL"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _set_migration_lock_timeout() -> None:
    """Bound every lock wait on the migration transaction (fail loud)."""
    op.execute(sa.text(_SET_MIGRATION_LOCK_TIMEOUT))


def _autocommit_connection() -> Any:
    """A dedicated AUTOCOMMIT connection for the ``eval_results`` work.

    Deliberately NOT Alembic's connection: committing that closes the migration
    transaction (``Can't operate on closed transaction``). The lock timeout
    makes a future conflict a clear error rather than a hang.
    """
    bind = op.get_bind()
    conn = bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(sa.text(_SET_SIDE_LOCK_TIMEOUT))
    return conn


def _relation_visible(conn: Any) -> bool:
    """True when the AUTOCOMMIT connection can see ``eval_results``.

    On a FRESH chain the table was created by an earlier migration whose DDL is
    still uncommitted inside Alembic's transaction, so a side connection cannot
    see it. That only happens on a database with no traffic and no data.
    """
    return conn.execute(sa.text("SELECT to_regclass('public.eval_results')")).scalar() is not None


def _migration_transaction_locks_eval_results() -> bool:
    """True when Alembic's own transaction already locks ``eval_results``.

    An earlier revision in the SAME transaction can take a lock the side
    connection cannot get past: ``0328_evidence_index_aggregate_indexes``
    creates an index on ``eval_results``, holding SHARE until the transaction
    ends, so the side connection's ACCESS EXCLUSIVE ``ALTER`` would wait on a
    lock this very transaction holds - a self-deadlock bounded by
    ``lock_timeout``. A chain from ``0328`` alone does not hit it; an upgrade
    that applies ``0328`` and this revision together does.
    """
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() "
                "AND relation = to_regclass('public.eval_results')"
            )
        )
        .scalar()
    )
    return bool(count)


def _side_connection_can_write_eval_results(conn: Any) -> bool:
    """True when the side connection can run ``eval_results`` DDL/DML.

    False on a FRESH chain (the table is still uncommitted, so invisible to
    another session) and when this transaction already locks the table (the
    ``0328`` index). Both cases fall back to the migration connection, which
    can upgrade its own lock.
    """
    return _relation_visible(conn) and not _migration_transaction_locks_eval_results()


def _execute_migration(statements: tuple[str, ...]) -> None:
    """Run DDL/DML on the migration connection (``evals`` always goes here)."""
    for statement in statements:
        op.execute(sa.text(statement))


def _execute_side(statements: tuple[str, ...]) -> None:
    """Run ``eval_results`` DDL on the AUTOCOMMIT side connection.

    Each statement commits on its own, so an ACCESS EXCLUSIVE is released before
    the next statement's ``VALIDATE`` scan. On a fresh chain (table not visible)
    or when this transaction already locks the table, the statements run on the
    migration connection instead.
    """
    with _autocommit_connection() as conn:
        if not _side_connection_can_write_eval_results(conn):
            for statement in statements:
                op.execute(sa.text(statement))
            return
        for statement in statements:
            conn.execute(sa.text(statement))


def _readiness_check() -> None:
    """Assert no NULL outcomes and no llm_judge rows before proceeding.

    Older-image inserts during the rollout window can leave both behind.
    This is the Stage 2 gate — if either count is non-zero, the migration
    fails loudly and the deploy should be held until the fleet has fully
    rolled over and any straggler rows are cleaned up.
    """
    bind = op.get_bind()

    null_outcomes = bind.execute(sa.text("SELECT count(*) FROM eval_results WHERE outcome IS NULL")).scalar()
    if null_outcomes and null_outcomes > 0:
        raise RuntimeError(
            f"READINESS FAIL: eval_results has {null_outcomes} row(s) with NULL outcome. "
            "Stage 2 requires all machines to have rolled to the new image so that "
            "every writer sets outcome. Wait for full rollout or clean up stragglers "
            "before running this migration."
        )

    llm_judge_rows = bind.execute(sa.text("SELECT count(*) FROM evals WHERE eval_type = 'llm_judge'")).scalar()
    if llm_judge_rows and llm_judge_rows > 0:
        raise RuntimeError(
            f"READINESS FAIL: evals has {llm_judge_rows} row(s) with eval_type='llm_judge'. "
            "Stage 2 requires all machines to have rolled to the new image so that "
            "every writer uses 'model_judge'. Wait for full rollout or clean up stragglers "
            "before running this migration."
        )

    logger.info(
        "0331_eval_outcome_not_null_narrow_vocab: readiness gate passed (NULL outcomes=%d, llm_judge rows=%d)",
        null_outcomes or 0,
        llm_judge_rows or 0,
    )


def upgrade() -> None:
    if not _is_postgres():
        # Matching 0323 / 0324_purge: the non-Postgres schema comes from the
        # ORM create_all.
        return

    _set_migration_lock_timeout()

    # READINESS GATE — must pass before any DDL
    _readiness_check()

    # 1. evals vocabulary narrowing (on the migration connection).
    #    Drop the TRANSITIONAL CHECK (accepting both llm_judge and model_judge),
    #    add the FINAL CHECK with narrowed vocabulary, VALIDATE it.
    _execute_migration((_DROP_TRANSITIONAL_EVAL_TYPE_CHECK, _ADD_FINAL_EVAL_TYPE_CHECK))
    _execute_migration((_VALIDATE_FINAL_EVAL_TYPE_CHECK,))

    # 2. eval_results.outcome NOT NULL via validated CHECK (on AUTOCOMMIT side).
    #    ADD CHECK ... NOT VALID (metadata-only) -> VALIDATE (SHARE UPDATE EXCLUSIVE,
    #    no DML block) -> SET NOT NULL (metadata-only, skips full scan because
    #    validated CHECK proves all rows satisfy it) -> DROP CHECK.
    _execute_side((_ADD_OUTCOME_NOT_NULL_CHECK, _VALIDATE_OUTCOME_NOT_NULL_CHECK))
    _execute_side((_SET_OUTCOME_NOT_NULL,))
    _execute_side((_DROP_OUTCOME_NOT_NULL_CHECK,))

    logger.info(
        "0331_eval_outcome_not_null_narrow_vocab: Stage 2 complete — "
        "eval_results.outcome is NOT NULL, evals vocabulary narrowed"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    _set_migration_lock_timeout()

    # Restore eval_results.outcome nullable (on AUTOCOMMIT side)
    _execute_side((_DROP_OUTCOME_NOT_NULL,))

    # Restore TRANSITIONAL evals vocabulary (on migration connection)
    _execute_migration((_DROP_TRANSITIONAL_EVAL_TYPE_CHECK,))
    _execute_migration((_ADD_TRANSITIONAL_EVAL_TYPE_CHECK, _VALIDATE_TRANSITIONAL_EVAL_TYPE_CHECK))

    logger.info(
        "0331_eval_outcome_not_null_narrow_vocab: downgraded — "
        "eval_results.outcome nullable again, evals vocabulary restored to TRANSITIONAL"
    )
