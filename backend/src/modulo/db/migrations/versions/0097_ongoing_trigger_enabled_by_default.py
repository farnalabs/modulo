"""Enable the ongoing_trigger feature flag by default (ON)

Revision ID: 0097_ongoing_trigger_enabled_by_default
Revises: 0096_hitl_claims_overdue_notified
Create Date: 2026-08-13

``0095_ongoing_trigger_flag`` shipped the ``ongoing`` trigger type (FAR-158)
as a feature-flagged experiment, upserting ``ongoing_trigger`` into
``feature_flag_catalog`` with ``is_active=false`` (default OFF on every tier).
The product decision is now that the flag ships ON by default (community
tier), so this migration flips the catalog row to ``is_active=true``.

Migration tree: ``0093_run_number_sequence`` -> ``0094_ongoing_trigger_type``
-> ``0095_ongoing_trigger_flag`` -> ``0096_hitl_claims_overdue_notified``
-> ``0097_ongoing_trigger_enabled_by_default`` (sole head).

It mirrors ``0095_ongoing_trigger_flag``'s upsert structure: on Postgres the
``ON CONFLICT (name) DO UPDATE`` flips existing rows in place (the seed paths
use ``ON CONFLICT DO NOTHING``, so existing deployments would otherwise never
pick up the active state); on non-Postgres dev backends the upsert is emulated
as delete-then-insert. The ``community``/``team`` tier rows are re-seeded (ON
CONFLICT DO NOTHING) because ``feature_flag_catalog.tier_id`` is FK-constrained
to ``tier_catalog``. Data-only — no tables or columns are created or dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0097_ongoing_trigger_enabled_by_default"
down_revision: str | None = "0096_hitl_claims_overdue_notified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Flag registered in ``modulo.core.feature_flags._KNOWN_FLAGS`` and seeded in
# ``modulo.core.seed_data.catalog.FLAGS``. name -> (tier_id, description).
_FLAGS: dict[str, tuple[str, str]] = {
    "ongoing_trigger": ("community", "Keep a pipeline topped up to a target number of in-flight runs"),
}

# Desired active state after this migration. ``0095_ongoing_trigger_flag``
# seeded the row ``is_active=false``; this migration flips it to true so the
# ``ongoing`` trigger type ships ON by default on the community tier.
_ACTIVE: bool = True

# Tier catalog seed rows (mirrors ``modulo.core.seed_data.catalog.TIERS``). The
# ``feature_flag_catalog.tier_id`` column is FK-constrained to ``tier_catalog``,
# and ``tier_catalog`` is populated by the app startup seed — which runs AFTER
# Alembic migrations. A fresh deployment therefore has an empty ``tier_catalog``
# when this migration runs, so the flag upsert below would violate the FK. Seed
# the tiers here first (ON CONFLICT DO NOTHING keeps existing rows untouched).
_TIERS: list[dict[str, object]] = [
    {
        "tier_id": "community",
        "label": "Community",
        "rank": 0,
        "requires_license": False,
        "description": "Free tier, no license key required",
    },
    {
        "tier_id": "team",
        "label": "Team",
        "rank": 1,
        "requires_license": True,
        "description": "Self-serve paid tier with team features",
    },
]


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _seed_tiers() -> None:
    for tier in _TIERS:
        op.execute(
            sa.text(
                """
                INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description)
                VALUES (:tier_id, :label, :rank, :requires_license, :description)
                ON CONFLICT (tier_id) DO NOTHING
                """
            ).bindparams(**tier)
        )


def _upsert(name: str, tier_id: str, description: str, is_active: bool) -> None:
    _seed_tiers()
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute(
            sa.text(
                """
                INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active)
                VALUES (:name, :description, :tier_id, NULL, :is_active)
                ON CONFLICT (name) DO UPDATE SET
                    tier_id = excluded.tier_id,
                    is_active = excluded.is_active,
                    description = excluded.description,
                    depends_on = excluded.depends_on
                """
            ).bindparams(
                name=name,
                description=description,
                tier_id=tier_id,
                is_active=is_active,
            )
        )
    else:
        # Non-Postgres dev backends lack ``ON CONFLICT ... DO UPDATE``; emulate
        # the upsert as delete-then-insert (the catalog is seed data, re-seeded
        # at startup).
        op.execute(sa.text("DELETE FROM feature_flag_catalog WHERE name = :name").bindparams(name=name))
        op.execute(
            sa.text(
                "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
                "VALUES (:name, :description, :tier_id, NULL, :is_active)"
            ).bindparams(
                name=name,
                description=description,
                tier_id=tier_id,
                is_active=is_active,
            )
        )


def upgrade() -> None:
    for name, (tier_id, description) in _FLAGS.items():
        _upsert(name=name, tier_id=tier_id, description=description, is_active=_ACTIVE)


def downgrade() -> None:
    # Restore the state 0095_ongoing_trigger_flag produced: row present but
    # inactive (default OFF).
    for name, (tier_id, description) in _FLAGS.items():
        _upsert(name=name, tier_id=tier_id, description=description, is_active=False)
