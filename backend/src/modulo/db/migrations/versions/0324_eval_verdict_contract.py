"""Eval judge verdict contract: ``outcome``, error state, judge envelope (ADR 066 S1).

Revision ID: 0324_eval_verdict_contract
Revises: 0323_pipeline_snapshots_substantive_version_index
Create Date: 2026-10-21

Three changes, one cutover:

1. **``llm_judge`` -> ``model_judge`` (ADR 066 D2).** The stored eval-type name
   was a false claim for a non-LLM judge. The data is renamed and the CHECK
   vocabulary dropped/re-added in the same migration. No alias: a wire alias
   would recreate the dual vocabulary the ADR removes. This is the one cutover
   that resets suite baselines (the definition checksum contains ``eval_type``),
   and it resets them exactly once.
2. **``eval_results.outcome`` (D5/D6).** A third state — ``pass`` | ``fail`` |
   ``error`` — with ``error`` meaning *no verdict was produced*. Backfilled from
   ``passed`` and then set NOT NULL **with no server default**, so a write that
   forgets the outcome fails instead of recording a failure.
3. **Verdict provenance (D5).** ``judge_result_json`` (schema-versioned
   envelope, physical ``jsonb`` via the shared ``JSONB_COL`` alias),
   ``judge_confidence_kind`` (closed vocabulary) and ``error_code`` (the coded
   reason, NULL on pass/fail).

``passed`` is retained as the compatibility projection of ``outcome``; existing
readers keep working and the policy gate vocabulary is unchanged (D6).

Downgrade reverses the three changes and restores the historical vocabulary.
The outcome backfill is not reversible per-row, but ``passed`` remains and is
re-derived by the previous code path, so no information needed by the old
schema is lost.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0324_eval_verdict_contract"
down_revision: str | None = "0323_pipeline_snapshots_substantive_version_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_OUTCOME_CHECK = "ck_eval_results_outcome"
_CONFIDENCE_CHECK = "ck_eval_results_judge_confidence_kind"
_EVAL_TYPE_CHECK = "ck_evals_type"
_LEGACY_EVAL_TYPE_CHECK = "ck_eval_definitions_type"

_OUTCOME_PREDICATE = "outcome IN ('pass', 'fail', 'error')"
_CONFIDENCE_PREDICATE = (
    "judge_confidence_kind IS NULL OR judge_confidence_kind IN "
    "('vendor_claimed_calibrated', 'model_self_reported', 'unavailable')"
)
_RENAME_EVAL_TYPES = "UPDATE evals SET eval_type = 'model_judge' WHERE eval_type = 'llm_judge'"
_RESTORE_EVAL_TYPES = "UPDATE evals SET eval_type = 'llm_judge' WHERE eval_type = 'model_judge'"

_ADD_OUTCOME_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome' "
    "AND conrelid = 'public.eval_results'::regclass) "
    "THEN ALTER TABLE public.eval_results ADD CONSTRAINT "
    "ck_eval_results_outcome CHECK (outcome IN ('pass', 'fail', 'error')) NOT VALID; END IF; END $$;"
)
_VALIDATE_OUTCOME_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_outcome' "
    "AND conrelid = 'public.eval_results'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.eval_results VALIDATE CONSTRAINT ck_eval_results_outcome; END IF; END $$;"
)
_ADD_CONFIDENCE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_results_judge_confidence_kind' "
    "AND conrelid = 'public.eval_results'::regclass) "
    "THEN ALTER TABLE public.eval_results ADD CONSTRAINT "
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
_DROP_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_type' "
    "AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_type; END IF; END $$;"
)
_ADD_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_evals_type' AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['model_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying, "
    "'human_set'::character varying]::text[])); END IF; END $$;"
)
_RESTORE_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_type' "
    "AND conrelid = 'public.evals'::regclass) THEN "
    "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_type; END IF; "
    "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['llm_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying, "
    "'human_set'::character varying]::text[])); END $$;"
)
#: ``eval_definitions`` is the retired pre-0254 table. It still exists
#: physically, so its historical CHECK is kept in sync while it does.
_DROP_LEGACY_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF to_regclass('public.eval_definitions') IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_definitions_type' "
    "AND conrelid = 'public.eval_definitions'::regclass) THEN "
    "ALTER TABLE public.eval_definitions DROP CONSTRAINT ck_eval_definitions_type; "
    "END IF; END $$;"
)
_RENAME_LEGACY_EVAL_TYPES = (
    "DO $$ BEGIN IF to_regclass('public.eval_definitions') IS NOT NULL THEN "
    "UPDATE public.eval_definitions SET eval_type = 'model_judge' WHERE eval_type = 'llm_judge'; "
    "END IF; END $$;"
)
_RESTORE_LEGACY_EVAL_TYPES = (
    "DO $$ BEGIN IF to_regclass('public.eval_definitions') IS NOT NULL THEN "
    "UPDATE public.eval_definitions SET eval_type = 'llm_judge' WHERE eval_type = 'model_judge'; "
    "END IF; END $$;"
)
_ADD_LEGACY_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_eval_definitions_type' "
    "AND conrelid = 'public.eval_definitions'::regclass) THEN "
    "ALTER TABLE public.eval_definitions ADD CONSTRAINT ck_eval_definitions_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['model_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying]::text[])); "
    "END IF; END $$;"
)
_RESTORE_LEGACY_EVAL_TYPE_CHECK = (
    "DO $$ BEGIN IF to_regclass('public.eval_definitions') IS NOT NULL THEN "
    "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_definitions_type' "
    "AND conrelid = 'public.eval_definitions'::regclass) THEN "
    "ALTER TABLE public.eval_definitions DROP CONSTRAINT ck_eval_definitions_type; END IF; "
    "ALTER TABLE public.eval_definitions ADD CONSTRAINT ck_eval_definitions_type CHECK ("
    "(eval_type)::text = ANY (ARRAY['llm_judge'::character varying, "
    "'regex'::character varying, 'json_schema'::character varying, "
    "'custom_function'::character varying, 'guardrail'::character varying]::text[])); "
    "END IF; END $$;"
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
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _upgrade_eval_type_vocabulary() -> None:
    if _is_postgres():
        # Drop the OLD CHECK before renaming: PostgreSQL validates the
        # constraint on the UPDATE, so a rename-first order cannot succeed.
        op.execute(sa.text(_DROP_EVAL_TYPE_CHECK))
        op.execute(sa.text(_RENAME_EVAL_TYPES))
        op.execute(sa.text(_ADD_EVAL_TYPE_CHECK))
        op.execute(sa.text(_DROP_LEGACY_EVAL_TYPE_CHECK))
        op.execute(sa.text(_RENAME_LEGACY_EVAL_TYPES))
        op.execute(sa.text(_ADD_LEGACY_EVAL_TYPE_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table("evals") as batch_op:
            batch_op.drop_constraint(_EVAL_TYPE_CHECK, type_="check")
        op.execute(sa.text(_RENAME_EVAL_TYPES))
        with op.batch_alter_table("evals") as batch_op:
            batch_op.create_check_constraint(
                _EVAL_TYPE_CHECK,
                "eval_type IN ('model_judge', 'regex', 'json_schema', 'custom_function', 'guardrail', 'human_set')",
            )


def _downgrade_eval_type_vocabulary() -> None:
    if _is_postgres():
        op.execute(sa.text(_DROP_EVAL_TYPE_CHECK))
        op.execute(sa.text(_RESTORE_EVAL_TYPES))
        op.execute(sa.text(_RESTORE_EVAL_TYPE_CHECK))
        op.execute(sa.text(_RESTORE_LEGACY_EVAL_TYPE_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table("evals") as batch_op:
            batch_op.drop_constraint(_EVAL_TYPE_CHECK, type_="check")
        op.execute(sa.text(_RESTORE_EVAL_TYPES))
        with op.batch_alter_table("evals") as batch_op:
            batch_op.create_check_constraint(
                _EVAL_TYPE_CHECK,
                "eval_type IN ('llm_judge', 'regex', 'json_schema', 'custom_function', 'guardrail', 'human_set')",
            )


def _upgrade_outcome() -> None:
    if _is_postgres():
        op.execute(sa.text("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS outcome VARCHAR(16)"))
        op.execute(
            sa.text(
                "UPDATE eval_results SET outcome = CASE WHEN passed THEN 'pass' ELSE 'fail' END WHERE outcome IS NULL"
            )
        )
        op.execute(sa.text("ALTER TABLE eval_results ALTER COLUMN outcome SET NOT NULL"))
        op.execute(sa.text(_ADD_OUTCOME_CHECK))
        op.execute(sa.text(_VALIDATE_OUTCOME_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table("eval_results") as batch_op:
            batch_op.add_column(sa.Column("outcome", sa.String(16), nullable=True))
        op.execute(
            sa.text(
                "UPDATE eval_results SET outcome = CASE WHEN passed THEN 'pass' ELSE 'fail' END WHERE outcome IS NULL"
            )
        )
        with op.batch_alter_table("eval_results") as batch_op:
            batch_op.alter_column("outcome", existing_type=sa.String(16), nullable=False)
            batch_op.create_check_constraint(_OUTCOME_CHECK, _OUTCOME_PREDICATE)


def _upgrade_provenance() -> None:
    json_type = JSONB() if _is_postgres() else sa.JSON()
    if _is_postgres():
        op.execute(sa.text("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS judge_result_json JSONB"))
        op.execute(sa.text("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS judge_confidence_kind VARCHAR(32)"))
        op.execute(sa.text("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS error_code VARCHAR(64)"))
        op.execute(sa.text(_ADD_CONFIDENCE_CHECK))
        op.execute(sa.text(_VALIDATE_CONFIDENCE_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table("eval_results") as batch_op:
            batch_op.add_column(sa.Column("judge_result_json", json_type, nullable=True))
            batch_op.add_column(sa.Column("judge_confidence_kind", sa.String(32), nullable=True))
            batch_op.add_column(sa.Column("error_code", sa.String(64), nullable=True))
            batch_op.create_check_constraint(_CONFIDENCE_CHECK, _CONFIDENCE_PREDICATE)


def upgrade() -> None:
    _upgrade_eval_type_vocabulary()
    _upgrade_outcome()
    _upgrade_provenance()


def downgrade() -> None:
    if _is_postgres():
        op.execute(sa.text(_DROP_CONFIDENCE_CHECK))
        op.execute(sa.text(_DROP_OUTCOME_CHECK))
        op.execute(sa.text("ALTER TABLE eval_results DROP COLUMN IF EXISTS error_code"))
        op.execute(sa.text("ALTER TABLE eval_results DROP COLUMN IF EXISTS judge_confidence_kind"))
        op.execute(sa.text("ALTER TABLE eval_results DROP COLUMN IF EXISTS judge_result_json"))
        op.execute(sa.text("ALTER TABLE eval_results DROP COLUMN IF EXISTS outcome"))
    elif _is_sqlite():
        with op.batch_alter_table("eval_results") as batch_op:
            batch_op.drop_constraint(_CONFIDENCE_CHECK, type_="check")
            batch_op.drop_constraint(_OUTCOME_CHECK, type_="check")
            batch_op.drop_column("error_code")
            batch_op.drop_column("judge_confidence_kind")
            batch_op.drop_column("judge_result_json")
            batch_op.drop_column("outcome")
    _downgrade_eval_type_vocabulary()
