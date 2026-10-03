"""FAR-1445 database-hygiene readiness check against a real Postgres.

Unit tests grade the reading at its threshold boundaries; this module proves
the REAL query shape — one round trip over ``pg_stat_user_tables`` +
``age(datfrozenxid)`` — against a live server, seeded with dead tuples so the
bloat branch is exercised end-to-end (row shape, the CTE's LEFT JOIN when
nothing is over the floor, and the ``pg_stat_user_tables`` column semantics
can all be wrong in a way no mocked row can reveal).

No wall-clock dependence: the seed forces its statistics to the collector
with ``pg_stat_force_next_flush()`` instead of sleeping.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from modulo.api.routes.health import _check_db_hygiene
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

#: Own table for the seed — vacuum would reset ``n_dead_tup``, so the seed
#: disables autovacuum on it for the duration of the test.
_SEED_TABLE = "hygiene_seed_bloat"


async def _drop_seed_table(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS hygiene_seed_bloat"))
            await conn.commit()
    finally:
        await engine.dispose()


async def _seed_bloated_table(url: str) -> None:
    """Create a table at ~95% dead and push the counters to the collector.

    200,000 rows inserted, 190,000 deleted: 190,000 dead (far over the
    10,000 floor) at a ~95% ratio (far over the 60% threshold) — the shape
    of the FAR-1445 incident, at a size the test can build quickly.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS hygiene_seed_bloat"))
            await conn.execute(text("CREATE TABLE hygiene_seed_bloat (id bigint PRIMARY KEY)"))
            # A vacuum would RESET n_dead_tup and race the assertion.
            await conn.execute(text("ALTER TABLE hygiene_seed_bloat SET (autovacuum_enabled = off)"))
            await conn.execute(text("INSERT INTO hygiene_seed_bloat SELECT generate_series(1, 200000)"))
            await conn.execute(text("DELETE FROM hygiene_seed_bloat WHERE id <= 190000"))
            # Statistics are approximate by design; this guarantees the
            # collector owns the pending counts when this transaction commits.
            await conn.execute(text("SELECT pg_stat_force_next_flush()"))
            await conn.commit()
    finally:
        await engine.dispose()


class TestDatabaseHygieneCheckAgainstRealPostgres:
    async def test_healthy_migrated_database_grades_ok(self, migrated_db_url: str) -> None:
        """A freshly migrated database — nothing over the dead-tuple floor —
        reads ``ok`` through the real engine, settings and query."""
        get_settings.cache_clear()
        result = await _check_db_hygiene()
        assert result.status == "ok"
        assert result.detail is not None
        assert "within thresholds" in result.detail
        assert "freeze age" in result.detail
        assert result.latency_ms is not None

    async def test_seeded_bloat_grades_degraded_and_names_the_table(self, migrated_db_url: str) -> None:
        await _seed_bloated_table(migrated_db_url)
        try:
            get_settings.cache_clear()
            result = await _check_db_hygiene()
        finally:
            await _drop_seed_table(migrated_db_url)
        assert result.status == "degraded"
        assert result.detail is not None
        assert _SEED_TABLE in result.detail
        assert "DEAD-TUPLE BLOAT" in result.detail

    async def test_freeze_age_is_read_from_the_live_server(self, migrated_db_url: str) -> None:
        """The wraparound half must read the server's own
        ``autovacuum_freeze_max_age`` and this database's ``datfrozenxid`` —
        not a hard-coded ceiling."""
        get_settings.cache_clear()
        result = await _check_db_hygiene()
        assert result.detail is not None
        # postgres:16-alpine default, and a young test database's age is
        # nowhere near it — both halves of the reading are live values.
        assert "/200,000,000" in result.detail
        assert "AT/ABOVE" not in result.detail
