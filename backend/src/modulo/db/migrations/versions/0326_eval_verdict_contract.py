"""Eval judge verdict contract: ``outcome``, error state, judge envelope (ADR 066 S1).

Revision ID: 0326_eval_verdict_contract
Revises: 0325_run_seam_coverage
Create Date: 2026-10-21

Renumbered from ``0325_eval_verdict_contract`` when main independently landed
``0325_run_seam_coverage``: two files can share a number without a git merge
conflict, so the chain is re-pointed here rather than leaving a second root.""

**EXPAND release — this migration deliberately does NOT set ``outcome`` NOT NULL
and does NOT narrow the eval-type vocabulary.** Deploy ordering is
``release.sh -> alembic upgrade heads`` BEFORE any machine rolls: the OLD image
and worker keep serving while this migration commits. The old ORM has no
``outcome`` column at all, and writes ``eval_type='llm_judge'``, so committing
either a NOT NULL ``outcome`` or a narrowed vocabulary here would break every
old-image insert during the rollout window (guardrail-carrying run creation,
and judge-definition writes). The contract is therefore two releases:

* **This release (expand).** Add ``outcome`` NULLABLE; backfill it; add the
  value CHECK ``NOT VALID`` then ``VALIDATE``. Add a TRANSITIONAL ``eval_type``
  CHECK accepting BOTH ``llm_judge`` and ``model_judge`` (plus the rest), then
  rename the stored data to ``model_judge``. "Every writer sets ``outcome``" is
  enforced in the APPLICATION layer for this release (a SQLAlchemy
  ``before_insert`` guard on ``EvalResult``), so a NULL is never *introduced* by
  the new image even though the DB briefly allows one.
* **Stage 2 (follow-up ticket).** Once every machine runs the new image:
  ``ALTER TABLE eval_results ALTER COLUMN outcome SET NOT NULL`` (no server
  default) and narrow ``ck_evals_type`` to the new vocabulary. Readiness check:
  ``SELECT count(*) FROM eval_results WHERE outcome IS NULL`` = 0 after the
  rollout (older-image inserts during the window can leave NULLs behind).
  Stage 2 should prove NOT NULL from a validated
  ``CHECK (outcome IS NOT NULL)`` and drop that check afterwards so
  ``SET NOT NULL`` can skip the full scan.

Contents:

1. **``llm_judge`` -> ``model_judge`` (ADR 066 D2).** The stored name is a false
   claim for a non-LLM judge. This is the cutover that resets suite baselines
   (the definition checksum contains ``eval_type``), and it happens once.
2. **``eval_results.outcome`` (D5/D6).** ``pass`` | ``fail`` | ``error``, where
   ``error`` means *no verdict was produced*. Backfilled from ``passed``.
3. **Verdict provenance (D5).** ``judge_result_json`` (schema-versioned
   envelope, physical ``jsonb``), ``judge_confidence_kind`` (closed
   vocabulary) and ``error_code`` (the coded reason, NULL on pass/fail).

Locking / online safety (FAR-403 class, and a deadlock found in review):

* ``evals`` DDL/DML stays on the **migration connection**. ``0324_purge`` (an
  earlier revision, so the same Alembic transaction) holds ACCESS SHARE on
  ``public.evals``; a side connection taking ACCESS EXCLUSIVE there would wait
  on a lock the same transaction holds — a self-deadlock. The migration
  connection can upgrade its own lock. ``0325_run_seam_coverage`` now sits
  between the two revisions; it only adds a nullable column to ``runs``, so it
  does not change the lock story.
* ``eval_results`` DDL and the backfill run on a dedicated AUTOCOMMIT
  connection: every ``ALTER`` commits immediately (so its ACCESS EXCLUSIVE is
  held for that statement only, never across the following ``VALIDATE`` scan,
  which runs under SHARE UPDATE EXCLUSIVE and does not block DML).
* Both connections set an explicit ``lock_timeout`` so a future conflict fails
  loudly instead of hanging.
* The backfill is a PRIMARY-KEY KEYSET loop over ``id`` (not
  ``WHERE outcome IS NULL LIMIT n``, which seq-scans the whole table on every
  batch), advancing the cursor per batch.
* ``alembic``'s ``autocommit_block()`` is NOT usable here: ``env.py`` wraps the
  whole upgrade in one transaction (the same constraint ``0193``/``0200``
  document).
* A FRESH chain cannot use a side connection for ``eval_results`` — the table
  was created by an earlier migration whose DDL is still uncommitted — so that
  path runs the ``eval_results`` statements on the migration connection. There
  is no traffic or data on a fresh chain.

Non-Postgres backends are a no-op, matching ``0323`` and
``0324_purge_eval_definitions``: their schemas come from the ORM
``create_all``.

``passed`` is retained as the compatibility projection of ``outcome``; existing
readers keep working and the policy gate vocabulary is unchanged (D6).

Downgrade restores the historical vocabulary and drops the added columns.
**After a downgrade an ``outcome='error'`` row is indistinguishable from a
genuine failure** — the previous schema has nowhere to record "no verdict was
produced", so the distinction is lost. That loss is the point of the ticket;
the backfill is not reversible per-row, but ``passed`` remains and is re-derived
by the previous code path.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0326_eval_verdict_contract"
down_revision: str | None = "0325_run_seam_coverage"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Batch size for the primary-key keyset backfill.
_BATCH_SIZE = 1000

#: Progress cadence: a large table is thousands of batches, and a silent
#: release command looks like a hung deploy. Emit at most one line per 10
#: batches or per 60s, whichever comes first (never one line per batch).
_PROGRESS_EVERY_BATCHES = 10
_PROGRESS_EVERY_SECONDS = 60.0

#: Fail loudly rather than wait forever on a lock the migration cannot get.
_SET_MIGRATION_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '15s'"
_SET_SIDE_LOCK_TIMEOUT = "SET lock_timeout = '15s'"

_OUTCOME_CHECK = "ck_eval_results_outcome"
_CONFIDENCE_CHECK = "ck_eval_results_judge_confidence_kind"
_EVAL_TYPE_CHECK = "ck_evals_type"

# --- eval_results columns (nullable in this EXPAND release) -----------------
_ADD_OUTCOME = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS outcome VARCHAR(16)"
_ADD_JUDGE_RESULT = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS judge_result_json JSONB"
_ADD_CONFIDENCE = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS judge_confidence_kind VARCHAR(32)"
_ADD_ERROR_CODE = "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS error_code VARCHAR(64)"

# --- primary-key keyset backfill (index-backed; never a full seq scan) ------
_BACKFILL_OUTCOME = (
    "WITH batch AS ("
    "SELECT id FROM eval_results WHERE id > :last AND outcome IS NULL "
    "ORDER BY id LIMIT :batch_size"
    ") "
    "UPDATE eval_results AS e "
    "SET outcome = CASE WHEN e.passed THEN 'pass' ELSE 'fail' END "
    "FROM batch WHERE e.id = batch.id "
    "RETURNING e.id"
)
_ZERO_UUID = uuid.UUID(int=0)

# --- evals vocabulary (TRANSITIONAL: both stored names are accepted) --------
_RENAME_EVAL_TYPES = "UPDATE evals SET eval_type = 'model_judge' WHERE eval_type = 'llm_judge'"
_RESTORE_EVAL_TYPES = "UPDATE evals SET eval_type = 'llm_judge' WHERE eval_type = 'model_judge'"
_DROP_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_type' "
    "AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_type; END IF; END $$;"
)
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
_ADD_OLD_EVAL_TYPE_CHECK = (
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

# --- eval_results CHECKs (added NOT VALID, validated separately) ------------
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


def _execute_migration(statements: tuple[str, ...]) -> None:
    """Run DDL/DML on the migration connection (``evals`` always goes here)."""
    for statement in statements:
        op.execute(sa.text(statement))


def _execute_side(statements: tuple[str, ...]) -> None:
    """Run ``eval_results`` DDL on the AUTOCOMMIT side connection.

    Each statement commits on its own, so an ACCESS EXCLUSIVE is released before
    the next statement's ``VALIDATE`` scan. On a fresh chain (table not visible)
    the statements run on the migration connection instead.
    """
    with _autocommit_connection() as conn:
        if not _relation_visible(conn):
            for statement in statements:
                op.execute(sa.text(statement))
            return
        for statement in statements:
            conn.execute(sa.text(statement))


def _backfill_outcome() -> int:
    """Primary-key keyset backfill of ``outcome`` (index-backed, resumable)."""
    total = 0
    batches = 0
    last: Any = _ZERO_UUID
    with _autocommit_connection() as conn:
        if not _relation_visible(conn):
            logger.info("0326_eval_verdict_contract: fresh chain, no rows to backfill")
            return 0
        logger.info(
            "0326_eval_verdict_contract: starting online outcome backfill "
            "(batch_size=%d, primary-key keyset, resumable via WHERE outcome IS NULL; "
            "progress every %d batches or %ds)",
            _BATCH_SIZE,
            _PROGRESS_EVERY_BATCHES,
            int(_PROGRESS_EVERY_SECONDS),
        )
        progress_started = time.monotonic()
        while True:
            rows = conn.execute(
                sa.text(_BACKFILL_OUTCOME),
                {"last": last, "batch_size": _BATCH_SIZE},
            ).fetchall()
            if not rows:
                break
            ids = [row[0] for row in rows]
            total += len(ids)
            batches += 1
            last = max(ids)
            if batches % _PROGRESS_EVERY_BATCHES == 0 or (
                time.monotonic() - progress_started >= _PROGRESS_EVERY_SECONDS
            ):
                logger.info(
                    "0326_eval_verdict_contract: backfill progress rows=%d batches=%d cursor=%s",
                    total,
                    batches,
                    last,
                )
                progress_started = time.monotonic()
    logger.info(
        "0326_eval_verdict_contract: backfilled outcome on %d row(s) in %d batch(es)",
        total,
        batches,
    )
    return total


def upgrade() -> None:
    if not _is_postgres():
        # Matching 0323 / 0324_purge: the non-Postgres schema comes from the
        # ORM create_all.
        return

    _set_migration_lock_timeout()

    # 1. evals vocabulary (EXPAND, on the migration connection). Drop the
    #    llm_judge-only CHECK, add the TRANSITIONAL both-values CHECK NOT VALID,
    #    rename the data, then VALIDATE. No narrowing in this release, so the
    #    old image's llm_judge writes remain valid during the rollout.
    _execute_migration((_DROP_EVAL_TYPE_CHECK, _ADD_TRANSITIONAL_EVAL_TYPE_CHECK))
    _execute_migration((_RENAME_EVAL_TYPES,))
    _execute_migration((_VALIDATE_EVAL_TYPE_CHECK,))

    # 2. eval_results columns (EXPAND: nullable) + backfill on the side
    #    connection, where each ALTER commits immediately.
    _execute_side((_ADD_OUTCOME, _ADD_JUDGE_RESULT, _ADD_CONFIDENCE, _ADD_ERROR_CODE))
    _backfill_outcome()

    # 3. Value checks. ADD ... NOT VALID is metadata-only and commits on its
    #    own; VALIDATE scans under SHARE UPDATE EXCLUSIVE (no DML block). No
    #    scan ever follows a held ACCESS EXCLUSIVE.
    _execute_side((_ADD_OUTCOME_CHECK, _VALIDATE_OUTCOME_CHECK))
    _execute_side((_ADD_CONFIDENCE_CHECK, _VALIDATE_CONFIDENCE_CHECK))


def downgrade() -> None:
    if not _is_postgres():
        return

    _set_migration_lock_timeout()

    # Restore the historical vocabulary on the migration connection.
    _execute_migration((_DROP_EVAL_TYPE_CHECK,))
    _execute_migration((_RESTORE_EVAL_TYPES,))
    _execute_migration((_ADD_OLD_EVAL_TYPE_CHECK, _VALIDATE_EVAL_TYPE_CHECK))

    _execute_side(
        (
            _DROP_CONFIDENCE_CHECK,
            _DROP_OUTCOME_CHECK,
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS error_code",
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS judge_confidence_kind",
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS judge_result_json",
            "ALTER TABLE eval_results DROP COLUMN IF EXISTS outcome",
        )
    )
