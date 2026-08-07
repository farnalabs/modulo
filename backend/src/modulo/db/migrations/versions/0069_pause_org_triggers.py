"""Org-wide "pause all pipeline triggers" kill-switch.

Revision ID: 0069_pause_org_triggers
Revises: 0068_add_teams_deleted_at
Create Date: 2026-08-04

Two changes:

(a) ``organisations`` gains ``triggers_paused`` (BOOLEAN NOT NULL DEFAULT
FALSE) and ``triggers_paused_at`` (TIMESTAMPTZ NULL) plus a CHECK constraint
``NOT triggers_paused OR triggers_paused_at IS NOT NULL`` so an admin toggle
can never be persisted without an audit timestamp.

(b) ``trigger_events.ck_trigger_events_validation_result`` is re-created with
the FULL 19-value vocabulary. This fixes a pre-existing bug: the code already
writes ``event_type_not_accepted``, ``spend_limit_reached``, ``no_pipeline``,
and ``test`` today, all of which are absent from the old 14-value constraint —
those writes currently fail with an IntegrityError that surfaces as a 503.
A defensive ``SELECT`` runs before ``VALIDATE CONSTRAINT`` and raises (without
mutating rows) if any existing row violates the new vocabulary.

The vocabulary is HARDCODED here — migrations never import app constants.

Multi-backend notes (M7): ``NOT VALID`` / ``VALIDATE CONSTRAINT`` /
``ALTER TABLE ... ADD/DROP CONSTRAINT`` are Postgres-only. On SQLite
(dev/tests, ``env.py`` uses ``render_as_batch``) the constraint re-creation
goes through ``batch_alter_table`` (full table rebuild, which enforces the
check on existing rows — the SQLite analogue of VALIDATE) and the columns use
portable ``sa.Boolean`` / ``sa.TIMESTAMP(timezone=True)`` (SQLite ignores the
timezone flag but accepts the type).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import bindparam, text

revision: str = "0069_pause_org_triggers"
down_revision: str | None = "0068_add_teams_deleted_at"

# Full validation_result vocabulary. Keep in sync with
# ``modulo.db.models.trigger_event.VALIDATION_RESULT_VALUES``.
_VALIDATION_RESULT_VALUES = (
    "accepted",
    "passed",
    "hmac_failed",
    "schema_validation_failed",
    "deduplicated",
    "concurrency_limit_reached",
    "flood_rejected",
    "timestamp_expired",
    "validation_failed",
    "rate_limited",
    "no_match",
    "condition_met",
    "poll_error",
    "signal_fired",
    "event_type_not_accepted",
    "spend_limit_reached",
    "no_pipeline",
    "test",
    "paused",
)

_CHECK_CONSTRAINT_NAME = "ck_trigger_events_validation_result"
_ORG_CHECK_CONSTRAINT_NAME = "ck_organisations_triggers_paused_at"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()
    is_postgres = _is_postgres()

    # (a) organisations — additive columns + consistency CHECK. Portable column
    # types (sa.Boolean / sa.TIMESTAMP(timezone=True)); the CHECK constraint
    # needs dialect-specific DDL (raw ALTER on Postgres, batch rebuild on
    # SQLite which cannot ADD CONSTRAINT).
    op.add_column(
        "organisations",
        sa.Column("triggers_paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "organisations",
        sa.Column("triggers_paused_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    if is_postgres:
        op.execute(
            f"ALTER TABLE organisations ADD CONSTRAINT {_ORG_CHECK_CONSTRAINT_NAME} "
            "CHECK (NOT triggers_paused OR triggers_paused_at IS NOT NULL)"
        )
    else:
        with op.batch_alter_table("organisations") as batch:
            batch.create_check_constraint(
                _ORG_CHECK_CONSTRAINT_NAME,
                "NOT triggers_paused OR triggers_paused_at IS NOT NULL",
            )

    # (b) trigger_events — drop the old 14-value constraint and re-add the full
    # vocabulary. Postgres: ``NOT VALID`` skips the full-table scan during the
    # ADD; the explicit VALIDATE catches pre-existing offenders. SQLite: batch
    # mode rebuilds the table and enforces the check on existing rows.
    vocab_sql = ", ".join(f"'{v}'" for v in _VALIDATION_RESULT_VALUES)
    if is_postgres:
        op.execute(f"ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT_NAME}")
        op.execute(
            f"ALTER TABLE trigger_events ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} "
            f"CHECK (validation_result IN ({vocab_sql})) NOT VALID"
        )

        # Defensive check — do NOT mutate rows, only surface offenders loudly.
        # Expanding bind parameter: injection-safe, no string interpolation.
        bad = (
            conn.execute(
                text(
                    "SELECT DISTINCT validation_result FROM trigger_events WHERE validation_result NOT IN :vocab"
                ).bindparams(bindparam("vocab", expanding=True)),
                {"vocab": list(_VALIDATION_RESULT_VALUES)},
            )
            .scalars()
            .all()
        )
        if bad:
            raise RuntimeError(
                "Cannot widen ck_trigger_events_validation_result: existing rows carry "
                f"out-of-vocabulary validation_result values: {sorted(str(v) for v in bad)}"
            )

        op.execute(f"ALTER TABLE trigger_events VALIDATE CONSTRAINT {_CHECK_CONSTRAINT_NAME}")
    else:
        with op.batch_alter_table("trigger_events") as batch:
            batch.drop_constraint(_CHECK_CONSTRAINT_NAME, type_="check")
            batch.create_check_constraint(
                _CHECK_CONSTRAINT_NAME,
                f"validation_result IN ({vocab_sql})",
            )


def downgrade() -> None:
    is_postgres = _is_postgres()
    vocab_sql = (
        "'accepted', 'passed', 'hmac_failed', 'schema_validation_failed', "
        "'deduplicated', 'concurrency_limit_reached', 'flood_rejected', "
        "'timestamp_expired', 'validation_failed', 'rate_limited', 'no_match', "
        "'condition_met', 'poll_error', 'signal_fired'"
    )
    if is_postgres:
        op.execute(f"ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT_NAME}")
        op.execute(
            f"ALTER TABLE trigger_events ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} "
            f"CHECK (validation_result IN ({vocab_sql}))"
        )
        op.execute(f"ALTER TABLE organisations DROP CONSTRAINT IF EXISTS {_ORG_CHECK_CONSTRAINT_NAME}")
        op.execute("ALTER TABLE organisations DROP COLUMN IF EXISTS triggers_paused_at")
        op.execute("ALTER TABLE organisations DROP COLUMN IF EXISTS triggers_paused")
    else:
        with op.batch_alter_table("trigger_events") as batch:
            batch.drop_constraint(_CHECK_CONSTRAINT_NAME, type_="check")
            batch.create_check_constraint(
                _CHECK_CONSTRAINT_NAME,
                f"validation_result IN ({vocab_sql})",
            )
        with op.batch_alter_table("organisations") as batch:
            batch.drop_constraint(_ORG_CHECK_CONSTRAINT_NAME, type_="check")
        op.drop_column("organisations", "triggers_paused_at")
        op.drop_column("organisations", "triggers_paused")
