"""FAR-1445 database-hygiene readiness check against a real Postgres.

Unit tests grade the reading at its threshold boundaries; this module proves
the REAL query shape — one round trip over ``pg_stat_user_tables`` +
``age(datfrozenxid)`` — against a live server (row shape, the CTE's LEFT JOIN
when nothing is over the floor, the ``ORDER BY`` worst-table pick, and the
``pg_stat_user_tables`` column semantics can all be wrong in a way no mocked
row can reveal).

Two determinism rules, both learned from an observed intermittent failure of
this module (a flaky test is worse than no test):

1. **Every test binds the check to a database it controls.** The check grades
   the WHOLE database, so sharing the session's migrated database makes every
   assertion depend on state other tests create: migration leftovers and
   other tests' dead tuples race autovacuum's timing, and under parallel
   scheduling another test's seeded bloat table can be live while this test
   reads. Bloat tests therefore run in a freshly created, empty database that
   contains only what they seed; the freeze-age test binds the migrated
   database (its assertions — the server's own ``autovacuum_freeze_max_age``
   and a young database's age — are contamination-proof by construction).

2. **This module widens the per-check budget via env** (see
   ``_widen_check_budget``): the default 1s production budget must cover
   connection-pool checkout (TCP + auth + pre-ping + the RLS ``SET`` checkout
   hook), which on a loaded machine consumed it entirely — observed as a
   ``TimeoutError`` inside ``_check_db_hygiene`` under ``pytest -n 2``, making
   the healthy test report ``degraded`` with the probe-did-not-complete detail
   (FAR-1510: such an outcome is advisory, so it no longer gates readiness
   either). The budget's TIMEOUT behaviour itself is pinned by the unit test
   ``test_timeout_reports_degraded_never_unavailable``; what this module
   verifies is the query and the GRADING, which must not depend on machine
   load.

Statistics are forced to the collector with ``pg_stat_force_next_flush()``
inside the seeding transaction instead of sleeping: the flush is synchronous
at commit (probed directly — with force, an immediate read from a second
connection saw the counts 5/5; WITHOUT force the read missed 5/5, because
transaction-end flushes are rate-limited).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from modulo.api.dependencies import get_or_create_engine
from modulo.api.routes.health import _check_db_hygiene
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

#: Own table for the bloat seed — vacuum would reset ``n_dead_tup``, so the
#: seed disables autovacuum on it for the duration of the test.
_SEED_TABLE = "hygiene_seed_bloat"

#: Freshly created, never-touched table: 0 live + 0 dead → its ``dead_ratio``
#: is NULL (0/0), the row a ``min_dead = 0`` floor would otherwise admit.
_ZERO_TABLE = "hygiene_zero_rows"


@pytest.fixture(autouse=True)
def _widen_check_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the live-server checks a machine-speed budget, not a CI-speed one.

    30s: generous enough that connection checkout under parallel test load
    never eats the budget (the observed failure), still bounded so a genuinely
    hung query surfaces as ``degraded`` with the "probe did not complete"
    detail (advisory since FAR-1510) instead of hanging the run
    (``--timeout=600`` bounds the test itself).
    The 1s default and its timeout path are covered by unit tests.
    """
    monkeypatch.setenv("MODULO_HEALTH_DB_HYGIENE_TIMEOUT_SECONDS", "30")


async def _dispose_shared_engines() -> None:
    """Dispose and forget the process-global engines/session factory.

    ``get_or_create_engine`` builds the engine ONCE per process from the
    settings in force at first use and never re-reads the URL. Nothing in
    production ever changes ``DATABASE_URL`` mid-process, but tests do — so a
    suite run can carry an engine pointing at a different database into this
    module, and the check would silently grade the WRONG database. Dropping
    the globals forces a rebuild from the current settings on the next call.
    """
    from modulo.api import dependencies as api_dependencies
    from modulo.db import session as db_session

    for module, attr in ((api_dependencies, "_engine"), (db_session, "_shared_engine")):
        engine = getattr(module, attr)
        if engine is not None:
            await engine.dispose()
            setattr(module, attr, None)
    api_dependencies._session_factory = None


async def _bind_database(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Point settings AND the process-global engines at ``url``, verified.

    The assertion is the guard: if anything hands the check an engine whose
    URL is not the one this test seeded, the test fails loudly instead of
    grading some other database.
    """
    await _dispose_shared_engines()
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = get_or_create_engine(get_settings())
    assert engine.url == make_url(url), f"check would read {engine.url}, not {url}"


async def _unbind_database() -> None:
    """Tear the bind down so the NEXT test rebuilds from the session env."""
    await _dispose_shared_engines()


def _with_database(url: str, dbname: str) -> str:
    """Replace the URL's database name (keeping driver, auth and query args)."""
    head, sep, query = url.partition("?")
    rebuilt = head.rsplit("/", 1)[0] + "/" + dbname
    return rebuilt + (sep + query if sep else "")


@pytest_asyncio.fixture
async def clean_hygiene_db(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[str]:
    """A freshly created database containing NOTHING but what the test seeds.

    The only table present is the one this test creates, so the worst-table
    pick is deterministic by construction: no migration leftovers, no other
    test's rows, no autovacuum timing. The seed's statistics are visible the
    moment the creating backend's transaction commits (forced flush), and the
    fixture's own connections close before the check runs.
    """
    dbname = f"hygiene_it_{uuid.uuid4().hex[:12]}"
    maintenance_url = _with_database(db_url, "postgres")
    admin = create_async_engine(maintenance_url, poolclass=NullPool)
    try:
        async with admin.connect() as conn:
            # CREATE/DROP DATABASE cannot run inside a transaction block.
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()

    test_url = _with_database(db_url, dbname)
    await _bind_database(monkeypatch, test_url)
    try:
        yield test_url
    finally:
        await _unbind_database()
        admin = create_async_engine(maintenance_url, poolclass=NullPool)
        try:
            async with admin.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        finally:
            await admin.dispose()


@pytest_asyncio.fixture
async def bound_migrated_db(
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[str]:
    """Bind the check to the session's migrated database (verified URL)."""
    await _bind_database(monkeypatch, migrated_db_url)
    try:
        yield migrated_db_url
    finally:
        await _unbind_database()


async def _seed_bloated_table(url: str) -> None:
    """Create a table at ~95% dead and push the counters to the collector.

    200,000 rows inserted, 190,000 deleted: 190,000 dead (far over the
    10,000 floor) at a ~95% ratio (far over the 60% threshold) — the shape
    of the FAR-1445 incident, at a size the test can build quickly.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {_SEED_TABLE}"))
            await conn.execute(text(f"CREATE TABLE {_SEED_TABLE} (id bigint PRIMARY KEY)"))
            # A vacuum would RESET n_dead_tup and race the assertion.
            await conn.execute(text(f"ALTER TABLE {_SEED_TABLE} SET (autovacuum_enabled = off)"))
            await conn.execute(text(f"INSERT INTO {_SEED_TABLE} SELECT generate_series(1, 200000)"))
            await conn.execute(text(f"DELETE FROM {_SEED_TABLE} WHERE id <= 190000"))  # noqa: S608 - table name is a module constant, never input
            # Statistics are approximate by design; this guarantees the
            # collector owns the pending counts when this transaction commits.
            await conn.execute(text("SELECT pg_stat_force_next_flush()"))
            await conn.commit()
    finally:
        await engine.dispose()


async def _create_zero_tuple_table(url: str) -> None:
    """Create an empty table and make its 0/0 stats row visible.

    A freshly CREATEd table appears in ``pg_stat_user_tables`` with
    ``n_live_tup + n_dead_tup == 0`` (probed directly), so its dead ratio is
    NULL — the row ``ORDER BY dead_ratio DESC`` would rank FIRST (NULLs sort
    first under DESC) if the query did not exclude zero-size relations. The
    forced flush plus the closing connection (a backend reports its stats on
    exit) make the row visible to the check's separate connection.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {_ZERO_TABLE}"))
            await conn.execute(text(f"CREATE TABLE {_ZERO_TABLE} (id bigint PRIMARY KEY)"))
            await conn.execute(text("SELECT pg_stat_force_next_flush()"))
            await conn.commit()
    finally:
        await engine.dispose()


class TestDatabaseHygieneCheckAgainstRealPostgres:
    async def test_clean_database_grades_ok(self, clean_hygiene_db: str) -> None:
        """A database with nothing over the dead-tuple floor reads ``ok``
        through the real engine, settings and query — in a database this test
        controls, so no other test's rows can put it over the floor."""
        result = await _check_db_hygiene()
        assert result.status == "ok", result.detail
        assert result.detail is not None
        assert "within thresholds" in result.detail, result.detail
        assert "freeze age" in result.detail, result.detail
        assert result.latency_ms is not None

    async def test_seeded_bloat_grades_degraded_and_names_the_table(self, clean_hygiene_db: str) -> None:
        await _seed_bloated_table(clean_hygiene_db)
        result = await _check_db_hygiene()
        assert result.status == "degraded", result.detail
        assert result.detail is not None
        assert _SEED_TABLE in result.detail, result.detail
        assert "DEAD-TUPLE BLOAT" in result.detail, result.detail

    async def test_zero_tuple_table_cannot_win_the_pick_at_zero_floor(
        self,
        clean_hygiene_db: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M1 regression: ``min_dead = 0`` (legal — the field is ``ge=0``,
        the natural max-sensitivity setting) must not let a zero-size table
        silently fail the check open.

        A 0/0 table's dead_ratio is NULL and Postgres sorts NULLs FIRST for
        ``DESC``, so without the zero-size exclusion the empty table wins the
        worst-table pick, the grader sees ``dead_ratio is None`` and reads
        ``ok`` — while the 95%-dead table sits right there. The worst pick
        must be the bloated table, and it must grade ``degraded``.
        """
        await _seed_bloated_table(clean_hygiene_db)
        await _create_zero_tuple_table(clean_hygiene_db)
        monkeypatch.setenv("MODULO_HEALTH_DB_HYGIENE_MIN_DEAD_TUPLES", "0")
        get_settings.cache_clear()

        result = await _check_db_hygiene()
        assert result.status == "degraded", result.detail
        assert result.detail is not None
        assert _SEED_TABLE in result.detail, result.detail
        assert _ZERO_TABLE not in result.detail, result.detail

    async def test_freeze_age_is_read_from_the_live_server(self, bound_migrated_db: str) -> None:
        """The wraparound half must read the server's own
        ``autovacuum_freeze_max_age`` and this database's ``datfrozenxid`` —
        not a hard-coded ceiling.

        Bound to the migrated database: running the query against a real,
        populated schema (hundreds of stats rows) is the migrated-schema
        coverage, and both assertions read server facts no other test can
        contaminate.
        """
        result = await _check_db_hygiene()
        assert result.detail is not None, result.detail
        # postgres:16-alpine default, and a young test database's age is
        # nowhere near it — both halves of the reading are live values.
        assert "/200,000,000" in result.detail, result.detail
        assert "AT/ABOVE" not in result.detail, result.detail
