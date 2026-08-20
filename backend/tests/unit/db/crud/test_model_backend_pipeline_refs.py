"""Unit tests for ``list_pipeline_references_for_backend`` CRUD function.

Scans pipeline graph_nodes_json for direct node references (model_backend_id)
and indirect agent references (agent_id -> Agent.model_backend_id).
Uses a real in-memory SQLite database.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.model_backend import list_pipeline_references_for_backend
from modulo.db.models.agent import Agent
from modulo.db.models.base import Base
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[ModelBackend.__table__, Pipeline.__table__, Agent.__table__],
            )
        )
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = _ORG_A,
    name: str = "test-backend",
) -> ModelBackend:
    backend = ModelBackend(
        organisation_id=org_id,
        name=name,
        display_name=name.title(),
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"encrypted",
        account_id=_ACCOUNT,
    )
    session.add(backend)
    await session.flush()
    return backend


async def _seed_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = _ORG_A,
    name: str = "test-pipeline",
    graph_nodes: list[dict] | None = None,
    deleted_at: datetime | None = None,
) -> Pipeline:
    pipeline = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=_ACCOUNT,
        graph_nodes_json=graph_nodes or [],
        deleted_at=deleted_at,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


async def _seed_agent(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = _ORG_A,
    name: str = "test-agent",
    model_backend_id: uuid.UUID | None = None,
) -> Agent:
    agent = Agent(
        organisation_id=org_id,
        name=name,
        prompt_template="Do something",
        account_id=_ACCOUNT,
        model_backend_id=model_backend_id,
    )
    session.add(agent)
    await session.flush()
    return agent


async def test_no_references_returns_empty(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    await _seed_pipeline(session, graph_nodes=[{"id": "n1"}])
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert not result.items
    assert result.total == 0


async def test_direct_node_reference(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    await _seed_pipeline(
        session,
        name="my-pipeline",
        graph_nodes=[{"id": "n1", "model_backend_id": str(backend.id)}],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 1
    ref = result.items[0]
    assert ref["reference_type"] == "direct_node"
    assert ref["pipeline_name"] == "my-pipeline"
    assert ref["agent_name"] is None
    assert ref["agent_id"] is None


async def test_agent_reference(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    agent = await _seed_agent(session, name="my-agent", model_backend_id=backend.id)
    await _seed_pipeline(
        session,
        name="agent-pipeline",
        graph_nodes=[{"id": "n1", "agent_id": str(agent.id)}],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 1
    ref = result.items[0]
    assert ref["reference_type"] == "agent"
    assert ref["pipeline_name"] == "agent-pipeline"
    assert ref["agent_name"] == "my-agent"
    assert ref["agent_id"] == agent.id


async def test_mixed_references(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    agent = await _seed_agent(session, name="linked-agent", model_backend_id=backend.id)
    await _seed_pipeline(
        session,
        name="mixed-pipeline",
        graph_nodes=[
            {"id": "n1", "model_backend_id": str(backend.id)},
            {"id": "n2", "agent_id": str(agent.id)},
        ],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 2
    types = {r["reference_type"] for r in result.items}
    assert types == {"direct_node", "agent"}


async def test_soft_deleted_pipelines_excluded(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    await _seed_pipeline(
        session,
        name="deleted-pipeline",
        graph_nodes=[{"id": "n1", "model_backend_id": str(backend.id)}],
        deleted_at=datetime.now(UTC),
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 0


async def test_agents_from_other_orgs_excluded(session: AsyncSession) -> None:
    backend = await _seed_backend(session, org_id=_ORG_A)
    # Agent in org B references the same backend id (should not match)
    other_agent = await _seed_agent(session, org_id=_ORG_B, name="other-org-agent", model_backend_id=backend.id)
    await _seed_pipeline(
        session,
        org_id=_ORG_A,
        name="org-a-pipeline",
        graph_nodes=[{"id": "n1", "agent_id": str(other_agent.id)}],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    # The agent query filters by org_id, so the other-org agent won't be found
    # in agent_backend_map. The pipeline has an agent_id but no match -> no ref.
    assert result.total == 0


async def test_agent_with_different_backend_not_reported(session: AsyncSession) -> None:
    backend_a = await _seed_backend(session, name="backend-a")
    backend_b = await _seed_backend(session, name="backend-b")
    agent = await _seed_agent(session, name="agent-b", model_backend_id=backend_b.id)
    await _seed_pipeline(
        session,
        name="pipeline-b",
        graph_nodes=[{"id": "n1", "agent_id": str(agent.id)}],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend_a.id)
    assert result.total == 0


async def test_pagination(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    for i in range(5):
        await _seed_pipeline(
            session,
            name=f"pipeline-{i}",
            graph_nodes=[{"id": "n1", "model_backend_id": str(backend.id)}],
        )
    page1 = await list_pipeline_references_for_backend(
        session, org_id=_ORG_A, backend_id=backend.id, page=1, page_size=2
    )
    assert page1.total == 5
    assert len(page1.items) == 2
    assert page1.page == 1
    assert page1.page_size == 2

    page3 = await list_pipeline_references_for_backend(
        session, org_id=_ORG_A, backend_id=backend.id, page=3, page_size=2
    )
    assert len(page3.items) == 1


async def test_malformed_graph_nodes_json_handled(session: AsyncSession) -> None:
    """Malformed agent_id values (non-UUID) are skipped gracefully."""
    backend = await _seed_backend(session)
    await _seed_pipeline(
        session,
        name="bad-nodes",
        graph_nodes=[{"id": "n1", "agent_id": "not-a-uuid"}],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 0


async def test_empty_graph_nodes(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    await _seed_pipeline(session, name="empty-pipeline", graph_nodes=[])
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 0


async def test_none_graph_nodes(session: AsyncSession) -> None:
    """Pipeline with None graph_nodes_json doesn't crash."""
    backend = await _seed_backend(session)
    pipeline = Pipeline(
        organisation_id=_ORG_A,
        name="null-nodes",
        account_id=_ACCOUNT,
        graph_nodes_json=None,
    )
    session.add(pipeline)
    await session.flush()
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 0


async def test_dedup_agent_reference_per_pipeline(session: AsyncSession) -> None:
    """Same agent referenced in multiple nodes of one pipeline counts once."""
    backend = await _seed_backend(session)
    agent = await _seed_agent(session, name="dup-agent", model_backend_id=backend.id)
    await _seed_pipeline(
        session,
        name="dup-pipeline",
        graph_nodes=[
            {"id": "n1", "agent_id": str(agent.id)},
            {"id": "n2", "agent_id": str(agent.id)},
        ],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    agent_refs = [r for r in result.items if r["reference_type"] == "agent"]
    assert len(agent_refs) == 1


async def test_multiple_pipelines_sorted_by_name(session: AsyncSession) -> None:
    backend = await _seed_backend(session)
    await _seed_pipeline(
        session,
        name="zebra-pipeline",
        graph_nodes=[{"id": "n1", "model_backend_id": str(backend.id)}],
    )
    await _seed_pipeline(
        session,
        name="alpha-pipeline",
        graph_nodes=[{"id": "n1", "model_backend_id": str(backend.id)}],
    )
    result = await list_pipeline_references_for_backend(session, org_id=_ORG_A, backend_id=backend.id)
    assert result.total == 2
    assert result.items[0]["pipeline_name"] == "alpha-pipeline"
    assert result.items[1]["pipeline_name"] == "zebra-pipeline"
