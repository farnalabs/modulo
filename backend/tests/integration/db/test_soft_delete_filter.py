"""Integration tests for the global soft-delete filter (FAR-1025).

Proves that the ``do_orm_execute`` listener auto-applies ``deleted_at IS NULL``
to every SELECT targeting a ``SoftDeleteMixin`` model, and that the
``include_soft_deleted`` opt-out correctly re-includes soft-deleted rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, OrgScoped, SoftDeleteMixin

# ---------------------------------------------------------------------------
# Minimal soft-deleted model for test isolation
# ---------------------------------------------------------------------------


class _TestSoftEntity(OrgScoped, SoftDeleteMixin):
    """Minimal model with SoftDeleteMixin for testing the global filter."""

    __tablename__ = "_test_soft_entity"

    name: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _ensure_test_table(db_engine: AsyncEngine) -> None:  # type: ignore[misc]
    """Create and tear down the test table per test."""
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def _seed_soft_deleted(db_engine: AsyncEngine, test_org: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one live row and one soft-deleted row, return both IDs."""
    live_id = uuid.uuid4()
    deleted_id = uuid.uuid4()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(_TestSoftEntity(id=live_id, organisation_id=test_org, name="live"))
        deleted = _TestSoftEntity(id=deleted_id, organisation_id=test_org, name="deleted")
        deleted.deleted_at = datetime.now(UTC)
        session.add(deleted)
        await session.commit()

    return live_id, deleted_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.no_docker(reason="integration test — Docker required")
class TestSoftDeleteFilter:
    """Global soft-delete filter behaviour against real Postgres."""

    @pytest.mark.asyncio
    async def test_bare_select_excludes_soft_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
        test_org: uuid.UUID,
    ) -> None:
        """A plain SELECT on a SoftDeleteMixin model must NOT return the soft-deleted row."""
        live_id, deleted_id = _seed_soft_deleted
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            session.info["org_id"] = test_org
            result = await session.execute(select(_TestSoftEntity).where(_TestSoftEntity.organisation_id == test_org))
            rows = result.scalars().all()
            row_ids = [r.id for r in rows]

        assert live_id in row_ids, "live row must be returned"
        assert deleted_id not in row_ids, "soft-deleted row must be excluded"

    @pytest.mark.asyncio
    async def test_include_soft_deleted_opt_includes_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
        test_org: uuid.UUID,
    ) -> None:
        """include_soft_deleted() must re-include the soft-deleted row."""
        from modulo.db.soft_delete import include_soft_deleted

        live_id, deleted_id = _seed_soft_deleted
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            session.info["org_id"] = test_org
            result = await session.execute(
                include_soft_deleted(
                    select(_TestSoftEntity).where(
                        _TestSoftEntity.organisation_id == test_org,
                    )
                )
            )
            rows = result.scalars().all()
            row_ids = [r.id for r in rows]

        assert live_id in row_ids, "live row must be returned"
        assert deleted_id in row_ids, "soft-deleted row must be included via opt-out"

    @pytest.mark.asyncio
    async def test_aggregate_excludes_soft_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
        test_org: uuid.UUID,
    ) -> None:
        """An aggregate (COUNT) must not count soft-deleted rows."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            session.info["org_id"] = test_org
            result = await session.execute(
                select(func.count()).select_from(_TestSoftEntity).where(_TestSoftEntity.organisation_id == test_org)
            )
            count = result.scalar_one()

        assert count == 1, "only the live row must be counted"

    @pytest.mark.asyncio
    async def test_subquery_excludes_soft_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
        test_org: uuid.UUID,
    ) -> None:
        """A subquery on a SoftDeleteMixin model must also exclude soft-deleted rows.

        This verifies that with_loader_criteria with propagate_to_loaders=True
        applies the filter to subqueries as well as top-level selects.
        """
        live_id, deleted_id = _seed_soft_deleted
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            session.info["org_id"] = test_org
            # Subquery without opt-out — should exclude deleted
            subq = (select(_TestSoftEntity.id).where(_TestSoftEntity.organisation_id == test_org)).subquery()

            result = await session.execute(select(subq.c.id))
            ids = [row[0] for row in result.all()]

            assert live_id in ids, "live row must be in subquery"
            assert deleted_id not in ids, "soft-deleted row must be excluded from subquery"
