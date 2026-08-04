"""Org-wide "pause all pipeline triggers" kill-switch.

Revision ID: 0065_pause_org_triggers
Revises: 0064_merge_heads_0037
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
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import bindparam, text

revision: str = "0065_pause_org_triggers"
down_revision: str | None = "0064_merge_heads_0037"

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


def upgrade() -> None:
    conn = op.get_bind()

    # (a) organisations — additive columns + consistency CHECK.
    op.execute("ALTER TABLE organisations ADD COLUMN triggers_paused BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE organisations ADD COLUMN triggers_paused_at TIMESTAMPTZ NULL")
    op.execute(
        "ALTER TABLE organisations ADD CONSTRAINT ck_organisations_triggers_paused_at "
        "CHECK (NOT triggers_paused OR triggers_paused_at IS NOT NULL)"
    )

    # (b) trigger_events — drop the old 14-value constraint and re-add the full
    # vocabulary as NOT VALID, then validate. ``NOT VALID`` skips the full-table
    # scan during the ADD; the explicit VALIDATE catches pre-existing offenders.
    op.execute(f"ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT_NAME}")
    vocab_sql = ", ".join(f"'{v}'" for v in _VALIDATION_RESULT_VALUES)
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


def downgrade() -> None:
    op.execute(f"ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT_NAME}")
    op.execute(
        "ALTER TABLE trigger_events ADD CONSTRAINT ck_trigger_events_validation_result "
        "CHECK (validation_result IN ('accepted', 'passed', 'hmac_failed', "
        "'schema_validation_failed', 'deduplicated', 'concurrency_limit_reached', "
        "'flood_rejected', 'timestamp_expired', 'validation_failed', 'rate_limited', "
        "'no_match', 'condition_met', 'poll_error', 'signal_fired'))"
    )
    op.execute("ALTER TABLE organisations DROP CONSTRAINT IF EXISTS ck_organisations_triggers_paused_at")
    op.execute("ALTER TABLE organisations DROP COLUMN IF EXISTS triggers_paused_at")
    op.execute("ALTER TABLE organisations DROP COLUMN IF EXISTS triggers_paused")
