"""Team-gate parity on the pipeline READ/WRITE surfaces, on real Postgres.

The endpoints under test:

* ``GET /api/v1/pipelines/{id}/graph`` (FAR-1311) carried NO team dependency
  while its sibling ``GET /api/v1/pipelines/{id}`` did - so on the layers
  where RLS does not apply any org member with ``pipeline.graph.read`` could
  read a TEAM-PRIVATE pipeline's full graph (prompts,
  ``connector_bindings_json``, model pins),
* ``POST /api/v1/pipelines/{id}/snapshots`` (save-edit, FAR-1312) and
  ``PATCH /api/v1/pipelines/{id}/snapshots/{snapshot_id}`` (tag, FAR-1312)
  carried the request-time dependency but no
  ``_reapply_team_gate_inside_mutation_txn`` re-check inside the mutation
  transaction, unlike update / replace-graph / convert / revert / rollback /
  delete-snapshot,
* ``POST /{id}/snapshots/diff`` (FAR-1362) carried the request-time
  dependency but no in-txn re-check, and resolved BOTH snapshot ids by primary
  key alone - the ``{id}`` path segment was decorative (FAR-1360),
* ``POST /{id}/archive`` / ``/unarchive`` / ``/restore`` (FAR-1362) carried
  the in-txn re-check but NO request-time dependency, and ``/restore`` needs a
  deleted-inclusive resolver because its target row is soft-deleted,
* ``PATCH /{id}/snapshots/{snapshot_id}`` also scoped its read to the path
  pipeline (FAR-1360).

None of these is a live Postgres hole: migration 0124 drops the
OR-combined ``rls_org_isolation`` policy on ``pipelines`` and leaves
``rls_team_isolation`` as the sole policy, so a non-member's read already
returns no row. These tests PROVE that - they are the evidence for the
rationale, and the reason the unit tests (which cover the dependency's own 403
branch against a session double) are not the whole story. The in-txn half of
FAR-1312 / FAR-1362 (a denial overriding a passing request-time gate) is
covered by ``tests/unit/api/test_pipeline_graph_snapshot_team_gate.py`` and
``tests/unit/api/test_snapshot_path_scoping_and_residual_gates.py``: on
Postgres a non-member never gets that far, because RLS hides the row first.

Runs against the migrated testcontainer with the real auth stack:

* non-member operator -> DENIED, as a 404 "Resource not found" (RLS-parity):
  the team-scope resolver's SELECT returns no row, so the dependency 404s
  BEFORE the handler - distinguished from the handler's own 404 by its detail
  string ("Resource not found" vs "Pipeline not found"),
* member operator -> the request REACHES THE HANDLER (200),
* org admin -> same (admin bypass, RLS parity).

FAR-1515 CRITICAL 1 adds the WRITE-side counterpart on the same fixtures: a
NON-ADMIN Team-A member binding Team-B's team-private connector through
``PATCH /{id}/graph`` gets 409 ``connector_team_mismatch`` — proved against
REAL RLS with an unmocked lookup, so it fails (some other status, and never
the connector's name in the detail) without the team-blind candidate read.
"""

from __future__ import annotations

import json
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
    *,
    version: int = 1,
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
                "VALUES (:id, :pid, :oid, :ver, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id), "ver": version},
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


# ---------------------------------------------------------------------------
# FAR-1360: the {pipeline_id} path segment must scope the snapshot read
# ---------------------------------------------------------------------------


async def _seed_two_pipelines_one_team(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    label: str,
) -> dict[str, object]:
    """TWO team-private pipelines in ONE team, two snapshots each.

    Both pipelines share the team, so the caller passes both team gates: the
    thing under test here is the snapshot -> pipeline BINDING, not team
    visibility. Returns ``pipeline_a`` / ``pipeline_b``, ``snaps_a`` /
    ``snaps_b`` (two each, so the diff endpoint has a real pair), and the
    member / outsider accounts.
    """
    member = await _seed_operator_account(db_engine, org_id, f"{label}-member")
    outsider = await _seed_operator_account(db_engine, org_id, f"{label}-outsider")
    team_id = await _seed_team(db_engine, org_id, owner_id, member_id=member, label=label)
    pipeline_a = await _insert_team_private_pipeline(db_engine, org_id, owner_id, team_id)
    pipeline_b = await _insert_team_private_pipeline(db_engine, org_id, owner_id, team_id)
    snaps_a = [
        await _insert_snapshot(db_engine, org_id, pipeline_a),
        await _insert_snapshot(db_engine, org_id, pipeline_a, version=2),
    ]
    snaps_b = [
        await _insert_snapshot(db_engine, org_id, pipeline_b),
        await _insert_snapshot(db_engine, org_id, pipeline_b, version=2),
    ]
    return {
        "pipeline_a": pipeline_a,
        "pipeline_b": pipeline_b,
        "snaps_a": snaps_a,
        "snaps_b": snaps_b,
        "member": member,
        "outsider": outsider,
    }


async def _read_tag(db_engine: AsyncEngine, snapshot_id: uuid.UUID) -> object:
    async with db_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT tag FROM pipeline_snapshots WHERE id = :id"), {"id": str(snapshot_id)})
        ).scalar_one_or_none()


async def test_tag_snapshot_through_a_foreign_pipeline_path_is_404(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1360: a snapshot of pipeline B addressed via pipeline A's path 404s.

    The caller is a MEMBER of both pipelines' team, so both team gates pass -
    the 404 comes from the snapshot -> pipeline binding alone, and the tag is
    never written.
    """
    seed = await _seed_two_pipelines_one_team(db_engine, test_org, test_user, label="tag-foreign")
    pipeline_a = seed["pipeline_a"]
    snap_b = seed["snaps_b"][0]
    member = seed["member"]
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_a}/snapshots/{snap_b}",
            json={"tag": "prod"},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Snapshot not found"
        assert await _read_tag(db_engine, snap_b) is None, "the denied request wrote a tag"
    finally:
        await _cleanup(db_engine, pipeline_a)
        await _cleanup(db_engine, seed["pipeline_b"])


async def test_diff_through_a_foreign_pipeline_path_is_404(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1360: the diff endpoint scoped BOTH ids to the path pipeline."""
    seed = await _seed_two_pipelines_one_team(db_engine, test_org, test_user, label="diff-foreign")
    pipeline_a = seed["pipeline_a"]
    snaps_b = seed["snaps_b"]
    member = seed["member"]
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_a}/snapshots/diff",
            json={"snapshot_a_id": str(snaps_b[0]), "snapshot_b_id": str(snaps_b[1])},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["detail"].lower()
    finally:
        await _cleanup(db_engine, pipeline_a)
        await _cleanup(db_engine, seed["pipeline_b"])


async def test_diff_in_path_snapshots_succeeds_for_a_member(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The scoping does not break the in-path case (the real diff runs)."""
    seed = await _seed_two_pipelines_one_team(db_engine, test_org, test_user, label="diff-member")
    pipeline_a = seed["pipeline_a"]
    snaps_a = seed["snaps_a"]
    member = seed["member"]
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_a}/snapshots/diff",
            json={"snapshot_a_id": str(snaps_a[0]), "snapshot_b_id": str(snaps_a[1])},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["snapshot_a"]["id"] == str(snaps_a[0])
    finally:
        await _cleanup(db_engine, pipeline_a)
        await _cleanup(db_engine, seed["pipeline_b"])


async def test_diff_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1362's request-time layer on diff, observed on real Postgres."""
    pipeline_id, _snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/snapshots/diff",
            json={"snapshot_a_id": str(uuid.uuid4()), "snapshot_b_id": str(uuid.uuid4())},
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


# ---------------------------------------------------------------------------
# FAR-1362: archive / unarchive / restore gain the request-time dependency
# ---------------------------------------------------------------------------


async def _read_archived_at(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> object:
    async with db_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT archived_at FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        ).scalar_one_or_none()


async def _read_deleted_at(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> object:
    async with db_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT deleted_at FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        ).scalar_one_or_none()


async def _soft_delete(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipelines SET deleted_at = now() WHERE id = :id"),
            {"id": str(pipeline_id)},
        )


async def test_archive_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, _snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/archive",
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
        assert await _read_archived_at(db_engine, pipeline_id) is None, "the denied request archived the row"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_archive_member_succeeds(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, _snapshot_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/archive",
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_at"] is not None
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_unarchive_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, _snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/unarchive",
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_unarchive_member_succeeds(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    pipeline_id, _snapshot_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        archived = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/archive",
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert archived.status_code == 200, archived.text

        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/unarchive",
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_at"] is None
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_restore_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The deleted-inclusive resolver still 404s a non-member's row (RLS hides it)."""
    pipeline_id, _snapshot_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        await _soft_delete(db_engine, pipeline_id)
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/restore",
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
        assert await _read_deleted_at(db_engine, pipeline_id) is not None, "the denied request restored the row"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_restore_member_succeeds_on_a_deleted_row(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The whole reason restore cannot reuse the stock resolver.

    ``resolve_pipeline_team_scope`` filters ``deleted_at IS NULL``, so wiring the
    stock dependency to restore would 404 EVERY non-admin restore before the
    handler. This member (non-admin) restore must reach the handler and clear
    ``deleted_at``.
    """
    pipeline_id, _snapshot_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        await _soft_delete(db_engine, pipeline_id)
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/restore",
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        assert await _read_deleted_at(db_engine, pipeline_id) is None, "the restore did not clear deleted_at"
    finally:
        await _cleanup(db_engine, pipeline_id)


# ---------------------------------------------------------------------------
# FAR-1515 CRITICAL 1: the RLS-hidden candidate read, on REAL Postgres.
#
# ``rls_team_isolation`` (migration 0124) hides Team-B's team-private
# connector from a Team-A-only member, so the connector-team gate's candidate
# SELECT used to return NOTHING for it: the mismatch loop skipped the row
# silently and PATCH /graph accepted a binding whose every run would use
# Team-B's credentials. The candidate rows are now read team-blind but
# org-scoped, so the predicate SEES the hidden row - and the detail names the
# connector, which only a successful read can do.
#
# The lookup is deliberately NOT mocked: this is the proof the unit doubles
# cannot give (real policy, real session context, non-admin caller).
# ---------------------------------------------------------------------------


async def _seed_team_private_connector(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    owner_team_id: uuid.UUID,
    *,
    label: str,
) -> tuple[uuid.UUID, str]:
    """A TEAM-PRIVATE connector owned by ``owner_team_id`` (committed).

    Seeded with the execution-context escape hatch (the same one the
    background machinery uses) so the team-visibility RLS ``WITH CHECK`` lets
    the row in - the CALLER under test never gets that context.
    """
    from modulo.db.crud.connector_instance import create_connector_instance

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await session.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        name = f"parity-teamb-{label}-{uuid.uuid4().hex[:6]}"
        connector = await create_connector_instance(
            session,
            org_id=org_id,
            name=name,
            connector_type_id="github",
            account_id=account_id,
            credentials_ciphertext=b"ciphertext",
            visibility="team",
            owner_team_id=owner_team_id,
        )
        return connector.id, name


async def _cleanup_connector(db_engine: AsyncEngine, org_id: uuid.UUID, connector_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        await conn.execute(text("DELETE FROM connector_instances WHERE id = :id"), {"id": str(connector_id)})


async def test_team_a_member_binding_team_b_connector_is_rejected_409(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A non-admin Team-A member binding Team-B's team-private connector -> 409.

    FAILS without the team-blind candidate read: the gate's SELECT runs under
    the member's own RLS context, Team-B's row is hidden from it, and the old
    code skipped the "absent" binding - the request then failed LATER (the
    unresolved agent id 422s in ``_resolve_graph_references``) or succeeded,
    but never with this named 409 and never with the connector's NAME in the
    detail (a hidden row has no name to report).
    """
    member = await _seed_operator_account(db_engine, test_org, "teambinding")
    team_a = await _seed_team(db_engine, test_org, test_user, member_id=member, label="a")
    team_b = await _seed_team(db_engine, test_org, test_user, member_id=None, label="b")
    pipeline_id = await _insert_team_private_pipeline(db_engine, test_org, test_user, team_a)
    connector_id, connector_name = await _seed_team_private_connector(
        db_engine, test_org, test_user, team_b, label="gate"
    )
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(uuid.uuid4()),
        "position": {"x": 0, "y": 0},
        "connector_binding": {"type": "github", "instance_id": str(connector_id)},
    }
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/graph",
            json={"nodes": [node], "edges": []},
            headers=_auth_headers(test_org, member, role="operator"),
            timeout=30.0,
        )
        assert resp.status_code == 409, resp.text
        detail = str(resp.json()["detail"])
        assert detail.startswith("connector_team_mismatch"), detail
        # The team-blind READ found the hidden row: the detail can only name a
        # row that was actually returned (the unresolvable-id refusal says
        # "does not resolve" and carries no name).
        assert connector_name in detail, detail
        assert "is team-private" in detail, detail
        assert "does not resolve" not in detail, detail

        # The gate fires BEFORE the write: the pipeline's stored graph is
        # still the seeded empty graph.
        async with db_engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(test_org)})
            await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
            raw = (
                await conn.execute(
                    text("SELECT graph_nodes_json FROM pipelines WHERE id = :id"),
                    {"id": str(pipeline_id)},
                )
            ).scalar_one()
        nodes = json.loads(raw) if isinstance(raw, str) else raw
        assert not nodes, f"the rejected binding must not have been persisted: {nodes}"
    finally:
        await _cleanup(db_engine, pipeline_id)
        await _cleanup_connector(db_engine, test_org, connector_id)
