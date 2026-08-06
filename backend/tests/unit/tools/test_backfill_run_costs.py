"""Unit tests for the run-cost backfill maintenance tool
(``modulo.tools.backfill_run_costs``).

Uses an in-memory aiosqlite engine with a minimal schema (``runs`` +
``cost_components`` only — the ARRAY column on ``onboarding_progress`` cannot
be rendered on SQLite), the REAL ``seed_cost_components_for_org`` logic, and the
REAL cost engine (``load_live_components`` / ``build_telemetry`` /
``build_cost_breakdown``), so the backfill path is exercised end-to-end.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.seed_data.cost_components import seed_cost_components_for_org
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.tools.backfill_run_costs import backfill_run_costs

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a3")

_TABLE_NAMES = frozenset({"runs", "cost_components"})

# Sentinel so callers can pass ``usage=None`` explicitly (a run with NO usage).
_UNSET: Any = object()


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for name, t in Base.metadata.tables.items() if name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def maker(engine: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    m = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    yield m


def _enriched_usage() -> dict[str, Any]:
    """The ENRICHED union a pre-fix terminal run stores: one sandbox node with a
    wall-clock duration and the split sandbox signal already set.
    """
    return {
        "sandbox-node": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "wall_clock_time_ms": 3_600_000,
            "sandbox_by_map": True,
            "is_sandbox_for_wallclock": True,
        }
    }


async def _seed_components(maker: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> None:
    async with maker() as session, session.begin():
        await seed_cost_components_for_org(session, org_id)


async def _add_run(
    maker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    run_number: int = 1,
    usage: dict[str, Any] | None = _UNSET,
    breakdown: list[dict[str, Any]] | None = None,
    total_cost_usd: Decimal | None = None,
    status: str = "complete",
) -> Run:
    if usage is _UNSET:
        usage = _enriched_usage()
    run = Run(
        id=uuid.uuid4(),
        organisation_id=org_id,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        run_number=run_number,
        account_id=_ACCOUNT,
        input_hash=f"hash-{run_number}-{uuid.uuid4()}",
        langgraph_thread_id=f"thread-{uuid.uuid4()}",
        status=status,
        node_token_usage=usage,
        cost_breakdown=breakdown,
        total_cost_usd=total_cost_usd,
    )
    async with maker() as session, session.begin():
        session.add(run)
    return run


async def _load_run(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> Run | None:
    async with maker() as session, session.begin():
        return await session.get(Run, run_id)


class TestBackfillRunCosts:
    async def test_recomputes_breakdown_and_total(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _seed_components(maker, _ORG_A)
        run = await _add_run(maker, _ORG_A)

        result = await backfill_run_costs(maker, org_id=_ORG_A)

        assert result.candidates == 1
        assert result.updated == 1
        assert result.skipped == 0
        assert result.errors == 0

        stored = await _load_run(maker, run.id)
        assert stored is not None
        assert stored.total_cost_usd is not None
        assert stored.total_cost_usd > 0
        assert isinstance(stored.cost_breakdown, list) and stored.cost_breakdown
        sandbox = next((e for e in stored.cost_breakdown if e.get("component") == "sandbox_infra"), None)
        assert sandbox is not None
        assert Decimal(str(sandbox["amount_usd"])) > 0
        # The union's per-node cost_usd is written back from the telemetry
        # authority (token-derived 0 for the estimated sandbox node).
        assert "cost_usd" in (stored.node_token_usage or {}).get("sandbox-node", {})

    async def test_second_pass_skips_already_backfilled(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _seed_components(maker, _ORG_A)
        await _add_run(maker, _ORG_A)

        first = await backfill_run_costs(maker, org_id=_ORG_A)
        assert first.updated == 1

        second = await backfill_run_costs(maker, org_id=_ORG_A)
        assert second.candidates == 0
        assert second.updated == 0
        assert second.errors == 0

    async def test_dry_run_changes_nothing(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _seed_components(maker, _ORG_A)
        run = await _add_run(maker, _ORG_A)

        result = await backfill_run_costs(maker, org_id=_ORG_A, dry_run=True)

        assert result.updated == 1  # reports what WOULD change
        stored = await _load_run(maker, run.id)
        assert stored is not None
        assert stored.total_cost_usd is None
        assert stored.cost_breakdown is None

    async def test_org_without_components_exits_cleanly(self, maker: async_sessionmaker[AsyncSession]) -> None:
        run = await _add_run(maker, _ORG_B)

        result = await backfill_run_costs(maker, org_id=_ORG_B)

        assert result.updated == 0
        assert result.errors == 0
        assert result.no_components_orgs == [_ORG_B]
        stored = await _load_run(maker, run.id)
        assert stored is not None
        assert stored.total_cost_usd is None
        assert stored.cost_breakdown is None

    async def test_ignores_non_terminal_and_usage_free_runs(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _seed_components(maker, _ORG_A)
        await _add_run(maker, _ORG_A, run_number=1, status="running", usage=_enriched_usage())
        await _add_run(maker, _ORG_A, run_number=2, usage=None)
        await _add_run(maker, _ORG_A, run_number=3, usage=_enriched_usage())

        result = await backfill_run_costs(maker, org_id=_ORG_A)

        assert result.candidates == 1
        assert result.updated == 1
        assert result.skipped == 0
