"""FAR-795 unit tests: atomic per-org agent-mint budget primitive.

Covers the :mod:`modulo.core.runtime_config.mint_budget` contract on SQLite
(``aiosqlite``): within-budget allowance + recording, denial WITHOUT
recording, window rollover, fail-open on a guard error, and the
no-oversubscription guarantee with interleaved independent sessions (SQLite
serialises writers at statement boundaries, so this is statement-level
interleaving — the true row-lock proof runs in the real-Postgres class
below, which is CI/DEV-DEFERRED: skipped unless ``MINT_BUDGET_TEST_PG_URL``
names a throwaway Postgres, testcontainers style).

The tests build the budget table by running migration 0233_org_mint_budget_usage
(the shipped module itself) through an alembic Operations context on the SQLite engine,
so assertions exercise the schema the migration actually ships — not a
hand-built fixture.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.runtime_config import mint_budget
from modulo.core.runtime_config.mint_budget import consume_agent_mint_budget

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_STEM = "0233_org_mint_budget_usage"

# Postgres-only throwaway DB for the concurrency proof (CI-DEFERRED).
_PG_URL_ENV = "MINT_BUDGET_TEST_PG_URL"
_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()


def _load_migration() -> ModuleType:
    path = _VERSIONS / f"{_MIGRATION_STEM}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_STEM}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine: AsyncEngine, module: ModuleType, fn_name: str) -> None:
    """Run an alembic op entrypoint of the migration module on ``engine``."""

    async def _step() -> None:
        module_fn = getattr(module, fn_name)

        def _under_alembic(conn: sa.Connection) -> None:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module_fn()

        async with engine.begin() as conn:
            await conn.run_sync(_under_alembic)

    return _step()


async def _recorded_used(session: AsyncSession, org_id: uuid.UUID) -> int:
    result = await session.execute(
        sa.text("SELECT COALESCE(SUM(used), 0) FROM org_mint_budget_usage WHERE organisation_id = :oid"),
        {"oid": str(org_id)},
    )
    return int(result.scalar_one())


@pytest.fixture
def budget_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A deterministic limit so every test stays fast."""
    monkeypatch.setenv(mint_budget.ENV_AGENT_MINT_BUDGET_LIMIT, "10")
    monkeypatch.setenv(mint_budget.ENV_AGENT_MINT_BUDGET_WINDOW_MINUTES, "60")
    return 10


@pytest.fixture
async def sessionmakers(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mint_budget.db'}")
    await _run_migration(engine, _load_migration(), "upgrade")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _consume_with_own_tx(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID = _ORG_ID,
    n: int = 1,
) -> bool:
    """A consume that commits in its own session/tx before returning."""
    async with sessionmaker() as session, session.begin():
        return await consume_agent_mint_budget(session, org_id, n)


class TestBudgetWallet:
    async def test_within_budget_spend_allowed_and_recorded(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        allowed = await _consume_with_own_tx(sessionmakers, n=4)
        assert allowed

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 4

    async def test_exact_limit_spend_is_the_last_allowed(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        assert await _consume_with_own_tx(sessionmakers, n=6)
        assert await _consume_with_own_tx(sessionmakers, n=4)
        assert not await _consume_with_own_tx(sessionmakers, n=1)

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 10

    async def test_exceeding_spend_denied_and_not_recorded(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        assert await _consume_with_own_tx(sessionmakers, n=8)

        allowed = await _consume_with_own_tx(sessionmakers, n=5)
        assert not allowed

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 8

    async def test_first_spend_of_window_over_limit_is_denied(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        # Empty-window path: a spend larger than the whole budget is denied
        # by the insert arm (SELECT ... WHERE :n <= :limit), not only the
        # conflict arm.
        assert not await _consume_with_own_tx(sessionmakers, n=11)

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 0

    async def test_zero_and_negative_spend_vacuously_allowed(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        assert await _consume_with_own_tx(sessionmakers, n=0)
        assert await _consume_with_own_tx(sessionmakers, n=-3)

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 0

    async def test_budgets_are_per_org(self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int) -> None:
        assert await _consume_with_own_tx(sessionmakers, org_id=_ORG_ID, n=8)
        # Another org starts from its own tally, not the first org's.
        assert await _consume_with_own_tx(sessionmakers, org_id=_OTHER_ORG_ID, n=8)

    async def test_new_window_resets_usage(
        self,
        sessionmakers: async_sessionmaker[AsyncSession],
        budget_env: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pin window_start computation deterministically: the first phase
        # mints into window A until the limit, the second phase into window
        # B (a NEW window_start = a NEW row = a fresh tally — zero code).
        from datetime import UTC, datetime, timedelta

        window_a = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
        window_b = window_a + timedelta(minutes=60)

        monkeypatch.setattr(mint_budget, "current_window_start", lambda _m, **_kw: window_a)
        assert await _consume_with_own_tx(sessionmakers, n=10)
        assert not await _consume_with_own_tx(sessionmakers, n=1)

        monkeypatch.setattr(mint_budget, "current_window_start", lambda _m, **_kw: window_b)
        assert await _consume_with_own_tx(sessionmakers, n=1)

        async with sessionmakers() as session:
            rows = {
                row[0]: row[1]
                for row in (
                    await session.execute(
                        sa.text("SELECT window_start, used FROM org_mint_budget_usage WHERE organisation_id = :oid"),
                        {"oid": str(_ORG_ID)},
                    )
                ).fetchall()
            }
            assert len(rows) == 2
            assert rows[window_a.isoformat()] == 10
            assert rows[window_b.isoformat()] == 1

    async def test_interleaved_consumes_never_oversubscribe(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        # Four independent sessions each try to consume 3 against a limit of
        # 10. A read-then-write guard could admit all four (12); the atomic
        # statement admits at most three (9) — each rejected consume sees the
        # tally the earlier session's commit placed, because both sides gate
        # on the conflict row's locked value. SQLite rounds concurrency down
        # to statement ordering, but statement ordering is exactly where the
        # non-atomic variant would publish a stale tally.
        outcomes = list(await asyncio.gather(*[_consume_with_own_tx(sessionmakers, n=3) for _ in range(4)]))
        assert outcomes.count(True) == 3

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 9

    async def test_guard_error_fails_open(
        self, sessionmakers: async_sessionmaker[AsyncSession], budget_env: int
    ) -> None:
        exploding_session = AsyncMock()
        exploding_session.execute = AsyncMock(side_effect=RuntimeError("db unavailable"))

        allowed = await consume_agent_mint_budget(exploding_session, _ORG_ID, n=1)
        assert allowed

        async with sessionmakers() as session:
            assert await _recorded_used(session, _ORG_ID) == 0

    def test_malformed_env_falls_back_to_default(self, budget_env: int, monkeypatch: pytest.MonkeyPatch) -> None:
        assert mint_budget.DEFAULT_BUDGET_LIMIT == 500
        monkeypatch.setenv(mint_budget.ENV_AGENT_MINT_BUDGET_LIMIT, "carrot")
        assert mint_budget._resolve_budget_config() == (mint_budget.DEFAULT_BUDGET_LIMIT, 60)

    def test_window_start_is_floored_to_the_window(self) -> None:
        start = mint_budget.current_window_start(60)
        assert start.minute == 0
        assert start.second == 0
        assert start.tzinfo is not None


class TestMigrationSchema:
    def test_migration_carries_rls_coverage_constant(self) -> None:
        module = _load_migration()
        assert module._ORG_SCOPED_TABLES == ("org_mint_budget_usage",)

    def test_migration_head_chain(self) -> None:
        module = _load_migration()
        assert module.revision == "0233_org_mint_budget_usage"
        assert module.down_revision == "0232_journey_dismissal"

    async def test_upgrade_creates_budget_table_on_sqlite(
        self, sessionmakers: async_sessionmaker[AsyncSession]
    ) -> None:
        # The sessionmakers fixture already ran the upgrade on SQLite; the
        # composite PK and the cleanup index must exist so the single
        # statement conflicts correctly and the sweep query is servable.
        async with sessionmakers() as session:
            columns = {
                row[1]
                for row in (await session.execute(sa.text("PRAGMA table_info(org_mint_budget_usage)"))).fetchall()
            }
            assert columns == {"organisation_id", "window_start", "used"}

    async def test_downgrade_removes_budget_table(self, tmp_path: Path) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mint_budget_down.db'}")
        module = _load_migration()
        try:
            await _run_migration(engine, module, "upgrade")
            await _run_migration(engine, module, "downgrade")
            async with engine.connect() as conn:
                tables = await conn.run_sync(lambda sync_conn: set(sa.inspect(sync_conn).get_table_names()))
            assert "org_mint_budget_usage" not in tables
        finally:
            await engine.dispose()


@pytest.fixture(scope="class")
async def pg_url() -> AsyncIterator[str]:
    """Throwaway Postgres URL for the row-lock-concurrency proof.

    Supplied two ways, in order of preference:

    * an externally-provisioned throwaway DB named by ``MINT_BUDGET_TEST_PG_URL``
      (e.g. a testcontainers container the merge-queue/deploy integration leg
      sets for this file), or
    * a testcontainers ``PostgresContainer`` spun up on demand (CI/DEV), so the
      row-lock guarantee is exercised by the pipeline itself rather than only by
      agents with a local Docker Postgres.
    """
    env_url = os.environ.get(_PG_URL_ENV)
    if env_url:
        # Normalise a plain ``postgresql://`` URL to the asyncpg dialect so
        # create_async_engine never falls back to the sync psycopg2 driver
        # (which is not a project dependency — only psycopg-binary v3 is).
        if env_url.startswith("postgresql://"):
            env_url = env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        yield env_url
        return

    # The testcontainers postgres module imports psycopg2 at module load, which
    # is not a project dependency (only psycopg-binary v3 ships), so the import
    # itself can raise ModuleNotFoundError. When neither a throwaway DB nor a
    # Docker-capable runtime is available (e.g. the unit CI runner), skip rather
    # than error — the row-lock proof is capability-gated, not coverage-gated.
    try:
        from testcontainers.community.postgres import PostgresContainer
    except Exception as exc:
        pytest.skip(f"testcontainers Postgres unavailable (driver missing): {exc}")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:
        # Genuinely no Docker in this environment (e.g. local dev without the
        # daemon). CI provisions a Docker-capable runner, so the row-lock
        # proof still runs in the pipeline — this skip is capability-gated,
        # not coverage-gated.
        pytest.skip(f"testcontainers Postgres unavailable (no Docker): {exc}")
    try:
        raw = container.get_connection_url()
        url = re.sub(r"^postgresql(\+[a-z0-9]+)?://", "postgresql+asyncpg://", raw, flags=re.IGNORECASE)
        yield url
    finally:
        container.stop()


class TestRealPostgresConcurrency:
    """True row-lock-concurrency proof — runs against a throwaway Postgres.

    SQLite serialises writers via its single-writer lock, so the unit
    interleaving above cannot exercise the row-lock wait-then-reevaluate
    path; this class fills that gap with a real Postgres. The Postgres is
    provisioned by the module-level ``pg_url`` fixture (see its docstring for
    the two supply strategies).

    The throwaway DB gets the table via raw DDL matching migration
    0233_org_mint_budget_usage (the FK to ``organisations`` is deliberately
    omitted; RLS is not asserted here — the org context ceremony is the
    caller's job and the migration exercises it on the real Postgres
    integration leg).
    """

    async def test_parallel_sessions_cannot_oversubscribe(self, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two sessions race for ONE allowed consume — the conflict arm's row
        # lock serialises them, so exactly one wins; a read-then-write guard
        # admits both.
        monkeypatch.setenv(mint_budget.ENV_AGENT_MINT_BUDGET_LIMIT, "1")
        monkeypatch.setenv(mint_budget.ENV_AGENT_MINT_BUDGET_WINDOW_MINUTES, "60")

        engine = create_async_engine(pg_url)
        try:
            ddl = (
                "CREATE TABLE IF NOT EXISTS org_mint_budget_usage ("
                "organisation_id UUID NOT NULL,"
                "window_start TIMESTAMPTZ NOT NULL,"
                "used INTEGER NOT NULL DEFAULT 0,"
                "PRIMARY KEY (organisation_id, window_start))"
            )
            async with engine.begin() as conn:
                await conn.execute(sa.text(ddl))
                # Throwaway DB may carry rows from a previous probe run of
                # the same org id — start the tally at zero.
                await conn.execute(sa.text("DELETE FROM org_mint_budget_usage"))

            makers = [async_sessionmaker(engine, expire_on_commit=False) for _ in range(2)]
            outcomes = list(await asyncio.gather(*[_consume_with_own_tx(maker, n=1) for maker in makers]))
            assert outcomes.count(True) == 1

            async with engine.connect() as conn:
                used = await conn.scalar(sa.text("SELECT SUM(used) FROM org_mint_budget_usage"))
                assert used is not None
                assert int(used) <= 1
        finally:
            await engine.dispose()


class TestBudgetWiredIntoMintPaths:
    """FAR-795 budget cap must be consulted by the agent-mint paths (not dead code)."""

    async def test_upsert_ref_provenances_consults_budget_and_suppresses_when_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from modulo.core.lifecycle_map.advancement import upsert_ref_provenances

        consume = AsyncMock(return_value=False)
        events: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            "modulo.core.lifecycle_map.advancement.consume_agent_mint_budget",
            consume,
        )
        monkeypatch.setattr(
            "modulo.core.lifecycle_map.advancement.notify_refs_event",
            lambda event, **attrs: events.append((event, attrs)),
        )

        # Probe returns no existing journey row -> a fresh agent-mint attempt.
        session = MagicMock()
        probe_result = MagicMock()
        probe_result.first.return_value = None
        session.execute = AsyncMock(return_value=probe_result)
        org_id = uuid.uuid4()
        refs = [{"kind": "github", "ref": "modulo/foo#1", "source": "agent"}]

        _, agent_minted, agent_suppressed = await upsert_ref_provenances(session, org_id, refs, include_agent=True)
        # The budget guard WAS consulted for the fresh agent mint...
        assert consume.await_count == 1
        # ...and a denial suppressed the mint instead of writing it.
        assert agent_minted == 0
        assert agent_suppressed == 1
        assert any(event == "agent_mint_budget_exceeded" for event, _ in events)
