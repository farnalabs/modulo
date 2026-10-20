"""FAR-1977 — a DB-level scanner failure must not fail the scanners after it.

``scan_all`` runs every scanner against ONE shared transaction. A failing
STATEMENT aborts that transaction on Postgres, so before the fix every LATER
scanner raised ``DBAPIError: current transaction is aborted`` and was marked
failed too — one root cause misattributed to N categories. The unit tests in
``tests/unit/core/test_housekeeping.py`` cover a Python-level raise (which
never aborts the transaction); this module pins the DB-level case against a
REAL Postgres 16, where the savepoint is what restores the transaction.

Proven here:

* ``TestSavepointIsolation`` — ok / bad / ok scanners: the bad scanner's
  category is the ONLY failed one, its ``error.exception`` is that scanner's
  own exception class, and the scanner AFTER it still runs and reports ``ok``.
* ``TestWithoutSavepointFailureMode`` — the negative control: the same two
  statements on a bare transaction make the second one raise ``DBAPIError``.
  Without this the isolation test could pass for the wrong reason (e.g. the
  first statement not actually aborting the transaction).
"""

import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.housekeeping import Candidate, Scanner, scan_all

_PG_URL_ENV = "HOUSEKEEPING_TEST_PG_URL"

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")

# A table that provably does not exist: Postgres rejects the statement with
# sqlstate 42P01, SQLAlchemy surfaces it as ``ProgrammingError``, and the
# enclosing transaction is left aborted.
_MISSING_TABLE_SQL = "SELECT * FROM housekeeping_savepoint_does_not_exist"


@pytest.fixture(scope="module")
async def pg_url() -> AsyncIterator[str]:
    """Throwaway Postgres URL: env-provisioned, else testcontainers.

    Capability-gated (skipped, never silently passing) where neither a
    pre-provisioned database nor Docker is available — the same shape as the
    sibling real-Postgres proofs in this directory.
    """
    env_url = os.environ.get(_PG_URL_ENV)
    if env_url:
        if env_url.startswith("postgresql://"):
            env_url = env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        yield env_url
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except Exception as exc:  # capability-gated skip, never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (driver missing): {exc}")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # capability-gated skip, never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (no Docker): {exc}")
    try:
        raw = container.get_connection_url()
        yield re.sub(r"^postgresql(\+[a-z0-9]+)?://", "postgresql+asyncpg://", raw, flags=re.IGNORECASE)
    finally:
        container.stop()


@pytest.fixture(scope="module")
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_url)
    yield engine
    await engine.dispose()


def _scanner(
    category: str,
    scan_func: Callable[[AsyncSession, uuid.UUID], Awaitable[list[Candidate]]],
    entity_type: str | None = "secret",
) -> Scanner:
    return Scanner(
        category=category,
        scan_func=scan_func,
        label=category,
        description="d",
        entity_type=entity_type,
    )


class TestSavepointIsolation:
    async def test_db_failure_leaves_later_scanner_ok(self, pg_engine: AsyncEngine) -> None:
        """ok / bad(DB) / ok: only the DB-failing category is ``failed``."""
        later_ran = False

        async def ok_before(session: AsyncSession, _org: uuid.UUID) -> list[Candidate]:
            await session.execute(text("SELECT 1"))
            return []

        async def bad_db(session: AsyncSession, _org: uuid.UUID) -> list[Candidate]:
            # A REAL failing statement: Postgres aborts the transaction here.
            await session.execute(text(_MISSING_TABLE_SQL))
            return []

        async def ok_after(session: AsyncSession, _org: uuid.UUID) -> list[Candidate]:
            nonlocal later_ran
            later_ran = True
            # Would raise DBAPIError ("current transaction is aborted")
            # without the per-scanner savepoint.
            await session.execute(text("SELECT 1"))
            return [Candidate(id="after", name="after", detail="d")]

        scanners = [
            _scanner("ok_before", ok_before),
            _scanner("bad_db", bad_db),
            _scanner("ok_after", ok_after),
        ]

        maker = async_sessionmaker(pg_engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("modulo.core.housekeeping._SCANNERS", scanners)
                results = await scan_all(session, _ORG)
            # The shared transaction is still usable after the rollback-to-
            # savepoint — prove it from the same session.
            await session.execute(text("SELECT 1"))

        by_category = {r.category: r for r in results}
        assert list(by_category) == ["ok_before", "bad_db", "ok_after"]

        assert by_category["ok_before"].status == "ok"
        assert by_category["ok_before"].error is None

        failed = by_category["bad_db"]
        assert failed.status == "failed"
        assert not failed.candidates
        assert failed.error == {"reason": "scanner_failed", "exception": "ProgrammingError"}

        # The scanner AFTER the DB failure ran and kept ``ok``: the savepoint
        # restored the transaction instead of leaving it aborted.
        assert later_ran is True
        assert by_category["ok_after"].status == "ok"
        assert by_category["ok_after"].error is None
        assert [c.id for c in by_category["ok_after"].candidates] == ["after"]


class TestWithoutSavepointFailureMode:
    async def test_aborted_transaction_makes_the_next_statement_raise_dbapierror(self, pg_engine: AsyncEngine) -> None:
        """Negative control: without a savepoint the later statement IS poisoned.

        This is the pre-fix behaviour the savepoint removes — pinned so the
        isolation test cannot pass for the wrong reason (e.g. the first
        statement not actually aborting the transaction).
        """
        maker = async_sessionmaker(pg_engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            with pytest.raises(ProgrammingError):
                await session.execute(text(_MISSING_TABLE_SQL))

            # No savepoint rolled back: the transaction is aborted.
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT 1"))
