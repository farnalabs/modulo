"""Widen ``trigger_events.ck_trigger_events_validation_result`` with ``guardrail_blocked``.

Revision ID: 0106_trigger_event_guardrail_blocked
Revises: 0105_guardrail_pins
Create Date: 2026-08-16

FAR-214 vocabulary widening (0069/0104-pattern): the pre-trigger guardrail pass
writes a TriggerEvent row with ``validation_result='guardrail_blocked'`` when a
block-action guardrail rejects a webhook delivery at the trigger boundary
(``TriggerEngine.handle_webhook``). The 20-value constraint created by 0104
does NOT include ``guardrail_blocked``, so on a real Postgres the CHECK rejects
the insert and the whole intake transaction rolls back — the block would be
silently lost and the delivery acked anyway.

This migration drops the 20-value constraint and re-adds the FULL 21-value
vocabulary exactly as 0104 did:

* ``NOT VALID`` on the ADD skips the full-table scan during the ALTER.
* A defensive ``SELECT`` runs before ``VALIDATE CONSTRAINT`` and raises
  (without mutating rows) if any existing row is outside the new vocabulary —
  there can be none in practice because the 20-value constraint was enforced,
  but the guard surfaces an offender loudly instead of an opaque VALIDATE
  failure.
* ``VALIDATE CONSTRAINT`` backfills the constraint on the existing rows.

The downgrade drops the 21-value constraint and re-adds the OLD 20-value one.
A defensive ``SELECT`` runs first and raises if any row carries
``guardrail_blocked`` (the value the old constraint cannot express) — fail
loudly rather than leave a constraint that silently rejects existing rows.

The vocabulary is HARDCODED here — migrations never import app constants.

Multi-backend notes (M7): ``NOT VALID`` / ``VALIDATE CONSTRAINT`` /
``ALTER TABLE ... ADD/DROP CONSTRAINT`` are Postgres-only. On SQLite
(dev/tests, ``env.py`` uses ``render_as_batch``) the constraint re-creation
goes through ``batch_alter_table`` (full table rebuild, which enforces the
check on existing rows — the SQLite analogue of VALIDATE).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import bindparam, text

revision: str = "0106_trigger_event_guardrail_blocked"
down_revision: str | None = "0105_guardrail_pins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
    "auto_deactivated",
    "guardrail_blocked",
)

# The vocabulary BEFORE this migration (0104's 20-value set). Kept as a
# literal here so the downgrade can restore exactly what 0104 created.
_OLD_VALIDATION_RESULT_VALUES = tuple(v for v in _VALIDATION_RESULT_VALUES if v != "guardrail_blocked")

_CHECK_CONSTRAINT_NAME = "ck_trigger_events_validation_result"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _orphan_check(conn: sa.Connection, vocab: Sequence[str]) -> None:
    """Surface existing rows outside *vocab* loudly (never mutate data)."""
    bad = (
        conn.execute(
            text(
                "SELECT DISTINCT validation_result FROM trigger_events WHERE validation_result NOT IN :vocab"
            ).bindparams(bindparam("vocab", expanding=True)),
            {"vocab": list(vocab)},
        )
        .scalars()
        .all()
    )
    if bad:
        raise RuntimeError(
            "Cannot recreate ck_trigger_events_validation_result: existing rows carry "
            f"out-of-vocabulary validation_result values: {sorted(str(v) for v in bad)}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    vocab_sql = ", ".join(f"'{v}'" for v in _VALIDATION_RESULT_VALUES)
    if _is_postgres():
        op.execute(f"ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT_NAME}")
        op.execute(
            f"ALTER TABLE trigger_events ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} "
            f"CHECK (validation_result IN ({vocab_sql})) NOT VALID"
        )

        _orphan_check(conn, _VALIDATION_RESULT_VALUES)

        op.execute(f"ALTER TABLE trigger_events VALIDATE CONSTRAINT {_CHECK_CONSTRAINT_NAME}")
    else:
        with op.batch_alter_table("trigger_events") as batch:
            batch.drop_constraint(_CHECK_CONSTRAINT_NAME, type_="check")
            batch.create_check_constraint(
                _CHECK_CONSTRAINT_NAME,
                f"validation_result IN ({vocab_sql})",
            )


def downgrade() -> None:
    conn = op.get_bind()
    old_vocab_sql = ", ".join(f"'{v}'" for v in _OLD_VALIDATION_RESULT_VALUES)
    if _is_postgres():
        # Fail loudly if any row carries guardrail_blocked — the old 20-value
        # constraint cannot express it and re-adding would scan-fail anyway.
        _orphan_check(conn, _OLD_VALIDATION_RESULT_VALUES)

        op.execute(f"ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT_NAME}")
        op.execute(
            f"ALTER TABLE trigger_events ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} "
            f"CHECK (validation_result IN ({old_vocab_sql}))"
        )
    else:
        with op.batch_alter_table("trigger_events") as batch:
            batch.drop_constraint(_CHECK_CONSTRAINT_NAME, type_="check")
            batch.create_check_constraint(
                _CHECK_CONSTRAINT_NAME,
                f"validation_result IN ({old_vocab_sql})",
            )
