"""Unit tests for the per-org atomic run_number sequence (FAR-168).

``db.crud.run._allocate_run_number`` replaces the racy
``SELECT MAX(run_number)+1`` allocation in ``create_run`` with a per-org atomic
counter on Postgres, falling back to MAX+1 on generic backends. These tests:

  * prove the Postgres path executes the atomic upsert (and the generic path
    the MAX+1 query) at the mock level;
  * prove the atomic counter allocates continuously and NEVER collides under
    real concurrency (against a real SQLite engine running the same SQL);
  * prove the MAX+1 fallback races under concurrency (two concurrent reads both
    return the same value — the collision the counter fixes) and that it still
    continues from the existing per-org max.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.db.crud import run as run_crud
from modulo.db.crud.run import _allocate_run_number
from modulo.db.models.base import Base
from modulo.db.models.run import RunNumberCounter

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG2 = uuid.UUID("00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Mock-level path selection
# ---------------------------------------------------------------------------


async def _mock_session(dialect: str, scalar: int) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = scalar
    session.execute = AsyncMock(return_value=result)

    async def _dialect(_session: object) -> str:
        return dialect

    patch_target = patch.object(run_crud, "_get_dialect_name", _dialect)
    return session, patch_target


class TestPathSelection:
    async def test_postgres_uses_atomic_upsert(self) -> None:
        session, p = await _mock_session("postgresql", 7)
        with p:
            value = await _allocate_run_number(session, _ORG)

        assert value == 7
        sql = str(session.execute.call_args.args[0])
        assert "INSERT INTO run_number_counters" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE SET next_run_number" in sql
        assert "RETURNING next_run_number" in sql
        assert session.execute.call_args.args[1] == {"org_id": _ORG.hex}

    async def test_generic_backend_falls_back_to_max_plus_one(self) -> None:
        session, p = await _mock_session("sqlite", 9)
        with p:
            value = await _allocate_run_number(session, _ORG)

        assert value == 9
        sql = str(session.execute.call_args.args[0])
        assert "MAX(run_number)" in sql
        assert "COALESCE" in sql

    async def test_mariadb_falls_back_to_max_plus_one(self) -> None:
        session, p = await _mock_session("mysql", 3)
        with p:
            value = await _allocate_run_number(session, _ORG)

        assert value == 3
        sql = str(session.execute.call_args.args[0])
        assert "MAX(run_number)" in sql


# ---------------------------------------------------------------------------
# Real-SQLite behaviour: atomic counter vs MAX+1 fallback
# ---------------------------------------------------------------------------


@pytest.fixture
async def file_engine(tmp_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed SQLite so concurrent sessions share one database.

    The busy timeout lets concurrent atomic upserts serialize on the counter
    row instead of raising ``database is locked``.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'runnumber.db'}",
        connect_args={"timeout": 10},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[RunNumberCounter.__table__]))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield engine
    await engine.dispose()


async def _seed_minimal_runs(engine: AsyncEngine, org_id: uuid.UUID, max_run_number: int) -> None:
    """Create a minimal ``runs`` table and seed it so the MAX+1 fallback sees a max."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE runs (organisation_id VARCHAR(32) NOT NULL, run_number INTEGER NOT NULL)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO runs (organisation_id, run_number) VALUES (:oid, :rn)",
            {"oid": org_id.hex, "rn": max_run_number},
        )


def _make_maker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _alloc(maker: async_sessionmaker, org_id: uuid.UUID) -> int:
    async with maker() as session, session.begin():
        return await _allocate_run_number(session, org_id)


async def _postgres_dialect(_session: object) -> str:
    return "postgresql"


class TestAtomicCounterBehaviour:
    async def test_allocates_continuously_in_sequence(self, file_engine: AsyncEngine) -> None:
        """Ten sequential allocations produce the continuous run 1..10."""
        maker = _make_maker(file_engine)
        with patch.object(run_crud, "_get_dialect_name", _postgres_dialect):
            values = [await _alloc(maker, _ORG) for _ in range(10)]

        assert values == list(range(1, 11))

    async def test_never_collides_under_concurrency(self, file_engine: AsyncEngine) -> None:
        """Concurrent allocations in the same org are all unique and continuous.

        Each task runs the atomic upsert in its own transaction; SQLite
        serializes them on the counter row, so no two tasks can observe the same
        next value — the property Postgres guarantees natively.
        """
        maker = _make_maker(file_engine)
        with patch.object(run_crud, "_get_dialect_name", _postgres_dialect):
            values = await asyncio.gather(*[_alloc(maker, _ORG) for _ in range(10)])

        assert sorted(values) == list(range(1, 11))
        assert len(set(values)) == 10

    async def test_orgs_are_independent(self, file_engine: AsyncEngine) -> None:
        """Two orgs each get their own sequence starting at 1."""
        maker = _make_maker(file_engine)
        with patch.object(run_crud, "_get_dialect_name", _postgres_dialect):
            org_a = await _alloc(maker, _ORG)
            org_b = await _alloc(maker, _ORG2)
            org_a2 = await _alloc(maker, _ORG)

        assert org_a == 1
        assert org_b == 1
        assert org_a2 == 2


class TestMaxPlusOneFallback:
    async def test_fallback_races_under_concurrency(self, file_engine: AsyncEngine) -> None:
        """Prove-the-fix: the MAX+1 baseline collides under concurrency.

        Two concurrent allocations both read the same committed max (5) before
        either inserts a run, so both return 6 — inserting two runs with
        run_number=6 would violate ``uq_runs_org_run_number``. The atomic
        counter exists precisely to make this impossible.
        """
        await _seed_minimal_runs(file_engine, _ORG, max_run_number=5)
        maker = _make_maker(file_engine)

        values = await asyncio.gather(_alloc(maker, _ORG), _alloc(maker, _ORG))

        assert values == [6, 6]

    async def test_fallback_continues_from_existing_max(self, file_engine: AsyncEngine) -> None:
        """The fallback behaves like the historical MAX+1: next is max+1."""
        await _seed_minimal_runs(file_engine, _ORG, max_run_number=5)
        maker = _make_maker(file_engine)

        value = await _alloc(maker, _ORG)

        assert value == 6
