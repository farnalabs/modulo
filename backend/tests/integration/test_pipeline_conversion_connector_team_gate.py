"""FAR-1278: cross-team connector rejection on node conversion, against real Postgres.

PR #1075 made the node-conversion save path (``_save_locked_graph``) run the
same save-time enforcement the sibling graph-write endpoints run:

1. ``_enforce_connector_team_bindings`` - 409 ``connector_team_mismatch`` when a
   team-private connector is bound to another team's pipeline,
2. ``_resolve_graph_references`` - the FAR-418 capability-scope widening guard
   (422), unknown agent/schema ids (422) and the model-backend team check (409),
3. the write,
4. ``_validate_graph_save`` - blocking codes raise 422 after the write.

That coverage was unit-only (a session double answering the queries). These
tests drive the REAL endpoint against the migrated testcontainer with REAL
rows: a team-private connector owned by ANOTHER team, bound through
``POST /{id}/nodes/{node_id}/convert-to-agent``.

Why an org-admin caller: ``connector_instances`` carries ``rls_team_isolation``
as its sole policy (migration 0124), so a caller outside the connector's team
cannot even SEE that row - the endpoint's own connector lookup would 404 before
the enforcement ran. The mismatch the gate is about (pipeline team != connector
team) is orthogonal to who is calling, so an admin principal - who sees both rows
- is what makes the gate itself observable. The enforcement is what must reject;
the caller's rights are not under test here.

The third test is the load-bearing one: the SAME request, with the enforcement
patched out of the save path, must get past the connector gate. Without it, a
green 409 would only prove that SOMETHING rejected the request.

FAR-1515 widened the gate to both directions; FAR-1618 narrowed it back to the
team-PRIVATE direction only (teams are a visibility grouping, not a credential
trust boundary), so the direction matrix here is: a team-private connector from
another team -> 409; a team pipeline pinning an ORG-VISIBILITY connector ->
accepted (org resources stay shared); a team pipeline with its own team's
connector -> accepted; an ORG pipeline with an org connector -> accepted
(unchanged).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_GITHUB = "github"
_PREFIX = "modulo.api.routes.pipelines."


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID, role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role=role,
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _seed_team(db_engine: AsyncEngine, org_id: uuid.UUID, owner_id: uuid.UUID, label: str) -> uuid.UUID:
    from modulo.db.crud.team import create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(
            session, org_id=org_id, name=f"conv-conn-{label}-{uuid.uuid4().hex[:6]}", account_id=owner_id
        )
        return team.id


async def _seed_agent(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    connector_type_refs: list[dict[str, object]],
) -> uuid.UUID:
    from modulo.db.crud.agent import create_agent

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await session.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        agent = await create_agent(
            session,
            org_id=org_id,
            name=f"conv-conn-agent-{uuid.uuid4().hex[:6]}",
            account_id=account_id,
            prompt_template="You are a test agent.",
            connector_type_refs=list(connector_type_refs),
        )
        return agent.id


async def _seed_model_backend(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    from modulo.db.crud.model_backend import create_model_backend

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await session.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        backend = await create_model_backend(
            session,
            org_id=org_id,
            name=f"conv-conn-backend-{uuid.uuid4().hex[:6]}",
            display_name="Conv Conn Backend",
            # ck_model_backends_provider is a closed vocabulary ("stub" is not
            # in it); "custom" is.
            provider="custom",
            model_id="stub-model",
            credentials_ciphertext=b"ciphertext",
            account_id=account_id,
        )
        return backend.id


async def _seed_connector(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    owner_team_id: uuid.UUID,
) -> uuid.UUID:
    """A TEAM-PRIVATE connector owned by ``owner_team_id`` (execution context so
    the team-visibility RLS WITH CHECK lets the row in - the same escape hatch
    the background machinery uses)."""
    from modulo.db.crud.connector_instance import create_connector_instance

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await session.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        connector = await create_connector_instance(
            session,
            org_id=org_id,
            name=f"conv-conn-{uuid.uuid4().hex[:6]}",
            connector_type_id=_GITHUB,
            account_id=account_id,
            credentials_ciphertext=b"ciphertext",
            visibility="team",
            owner_team_id=owner_team_id,
        )
        return connector.id


async def _seed_pipeline_with_manual_node(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    team_id: uuid.UUID | None,
    *,
    node: dict[str, object],
) -> uuid.UUID:
    """A committed pipeline holding one manual node.

    ``team_id=None`` seeds an ORG-scoped pipeline (``visibility='org'``,
    ``owner_team_id=NULL``) — the "does an org pipeline still save" direction
    needs it: an ORG pipeline keeps accepting an org-visibility connector, and
    (the direction FAR-1618 keeps) still refuses a team-private one.
    """
    pipeline_id = uuid.uuid4()
    visibility = "team" if team_id is not None else "org"
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, CAST(:nodes AS json), "
                "'manual_approval', :visibility, :tid)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "tid": str(team_id) if team_id is not None else None,
                "visibility": visibility,
                "name": f"conv-conn-{pipeline_id.hex[:8]}",
                "nodes": json.dumps([node]),
            },
        )
    return pipeline_id


def _manual_node(*, capability_scope: dict[str, object] | None = None) -> dict[str, object]:
    node: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
        "label": "convert me",
    }
    if capability_scope is not None:
        node["capability_scope"] = capability_scope
    return node


async def _cleanup(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    scenario: _Scenario,
    *extra_connector_ids: uuid.UUID,
) -> None:
    """Delete this test's rows, with the RLS context the team-scoped tables need.

    ``pipelines`` / ``connector_instances`` / ``model_backends`` carry
    ``rls_team_isolation`` as their sole policy, so a connection with no
    ``app.organisation_id`` / ``app.execution_context`` would see (and therefore
    delete) nothing. Setting both makes the cleanup work whether or not the test
    connection is policy-exempt. FK order matters: pipelines and connectors
    RESTRICT against teams, and agents RESTRICT against model backends.
    """
    async with db_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"), {"pid": str(scenario.pipeline_id)}
        )
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(scenario.pipeline_id)})
        await conn.execute(
            text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"), {"pid": str(scenario.pipeline_id)}
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(scenario.pipeline_id)})
        for connector_id in (*extra_connector_ids, scenario.connector_id):
            await conn.execute(text("DELETE FROM connector_instances WHERE id = :id"), {"id": str(connector_id)})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": str(scenario.agent_id)})
        await conn.execute(text("DELETE FROM model_backends WHERE id = :id"), {"id": str(scenario.backend_id)})
        for team_id in (scenario.team_a, scenario.team_b):
            await conn.execute(text("DELETE FROM team_memberships WHERE team_id = :tid"), {"tid": str(team_id)})
            await conn.execute(text("DELETE FROM teams WHERE id = :id"), {"id": str(team_id)})


class _Scenario:
    def __init__(
        self,
        *,
        pipeline_id: uuid.UUID,
        node_id: uuid.UUID,
        agent_id: uuid.UUID,
        backend_id: uuid.UUID,
        connector_id: uuid.UUID,
        team_a: uuid.UUID,
        team_b: uuid.UUID,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.node_id = node_id
        self.agent_id = agent_id
        self.backend_id = backend_id
        self.connector_id = connector_id
        self.team_a = team_a
        self.team_b = team_b


async def _seed_scenario(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    *,
    capability_scope: dict[str, object] | None = None,
    agent_connector_type_refs: list[dict[str, object]] | None = None,
    pipeline_scoped_to_team: bool = True,
) -> _Scenario:
    """Team A owns the pipeline; Team B owns the connector bound into it.

    ``pipeline_scoped_to_team=False`` seeds an ORG-scoped pipeline instead
    (``visibility='org'``, ``owner_team_id=NULL``) — the FAR-1515 regression
    guard that must KEEP saving an org-visibility connector (and since
    FAR-1618 so must a team-scoped pipeline).

    ``agent_connector_type_refs`` defaults to a GitHub grant (so the ordinary
    cases have a conforming Agent); the scope-widening test passes ``[]`` so the
    node's ``allowed_connectors`` is a WIDEN rather than a narrowing.

    Cleans up its own partial seed on failure: the rows are created one at a
    time, so a failure on step N would otherwise strand steps 1..N-1 in the
    shared database for the rest of the session.
    """
    created: dict[str, list[uuid.UUID]] = {"teams": [], "agents": [], "backends": [], "connectors": [], "pipelines": []}
    try:
        team_a = await _seed_team(db_engine, test_org, test_user, "a")
        created["teams"].append(team_a)
        team_b = await _seed_team(db_engine, test_org, test_user, "b")
        created["teams"].append(team_b)
        agent_id = await _seed_agent(
            db_engine,
            test_org,
            test_user,
            connector_type_refs=(
                [{"connector_type": _GITHUB, "capabilities": ["issue_read"]}]
                if agent_connector_type_refs is None
                else agent_connector_type_refs
            ),
        )
        created["agents"].append(agent_id)
        backend_id = await _seed_model_backend(db_engine, test_org, test_user)
        created["backends"].append(backend_id)
        connector_id = await _seed_connector(db_engine, test_org, test_user, team_b)
        created["connectors"].append(connector_id)
        node = _manual_node(capability_scope=capability_scope)
        pipeline_id = await _seed_pipeline_with_manual_node(
            db_engine,
            test_org,
            test_user,
            team_a if pipeline_scoped_to_team else None,
            node=node,
        )
        created["pipelines"].append(pipeline_id)
    except Exception:
        await _cleanup_partial_seed(db_engine, test_org, created)
        raise
    return _Scenario(
        pipeline_id=pipeline_id,
        node_id=uuid.UUID(str(node["id"])),
        agent_id=agent_id,
        backend_id=backend_id,
        connector_id=connector_id,
        team_a=team_a,
        team_b=team_b,
    )


async def _cleanup_partial_seed(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    created: dict[str, list[uuid.UUID]],
) -> None:
    """Best-effort removal of a half-built scenario (never raises)."""
    try:
        async with db_engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
            await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
            for pipeline_id in created["pipelines"]:
                await conn.execute(
                    text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)}
                )
                await conn.execute(
                    text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)}
                )
                await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
            for connector_id in created["connectors"]:
                await conn.execute(text("DELETE FROM connector_instances WHERE id = :id"), {"id": str(connector_id)})
            for agent_id in created["agents"]:
                await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": str(agent_id)})
            for backend_id in created["backends"]:
                await conn.execute(text("DELETE FROM model_backends WHERE id = :id"), {"id": str(backend_id)})
            for team_id in created["teams"]:
                await conn.execute(text("DELETE FROM team_memberships WHERE team_id = :tid"), {"tid": str(team_id)})
                await conn.execute(text("DELETE FROM teams WHERE id = :id"), {"id": str(team_id)})
    except Exception:
        # Best-effort: cleanup must never mask the original seeding failure.
        return


def _convert_body(scenario: _Scenario, *, connector_id: uuid.UUID | None = None) -> dict[str, object]:
    return {
        "agent_id": str(scenario.agent_id),
        "connector_binding": {
            "type": _GITHUB,
            "instance_id": str(connector_id or scenario.connector_id),
        },
        "model_backend_id": str(scenario.backend_id),
    }


# ---------------------------------------------------------------------------
# 1. Cross-team connector binding: 409
# ---------------------------------------------------------------------------


async def test_cross_team_connector_binding_is_rejected_409(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A team-private connector owned by ANOTHER team must not be persisted.

    The named error in ``detail`` is what lets a client branch on it, so assert
    on it rather than only the status - the same detail ``PATCH /graph`` returns
    for the identical mismatch.
    """
    scenario = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_convert_body(scenario),
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code == 409, resp.text
        assert "connector_team_mismatch" in str(resp.json()["detail"]), resp.text

        # Nothing was written: the gate fires BEFORE the graph write.
        async with db_engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(test_org)})
            await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
            raw = (
                await conn.execute(
                    text("SELECT graph_nodes_json FROM pipelines WHERE id = :id"),
                    {"id": str(scenario.pipeline_id)},
                )
            ).scalar_one()
        nodes = json.loads(raw) if isinstance(raw, str) else raw
        assert nodes, "the seeded node must still be there"
        assert nodes[0]["node_type"] == "manual", "the rejected conversion must not have been persisted"
    finally:
        await _cleanup(db_engine, test_org, scenario)


async def test_same_team_connector_binding_is_accepted(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The gate must not blanket-block: a connector owned by the PIPELINE's team passes it.

    Reaches whatever the next save-time gate says - what matters here is that
    it is NOT the cross-team 409.
    """
    scenario = await _seed_scenario(db_engine, test_org, test_user)
    same_team_connector = await _seed_connector(db_engine, test_org, test_user, scenario.team_a)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_convert_body(scenario, connector_id=same_team_connector),
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code != 409, f"same-team binding must not be a connector_team_mismatch: {resp.text}"
        assert "connector_team_mismatch" not in str(resp.json().get("detail", "")), resp.text
    finally:
        await _cleanup(db_engine, test_org, scenario, same_team_connector)


async def test_rejection_comes_from_the_enforcement_itself(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Prove the 409 is load-bearing: remove the enforcement, lose the 409.

    The same request is replayed with ``_enforce_connector_team_bindings``
    stubbed to a no-op. If the 409 came from anywhere else (RLS, the endpoint's
    own connector lookup, a validator), this would still be 409 and the test
    would fail. Anything but 409 here is the point - the exact status beyond
    that is whatever the NEXT save-time gate says.
    """
    scenario = await _seed_scenario(db_engine, test_org, test_user)
    try:
        with patch(f"{_PREFIX}_enforce_connector_team_bindings", new=AsyncMock()):
            resp = await integration_client.post(
                f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
                json=_convert_body(scenario),
                headers=_auth_headers(test_org, test_user, role="admin"),
                timeout=30.0,
            )
        assert resp.status_code != 409, (
            f"the cross-team 409 survived the enforcement being removed - it is coming from somewhere else: {resp.text}"
        )
        assert "connector_team_mismatch" not in str(resp.json().get("detail", "")), resp.text
    finally:
        await _cleanup(db_engine, test_org, scenario)


# ---------------------------------------------------------------------------
# 1b. FAR-1618: team pipeline + ORG-VISIBILITY connector is ACCEPTED
# ---------------------------------------------------------------------------


async def test_org_visibility_connector_on_team_pipeline_is_accepted(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1618: an org-wide connector binds to a team pipeline just like any other.

    Reverses the FAR-1515 reverse direction (and the FAR-516 run-gate it
    mirrored): the executor no longer scopes a run against the connector's
    visibility, so there is no dead-on-arrival graph to refuse. What matters
    here is that the request is NOT stopped by this gate — it must not answer
    409 / ``connector_team_mismatch``. Whatever the NEXT save-time gate says
    is out of scope, exactly as in the other accepted cases.
    """
    scenario = await _seed_scenario(db_engine, test_org, test_user)
    org_connector = await _seed_org_connector(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_convert_body(scenario, connector_id=org_connector),
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code != 409, (
            f"team pipeline + org-visibility connector must not be a connector_team_mismatch: {resp.text}"
        )
        assert "connector_team_mismatch" not in str(resp.json().get("detail", "")), resp.text
    finally:
        await _cleanup(db_engine, test_org, scenario, org_connector)


async def test_org_pipeline_with_org_connector_is_accepted(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1515: the ORG-pipeline rule must NOT change - org + org still saves.

    Mirrors ``test_same_team_connector_binding_is_accepted``: what matters is
    that the request is NOT stopped by this gate, so it must not answer 409 /
    ``connector_team_mismatch``. Whatever the NEXT save-time gate says is out
    of scope here.
    """
    scenario = await _seed_scenario(db_engine, test_org, test_user, pipeline_scoped_to_team=False)
    org_connector = await _seed_org_connector(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_convert_body(scenario, connector_id=org_connector),
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code != 409, (
            f"org pipeline + org connector must not be a connector_team_mismatch: {resp.text}"
        )
        assert "connector_team_mismatch" not in str(resp.json().get("detail", "")), resp.text
    finally:
        await _cleanup(db_engine, test_org, scenario, org_connector)


# ---------------------------------------------------------------------------
# 2. FAR-418 capability-scope widening: 422
# ---------------------------------------------------------------------------


async def test_scope_widening_binding_is_rejected_422(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A node whose ``capability_scope`` names a connector type the Agent does not grant.

    The stored manual node declares ``allowed_connectors: ["github"]`` while the
    Agent grants nothing - a widen, which the graph save refuses (422) rather
    than silently persisting. This rides the SAME conversion save path, so it is
    the second half of PR #1075's save-time enforcement coverage.
    """
    scenario = await _seed_scenario(
        db_engine,
        test_org,
        test_user,
        capability_scope={"allowed_connectors": [_GITHUB]},
        # An Agent that grants NOTHING makes the node's allowed_connectors a
        # widen (github not in the grant set) rather than a narrowing.
        agent_connector_type_refs=[],
    )
    # The connector must clear the team gate here: since FAR-1515 an ORG-only
    # connector on this TEAM pipeline is its own 409, which would fire first
    # and mask the scope violation this test is about. Team A owns the
    # pipeline, so a Team-A connector is the one binding that passes.
    team_a_connector = await _seed_connector(db_engine, test_org, test_user, scenario.team_a)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_convert_body(scenario, connector_id=team_a_connector),
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code == 422, resp.text
        assert "scope.violation" in str(resp.json()["detail"]), resp.text
    finally:
        await _cleanup(db_engine, test_org, scenario, team_a_connector)


async def _seed_org_connector(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    from modulo.db.crud.connector_instance import create_connector_instance

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        connector = await create_connector_instance(
            session,
            org_id=org_id,
            name=f"conv-conn-org-{uuid.uuid4().hex[:6]}",
            connector_type_id=_GITHUB,
            account_id=account_id,
            credentials_ciphertext=b"ciphertext",
            visibility="org",
        )
        return connector.id
