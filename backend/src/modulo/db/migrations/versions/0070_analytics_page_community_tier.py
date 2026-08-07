"""Move analytics_page feature flag to community tier

Revision ID: 0070_analytics_page_community_tier
Revises: 0069_pause_org_triggers
Create Date: 2026-08-06

The ``analytics_page`` flag was seeded at ``team`` tier. The startup seed
(``_seed_tier_catalog`` in ``modulo.api.main``) uses ``ON CONFLICT (name) DO
NOTHING``, so existing deployments that already hold the catalog row keep it at
``team`` tier after the flag rollout. This migration UPSERTS the catalog row to
``community`` / active so the analytics dashboard is available on the free tier
everywhere. Data-only — no tables or columns are created or dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_analytics_page_community_tier"
down_revision: str | None = "0069_pause_org_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANALYTICS_PAGE_DESCRIPTION = "Run analytics dashboard (rolling-window run/cost/quality series)"


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _upsert(name: str, tier_id: str, is_active: bool) -> None:
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
                description=_ANALYTICS_PAGE_DESCRIPTION,
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
                description=_ANALYTICS_PAGE_DESCRIPTION,
                tier_id=tier_id,
                is_active=is_active,
            )
        )


def upgrade() -> None:
    _upsert(name="analytics_page", tier_id="community", is_active=True)


def downgrade() -> None:
    _upsert(name="analytics_page", tier_id="team", is_active=False)
