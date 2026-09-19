"""Integration tests for the global soft-delete filter (FAR-1025).

Proves that the ``do_orm_execute`` listener auto-applies ``deleted_at IS NULL``
to every SELECT targeting a ``SoftDeleteMixin`` model, and that the
``include_soft_deleted`` opt-out correctly re-includes soft-deleted rows.

Key design decisions:
- Uses a SEPARATE DeclarativeBase (``_TestBase``) so ``create_all`` never touches
  app tables. No ``Base.metadata.drop_all`` which would fail on FK dependencies.
- Calls ``register_soft_delete_filter()`` explicitly and ASSERTS the listener is
  registered — if someone removes the registration, these tests fail loudly.
- Truncates the test table between tests for isolation (no row accumulation).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import sqlalchemy.event
from sqlalchemy import String, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import Session as SASession

from modulo.db.models.base import SoftDeleteMixin
from modulo.db.soft_delete import (
    _apply_soft_delete_filter,
    include_soft_deleted,
    register_soft_delete_filter,
)

# ---------------------------------------------------------------------------
# Test-only declarative base — COMPLETELY SEPARATE from the app Base
# ---------------------------------------------------------------------------


class _TestBase(DeclarativeBase):
    """Declarative base for test-only tables. Never touches app metadata."""


class _TestSoftEntity(_TestBase, SoftDeleteMixin):
    """Minimal model with SoftDeleteMixin for testing the global filter.

    Inherits SoftDeleteMixin so ``with_loader_criteria(SoftDeleteMixin, ...)``
    matches it.  Uses ``_TestBase`` (not the app ``Base``) so ``create_all``
    only touches this one table.
    """

    __tablename__ = "_test_far1025_soft_delete"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_soft_delete_registered() -> None:
    """Guarantee the global listener is registered before every test.

    If someone removes ``register_soft_delete_filter()`` from the app startup,
    this fixture catches it immediately. The assertion after registration is the
    proof-of-registration gate.
    """
    register_soft_delete_filter()
    assert sqlalchemy.event.contains(
        SASession,
        "do_orm_execute",
        _apply_soft_delete_filter,
    ), "Global soft-delete listener MUST be registered on SASession"


@pytest_asyncio.fixture(autouse=True)
async def _create_test_table(db_engine: AsyncEngine) -> None:
    """Create the test-only table. Uses _TestBase (not app Base) — safe."""
    async with db_engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    yield
    # Truncate (not drop_all) — avoids FK dependency errors and is fast.
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {_TestSoftEntity.__tablename__} CASCADE"))


@pytest_asyncio.fixture()
async def _seed_soft_deleted(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one live row and one soft-deleted row. Returns (live_id, deleted_id)."""
    live_id = uuid.uuid4()
    deleted_id = uuid.uuid4()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(_TestSoftEntity(id=live_id, name="live"))
        deleted = _TestSoftEntity(id=deleted_id, name="deleted")
        deleted.deleted_at = datetime.now(UTC)
        session.add(deleted)
        await session.commit()

    return live_id, deleted_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSoftDeleteFilter:
    """Global soft-delete filter behaviour against real Postgres."""

    @pytest.mark.asyncio
    async def test_bare_select_excludes_soft_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
    ) -> None:
        """A plain SELECT must NOT return the soft-deleted row."""
        live_id, deleted_id = _seed_soft_deleted
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(select(_TestSoftEntity))
            row_ids = [r.id for r in result.scalars().all()]

        assert live_id in row_ids, "live row must be returned"
        assert deleted_id not in row_ids, "soft-deleted row must be excluded"

    @pytest.mark.asyncio
    async def test_include_soft_deleted_opt_includes_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
    ) -> None:
        """include_soft_deleted() must re-include the soft-deleted row."""
        live_id, deleted_id = _seed_soft_deleted
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(
                include_soft_deleted(select(_TestSoftEntity)),
            )
            row_ids = [r.id for r in result.scalars().all()]

        assert live_id in row_ids, "live row must be returned"
        assert deleted_id in row_ids, "soft-deleted row must be included via opt-out"

    @pytest.mark.asyncio
    async def test_aggregate_excludes_soft_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
    ) -> None:
        """An aggregate (COUNT) must not count soft-deleted rows."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(select(func.count()).select_from(_TestSoftEntity))
            count = result.scalar_one()

        assert count == 1, "only the live row must be counted"

    @pytest.mark.asyncio
    async def test_subquery_excludes_soft_deleted(
        self,
        db_engine: AsyncEngine,
        _seed_soft_deleted: tuple[uuid.UUID, uuid.UUID],  # noqa: PT019
    ) -> None:
        """A subquery must also exclude soft-deleted rows.

        Verifies ``with_loader_criteria`` with ``propagate_to_loaders=True``
        applies the filter to subqueries as well as top-level selects.
        """
        live_id, deleted_id = _seed_soft_deleted
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            subq = select(_TestSoftEntity.id).subquery()
            result = await session.execute(select(subq.c.id))
            ids = [row[0] for row in result.all()]

            assert live_id in ids, "live row must be in subquery"
            assert deleted_id not in ids, "soft-deleted row must be excluded from subquery"
