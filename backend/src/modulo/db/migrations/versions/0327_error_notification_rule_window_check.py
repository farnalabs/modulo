"""Relax ck_error_notification_rule_window to allow the supported 0 window.

Revision ID: 0327_error_notification_rule_window_check
Revises: 0326_error_tracking_lookup_indexes
Create Date: 2026-10-23

``0165_add_check_constraints`` added
``CHECK (condition_window_seconds > 0)`` on ``error_notification_rules``. The
application's contract for that column is ``>= 0``, where **0 means "no
sliding window"** (use the incoming lifetime count instead):

* ``AlertEngine._resolve_effective_count``
  (``core/error_tracking/alerting.py``) branches on
  ``window_seconds = rule.condition_window_seconds or 0`` and returns the raw
  incoming ``count`` when it is ``<= 0`` — 0 is a handled, meaningful value,
  not an edge case it guards against.
* ``seed_default_alert_rules_for_org``
  (``core/error_tracking/__init__.py``) writes ``condition_window_seconds=0``
  for every seeded default rule (one per signal). The seed's own existence
  check (0165) assumed "the app only ever writes in-set values" — that
  assumption is false for this column.
* The rule factories and BDD steps use ``condition_window_seconds=0`` to
  express a lifetime-count rule.

Consequence of the strict check: on Postgres every default-rule INSERT is
rejected, so ``seed_default_alert_rules_for_org`` raises for each org and
``seed_default_alert_rules`` swallows it per-org (logged, org skipped) — the
shipped defaults never seed, and a Team org cannot create a "no window" rule
through the service layer either. Unit tests build their schema from ORM
metadata (the model declares no such CHECK), so the suite stays green while
the migrated database rejects the write.

The relaxed predicate is strictly weaker than the one it replaces, so every
row that satisfied the old check satisfies the new one; VALIDATE cannot fail
on existing data. The route-level contract is unchanged (``ge=1`` for
tenant-supplied rules); this only stops the database from rejecting the
supported internal value.

Postgres only: ``ALTER TABLE ... DROP/ADD CONSTRAINT`` is not supported by
SQLite, whose test schema is built from ORM metadata and never carries this
migration-owned check in the first place (the 0165/0292 precedent).

Downgrade restores the ``> 0`` predicate ``NOT VALID`` — a validated
constraint would abort the downgrade on rows the upgrade deliberately made
legal (any seeded rule with a 0 window).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0327_error_notification_rule_window_check"
down_revision: str | None = "0326_error_tracking_lookup_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_CONSTRAINT = "ck_error_notification_rule_window"
_TABLE = "error_notification_rules"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.execute(
        sa.text(
            "ALTER TABLE error_notification_rules "
            "ADD CONSTRAINT ck_error_notification_rule_window "
            "CHECK (condition_window_seconds >= 0) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE error_notification_rules VALIDATE CONSTRAINT ck_error_notification_rule_window"))


def downgrade() -> None:
    if not _is_postgres():
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.execute(
        sa.text(
            "ALTER TABLE error_notification_rules "
            "ADD CONSTRAINT ck_error_notification_rule_window "
            "CHECK (condition_window_seconds > 0) NOT VALID"
        )
    )
