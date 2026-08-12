"""Register the ongoing_trigger feature flag as inactive (default OFF)

Revision ID: 0087_ongoing_trigger_flag
Revises: 0086_ongoing_trigger_type
Create Date: 2026-08-12

The ``ongoing`` trigger type (FAR-158) ships as a feature-flagged
experiment, default OFF on every tier. The flag is registered in
``modulo.core.feature_flags._KNOWN_FLAGS`` and seeded in
``modulo.core.seed_data.catalog.FLAGS``, but those seed paths use ``ON
CONFLICT (name) DO NOTHING`` on existing deployments — so existing databases
never pick the new row up without a migration.

This migration UPSERTS ``ongoing_trigger`` into ``feature_flag_catalog``
with ``is_active=false`` (mirroring 0080_add_mobile_sidebar_rail_flag). It
also seeds the ``community``/``team`` rows into ``tier_catalog`` (ON CONFLICT
DO NOTHING) because ``feature_flag_catalog.tier_id`` is FK-constrained to
``tier_catalog`` and that table is only populated by the app startup seed,
which runs after Alembic — on a fresh deployment the upsert would otherwise
violate the FK. Data-only — no tables or columns are created or dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087_ongoing_trigger_flag"
down_revision: str | None = "0086_ongoing_trigger_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Flag registered in ``modulo.core.feature_flags._KNOWN_FLAGS`` and seeded in
# ``modulo.core.seed_data.catalog.FLAGS``. name -> (tier_id, description).
_FLAGS: dict[str, tuple[str, str]] = {
    "ongoing_trigger": ("community", "Keep a pipeline topped up to a target number of in-flight runs"),
}

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
        _upsert(name=name, tier_id=tier_id, description=description, is_active=False)


def downgrade() -> None:
    # The flag did not exist in the DB catalog before this migration; revert to
    # that state.
    for name in _FLAGS:
        op.execute(sa.text("DELETE FROM feature_flag_catalog WHERE name = :name").bindparams(name=name))
