"""Widen run trigger_type CHECK vocabularies for the 'rerun' trigger type.

Revision ID: 0213_runs_rerun_trigger_type
Revises: 0212_variant_batch_state_updated_at
Create Date: 2026-09-11

POST /api/v1/runs/{run_id}/rerun re-executes a terminal run against the SAME
snapshot with a COPIED input payload, stamped ``trigger_type='rerun'`` and
``parent_run_id=<source run>``. Both ``ck_runs_trigger_type`` (runs) and
``ck_run_daily_facts_trigger_type`` (run_daily_facts) enumerate the legal
trigger vocabularies and reject the new value until widened.

Widen ceremony per the 0165/0197 precedent: drop the constraint, re-add with
the extended vocabulary NOT VALID (no ACCESS EXCLUSIVE table scan on a
populated table), then VALIDATE online (SHARE UPDATE EXCLUSIVE only).
ORM-created schemas (unit tests on SQLite) pick up the widened vocabulary
from the Run model's CheckConstraint directly.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from modulo.db.migrations._rls_ceremony import is_postgres as _is_postgres

revision: str = "0213_runs_rerun_trigger_type"
down_revision: str | None = "0212_variant_batch_state_updated_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The OLD vocabularies (pre-0213) and the WIDENED ones. Kept side by side so
# the downgrade restores the exact pre-migration expressions.
_RUNS_TRIGGER_TYPE_OLD = (
    "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', "
    "'correction', 'slack_app_mention')"
)
_RUNS_TRIGGER_TYPE_NEW = (
    "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', "
    "'correction', 'slack_app_mention', 'rerun')"
)
_FACTS_TRIGGER_TYPE_OLD = (
    "trigger_type IN ('manual','webhook','cron','polling','agent_signal','ongoing','correction','slack_app_mention')"
)
_FACTS_TRIGGER_TYPE_NEW = "trigger_type IN ('manual','webhook','cron','polling','agent_signal','ongoing','correction','slack_app_mention','rerun')"


def _widen(table: str, name: str, old_expr: str, new_expr: str) -> None:
    """Drop + re-add a trigger-vocabulary CHECK with the widened vocabulary."""
    op.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
    # NOT VALID: new writes are checked immediately, existing rows are not
    # rescanned at ADD time (no ACCESS EXCLUSIVE scan on populated tables);
    # VALIDATE then scans online under only a SHARE UPDATE EXCLUSIVE lock.
    op.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({new_expr}) NOT VALID"))
    op.execute(text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        # SQLite (ORM-created unit-test schemas) carries the widened vocabulary
        # via the Run model's CheckConstraint; no DDL to apply.
        return
    _widen("runs", "ck_runs_trigger_type", _RUNS_TRIGGER_TYPE_OLD, _RUNS_TRIGGER_TYPE_NEW)
    _widen(
        "run_daily_facts",
        "ck_run_daily_facts_trigger_type",
        _FACTS_TRIGGER_TYPE_OLD,
        _FACTS_TRIGGER_TYPE_NEW,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    _widen("runs", "ck_runs_trigger_type", _RUNS_TRIGGER_TYPE_NEW, _RUNS_TRIGGER_TYPE_OLD)
    _widen(
        "run_daily_facts",
        "ck_run_daily_facts_trigger_type",
        _FACTS_TRIGGER_TYPE_NEW,
        _FACTS_TRIGGER_TYPE_OLD,
    )
