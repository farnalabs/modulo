"""Integration tests for Pipeline CRUD.

RLS is set to test_org; all inserts are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy import text
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
from modulo.db.rls import set_rls_org, set_rls_user_context

pytestmark = pytest.mark.integration


async def _seed_nodes(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
    node_ids: list[uuid.UUID],
) -> None:
    """Materialise graph node rows for the pipeline_edges node references.

    ``pipeline_edges_(source|target)_node_id_fkey`` was added by the ORIGINAL
    ``0164_add_missing_foreign_keys`` and has since been dropped from it
    (edit-in-place, 0164-round-2): ``nodes`` is the DEPRECATED composite-template
    table, and graph node IDs live only in ``pipelines.graph_nodes_json`` — they
    never materialise into ``nodes``, so the constraint was unsatisfiable. No FK
    on ``*_node_id`` exists in the chain today, so this seeding is not currently
    load-bearing; it is kept because ``replace_pipeline_graph`` persists only
    ``graph_nodes_json``, and writing the referenced rows keeps these tests
    referentially honest — the precondition 0166/0170 name for ever re-adding a
    real FK.
    """
    for nid in node_ids:
        await session.execute(
            text(
                "INSERT INTO nodes (id, organisation_id, pipeline_id, name, account_id, timeout_seconds) "
                "VALUES (:id, :oid, :pid, :name, :aid, 300) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": str(nid),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "name": f"node-{nid.hex[:8]}",
                "aid": str(user_id),
            },
        )
    await session.flush()


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
    await _seed_nodes(rls_session, test_org, pipeline.id, test_user, [first_node, second_node])
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
        is_privileged=True,
        caller_type="rest",
    )

    assert saved is not None
    loaded = await get_pipeline_graph(rls_session, pipeline.id)
    assert loaded is not None
    loaded_nodes, loaded_edges = loaded
    assert loaded_nodes == nodes
    assert len(loaded_edges) == 1
    assert loaded_edges[0].id == edge_id
    assert loaded_edges[0].pipeline_id == pipeline.id


async def test_replace_pipeline_graph_multi_edge_round_trip(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline = await create_pipeline(
        rls_session,
        org_id=test_org,
        name="Multi-edge graph save",
        account_id=test_user,
    )
    node_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
    node_uuids = [uuid.UUID(n) for n in node_ids]
    nodes = [
        {
            "id": node_ids[0],
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 0, "y": 0},
            "connector_binding": None,
        },
        {
            "id": node_ids[1],
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 200, "y": 0},
            "connector_binding": None,
        },
        {
            "id": node_ids[2],
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 400, "y": 0},
            "connector_binding": None,
        },
    ]
    edge_ids = [uuid.uuid4(), uuid.uuid4()]
    edges = [
        {
            "id": edge_ids[0],
            "source_node_id": node_ids[0],
            "target_node_id": node_ids[1],
            "edge_type": "normal",
            "hitl_gate_config": None,
        },
        {
            "id": edge_ids[1],
            "source_node_id": node_ids[1],
            "target_node_id": node_ids[2],
            "edge_type": "normal",
            "hitl_gate_config": None,
        },
    ]
    await _seed_nodes(rls_session, test_org, pipeline.id, test_user, node_uuids)
    saved = await replace_pipeline_graph(
        rls_session,
        pipeline_id=pipeline.id,
        org_id=test_org,
        nodes=nodes,
        edges=edges,
        is_privileged=True,
        caller_type="rest",
    )

    assert saved is not None
    loaded = await get_pipeline_graph(rls_session, pipeline.id)
    assert loaded is not None
    loaded_nodes, loaded_edges = loaded
    assert len(loaded_nodes) == 3
    assert len(loaded_edges) == 2
    assert {edge.id for edge in loaded_edges} == set(edge_ids)
    assert all(edge.pipeline_id == pipeline.id for edge in loaded_edges)


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
    await _seed_nodes(rls_session, test_org, source.id, test_user, [first_node, second_node])
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
        is_privileged=True,
        caller_type="rest",
    )

    # The clone's step-(a) read session is a separate connection (its own pool
    # checkout), so under READ COMMITTED it cannot see rls_session's uncommitted
    # rows. Mirror production, where the source is already committed when the
    # clone endpoint runs: commit the source, then re-establish the RLS context
    # that SET LOCAL reset at commit.
    await rls_session.commit()
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")

        cloned = await clone_pipeline(
            rls_session,
            org_id=test_org,
            pipeline_id=source.id,
            account_id=test_user,
            org_role="admin",
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
        is_privileged=True,
        caller_type="rest",
    )

    # Commit the source so the clone's separate step-(a) read connection can
    # see it (READ COMMITTED hides rls_session's uncommitted rows), then
    # re-establish the RLS context that SET LOCAL reset at commit.
    await rls_session.commit()
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")

        cloned = await clone_pipeline(
            rls_session,
            org_id=test_org,
            pipeline_id=source.id,
            account_id=test_user,
            org_role="admin",
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
            is_privileged=True,
            caller_type="rest",
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
    target_node_id = uuid.uuid4()
    await _seed_nodes(rls_session, test_org, pipeline.id, test_user, [node_id, target_node_id])
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=pipeline.id,
        org_id=test_org,
        nodes=[],
        edges=[
            {
                "id": uuid.uuid4(),
                "source_node_id": node_id,
                "target_node_id": target_node_id,
                "edge_type": "normal",
                "hitl_gate_config": None,
            },
        ],
        is_privileged=True,
        caller_type="rest",
    )
    await replace_pipeline_graph(
        rls_session,
        pipeline_id=pipeline.id,
        org_id=test_org,
        nodes=[],
        edges=[],
        is_privileged=True,
        caller_type="rest",
    )

    loaded = await get_pipeline_graph(rls_session, pipeline.id)
    assert loaded is not None
    assert not loaded[1]
