"""Seed the tier catalog tables with default tier definitions.

Usage:
    uv run python -m scripts.seed_tier_catalog

Requires DATABASE_URL env var or a running modulo instance with settings loaded.

The tier/flag definitions are imported from ``modulo.core.seed_data.catalog``
— the same source the application boot seed (``main._seed_tier_catalog``) uses
— so this standalone script can never drift from the runtime catalog. Seeding
is idempotent: re-running on an already seeded database is a no-op
(``ON CONFLICT ... DO NOTHING``). The script validates its output by reading
back the seeded row counts and fails loudly if either table is empty.
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from modulo.core.seed_data.catalog import FLAGS, TIERS
from modulo.settings import get_settings


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with AsyncSession(engine) as session:
        for tier in TIERS:
            await session.execute(
                text("""
                    INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description)
                    VALUES (:tier_id, :label, :rank, :requires_license, :description)
                    ON CONFLICT (tier_id) DO NOTHING
                """),
                tier,
            )
        for flag in FLAGS:
            await session.execute(
                text("""
                    INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active)
                    VALUES (:name, :description, :tier_id, :depends_on, true)
                    ON CONFLICT (name) DO NOTHING
                """),
                flag,
            )
        await session.commit()

        tier_count = (await session.execute(text("SELECT count(*) FROM tier_catalog"))).scalar_one()
        flag_count = (await session.execute(text("SELECT count(*) FROM feature_flag_catalog"))).scalar_one()
    await engine.dispose()

    print(
        f"Tier catalog seeded successfully: {tier_count} tiers, {flag_count} feature flags.",
        file=sys.stderr,
    )
    if tier_count == 0 or flag_count == 0:
        raise RuntimeError(
            f"Tier catalog seed verification failed: seeded {tier_count} tiers and {flag_count} feature flags."
        )


if __name__ == "__main__":
    asyncio.run(seed())
