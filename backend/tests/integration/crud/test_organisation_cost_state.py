"""Postgres-backed integration tests for the org cost-state CRUD (FAR-1833).

The upsert statements use Postgres ``INSERT ... ON CONFLICT`` (``pg_insert``),
so they are exercised against a real migrated Postgres rather than a SQLite
stand-in. Each test drives the crud directly through the shared ``rls_session``
(rolled back at teardown), so no state leaks into sibling tests.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.db.crud.organisation_cost_state import (
    get_org_cost_state,
    increment_org_cumulative_spend,
    upsert_org_cost_state,
)
from modulo.db.models.organisation_cost_state import OrganisationCostState
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


async def test_missing_row_reads_as_none(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    """A missing row is the defaults contract, not an error."""
    state = await get_org_cost_state(rls_session, test_org)
    assert state is None


async def test_increment_accumulates(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    first = await increment_org_cumulative_spend(rls_session, test_org, delta_cents=5)
    assert first == 5
    second = await increment_org_cumulative_spend(rls_session, test_org, delta_cents=7)
    assert second == 12


async def test_ceiling_upsert_does_not_clobber_cumulative(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    await increment_org_cumulative_spend(rls_session, test_org, delta_cents=250)
    await upsert_org_cost_state(
        rls_session,
        test_org,
        provided={"max_run_cost_cents": 1500, "spend_ceiling_cents": 250000},
    )
    state = await get_org_cost_state(rls_session, test_org)
    assert state is not None
    assert state.max_run_cost_cents == 1500
    assert state.spend_ceiling_cents == 250000
    assert state.org_cumulative_spend_cents == 250


async def test_increment_does_not_clobber_ceiling(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    await upsert_org_cost_state(
        rls_session,
        test_org,
        provided={"max_run_cost_cents": 900, "spend_ceiling_cents": 90000},
    )
    total = await increment_org_cumulative_spend(rls_session, test_org, delta_cents=42)
    assert total == 42
    state = await get_org_cost_state(rls_session, test_org)
    assert state is not None
    assert state.max_run_cost_cents == 900
    assert state.spend_ceiling_cents == 90000


async def test_upsert_rejects_cumulative_column(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    """FAR-1833 P2-1: the cumulative is written ONLY by the atomic increment.

    ``upsert_org_cost_state`` must refuse ``org_cumulative_spend_cents`` so no
    caller can absolute-write the cumulative concurrently with an accrual (which
    would silently lose an update) — the atomic-accrual guarantee is structural,
    not caller discipline. ``increment_org_cumulative_spend`` stays the sole
    cumulative writer.
    """
    with pytest.raises(ValueError, match="unknown column"):
        await upsert_org_cost_state(rls_session, test_org, provided={"org_cumulative_spend_cents": 100})

    # The atomic increment is still the (only) cumulative writer.
    total = await increment_org_cumulative_spend(rls_session, test_org, delta_cents=5)
    assert total == 5


async def test_explicit_none_clears_a_ceiling(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    await upsert_org_cost_state(rls_session, test_org, provided={"max_run_cost_cents": 1500})
    await upsert_org_cost_state(rls_session, test_org, provided={"max_run_cost_cents": None})
    state = await get_org_cost_state(rls_session, test_org)
    assert state is not None
    assert state.max_run_cost_cents is None


async def test_upsert_rejects_unknown_column(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    with pytest.raises(ValueError, match="unknown column"):
        await upsert_org_cost_state(rls_session, test_org, provided={"not_a_column": 1})


async def test_upsert_rejects_empty_mapping(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    with pytest.raises(ValueError, match="at least one column"):
        await upsert_org_cost_state(rls_session, test_org, provided={})


async def test_rls_isolates_cost_state_between_orgs(
    db_engine: AsyncEngine,
    modulo_app_engine: AsyncEngine,
) -> None:
    """FAR-1833 P4: a ``modulo_app`` session scoped to org A cannot read org B's row.

    On Postgres the ORM tenant filter is a no-op, so RLS is the only DB-level
    backstop. The policy is written explicitly against ``org_id`` (this table is
    outside the generic ``organisation_id`` sweep), and a null/empty
    ``app.organisation_id`` fails closed.
    """
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    async with db_engine.begin() as conn:
        for org_id in (org_a, org_b):
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
                {"id": str(org_id), "n": f"rls-{org_id.hex[:8]}", "s": f"rls-{org_id.hex[:8]}"},
            )
            await conn.execute(
                text("INSERT INTO organisation_cost_state (org_id, org_cumulative_spend_cents) VALUES (:id, :cents)"),
                {"id": str(org_id), "cents": 7},
            )

    factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            visible = (
                (
                    await session.execute(
                        select(OrganisationCostState.org_id).where(OrganisationCostState.org_id.in_([org_a, org_b]))
                    )
                )
                .scalars()
                .all()
            )
            assert set(visible) == {org_a}

        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            # Even an explicit cross-org predicate returns nothing.
            cross = await session.scalar(
                select(OrganisationCostState.org_id).where(OrganisationCostState.org_id == org_b)
            )
            assert cross is None
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM organisations WHERE id IN (:a, :b)"),
                {"a": str(org_a), "b": str(org_b)},
            )
