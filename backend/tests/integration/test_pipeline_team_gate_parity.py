"""FAR-1311 + FAR-1312: team-gate parity on the graph read and the snapshot writes, on real Postgres.

The three endpoints under test:

* ``GET /api/v1/pipelines/{id}/graph`` (FAR-1311) carried NO team dependency
  while its sibling ``GET /api/v1/pipelines/{id}`` did - so on the layers
  where RLS does not apply any org member with ``pipeline.graph.read`` could
  read a team-private pipeline's full graph (prompts,
  ``connector_bindings_json``, model pins),
* ``POST /api/v1/pipelines/{id}/snapshots`` (save-edit, FAR-1312) and
  ``PATCH /api/v1/pipelines/{id}/snapshots/{snapshot_id}`` (tag, FAR-1312)
  carried the request-time dependency but no
  ``_reapply_team_gate_inside_mutation_txn`` re-check inside the mutation
  transaction, unlike update / replace-graph / convert / revert / rollback /
  delete-snapshot.

None of the three is a live Postgres hole: migration 0124 drops the
OR-combined ``rls_org_isolation`` policy on ``pipelines`` and leaves
``rls_team_isolation`` as the sole policy, so a non-member's read already
returns no row. These tests PROVE that - they are the evidence for the
rationale, and the reason the unit tests (which cover the dependency's own 403
branch against a session double) are not the whole story. The in-txn half of
FAR-1312 (a denial overriding a passing request-time gate) is covered by
``tests/unit/api/test_pipeline_graph_snapshot_team_gate.py``: on Postgres a
non-member never gets that far, because RLS hides the row first.

Runs against the migrated testcontainer with the real auth stack:

* non-member operator -> DENIED, as a 404 "Resource not found" (RLS-parity):
  the team-scope resolver's SELECT returns no row, so the dependency 404s
  BEFORE the handler - distinguished from the handler's own 404 by its detail
  string ("Resource not found" vs "Pipeline not found"),
* member operator -> the request REACHES THE HANDLER (200),
* org admin -> same (admin bypass, RLS parity).
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
    ``pipeline.graph.read`` floors at ``viewer``; the snapshot writes floor at
    ``pipeline.graph.update`` / ``pipeline.update`` (``operator``).
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
                "name": f"team-gate-parity {label}",
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
            name=f"parity-{label}-{uuid.uuid4().hex[:6]}",
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
    """A committed TEAM-PRIVATE pipeline with an EMPTY graph."""
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


async def _insert_snapshot(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> uuid.UUID:
    """A committed snapshot row for *pipeline_id* (the tag endpoint's target)."""
    snapshot_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


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


async def _seed_scenario(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns ``(pipeline_id, snapshot_id, member_id, non_member_id)``."""
    member = await _seed_operator_account(db_engine, test_org, "member")
    outsider = await _seed_operator_account(db_engine, test_org, "outsider")
    team_id = await _seed_team(db_engine, test_org, test_user, member_id=member, label="gate")
    pipeline_id = await _insert_team_private_pipeline(db_engine, test_org, test_user, team_id)
    snapshot_id = await _insert_snapshot(db_engine, test_org, pipeline_id)
    return pipeline_id, snapshot_id, member, outsider


# ---------------------------------------------------------------------------
# FAR-1311: GET /api/v1/pipelines/{id}/graph
# ---------------------------------------------------------------------------


async def test_get_graph_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Denied end-to-end: the row is RLS-invisible, so the dependency 404s.

    "Resource not found" is the team-scope dependency's detail - it fires when
    the resolver's SELECT comes back empty. The HANDLER's own 404 (what this
    endpoint answered BEFORE the dependency was added, under the same RLS
    read) is "Pipeline not found", so the detail string is what proves the
    gate - not the handler - produced this 404.
    """
    pipeline_id, _snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_get_graph_member_reads_the_graph(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Past the gate: a member reads the team-private pipeline's (empty) graph."""
    pipeline_id, _snapshot_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert not body["nodes"]
        assert not body["edges"]
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_get_graph_org_admin_reads_the_graph(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    pipeline_id, _snapshot_id, _member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            headers=_auth_headers(test_org, test_user, role="admin"),
        )
        assert resp.status_code == 200, resp.text
        assert not resp.json()["nodes"]
    finally:
        await _cleanup(db_engine, pipeline_id)


# ---------------------------------------------------------------------------
# FAR-1312: POST /api/v1/pipelines/{id}/snapshots (save-edit)
# ---------------------------------------------------------------------------


async def test_save_edit_snapshot_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, _snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/snapshots",
            json={"draft": True},
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_save_edit_snapshot_member_succeeds(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Past BOTH layers: a member's live-edit save writes an ``edit`` snapshot."""
    pipeline_id, _snapshot_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/snapshots",
            json={"draft": True, "channel": "stable"},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["draft"] is True
        assert body["version_kind"] == "edit"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_save_edit_snapshot_org_admin_succeeds(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, _snapshot_id, _member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/snapshots",
            json={},
            headers=_auth_headers(test_org, test_user, role="admin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["version_kind"] == "edit"
    finally:
        await _cleanup(db_engine, pipeline_id)


# ---------------------------------------------------------------------------
# FAR-1312: PATCH /api/v1/pipelines/{id}/snapshots/{snapshot_id} (tag)
# ---------------------------------------------------------------------------


async def test_tag_snapshot_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The new in-txn gate is behind the request-time one: the non-member is
    stopped at the dependency, and the tag is never written."""
    pipeline_id, snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}",
            json={"tag": "prod"},
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"

        # The denial really is a denial: the row kept its (absent) tag.
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT tag FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).scalar_one_or_none()
        assert stored is None, f"the denied request wrote tag={stored!r}"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_tag_snapshot_member_succeeds(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, snapshot_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}",
            json={"tag": "prod", "notes": "release"},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tag"] == "prod"

        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT tag FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).scalar_one()
        assert stored == "prod"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_tag_snapshot_org_admin_succeeds(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, snapshot_id, _member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}",
            json={"tag": "canary"},
            headers=_auth_headers(test_org, test_user, role="admin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tag"] == "canary"
    finally:
        await _cleanup(db_engine, pipeline_id)
