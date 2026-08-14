"""Add the ``guardrail`` eval type + eval_results.observed (FAR-208).

Revision ID: 0100_guardrails
Revises: 0099_run_raw_output_markers
Create Date: 2026-08-14

FAR-208 adds structured-credential boundary data-safety at the ingestion edge
via ``EvalDefinition``s with ``eval_type='guardrail'``. This migration:

* audits pre-existing ``eval_definitions.eval_type`` values — every distinct
  value MUST already be inside the allowed set, otherwise the migration fails
  with a precise report (a pre-existing out-of-set value would be silently
  locked out by the widened CHECK only after the fact);
* widens ``eval_definitions.ck_eval_definitions_type`` to accept
  ``'guardrail'`` (NOT VALID + VALIDATE pattern — the NOT VALID skips the long
  ACCESS EXCLUSIVE re-scan at DROP/ADD time; the explicit VALIDATE then
  catches any pre-existing offender in a second pass);
* adds ``eval_results.observed BOOLEAN NOT NULL DEFAULT FALSE`` — the rollout
  marker that an eval result came from a guardrail in ``observe`` mode
  (shadow-redact: compute + validate + discard + log would-block), so the
  guardrail_summary observed bucket is counted exactly once.

The vocabulary is HARDCODED here — migrations never import ORM constants.

Multi-backend notes (M7): ``NOT VALID`` / ``VALIDATE CONSTRAINT`` / raw
``ALTER TABLE ... ADD/DROP CONSTRAINT`` are Postgres-only. On SQLite/MariaDB
(dev/tests, ``env.py`` uses ``render_as_batch``) the constraint re-creation
goes through ``batch_alter_table`` (full table rebuild, which enforces the
check on existing rows — the non-Postgres analogue of VALIDATE).

Downgrade restores the pre-feature CHECK string and drops the ``observed``
column. Guardrail rows would still exist (soft-delete is the intended removal
path); restoring the CHECK would lock them out on next write only after the
downgrade ran, so the downgrade additionally CONVERTS any existing
``guardrail`` rows to ``regex`` (with an audit note in the migration log) to
keep the table consistent with the restored constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0100_guardrails"
down_revision: str | None = "0099_run_raw_output_markers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_eval_definitions_type"

# Pre-feature constraint vocabulary — hardcoded, never imported.
_EVAL_VALUES_PRE = "'llm_judge', 'regex', 'json_schema', 'custom_function'"
_EVAL_VALUES_POST = "'llm_judge', 'regex', 'json_schema', 'custom_function', 'guardrail'"

_ALLOWED_EVAL_TYPES = frozenset({"llm_judge", "regex", "json_schema", "custom_function"})


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _recreate_check_postgres(table: str, name: str, expression: str) -> None:
    """Postgres NOT VALID + VALIDATE constraint re-creation (mirrors 0069)."""
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def _recreate_check_other(table: str, name: str, expression: str) -> None:
    """Non-Postgres batch-mode constraint re-creation (full table rebuild)."""
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, expression)


def _audit_pre_existing_eval_types() -> None:
    """Fail loudly if any pre-existing eval_type value is outside the allowed set.

    The migration must not silently shrink visibility of existing rows: a
    value outside ``_ALLOWED_EVAL_TYPES`` means data drift that the widened
    CHECK would only trap after the fact. Fail with a precise report instead.
    """
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT DISTINCT eval_type FROM eval_definitions")).fetchall()
    offending = sorted({str(row[0]) for row in rows if str(row[0]) not in _ALLOWED_EVAL_TYPES})
    if offending:
        raise RuntimeError(
            "0100_guardrails: pre-existing eval_type values outside the allowed set: "
            + ", ".join(offending)
            + " (expected one of "
            + ", ".join(sorted(_ALLOWED_EVAL_TYPES))
            + ")"
        )


def upgrade() -> None:
    _audit_pre_existing_eval_types()

    if _is_postgres():
        _recreate_check_postgres("eval_definitions", _CHECK_NAME, f"eval_type IN ({_EVAL_VALUES_POST})")
    else:
        _recreate_check_other("eval_definitions", _CHECK_NAME, f"eval_type IN ({_EVAL_VALUES_POST})")

    op.add_column("eval_results", sa.Column("observed", sa.Boolean, nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    # Convert any existing guardrail rows to regex so the restored CHECK does
    # not lock them out (soft-delete is the intended removal path; a guardrail
    # row that exists at downgrade time becomes a plain regex eval instead of
    # being destroyed).
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE eval_definitions SET eval_type='regex' WHERE eval_type='guardrail'"))

    if _is_postgres():
        _recreate_check_postgres("eval_definitions", _CHECK_NAME, f"eval_type IN ({_EVAL_VALUES_PRE})")
    else:
        _recreate_check_other("eval_definitions", _CHECK_NAME, f"eval_type IN ({_EVAL_VALUES_PRE})")

    op.drop_column("eval_results", "observed")
