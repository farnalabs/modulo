"""FAR-2089: connector binding ``resource``/``filters``/``data`` survive a graph round-trip.

``ConnectorBinding`` gained ``resource``/``filters``/``data`` so a connector node
can carry the query/write payload to the engine. Before the change the API
model's ``extra="ignore"`` silently DROPPED those keys on
``PUT /api/v1/pipelines/{id}/graph``: the write still succeeded and the binding
reached the engine empty, so a GitHub node could not perform a real query.

This test drives the REAL endpoints against the migrated testcontainer. It
fails without the model fields (the GET comes back with no ``resource`` /
``filters`` / ``data``) and passes with them; no session double can reproduce
the drop because it happens during request-body Pydantic validation.
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from farna.auth.jwt import create_access_token

os.environ.setdefault("FARNA_CONTROL_MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_GITHUB = "github"


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role="admin",
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed_agent(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        await conn.execute(
            text(
                "INSERT INTO agents (id, organisation_id, name, prompt_template, account_id, "
                "is_executable, prompt_always_visible, prompt_version_history, connector_type_refs, "
                "required_environment_capabilities, retry_policy) "
                "VALUES (:id, :oid, :n, 't', :uid, true, false, '[]'::json, '[]'::json, '[]'::json, '{}'::json)"
            ),
            {
                "id": str(agent_id),
                "oid": str(org_id),
                "n": f"binding-roundtrip-{agent_id.hex[:8]}",
                "uid": str(account_id),
            },
        )
    return agent_id


async def _seed_connector(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    allowed_operations: list[str] | None = None,
) -> uuid.UUID:
    """An ORG-visibility GitHub connector (the binding's existence/team gates pass)."""
    from farna.db.crud.connector_instance import create_connector_instance

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        connector = await create_connector_instance(
            session,
            org_id=org_id,
            name=f"binding-roundtrip-{uuid.uuid4().hex[:6]}",
            connector_type_id=_GITHUB,
            account_id=account_id,
            credentials_ciphertext=b"ciphertext",
            visibility="org",
            allowed_operations=allowed_operations,
        )
        return connector.id


async def _persisted_binding(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> dict:
    """The connector binding exactly as it is stored in ``pipelines.graph_nodes_json``.

    The graph read strips the same unset inputs the write persists, so the
    HTTP response and this row agree; the row is still read directly because it
    is the shape the engine consumes at run start.
    """
    from farna.db.models.pipeline import Pipeline

    async with db_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        nodes = (await conn.execute(select(Pipeline.__table__.c.graph_nodes_json).where(Pipeline.__table__.c.id == pipeline_id))).scalar_one()
    return dict(nodes[0]["connector_binding"])


async def _cleanup(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    connector_id: uuid.UUID,
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        await conn.execute(text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        await conn.execute(text("DELETE FROM connector_instances WHERE id = :id"), {"id": str(connector_id)})
        if agent_id is not None:
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": str(agent_id)})


async def test_binding_resource_filters_data_survive_a_graph_round_trip(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    headers = _auth_headers(test_org, test_user)
    agent_id = await _seed_agent(db_engine, test_org, test_user)
    connector_id = await _seed_connector(db_engine, test_org, test_user)

    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={"name": f"binding-roundtrip-{uuid.uuid4().hex[:8]}"},
    )
    assert create.status_code == 201, create.text
    pipeline_id = uuid.UUID(create.json()["id"])

    node = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(agent_id),
        "position": {"x": 0, "y": 0},
        "connector_binding": {
            "type": _GITHUB,
            "instance_id": str(connector_id),
            "resource": "repos",
            "filters": {"repo": "x/y", "state": "open"},
            "data": {"title": "t"},
        },
    }

    try:
        put = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json={"nodes": [node], "edges": []},
            headers=headers,
            timeout=30.0,
        )
        assert put.status_code == 200, put.text

        got = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=headers,
            timeout=30.0,
        )
        assert got.status_code == 200, got.text
        stored = got.json()["nodes"][0]["connector_binding"]
        assert stored["resource"] == "repos"
        assert stored["filters"] == {"repo": "x/y", "state": "open"}
        assert stored["data"] == {"title": "t"}
    finally:
        await _cleanup(db_engine, test_org, pipeline_id, agent_id, connector_id)


async def test_binding_without_new_inputs_resaves_without_persisting_nulls(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A fieldless binding keeps the keys ABSENT across a graph re-save.

    FAR-2089 review regression: the new optional fields must not turn into
    ``None`` when an existing graph (one that never set them) is edited and
    saved. Persisted ``filters: None`` raises ``TypeError`` inside the engine's
    ``_connector_inputs`` and ``resource: None`` bypasses its ``"command"``
    default, so every existing query/dispatch binding would crash on its next
    run — the newly added field-populated round trip cannot catch that.
    """
    from farna.core.pipeline_engine.node_runner import _connector_inputs

    headers = _auth_headers(test_org, test_user)
    agent_id = await _seed_agent(db_engine, test_org, test_user)
    connector_id = await _seed_connector(db_engine, test_org, test_user)

    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={"name": f"binding-null-roundtrip-{uuid.uuid4().hex[:8]}"},
    )
    assert create.status_code == 201, create.text
    pipeline_id = uuid.UUID(create.json()["id"])

    node = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(agent_id),
        "position": {"x": 0, "y": 0},
        "connector_binding": {
            "type": _GITHUB,
            "instance_id": str(connector_id),
            "operation": "query",
        },
    }

    try:
        put = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json={"nodes": [node], "edges": []},
            headers=headers,
            timeout=30.0,
        )
        assert put.status_code == 200, put.text

        got = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=headers,
            timeout=30.0,
        )
        assert got.status_code == 200, got.text

        # The READ contract is symmetric with the written/persisted shape:
        # a binding saved without the optional inputs reports them ABSENT, not
        # as ``null`` / ``{}``. Without the read-path strip the response
        # re-materialises the model defaults and this assertion fails.
        first_read = got.json()
        read_binding = first_read["nodes"][0]["connector_binding"]
        assert "resource" not in read_binding
        assert "filters" not in read_binding
        assert "data" not in read_binding

        # The regression is about what a RE-SAVE persists, so drive the actual
        # flow the editor takes: read the graph back and PUT that same payload
        # again. The read response now carries the same ABSENT keys the write
        # persisted, so the round trip stays clean end to end instead of
        # depending on the write path to re-strip read-side nulls.
        re_save = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json={"nodes": first_read["nodes"], "edges": first_read["edges"]},
            headers=headers,
            timeout=30.0,
        )
        assert re_save.status_code == 200, re_save.text

        # Read the PERSISTED graph (what the engine consumes at run start) and
        # pin that the unset inputs stayed ABSENT across the round trip. The
        # read response is now symmetric with this row (asserted above), so the
        # two agree; the row is asserted directly because it is the shape the
        # engine reads at run start.
        stored = await _persisted_binding(db_engine, test_org, pipeline_id)
        assert "resource" not in stored
        assert "filters" not in stored
        assert "data" not in stored

        resource, filters, data = _connector_inputs(stored, {"run_context": {"input": {}}})
        assert resource == "command"
        assert filters == {"provider_ref": "/"}
        assert data == {}
    finally:
        await _cleanup(db_engine, test_org, pipeline_id, agent_id, connector_id)


def _connector_node(connector_id: uuid.UUID) -> dict:
    """A first-class ``connector`` node issuing a GitHub query (FAR-1395)."""
    return {
        "id": str(uuid.uuid4()),
        "node_type": "connector",
        "position": {"x": 0, "y": 0},
        "connector_binding": {
            "type": _GITHUB,
            "instance_id": str(connector_id),
            "operation": "query",
            "resource": "pr_files",
            "filters": {"owner_repo": "x/y", "pull_number": 7},
            "data": {"note": "authored"},
        },
    }


async def test_connector_node_query_round_trips_through_rest(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1395: a ``connector`` query is authorable through the SUPPORTED surface.

    Drives the real ``PUT /pipelines/{id}/graph`` + ``GET`` against the migrated
    testcontainer: the node type, the routed verb and the query payload
    (``resource`` / ``filters`` / ``data``) all survive, and the persisted row —
    the shape ``node_runner._connector_inputs`` consumes — carries them.
    """
    headers = _auth_headers(test_org, test_user)
    connector_id = await _seed_connector(db_engine, test_org, test_user)
    node = _connector_node(connector_id)
    from farna.core.pipeline_engine.node_runner import _connector_inputs

    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={"name": f"connector-query-roundtrip-{uuid.uuid4().hex[:8]}"},
    )
    assert create.status_code == 201, create.text
    pipeline_id = uuid.UUID(create.json()["id"])

    try:
        put = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json={"nodes": [node], "edges": []},
            headers=headers,
            timeout=30.0,
        )
        assert put.status_code == 200, put.text

        got = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=headers,
            timeout=30.0,
        )
        assert got.status_code == 200, got.text
        returned = got.json()["nodes"][0]
        assert returned["node_type"] == "connector"
        binding = returned["connector_binding"]
        assert binding["operation"] == "query"
        assert binding["resource"] == "pr_files"
        assert binding["filters"] == {"owner_repo": "x/y", "pull_number": 7}
        assert binding["data"] == {"note": "authored"}

        stored = await _persisted_binding(db_engine, test_org, pipeline_id)
        resource, filters, _data = _connector_inputs(stored, {"run_context": {"input": {}}})
        assert resource == "pr_files"
        # ``_connector_inputs`` also stamps the shell connector's provider_ref
        # default, so assert the AUTHORED keys rather than whole-dict equality.
        assert filters["owner_repo"] == "x/y"
        assert filters["pull_number"] == 7
    finally:
        await _cleanup(db_engine, test_org, pipeline_id, None, connector_id)


async def test_a_query_outside_the_connector_allowlist_is_refused_at_save(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1395: ``allowed_operations`` actually gates authoring.

    The connector allows ``write`` only, so the routed ``query`` (``read``) must
    be refused with 422 ``CONNECTOR_MISSING_OPERATIONS`` — the save-time mirror
    of the run-time ``ConnectorACL``. Before the fix this issue was advisory and
    the graph persisted a binding every run would then deny.
    """
    headers = _auth_headers(test_org, test_user)
    connector_id = await _seed_connector(db_engine, test_org, test_user, allowed_operations=["write"])
    node = _connector_node(connector_id)
    node["connector_binding"]["resource"] = "issue"

    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={"name": f"connector-allowlist-{uuid.uuid4().hex[:8]}"},
    )
    assert create.status_code == 201, create.text
    pipeline_id = uuid.UUID(create.json()["id"])

    try:
        put = await integration_client.put(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json={"nodes": [node], "edges": []},
            headers=headers,
            timeout=30.0,
        )
        assert put.status_code == 422, put.text
        assert "does not allow 'read'" in put.text

        got = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=headers,
            timeout=30.0,
        )
        assert got.status_code == 200, got.text
        assert not got.json()["nodes"]
    finally:
        await _cleanup(db_engine, test_org, pipeline_id, None, connector_id)
