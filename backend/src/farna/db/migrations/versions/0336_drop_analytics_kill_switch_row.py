"""Delete the inert product-analytics licence-enforcement kill-switch row (FAR-2179).

Revision ID: 0336_drop_analytics_kill_switch_row
Revises: 0335_eval_verdict_contract_stage2
Create Date: 2026-10-27

FAR-2063 (PR #2458) retired the analytics licence-enforcement kill switch: the
``LICENSE_ENFORCEMENT_KILL_SWITCH_KEY`` constant, the
``kill_switch_disables_enforcement`` helper and every reader were deleted, and
the switch is now resolved by
``core.product_analytics.consent.resolve_instance_analytics_switch`` (stored
``product_analytics_enabled`` -> ``MODULO_PRODUCT_ANALYTICS_ENABLED`` -> the
licence's ``product_analytics_required`` default -> OFF).

Retiring the key was a code-only change. ``system_config`` rows are written only
where a feature reads them, and nothing writes this key, so a row exists on an
instance only if an operator or an older build inserted one. On such an instance
the leftover row is now inert: no code reads it, but it still LOOKS like a
meaningful setting to anyone inspecting the table. This migration deletes it.

The migration is a bounded, idempotent, existence-gated data cleanup:

* the table probe uses ``has_table`` so a fresh database where ``system_config``
  was never created completes without error (and a second run deletes nothing);
* the ``DELETE`` is scoped to the exact retired key and passes it as a bound
  parameter - no other ``system_config`` row is touched;
* the pre-delete value is logged before the row is removed, so the deploy log
  records what was removed (the value is capped for the log; the row itself is a
  small switch scalar, not a credential).

``downgrade`` is a documented no-op: the deleted row cannot be reconstructed
(it existed only where someone had written it, and its value is unknown here).
Absence is also the safe state for an older image - the retired reader treated a
missing row as "enforcement enabled", its default - so a rollback loses no
required behaviour. The row's historical value is in the ``upgrade`` log line if
an operator wants to restore it by hand.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

_log = logging.getLogger("alembic.runtime.migration")

revision: str = "0336_drop_analytics_kill_switch_row"
down_revision: str | None = "0335_eval_verdict_contract_stage2"
branch_labels: str | None = None
depends_on: str | None = None

#: The retired SystemConfig key (FAR-2063 deleted its only readers). Frozen as a
#: module constant on purpose: a migration must stay deterministic and must not
#: import the live application package at run time.
RETIRED_KILL_SWITCH_KEY: str = "product_analytics_license_enforcement_kill_switch"

_SYSTEM_CONFIG_TABLE = "system_config"

#: Bound on the value rendered in the deploy log (audit trail for a manual
#: restore without dumping an arbitrarily large stored document).
_LOG_VALUE_MAX_CHARS = 200


def _system_config_table_exists(bind: sa.Connection) -> bool:
    """True when ``system_config`` exists (a fresh/unmigrated DB may not have it)."""
    return sa.inspect(bind).has_table(_SYSTEM_CONFIG_TABLE)


def _stored_kill_switch_value(bind: sa.Connection) -> Any | None:
    """Read the retired row's value BEFORE deleting it, for the audit log."""
    return bind.execute(
        sa.text("SELECT value FROM system_config WHERE key = :key"),
        {"key": RETIRED_KILL_SWITCH_KEY},
    ).scalar_one_or_none()


def _describe_stored_value(value: Any) -> str:
    rendered = repr(value)
    if len(rendered) > _LOG_VALUE_MAX_CHARS:
        return rendered[:_LOG_VALUE_MAX_CHARS] + "...(truncated)"
    return rendered


def delete_retired_kill_switch_row(bind: sa.Connection) -> int:
    """Delete the retired kill-switch row if present; return rows deleted.

    Existence-gated on the table and scoped to the exact key, so it is
    idempotent (a second run returns 0) and can never touch another
    ``system_config`` row.
    """
    if not _system_config_table_exists(bind):
        return 0
    result = bind.execute(
        sa.text("DELETE FROM system_config WHERE key = :key"),
        {"key": RETIRED_KILL_SWITCH_KEY},
    )
    return result.rowcount or 0


def upgrade() -> None:
    bind = op.get_bind()
    stored = _stored_kill_switch_value(bind) if _system_config_table_exists(bind) else None
    deleted = delete_retired_kill_switch_row(bind)
    if deleted:
        # Observability: name the exact key and the value it held, so the
        # removal is an auditable act rather than an invisible side effect.
        _log.info(
            "migration 0336: deleted retired %s row (stored value=%s)",
            RETIRED_KILL_SWITCH_KEY,
            _describe_stored_value(stored),
        )
    else:
        _log.info("migration 0336: no %s row present - nothing to delete", RETIRED_KILL_SWITCH_KEY)


def downgrade() -> None:
    """No-op: the removed row cannot be reconstructed (see the module docstring).

    Absence is the safe state for an older image, whose reader treated a missing
    row as "enforcement enabled" (its default). The value that was removed is
    recorded in the ``upgrade`` log line for a manual restore if needed.
    """
    _log.info(
        "migration 0336: downgrade is a no-op - the retired %s row is not restored",
        RETIRED_KILL_SWITCH_KEY,
    )
