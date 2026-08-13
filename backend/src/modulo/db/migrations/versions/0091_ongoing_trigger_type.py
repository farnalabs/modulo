"""Add the ``ongoing`` trigger type (FAR-158) — topped-up worker-pool semantics.

Revision ID: 0091_ongoing_trigger_type
Revises: 0090_add_budget_exceeded_status
Create Date: 2026-08-12

The ``ongoing`` trigger keeps a pipeline topped up to ``max_concurrent_runs``
in-flight (active or queued) runs, forever. This migration:

* widens ``triggers.ck_triggers_type`` and ``runs.ck_runs_trigger_type`` to
  accept ``'ongoing'`` (NOT VALID + VALIDATE pattern, mirroring 0069 — the
  NOT VALID skips the long ACCESS EXCLUSIVE re-scan at DROP/ADD time; the
  explicit VALIDATE then catches any pre-existing offender in a second pass);
* adds two PARTIAL CHECKs on ``triggers`` that apply ONLY to ``ongoing`` rows:
  ``daily_spend_limit`` is REQUIRED (non-null, > 0) and the target pool size
  ``max_concurrent_runs`` is bounded 1..20 — the DB-level cost/runaway guard;
* adds ``(trigger_id, status)`` and ``(trigger_id, created_at)`` indexes on
  ``runs`` — the top-up count (``cron_helpers._count_ongoing_runs``) and the
  per-trigger daily-spend SUM both read via ``trigger_id``.

The vocabulary is HARDCODED here — migrations never import ORM constants.

Multi-backend notes (M7): ``NOT VALID`` / ``VALIDATE CONSTRAINT`` / raw
``ALTER TABLE ... ADD/DROP CONSTRAINT`` are Postgres-only. On SQLite/MariaDB
(dev/tests, ``env.py`` uses ``render_as_batch``) the constraint re-creation
goes through ``batch_alter_table`` (full table rebuild, which enforces the
check on existing rows — the non-Postgres analogue of VALIDATE).

Downgrade restores the pre-feature CHECK strings and drops the indexes. The
``'ongoing'`` enum value is dropped on downgrade — no pre-existing row can
carry it (the feature never shipped), so restoring the old strings is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0091_ongoing_trigger_type"
down_revision: str | None = "0090_add_budget_exceeded_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGERS_CHECK = "ck_triggers_type"
_RUNS_CHECK = "ck_runs_trigger_type"
_ONGOING_SPEND_CHECK = "ck_triggers_ongoing_spend_limit"
_ONGOING_TARGET_CHECK = "ck_triggers_ongoing_target_range"

# Pre-feature constraint vocabulary — hardcoded, never imported.
_TRIGGERS_VALUES_PRE = "'manual', 'webhook', 'cron', 'polling', 'agent_signal'"
_TRIGGERS_VALUES_POST = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing'"
_RUNS_VALUES_PRE = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction'"
_RUNS_VALUES_POST = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction', 'ongoing'"


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


def upgrade() -> None:
    if _is_postgres():
        _recreate_check_postgres("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_POST})")
        _recreate_check_postgres("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_POST})")
        _recreate_check_postgres(
            "triggers",
            _ONGOING_SPEND_CHECK,
            "trigger_type <> 'ongoing' OR (daily_spend_limit IS NOT NULL AND daily_spend_limit > 0)",
        )
        _recreate_check_postgres(
            "triggers",
            _ONGOING_TARGET_CHECK,
            "trigger_type <> 'ongoing' OR (max_concurrent_runs BETWEEN 1 AND 20)",
        )
    else:
        _recreate_check_other("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_POST})")
        _recreate_check_other("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_POST})")
        _recreate_check_other(
            "triggers",
            _ONGOING_SPEND_CHECK,
            "trigger_type <> 'ongoing' OR (daily_spend_limit IS NOT NULL AND daily_spend_limit > 0)",
        )
        _recreate_check_other(
            "triggers",
            _ONGOING_TARGET_CHECK,
            "trigger_type <> 'ongoing' OR (max_concurrent_runs BETWEEN 1 AND 20)",
        )

    # Top-up count + per-trigger daily-spend SUM both read via trigger_id.
    op.create_index("ix_runs_trigger_id_status", "runs", ["trigger_id", "status"], unique=False)
    op.create_index("ix_runs_trigger_id_created_at", "runs", ["trigger_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_trigger_id_created_at", table_name="runs")
    op.drop_index("ix_runs_trigger_id_status", table_name="runs")
    if _is_postgres():
        op.execute(f"ALTER TABLE triggers DROP CONSTRAINT IF EXISTS {_ONGOING_TARGET_CHECK}")
        op.execute(f"ALTER TABLE triggers DROP CONSTRAINT IF EXISTS {_ONGOING_SPEND_CHECK}")
        _recreate_check_postgres("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_PRE})")
        _recreate_check_postgres("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_PRE})")
    else:
        with op.batch_alter_table("triggers") as batch:
            batch.drop_constraint(_ONGOING_TARGET_CHECK, type_="check")
            batch.drop_constraint(_ONGOING_SPEND_CHECK, type_="check")
        _recreate_check_other("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_PRE})")
        _recreate_check_other("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_PRE})")
