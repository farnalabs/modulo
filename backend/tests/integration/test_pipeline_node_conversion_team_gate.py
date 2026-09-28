"""ITEM 4: the team gate on convert-to-agent / revert-to-manual, against real Postgres.

The two node-conversion endpoints mutate a pipeline's graph but carried NEITHER
the request-time ``require_team_membership_or_admin`` dependency NOR the
in-txn ``_reapply_team_gate_inside_mutation_txn`` re-check that their siblings
(``replace_pipeline_graph`` / ``update_pipeline``) have - so a non-member org
operator could convert or revert nodes on a TEAM-PRIVATE pipeline.

Runs against the migrated testcontainer with the real auth stack:

* non-member operator -> DENIED, as a 404 "Resource not found" (RLS-parity):
  ``rls_team_isolation`` hides a team-private pipeline row from a non-member,
  so the team-scope resolver's SELECT returns no row and the dependency 404s
  BEFORE the membership check - exactly what
  ``test_trigger_run_team_gate.py`` documents for the sibling gate. The
  dependency's own 403 branch is covered by the unit tests
  (``tests/unit/api/test_pipeline_node_conversion_team_gate.py``) and the
  dependency's PRESENCE is asserted by the route-introspection test. What
  matters end-to-end: the non-member never reaches the HANDLER (their 404
  detail differs from a member's "Node not found"),
* member operator -> the request REACHES THE HANDLER (404 "Node not found" on
  the empty graph - past both gates, before any node work),
* org admin -> same (admin bypass, RLS parity).

The two 404s are distinguished by their detail string: "Resource not found"
(dependency, row invisible) vs "Node not found" (handler, gate passed). A
member/admin reading the dependency's detail would mean the gate over-blocks.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


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


async def _seed_operator_account(db_engine: AsyncEngine, org_id: uuid.UUID, label: str) -> uuid.UUID:
    """A NON-admin ``operator`` account in ``org_id`` (committed).

    The shared ``test_user`` fixture holds the ``admin`` org role, which BOTH
    team-gate layers bypass - the membership half needs its own principal.
    ``pipeline.graph.update`` floors at ``operator``.
    """
    account_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(account_id),
                "email": f"{label}-{account_id.hex[:12]}@example.com",
                "name": f"node-conversion {label}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'operator')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_team(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    member_id: uuid.UUID | None,
    label: str,
) -> uuid.UUID:
    """A committed team, plus an optional membership row, via the ORM CRUD layer."""
    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(
            session,
            org_id=org_id,
            name=f"node-conv-{label}-{uuid.uuid4().hex[:6]}",
            account_id=owner_id,
        )
        if member_id is not None:
            await add_team_member(session, org_id=org_id, team_id=team.id, account_id=member_id, role="operator")
        return team.id


async def _insert_team_private_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    team_id: uuid.UUID,
) -> uuid.UUID:
    """A committed TEAM-PRIVATE pipeline with an EMPTY graph.

    The empty graph is deliberate: a caller who gets past both gates hits
    "Node not found" (404) before any agent/connector work, so the member and
    admin assertions need no further seeding.
    """
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, "
                "'manual_approval', 'team', :tid)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "tid": str(team_id),
                "name": f"team-private-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM runs WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


def _convert_body() -> dict[str, object]:
    return {
        "agent_id": str(uuid.uuid4()),
        "connector_binding": {"type": "github", "instance_id": str(uuid.uuid4())},
        "model_backend_id": str(uuid.uuid4()),
    }


async def _seed_scenario(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns ``(pipeline_id, member_id, non_member_id)``."""
    member = await _seed_operator_account(db_engine, test_org, "member")
    outsider = await _seed_operator_account(db_engine, test_org, "outsider")
    team_id = await _seed_team(db_engine, test_org, test_user, member_id=member, label="gate")
    pipeline_id = await _insert_team_private_pipeline(db_engine, test_org, test_user, team_id)
    return pipeline_id, member, outsider


async def test_convert_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Denied end-to-end: the row is RLS-invisible, so the dependency 404s.

    "Resource not found" is the team-scope dependency's detail - it fires when
    the resolver's SELECT comes back empty. A member's detail is "Node not
    found" (the HANDLER's 404), so this string proves the non-member never got
    past the gate.
    """
    pipeline_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/nodes/{uuid.uuid4()}/convert-to-agent",
            json=_convert_body(),
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_revert_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Sibling denial path for revert-to-manual (same dependency, same 404)."""
    pipeline_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/nodes/{uuid.uuid4()}/revert-to-manual",
            params={"snapshot_id": str(uuid.uuid4())},
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_convert_member_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Past BOTH gates: the empty graph yields 404 "Node not found", not 403."""
    pipeline_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/nodes/{uuid.uuid4()}/convert-to-agent",
            json=_convert_body(),
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Node not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_convert_org_admin_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    pipeline_id, _member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/nodes/{uuid.uuid4()}/convert-to-agent",
            json=_convert_body(),
            headers=_auth_headers(test_org, test_user, role="admin"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Node not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_revert_member_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/nodes/{uuid.uuid4()}/revert-to-manual",
            params={"snapshot_id": str(uuid.uuid4())},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Node not found"
    finally:
        await _cleanup(db_engine, pipeline_id)
