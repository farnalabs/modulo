"""#1177: pipeline CRUD helpers carry an ``organisation_id`` predicate.

Drives the REAL CRUD functions against in-memory SQLite (no RLS there), so a
cross-org call can only be refused by the query-layer predicate itself: an
org-A caller must not get, list, update, delete, restore, archive or read the
graph of an org-B pipeline.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline import (
    archive_pipeline,
    get_pipeline,
    get_pipeline_graph,
    list_pipelines,
    replace_pipeline_graph,
    restore_pipeline,
    soft_delete_pipeline,
    update_pipeline,
)
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.soft_delete import include_soft_deleted

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_PIPE_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_NODE_1 = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_NODE_2 = uuid.UUID("00000000-0000-0000-0000-0000000000c2")

_TABLES: list[Table] = [Organisation.__table__, Pipeline.__table__, PipelineEdge.__table__]


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        for org in (_ORG_A, _ORG_B):
            s.add(Organisation(id=org, name=f"org {org}", slug=f"org-{org}"))
        await s.flush()
        s.add(Pipeline(id=_PIPE_B, organisation_id=_ORG_B, name="b", account_id=_ORG_B, visibility="org"))
        await s.flush()
        s.add(
            PipelineEdge(
                pipeline_id=_PIPE_B,
                organisation_id=_ORG_B,
                source_node_id=_NODE_1,
                target_node_id=_NODE_2,
                edge_type="normal",
            )
        )
        await s.flush()
        yield s


async def _row(session: AsyncSession) -> Pipeline:
    return (await session.execute(include_soft_deleted(select(Pipeline).where(Pipeline.id == _PIPE_B)))).scalar_one()


async def test_get_pipeline_refuses_other_org(session: AsyncSession) -> None:
    assert await get_pipeline(session, _PIPE_B, organisation_id=_ORG_A) is None


async def test_get_pipeline_returns_own_org(session: AsyncSession) -> None:
    found = await get_pipeline(session, _PIPE_B, organisation_id=_ORG_B)

    assert found is not None
    assert found.id == _PIPE_B


async def test_list_pipelines_excludes_other_org(session: AsyncSession) -> None:
    other = await list_pipelines(session, organisation_id=_ORG_A)
    own = await list_pipelines(session, organisation_id=_ORG_B)

    assert other.total == 0
    assert own.total == 1


async def test_update_pipeline_refuses_other_org(session: AsyncSession) -> None:
    result = await update_pipeline(session, _PIPE_B, {"name": "hijacked"}, org_id=_ORG_A)

    assert result is None
    assert (await _row(session)).name == "b"


async def test_soft_delete_refuses_other_org(session: AsyncSession) -> None:
    result = await soft_delete_pipeline(session, _PIPE_B, organisation_id=_ORG_A)

    assert result is None
    assert (await _row(session)).deleted_at is None


async def test_restore_refuses_other_org(session: AsyncSession) -> None:
    assert await soft_delete_pipeline(session, _PIPE_B, organisation_id=_ORG_B) is not None

    result = await restore_pipeline(session, _PIPE_B, organisation_id=_ORG_A)

    assert result is None
    assert (await _row(session)).deleted_at is not None


async def test_archive_refuses_other_org(session: AsyncSession) -> None:
    result = await archive_pipeline(session, _PIPE_B, organisation_id=_ORG_A)

    assert result is None
    assert (await _row(session)).archived_at is None


async def test_get_pipeline_graph_refuses_other_org(session: AsyncSession) -> None:
    assert await get_pipeline_graph(session, _PIPE_B, organisation_id=_ORG_A) is None


async def test_get_pipeline_graph_scopes_edges_to_own_org(session: AsyncSession) -> None:
    graph = await get_pipeline_graph(session, _PIPE_B, organisation_id=_ORG_B)

    assert graph is not None
    assert len(graph[1]) == 1


async def test_replace_pipeline_graph_refuses_other_org(session: AsyncSession) -> None:
    result = await replace_pipeline_graph(
        session,
        pipeline_id=_PIPE_B,
        org_id=_ORG_A,
        nodes=[],
        edges=[],
        is_privileged=True,
        caller_type="rest",
    )

    assert result is None
    edges = (await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == _PIPE_B))).scalars().all()
    assert len(edges) == 1
