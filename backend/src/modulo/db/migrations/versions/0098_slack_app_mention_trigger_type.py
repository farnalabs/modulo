"""Add the ``slack_app_mention`` trigger type (FAR-57) — Slack @-mention triggers.

Revision ID: 0098_slack_app_mention_trigger_type
Revises: 0097_ongoing_trigger_enabled_by_default
Create Date: 2026-08-13

Migration tree: ``0093_run_number_sequence`` -> ``0094_ongoing_trigger_type``
-> ``0095_ongoing_trigger_flag`` -> ``0096_hitl_claims_overdue_notified``
-> ``0097_ongoing_trigger_enabled_by_default`` -> ``0098_slack_app_mention_trigger_type``
(sole head).

The ``slack_app_mention`` trigger fires a pipeline when a bot is @-mentioned in
Slack. It receives Slack Events API ``app_mention`` payloads over the route
``POST /api/v1/triggers/{trigger_id}/slack``, verifies the Slack signing
secret, dedupes by Slack ``event_id``, and maps the mention into pipeline
input.

This migration widens ``triggers.ck_triggers_type`` and ``runs.ck_runs_trigger_type``
to accept ``'slack_app_mention'`` (NOT VALID + VALIDATE pattern, mirroring 0094 —
the NOT VALID skips the long ACCESS EXCLUSIVE re-scan at DROP/ADD time; the
explicit VALIDATE then catches any pre-existing offender in a second pass).

The vocabulary is HARDCODED here — migrations never import ORM constants.

Multi-backend notes (M7): ``NOT VALID`` / ``VALIDATE CONSTRAINT`` / raw
``ALTER TABLE ... ADD/DROP CONSTRAINT`` are Postgres-only. On SQLite/MariaDB
(dev/tests, ``env.py`` uses ``render_as_batch``) the constraint re-creation
goes through ``batch_alter_table`` (full table rebuild).

Downgrade restores the pre-feature CHECK strings. The ``'slack_app_mention'``
enum value is dropped on downgrade — no pre-existing row can carry it (the
feature never shipped), so restoring the old strings is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0098_slack_app_mention_trigger_type"
down_revision: str | None = "0097_ongoing_trigger_enabled_by_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGERS_CHECK = "ck_triggers_type"
_RUNS_CHECK = "ck_runs_trigger_type"

# Pre-feature constraint vocabulary — hardcoded, never imported.
_TRIGGERS_VALUES_PRE = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing'"
_TRIGGERS_VALUES_POST = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'slack_app_mention'"
_RUNS_VALUES_PRE = "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'correction'"
_RUNS_VALUES_POST = (
    "'manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'correction', 'slack_app_mention'"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _recreate_check_postgres(table: str, name: str, expression: str) -> None:
    """Postgres NOT VALID + VALIDATE constraint re-creation (mirrors 0094)."""
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
    else:
        _recreate_check_other("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_POST})")
        _recreate_check_other("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_POST})")


def downgrade() -> None:
    if _is_postgres():
        _recreate_check_postgres("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_PRE})")
        _recreate_check_postgres("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_PRE})")
    else:
        _recreate_check_other("triggers", _TRIGGERS_CHECK, f"trigger_type IN ({_TRIGGERS_VALUES_PRE})")
        _recreate_check_other("runs", _RUNS_CHECK, f"trigger_type IN ({_RUNS_VALUES_PRE})")
