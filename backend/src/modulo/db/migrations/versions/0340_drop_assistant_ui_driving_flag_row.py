"""Delete the retired ``assistant_ui_driving`` feature-flag catalog row (FAR-2232).

Revision ID: 0340_drop_assistant_ui_driving_flag_row
Revises: 0339_triggers_name_not_null
Create Date: 2026-10-30

FAR-2232 removed Assistant browser UI driving: the frontend executor, the
backend tool surface, the ``assistant_ui_driving`` entry in
``modulo.core.feature_flags._KNOWN_FLAGS`` and its derived seed-catalog entry
were all deleted. Retiring a flag is a code-only change for the *reader*, but
not for an existing database: the boot seeder only upserts catalog rows with
``INSERT ... ON CONFLICT (name) DO NOTHING`` — it never deletes — so every
instance seeded while the flag existed (staging, production) keeps a
``feature_flag_catalog`` row for it. The admin Feature Flags listing is
DB-backed, so that row stays user-visible as a flag that gates nothing.

Migration 0317 fixed this class systematically by sweeping every catalog row
whose name is absent from its frozen live-registry snapshot. That snapshot was
taken before FAR-2232, so ``assistant_ui_driving`` was still "live" then and
survived the sweep. The flag has since been retired, so this migration removes
its row.

This is a bounded, idempotent, existence-gated DATA cleanup — NO schema change,
and ``tier_catalog`` is never mutated:

* the table probe uses ``sa.inspect(...).has_table`` so a fresh database where
  ``feature_flag_catalog`` was never created completes without error (and a
  second run deletes nothing);
* the ``DELETE`` is scoped to the exact retired name and passes it as a bound
  parameter — no other catalog row is touched;
* ``downgrade`` re-inserts the known historical row (name, description and
  ``community`` tier, captured from the last registry revision that declared
  it) with ``ON CONFLICT (name) DO NOTHING``, restricted to a tier that actually
  exists in ``tier_catalog`` so it is idempotent and cannot raise a foreign-key
  violation.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

_log = logging.getLogger("alembic.runtime.migration")

revision: str = "0340_drop_assistant_ui_driving_flag_row"
down_revision: str | None = "0339_triggers_name_not_null"
branch_labels: str | None = None
depends_on: str | None = None

#: The retired flag name (FAR-2232 removed its only reader). Frozen as a module
#: constant on purpose: a migration must stay deterministic and must not import
#: the live application package at run time.
RETIRED_FLAG_NAME: str = "assistant_ui_driving"

#: The row's historical description, captured from the last ``_KNOWN_FLAGS``
#: revision that declared it (``7b7cdf267``), used only by ``downgrade``.
RETIRED_FLAG_DESCRIPTION: str = (
    "Assistant browser UI driving — allows Assistant to navigate, click, and fill forms on your behalf."
)

#: The row's historical tier (``community``).
RETIRED_FLAG_TIER_ID: str = "community"

_CATALOG_TABLE = "feature_flag_catalog"
_TIER_TABLE = "tier_catalog"


def _has_table(bind: sa.Connection, name: str) -> bool:
    """True when ``name`` exists (a fresh/unmigrated DB may not have it)."""
    return sa.inspect(bind).has_table(name)


def delete_retired_flag_row(bind: sa.Connection) -> int:
    """Delete the retired catalog row if present; return rows deleted.

    Existence-gated on the table and scoped to the exact name, so it is
    idempotent (a second run returns 0) and can never touch another row.
    """
    if not _has_table(bind, _CATALOG_TABLE):
        return 0
    result = bind.execute(
        sa.text('DELETE FROM "feature_flag_catalog" WHERE name = :name'),
        {"name": RETIRED_FLAG_NAME},
    )
    return result.rowcount or 0


def restore_retired_flag_row(bind: sa.Connection) -> int:
    """Re-insert the removed row (``downgrade``); return rows inserted.

    Idempotent via ``ON CONFLICT (name) DO NOTHING`` and restricted to a
    ``tier_id`` that actually exists, so a database whose tier table was never
    seeded (or is absent) cannot raise an FK violation.
    """
    if not _has_table(bind, _CATALOG_TABLE) or not _has_table(bind, _TIER_TABLE):
        return 0
    result = bind.execute(
        sa.text(
            'INSERT INTO "feature_flag_catalog" (name, description, tier_id, depends_on, is_active) '
            "SELECT :name, :description, :tier_id, NULL, :is_active "
            'WHERE EXISTS (SELECT 1 FROM "tier_catalog" WHERE tier_id = :tier_check) '
            "ON CONFLICT (name) DO NOTHING"
        ),
        {
            "name": RETIRED_FLAG_NAME,
            "description": RETIRED_FLAG_DESCRIPTION,
            # A distinct bind name for the existence check: reusing ``tier_id``
            # in both the SELECT list and the WHERE makes the driver deduce two
            # inconsistent types for one parameter.
            "tier_id": RETIRED_FLAG_TIER_ID,
            "is_active": True,
            "tier_check": RETIRED_FLAG_TIER_ID,
        },
    )
    return result.rowcount or 0


def upgrade() -> None:
    deleted = delete_retired_flag_row(op.get_bind())
    # Observability: a 0-row sweep is distinguishable in the deploy log from a
    # wrong database, so the count is always reported.
    _log.info(
        "migration 0340: deleted %d retired %s feature_flag_catalog row(s)",
        deleted,
        RETIRED_FLAG_NAME,
    )


def downgrade() -> None:
    restored = restore_retired_flag_row(op.get_bind())
    _log.info(
        "migration 0340: restored %d retired %s feature_flag_catalog row(s)",
        restored,
        RETIRED_FLAG_NAME,
    )
