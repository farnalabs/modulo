"""Regression tests for ``seed_cost_components`` with production session shape.

The org-enumeration query in ``seed_cost_components`` used to run WITHOUT an
active transaction. The production session factories
(``modulo.api.dependencies.get_or_create_session_factory`` and
``modulo.db.session.AsyncSessionLocal``) are created with ``autobegin=False``,
so that query raised ``InvalidRequestError: Autobegin is disabled on this
Session`` at startup, the exception was swallowed by the lifespan
try/except, and NO org ever got default cost components — every run then
reported ``total_cost_usd = 0.000000``.

This test reproduces the production shape (``async_sessionmaker`` with
``autobegin=False`` over a real in-memory SQLite engine) and asserts the 3
default components are seeded. It fails before the fix and passes after.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.seed_data.cost_components import seed_cost_components, seed_cost_components_for_org
from modulo.db.models.base import Base
from modulo.db.models.cost_component import CostComponent
from modulo.db.models.organisation import Organisation

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG_FRESH = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TABLES = {"organisations", "cost_components"}


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    yield eng
    await eng.dispose()


@pytest.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # Production shape: expire_on_commit=False + autobegin=False (see
    # modulo.api.dependencies.get_or_create_session_factory).
    return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)


async def test_seed_cost_components_works_with_autobegin_false(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        session.add(Organisation(id=_ORG, name="Seed Org", slug="seed-org"))

    seeded = await seed_cost_components(factory)

    assert seeded == 1
    async with factory() as session, session.begin():
        result = await session.execute(select(CostComponent).where(CostComponent.organisation_id == _ORG))
        components = result.scalars().all()
    assert {c.name for c in components} == {"llm_tokens", "sandbox_infra", "model_tokens"}
    assert len(components) == 3
    assert all(c.enabled for c in components)


async def test_seed_cost_components_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        session.add(Organisation(id=_ORG, name="Seed Org", slug="seed-org"))

    first = await seed_cost_components(factory)
    second = await seed_cost_components(factory)

    assert first == 1
    assert second == 1  # seeded counts orgs processed, not rows inserted
    async with factory() as session, session.begin():
        result = await session.execute(select(CostComponent).where(CostComponent.organisation_id == _ORG))
        components = result.scalars().all()
    assert len(components) == 3  # no duplicates from the second pass


async def test_seed_cost_components_logs_seed_progress(
    factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The seed reports per-component and completion via the structured logger.

    The temporary ``print(..., flush=True)`` diagnostics (FAR-113) were
    removed once the seed was confirmed working on prod; the ``_log.info``
    lines are the observability record of the seed.
    """
    caplog.set_level(logging.INFO, logger="modulo.core.seed_data.cost_components")
    async with factory() as session, session.begin():
        session.add(Organisation(id=_ORG, name="Seed Org", slug="seed-org"))

    seeded = await seed_cost_components(factory)

    assert seeded == 1
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("cost_components.seeded") == 3
    assert messages.count("cost_components.seed_complete") == 1


async def test_seed_cost_components_no_orgs_is_a_noop(
    factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zero-org enumeration must not crash and must seed nothing."""
    caplog.set_level(logging.INFO, logger="modulo.core.seed_data.cost_components")
    seeded = await seed_cost_components(factory)

    assert seeded == 0
    messages = [r.getMessage() for r in caplog.records]
    assert "cost_components.seeded" not in messages
    assert "cost_components.seed_complete" in messages


async def test_seed_cost_components_for_org_seeds_fresh_org(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Org-creation wiring: a fresh org gets its 3 components immediately.

    Mirrors what ``_ensure_default_org`` and ``admin_create_org`` now invoke
    in the same transaction as the org row.
    """
    async with factory() as session, session.begin():
        session.add(Organisation(id=_ORG_FRESH, name="Fresh Org", slug="fresh-org"))

    async with factory() as session, session.begin():
        await seed_cost_components_for_org(session, _ORG_FRESH)

    async with factory() as session, session.begin():
        result = await session.execute(select(CostComponent).where(CostComponent.organisation_id == _ORG_FRESH))
        components = result.scalars().all()
    assert {c.name for c in components} == {"llm_tokens", "sandbox_infra", "model_tokens"}
    assert len(components) == 3
    assert all(c.enabled for c in components)
