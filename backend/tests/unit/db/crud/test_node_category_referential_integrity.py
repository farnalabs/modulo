"""Referential-integrity tests for node category deletion.

``node_category_id`` references live inside each pipeline's
``graph_nodes_json`` JSON column, so there is no relational FK to enforce
integrity — ``soft_delete_node_category`` must refuse to delete a category
that pipeline nodes still reference. Uses a real in-memory SQLite database.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.node_category import (
    NodeCategoryInUseError,
    get_node_category,
    node_category_in_use,
    soft_delete_node_category,
)
from modulo.db.models.base import Base
from modulo.db.models.node_category import NodeCategory
from modulo.db.models.pipeline import Pipeline

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_TABLES = {"node_categories", "pipelines"}


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_category(session: AsyncSession, *, org_id: uuid.UUID = _ORG_A, name: str = "LLM Call") -> NodeCategory:
    category = NodeCategory(organisation_id=org_id, name=name, account_id=_ACCOUNT)
    session.add(category)
    await session.flush()
    return category


async def _seed_pipeline(
    session: AsyncSession,
    *,
    name: str,
    nodes: list[dict],
    org_id: uuid.UUID = _ORG_A,
    deleted: bool = False,
) -> Pipeline:
    pipeline = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=_ACCOUNT,
        graph_nodes_json=nodes,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


async def test_no_references_returns_empty(session: AsyncSession) -> None:
    category = await _seed_category(session)
    await _seed_pipeline(session, name="no refs", nodes=[{"id": "n1", "type": "agent"}])
    assert await node_category_in_use(session, category.id, org_id=_ORG_A) == []


async def test_referencing_pipeline_reported(session: AsyncSession) -> None:
    category = await _seed_category(session)
    pipeline = await _seed_pipeline(
        session,
        name="uses category",
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(category.id)}],
    )
    referencing = await node_category_in_use(session, category.id, org_id=_ORG_A)
    assert len(referencing) == 1
    assert referencing[0] == {"id": str(pipeline.id), "name": "uses category"}


async def test_only_matching_category_reported(session: AsyncSession) -> None:
    category = await _seed_category(session)
    other = await _seed_category(session, name="Other Category")
    await _seed_pipeline(
        session,
        name="uses other",
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(other.id)}],
    )
    assert await node_category_in_use(session, category.id, org_id=_ORG_A) == []


async def test_other_org_references_ignored(session: AsyncSession) -> None:
    category = await _seed_category(session)
    await _seed_pipeline(
        session,
        name="other org",
        org_id=_ORG_B,
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(category.id)}],
    )
    assert await node_category_in_use(session, category.id, org_id=_ORG_A) == []


async def test_soft_deleted_pipeline_ignored(session: AsyncSession) -> None:
    category = await _seed_category(session)
    await _seed_pipeline(
        session,
        name="archived pipeline",
        deleted=True,
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(category.id)}],
    )
    assert await node_category_in_use(session, category.id, org_id=_ORG_A) == []


async def test_non_dict_and_malformed_entries_ignored(session: AsyncSession) -> None:
    category = await _seed_category(session)
    await _seed_pipeline(
        session,
        name="garbage nodes",
        nodes=[{"id": "n1"}, "not-a-dict", None, {"node_category_id": 12345}],
    )
    assert await node_category_in_use(session, category.id, org_id=_ORG_A) == []


async def test_soft_delete_blocked_when_referenced(session: AsyncSession) -> None:
    category = await _seed_category(session)
    pipeline = await _seed_pipeline(
        session,
        name="uses category",
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(category.id)}],
    )
    with pytest.raises(NodeCategoryInUseError) as exc_info:
        await soft_delete_node_category(session, category.id, org_id=_ORG_A)
    assert exc_info.value.category_id == category.id
    assert [p["id"] for p in exc_info.value.pipelines] == [str(pipeline.id)]
    assert "uses category" in str(exc_info.value)
    assert await get_node_category(session, category.id, org_id=_ORG_A) is not None


async def test_soft_delete_succeeds_when_unreferenced(session: AsyncSession) -> None:
    category = await _seed_category(session)
    await _seed_pipeline(session, name="no refs", nodes=[{"id": "n1", "type": "agent"}])
    deleted = await soft_delete_node_category(session, category.id, org_id=_ORG_A)
    assert deleted is not None
    assert await get_node_category(session, category.id, org_id=_ORG_A) is None


async def test_soft_delete_succeeds_when_reference_removed(session: AsyncSession) -> None:
    category = await _seed_category(session)
    pipeline = await _seed_pipeline(
        session,
        name="was using category",
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(category.id)}],
    )
    pipeline.graph_nodes_json = [{"id": "n1", "type": "agent"}]
    await session.flush()
    deleted = await soft_delete_node_category(session, category.id, org_id=_ORG_A)
    assert deleted is not None


async def test_soft_delete_other_org_unaffected(session: AsyncSession) -> None:
    category = await _seed_category(session)
    await _seed_pipeline(
        session,
        name="other org uses",
        org_id=_ORG_B,
        nodes=[{"id": "n1", "type": "agent", "node_category_id": str(category.id)}],
    )
    deleted = await soft_delete_node_category(session, category.id, org_id=_ORG_A)
    assert deleted is not None
    assert await get_node_category(session, category.id, org_id=_ORG_A) is None
