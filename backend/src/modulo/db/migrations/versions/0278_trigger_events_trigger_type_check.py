"""improve-database(trigger_event): trigger_type vocabulary CHECK.

Revision ID: 0278_trigger_events_trigger_type_check
Revises: 0277_trigger_events_listing_indexes
Create Date: 2026-10-03

Covers ``backend/src/modulo/db/models/trigger_event.py``
(``trigger_events``), same area as 0277. One data-integrity gap: the
``trigger_type`` column has no DB-level vocabulary check, while the
parent ``triggers`` table enforces ``ck_triggers_type`` over the exact
same 7-value vocabulary. Nothing at the DB level stops an event row from
disagreeing with the trigger vocabulary it logs.

* ``ck_trigger_events_trigger_type`` — every write site provably stores
  an in-vocabulary value: literals ``webhook`` (routes/webhooks,
  trigger_engine), ``agent_signal`` (trigger_engine/agent_signal),
  ``polling`` (trigger_engine/polling, cron_helpers), ``cron`` /
  ``ongoing`` (cron_helpers fire-job loggers), ``slack_app_mention``
  (routes/slack, trigger_busy) — or a passthrough of
  ``Trigger.trigger_type`` (routes/triggers test-fire,
  trigger_engine ``trigger_type or trigger.trigger_type``), which is
  itself CHECK-constrained. ``Run.trigger_type`` values outside this
  vocabulary (``rerun``, ``correction`` — 0213) never reach this table:
  reruns copy the source run's payload without writing an event row.

Deliberately NOT constrained: ``raw_payload_hash`` length. Every hash
write stores ``sha256_hex(...)`` (64 chars) EXCEPT the busy-replay path
(``api/trigger_busy.py``), which deliberately carries ``""`` onto the
audit row when the original payload is unavailable — a
``char_length = 64`` CHECK would reject that faithful-audit write.

Deploy-safety: the CHECK is added ``NOT VALID`` then ``VALIDATE``-d,
mirroring ``0151_fix_constraints`` / ``0165_add_check_constraints`` /
0269. The ADD takes only a brief lock and does NOT scan existing rows,
so a populated table never aborts the upgrade; the VALIDATE is online.
All write paths store in-set values, so VALIDATE succeeds in practice.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0278_trigger_events_trigger_type_check"
down_revision: str | None = "0277_trigger_events_listing_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# (add_sql, validate_sql) — added NOT VALID then VALIDATE-d. Literal SQL
# (no f-string interpolation): the semgrep raw-sql-fstring /
# migration-fstring-sql rules reject interpolated DDL.
_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ALTER TABLE trigger_events ADD CONSTRAINT ck_trigger_events_trigger_type CHECK (trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'slack_app_mention')) NOT VALID",
        "ALTER TABLE trigger_events VALIDATE CONSTRAINT ck_trigger_events_trigger_type",
    ),
)


def upgrade() -> None:
    # CHECK constraint: add NOT VALID (no row scan at ADD time) then
    # VALIDATE online, so a populated table never aborts the upgrade.
    for add_sql, validate_sql in _CHECKS:
        op.execute(text(add_sql))
        op.execute(text(validate_sql))


def downgrade() -> None:
    op.drop_constraint("ck_trigger_events_trigger_type", "trigger_events", type_="check")
