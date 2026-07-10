"""Integration tests for Pipeline CRUD.

RLS is set to test_org; all inserts are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.pipeline import (
    clone_pipeline,
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    get_pipeline_graph,
    list_pipelines,
    replace_pipeline_graph,
    update_pipeline,
)

pytestmark = pytest.mark.integration


async def test_create_pipeline(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    p = await create_pipeline(
        rls_session,
        org_id=test_org,
        name="My Pipeline",
        account_id=test_user,
    )
    assert p.id is not None
    assert p.name == "My Pipeline"
    assert p.organisation_id == test_org


async def test_get_pipeline_returns_existing(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    p = await create_pipeline(rls_session, org_id=test_org, name="Fetch Me", account_id=test_user)
    fetched = await get_pipeline(rls_session, p.id)
    assert fetched is not None
    assert fetched.id == p.id


async def test_get_pipeline_returns_none_for_unknown(
    rls_session: AsyncSession,
) -> None:
    assert await get_pipeline(rls_session, uuid.uuid4()) is None


async def test_list_pipelines_pagination(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    for i in range(3):
        await create_pipeline(rls_session, org_id=test_org, name=f"Pipeline {i}", account_id=test_user)

    page1 = await list_pipelines(rls_session, page=1, page_size=2)
    assert page1.total >= 3
    assert len(page1.items) == 2
    assert page1.page == 1


async def test_update_pipeline(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    p = await create_pipeline(rls_session, org_id=test_org, name="Old Name", account_id=test_user)
    updated = await update_pipeline(rls_session, p.id, {"name": "New Name"})
    assert updated is not None
    assert updated.name == "New Name"


async def test_update_pipeline_unknown_returns_none(
    rls_session: AsyncSession,
) -> None:
    assert await update_pipeline(rls_session, uuid.uuid4(), {"name": "x"}) is None


async def test_delete_pipeline(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    p = await create_pipeline(rls_session, org_id=test_org, name="Delete Me", account_id=test_user)
    assert await delete_pipeline(rls_session, p.id) is True
    assert await get_pipeline(rls_session, p.id) is None


async def test_delete_pipeline_unknown_returns_false(
    rls_session: AsyncSession,
) -> None:
    assert await delete_pipeline(rls_session, uuid.uuid4()) is False


async def test_replace_pipeline_graph_persists_nodes_and_first_class_edges(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline = await create_pipeline(
        rls_session,
        org_id=test_org,
        name="Graph persistence",
        account_id=test_user,
    )
    first_node = uuid.uuid4()
    second_node = uuid.uuid4()
    nodes = [
        {
            "id": str(first_node),
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 0, "y": 0},
            "connector_binding": None,
        },
        {
            "id": str(second_node),
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 200, "y": 0},
            "connector_binding": None,
        },
    ]
    edge_id = uuid.uuid4()
    saved = await replace_pipeline_graph(
        rls_session,
        pipeline_id=pipeline.id,
        org_id=test_org,
        nodes=nodes,
        edges=[
            {
                "id": edge_id,
                "source_node_id": first_node,
                "target_node_id": second_node,
                "edge_type": "normal",
                "hitl_gate_config": None,
            },
        ],
    )

    assert saved is not None
    loaded = await get_pipeline_graph(rls_session, pipeline.id)
    assert loaded is not None
    loaded_nodes, loaded_edges = loaded
    assert loaded_nodes == nodes
    assert len(loaded_edges) == 1
    assert loaded_edges[0].id == edge_id
    assert loaded_edges[0].pipeline_id == pipeline.id


async def test_clone_pipeline_returns_new_id_and_name_prefix(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    source = await create_pipeline(
        rls_session,
        org_id=test_org,
        name="Original Pipeline",
        account_id=test_user,
    )
    first_node = uuid.uuid4()
    second_node = uuid.uuid4()
    nodes = [
        {"id": str(first_node), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}},
        {"id": str(second_node), "agent_id": str(uuid.uuid4()), "position": {"x": 200, "y": 0}},
    ]
    edge_id = uuid.uuid4()
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=source.id,
        org_id=test_org,
        nodes=nodes,
        edges=[
            {
                "id": edge_id,
                "source_node_id": first_node,
                "target_node_id": second_node,
                "edge_type": "normal",
                "hitl_gate_config": None,
            },
        ],
    )

    cloned = await clone_pipeline(
        rls_session,
        org_id=test_org,
        pipeline_id=source.id,
        account_id=test_user,
    )

    assert cloned is not None
    assert cloned.id != source.id
    assert cloned.name == "Copy of Original Pipeline"
    assert cloned.organisation_id == test_org

    # Cloned graph nodes match original
    cloned_graph = await get_pipeline_graph(rls_session, cloned.id)
    assert cloned_graph is not None
    cloned_nodes, cloned_edges = cloned_graph
    assert len(cloned_nodes) == 2
    assert len(cloned_edges) == 1
    assert cloned_edges[0].source_node_id == first_node
    assert cloned_edges[0].target_node_id == second_node


async def test_clone_pipeline_independent_from_original(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    source = await create_pipeline(
        rls_session,
        org_id=test_org,
        name="Independent Test",
        account_id=test_user,
    )
    node_id = uuid.uuid4()
    nodes = [{"id": str(node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}]
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=source.id,
        org_id=test_org,
        nodes=nodes,
        edges=[],
    )

    cloned = await clone_pipeline(
        rls_session,
        org_id=test_org,
        pipeline_id=source.id,
        account_id=test_user,
    )
    assert cloned is not None

    # Modify original: rename and replace graph
    await update_pipeline(rls_session, source.id, {"name": "Modified Original"})
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=source.id,
        org_id=test_org,
        nodes=[],
        edges=[],
    )

    # Check clone is unchanged
    reloaded_clone = await get_pipeline(rls_session, cloned.id)
    assert reloaded_clone is not None
    assert reloaded_clone.name == "Copy of Independent Test"
    clone_graph = await get_pipeline_graph(rls_session, cloned.id)
    assert clone_graph is not None
    assert len(clone_graph[0]) == 1  # Clone still has its original node


async def test_clone_pipeline_not_found_returns_none(
    rls_session: AsyncSession,
) -> None:
    result = await clone_pipeline(
        rls_session,
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
    )
    assert result is None


async def test_replace_pipeline_graph_removes_stale_edges(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline = await create_pipeline(
        rls_session,
        org_id=test_org,
        name="Graph edge replacement",
        account_id=test_user,
    )
    node_id = uuid.uuid4()
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=pipeline.id,
        org_id=test_org,
        nodes=[],
        edges=[
            {
                "id": uuid.uuid4(),
                "source_node_id": node_id,
                "target_node_id": uuid.uuid4(),
                "edge_type": "normal",
                "hitl_gate_config": None,
            },
        ],
    )
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=pipeline.id,
        org_id=test_org,
        nodes=[],
        edges=[],
    )

    loaded = await get_pipeline_graph(rls_session, pipeline.id)
    assert loaded is not None
    assert loaded[1] == []
