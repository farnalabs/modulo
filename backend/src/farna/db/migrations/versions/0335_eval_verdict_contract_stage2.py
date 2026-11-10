"""Complete the staged eval verdict contract (FAR-2058 / ADR 066 S1).

Revision ID: 0335_eval_verdict_contract_stage2
Revises: 0334_runs_webhook_decay_sweep_index
Create Date: 2026-10-25

Stage 0330 was deliberately an expand migration: old application instances could
still write ``outcome = NULL`` and ``eval_type = 'llm_judge'`` while the new
image rolled out. This revision runs only after the rollout and closes that
window. It re-checks the readiness premise inside the migration, repairs the
known residuals with the same bounded operations, and fails closed if anything
remains before enforcing the contract.

``evals`` work stays on Alembic's migration connection. ``eval_results`` DDL
and backfill use a dedicated AUTOCOMMIT connection so ACCESS EXCLUSIVE locks
are held only for individual metadata statements; the migration connection is
used when a fresh in-flight chain cannot see the table or already holds a lock
on it. Every connection has an explicit lock timeout.

Downgrade restores the 0330 expand shape: ``outcome`` becomes nullable again,
and the transitional eval vocabulary accepts ``llm_judge`` and
``model_judge``. The added verdict columns and checks remain because they are
owned by 0330; an ``error`` outcome cannot be recovered as a distinct state by
the older application, but the column itself remains available for a later
stage-1 image.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0335_eval_verdict_contract_stage2"
down_revision: str | None = "0334_runs_webhook_decay_sweep_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_BATCH_SIZE = 1000
_PROGRESS_EVERY_BATCHES = 10
_PROGRESS_EVERY_SECONDS = 60.0
_MIGRATION_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '15s'"
_SIDE_LOCK_TIMEOUT = "SET lock_timeout = '15s'"
_ZERO_UUID = uuid.UUID(int=0)

_OUTCOME_NOT_NULL_CHECK = "ck_eval_results_outcome_not_null"
_OUTCOME_CHECK = "ck_eval_results_outcome"
_CONFIDENCE_CHECK = "ck_eval_results_judge_confidence_kind"
_EVAL_TYPE_CHECK = "ck_evals_type"

_BACKFILL_OUTCOME = (
    "WITH batch AS ("
    "SELECT id FROM eval_results WHERE id > :last AND outcome IS NULL "
    "ORDER BY id LIMIT :batch_size"
    ") UPDATE eval_results AS e "
    "SET outcome = CASE WHEN e.passed THEN 'pass' ELSE 'fail' END "
    "FROM batch WHERE e.id = batch.id RETURNING e.id"
)

_ADD_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid='public.eval_results'::regclass) THEN "
    "ALTER TABLE public.eval_results ADD CONSTRAINT ck_eval_results_outcome_not_null "
    "CHECK (outcome IS NOT NULL) NOT VALID; END IF; END $$;"
)
_VALIDATE_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid='public.eval_results'::regclass AND NOT convalidated) THEN "
    "ALTER TABLE public.eval_results VALIDATE CONSTRAINT ck_eval_results_outcome_not_null; "
    "END IF; END $$;"
)
_DROP_NOT_NULL_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome_not_null' "
    "AND conrelid='public.eval_results'::regclass) THEN "
    "ALTER TABLE public.eval_results DROP CONSTRAINT ck_eval_results_outcome_not_null; "
    "END IF; END $$;"
)
_NARROW_EVAL_TYPE_CHECK = (
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['model_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying, "
    "'human_set'::character varying]::text[])) NOT VALID"
)
_TRANSITIONAL_EVAL_TYPE_CHECK = (
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['llm_judge'::character varying, "
    "'model_judge'::character varying, 'regex'::character varying, "
    "'json_schema'::character varying, 'custom_function'::character varying, "
    "'guardrail'::character varying, 'human_set'::character varying]::text[])) NOT VALID"
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _rehearsal_mode() -> bool:
    """Keep the rehearsal transaction on Alembic's connection."""
    return os.environ.get("ALEMBIC_REHEARSAL", "") == "1"


def _set_migration_lock_timeout() -> None:
    op.execute(sa.text(_MIGRATION_LOCK_TIMEOUT))


def _migration_holds_eval_results_lock() -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM pg_locks "
                "WHERE pid = pg_backend_pid() "
                "AND relation = to_regclass('public.eval_results')"
            )
        )
        .scalar()
    )


@contextmanager
def _eval_results_connection() -> Iterator[Any]:
    """Yield a safe connection for eval_results DDL and backfill."""
    bind = op.get_bind()
    if _rehearsal_mode():
        yield bind
        return
    side = bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        side.execute(sa.text(_SIDE_LOCK_TIMEOUT))
        visible = side.execute(sa.text("SELECT to_regclass('public.eval_results')")).scalar() is not None
        yield bind if not visible or _migration_holds_eval_results_lock() else side
    finally:
        side.close()


def _backfill_outcome(conn: Any) -> int:
    total = 0
    batches = 0
    last: Any = _ZERO_UUID
    started = time.monotonic()
    logger.info("0334_eval_verdict_contract_stage2: starting outcome backfill (batch_size=%d)", _BATCH_SIZE)
    while True:
        rows = conn.execute(sa.text(_BACKFILL_OUTCOME), {"last": last, "batch_size": _BATCH_SIZE}).fetchall()
        if not rows:
            break
        ids = [row[0] for row in rows]
        total += len(ids)
        batches += 1
        last = max(ids)
        if batches % _PROGRESS_EVERY_BATCHES == 0 or time.monotonic() - started >= _PROGRESS_EVERY_SECONDS:
            logger.info(
                "0334_eval_verdict_contract_stage2: backfill progress rows=%d batches=%d cursor=%s",
                total,
                batches,
                last,
            )
            started = time.monotonic()
    logger.info(
        "0334_eval_verdict_contract_stage2: backfilled outcome on %d row(s) in %d batch(es)",
        total,
        batches,
    )
    return total


def _drop_eval_type_check() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_type' "
            "AND conrelid='public.evals'::regclass) THEN ALTER TABLE public.evals "
            "DROP CONSTRAINT ck_evals_type; END IF; END $$;"
        )
    )


def _validate_eval_type_check() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_type' "
            "AND conrelid='public.evals'::regclass AND NOT convalidated) THEN ALTER TABLE public.evals "
            "VALIDATE CONSTRAINT ck_evals_type; END IF; END $$;"
        )
    )


def _assert_readiness(eval_results_conn: Any) -> None:
    null_outcomes = eval_results_conn.execute(
        sa.text("SELECT count(*) FROM public.eval_results WHERE outcome IS NULL")
    ).scalar_one()
    if null_outcomes != 0:
        raise RuntimeError("FAR-2058 readiness failed: eval_results.outcome IS NULL count is " + str(null_outcomes))
    legacy_types = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM public.evals WHERE eval_type = 'llm_judge'")).scalar_one()
    )
    if legacy_types != 0:
        raise RuntimeError("FAR-2058 readiness failed: evals.eval_type='llm_judge' count is " + str(legacy_types))


def upgrade() -> None:
    if not _is_postgres():
        return
    _set_migration_lock_timeout()

    # Remediate residual stage-1 writes before asserting the rollout premise.
    op.execute(sa.text("UPDATE public.evals SET eval_type = 'model_judge' WHERE eval_type = 'llm_judge'"))
    with _eval_results_connection() as conn:
        _backfill_outcome(conn)
        _assert_readiness(conn)
        for statement in (_ADD_NOT_NULL_CHECK, _VALIDATE_NOT_NULL_CHECK):
            conn.execute(sa.text(statement))
        conn.execute(sa.text("ALTER TABLE public.eval_results ALTER COLUMN outcome SET NOT NULL"))
        conn.execute(sa.text(_DROP_NOT_NULL_CHECK))

    # Narrow only after all legacy values have been remediated and counted.
    _drop_eval_type_check()
    op.execute(sa.text(_NARROW_EVAL_TYPE_CHECK))
    _validate_eval_type_check()


def downgrade() -> None:
    if not _is_postgres():
        return
    _set_migration_lock_timeout()

    _drop_eval_type_check()
    op.execute(sa.text(_TRANSITIONAL_EVAL_TYPE_CHECK))
    _validate_eval_type_check()
    with _eval_results_connection() as conn:
        conn.execute(sa.text("ALTER TABLE public.eval_results ALTER COLUMN outcome DROP NOT NULL"))
        conn.execute(sa.text(_DROP_NOT_NULL_CHECK))
