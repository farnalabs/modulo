"""Boot seeder reconciliation (FAR-1875).

``modulo.api.main._seed_tier_catalog`` inserts the code registry's flags into
``feature_flag_catalog`` with ``INSERT ... ON CONFLICT (name) DO NOTHING`` and,
after this change, prunes every catalog row whose ``name`` is not in the current
code registry. Without the prune, retiring a flag is a code-only change that
leaves the retired flag's row behind forever, and the DB-backed admin Feature
Flags listing keeps surfacing a flag that gates nothing — the defect that
required a one-off migration for every retirement.

This proves, against REAL Postgres (the fully migrated testcontainer), that a
stale row present before boot is GONE after the seeder runs, that a live flag is
properly seeded, and that a second boot is idempotent. It is the standing
root-cause fix, so a future retirement never needs another migration.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from modulo.api import main

pytestmark = [pytest.mark.integration]

#: A name absent from the code registry — the row a real boot must prune.
STALE_SENTINEL = "zzz_far1875_boot_prune_sentinel"
#: A live registry flag the seeder must (re)seed.
LIVE_FLAG = "email_config"


async def _catalog_names(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT name FROM feature_flag_catalog"))).scalars().all()
        return set(rows)
    finally:
        await engine.dispose()


async def _remove_sentinel(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM feature_flag_catalog WHERE name = :n"), {"n": STALE_SENTINEL})
    finally:
        await engine.dispose()


async def test_seed_tier_catalog_prunes_stale_rows(migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(migrated_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # The two FK targets the seeder and the sentinel need.
            await conn.execute(
                text(
                    "INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description) VALUES "
                    "('community', 'Community', 0, false, 'Free tier'), "
                    "('team', 'Team', 1, true, 'Paid tier') ON CONFLICT (tier_id) DO NOTHING"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
                    "VALUES (:n, :n, 'team', NULL, true) ON CONFLICT (name) DO NOTHING"
                ),
                {"n": STALE_SENTINEL},
            )

        # Point the seeder's engine seam at this testcontainer for this call.
        monkeypatch.setattr("modulo.db.session.engine", engine)

        assert STALE_SENTINEL in await _catalog_names(migrated_db_url)

        await main._seed_tier_catalog()

        names = await _catalog_names(migrated_db_url)
        assert STALE_SENTINEL not in names, "boot seeder must prune a catalog row absent from the code registry"
        assert LIVE_FLAG in names, "boot seeder must still seed a live flag"

        # Idempotent: a second boot changes nothing.
        await main._seed_tier_catalog()
        assert await _catalog_names(migrated_db_url) == names
    finally:
        await engine.dispose()
        await _remove_sentinel(migrated_db_url)
