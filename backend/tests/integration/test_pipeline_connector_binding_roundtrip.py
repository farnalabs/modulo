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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
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


async def _seed_connector(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """An ORG-visibility GitHub connector (the binding's existence/team gates pass)."""
    from modulo.db.crud.connector_instance import create_connector_instance

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
        )
        return connector.id


async def _cleanup(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    agent_id: uuid.UUID,
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
    from modulo.core.pipeline_engine.node_runner import _connector_inputs

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
        stored = got.json()["nodes"][0]["connector_binding"]
        assert "resource" not in stored
        assert "filters" not in stored
        assert "data" not in stored

        resource, filters, data = _connector_inputs(stored, {"run_context": {"input": {}}})
        assert resource == "command"
        assert filters == {"provider_ref": "/"}
        assert data == {}
    finally:
        await _cleanup(db_engine, test_org, pipeline_id, agent_id, connector_id)
