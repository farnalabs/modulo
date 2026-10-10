"""ITEM 4: the team gate on convert-to-agent / revert-to-manual, against real Postgres.

PARITY + defence-in-depth: the two node-conversion endpoints mutate a
pipeline's graph but carried NEITHER the request-time
``require_team_membership_or_admin`` dependency NOR the in-txn
``_reapply_team_gate_inside_mutation_txn`` re-check that their siblings
(``replace_pipeline_graph`` / ``update_pipeline``) have.

That is a parity gap, not a live Postgres hole: migration 0124 drops the
OR-combined ``rls_org_isolation`` policy on ``pipelines`` and leaves
``rls_team_isolation`` as the sole policy, so a non-member's read already
returns no row. The tests below PROVE that - they are the evidence for the
rationale, and the reason the unit tests (which cover the dependency's own 403
branch against a session double) are not the whole story.

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
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token
from tests.integration.test_pipeline_conversion_connector_team_gate import (
    _auth_headers as _conv_auth_headers,
)
from tests.integration.test_pipeline_conversion_connector_team_gate import (
    _cleanup as _cleanup_conv_scenario,
)
from tests.integration.test_pipeline_conversion_connector_team_gate import (
    _convert_body as _conv_body,
)
from tests.integration.test_pipeline_conversion_connector_team_gate import (
    _seed_connector,
    _seed_org_connector,
)
from tests.integration.test_pipeline_conversion_connector_team_gate import (
    _seed_scenario as _seed_conv_scenario,
)

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


# ---------------------------------------------------------------------------
# FAR-1515 / FAR-1618: the connector team gate on the same conversion path
# ---------------------------------------------------------------------------


async def _seed_member_behind_a_conversion(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    label: str,
    *,
    pipeline_scoped_to_team: bool = True,
) -> tuple[uuid.UUID, Any]:
    """A plain TEAM MEMBER (not an admin) positioned to drive a conversion.

    The shared ``test_user`` fixture is an org admin, which bypasses BOTH
    team-gate layers — so the member is what makes a refusal observable as a
    BINDING rule rather than an admin-bypass artefact. Returns
    ``(member_account_id, scenario)``; the caller owns cleanup. The member
    joins Team A (the team the seeded team-private rows belong to), so RLS
    lets them see exactly those rows — a row the gate would need to judge must
    not be hidden from the caller first.
    """
    from modulo.db.crud.team_membership import add_team_member

    member = await _seed_operator_account(db_engine, test_org, label)
    scenario = await _seed_conv_scenario(
        db_engine, test_org, test_user, pipeline_scoped_to_team=pipeline_scoped_to_team
    )

    # The member joins Team A - enough to clear
    # ``require_team_membership_or_admin`` on a Team-A pipeline AND to see
    # Team A's team-private rows under ``rls_team_isolation``.
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(test_org)})
        await add_team_member(session, org_id=test_org, team_id=scenario.team_a, account_id=member, role="operator")
    return member, scenario


async def test_convert_member_binds_a_team_private_connector_to_an_org_pipeline_and_is_rejected_409(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A team MEMBER (not an admin) hitting the team-PRIVATE half of the gate.

    Every other case in the sibling conversion file drives an org admin. This
    one puts a plain member behind the request, so the refusal is shown to be
    about the BINDING — not about the admin bypass, and not about the
    membership gate, which the member passes first.

    The binding refused here is an ORG pipeline (no owner team) pinning Team
    A's own team-private connector: a team-private row is usable ONLY by a
    pipeline owned by its team, so an ownerless pipeline is the other
    direction of the rule FAR-1618 keeps. A member driving a TEAM pipeline at
    ANOTHER team's connector cannot be constructed honestly —
    ``rls_team_isolation`` hides that row from them and the endpoint 404s
    before the gate — which is exactly why the sibling file drives that case
    as an org admin (who sees both rows).

    It also pins the parity half: the same request carries an ORG model
    backend, and that must NOT be rejected (ModelBackendHub has no
    invocation-time visibility gate), so the only named error in the detail is
    ``connector_team_mismatch``.

    Uses the sibling module's seeding machinery — those helpers build the
    pipeline + agent + backend + node this path needs, and re-implementing
    them here would drift.
    """
    member, scenario = await _seed_member_behind_a_conversion(
        db_engine, test_org, test_user, "conv-gate-member", pipeline_scoped_to_team=False
    )
    team_connector = await _seed_connector(db_engine, test_org, test_user, scenario.team_a)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_conv_body(scenario, connector_id=team_connector),
            headers=_conv_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 409, resp.text
        detail = str(resp.json()["detail"])
        assert "connector_team_mismatch" in detail, resp.text
        assert "is team-private" in detail, resp.text
        assert "model_backend_team_mismatch" not in detail, resp.text
    finally:
        # Removes the teams, their memberships (including the one added above)
        # and every other row this scenario created.
        await _cleanup_conv_scenario(db_engine, test_org, scenario, team_connector)


async def test_convert_member_binds_an_org_visibility_connector_and_is_accepted(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1618: the same member, binding an ORG-wide connector, is NOT refused.

    Org resources stay shared across the organisation, so a team pipeline's
    member may pin an ``visibility=org`` connector — there is no
    ``connector_team_mismatch`` (and, as before, no
    ``model_backend_team_mismatch`` for the org backend). What matters is that
    the request is NOT stopped by this gate; whatever the NEXT save-time gate
    says is out of scope, exactly as in the other accepted cases.
    """
    member, scenario = await _seed_member_behind_a_conversion(db_engine, test_org, test_user, "conv-gate-org-member")
    org_connector = await _seed_org_connector(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{scenario.pipeline_id}/nodes/{scenario.node_id}/convert-to-agent",
            json=_conv_body(scenario, connector_id=org_connector),
            headers=_conv_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code != 409, (
            f"team pipeline + org-visibility connector must not be a connector_team_mismatch: {resp.text}"
        )
        assert "connector_team_mismatch" not in str(resp.json().get("detail", "")), resp.text
    finally:
        # Removes the teams, their memberships (including the one added above)
        # and every other row this scenario created.
        await _cleanup_conv_scenario(db_engine, test_org, scenario, org_connector)
