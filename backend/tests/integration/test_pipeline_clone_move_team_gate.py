"""FAR-1276: the team gate on clone / move-to-folder against real Postgres.

``POST /{id}/clone`` and ``PATCH /{id}/folder`` carried neither layer of the
two-layer team gate their sibling mutations (update / delete / replace-graph /
convert-to-agent / revert-to-manual) carry: the request-time
``require_team_membership_or_admin(resolve_pipeline_team_scope)`` dependency and
the in-txn ``_reapply_team_gate_inside_mutation_txn`` re-check. (``archive`` /
``unarchive`` / ``restore`` still carry only the in-txn layer.)

That is a parity gap, not a live Postgres hole: migration 0124 leaves
``rls_team_isolation`` as the sole policy on ``pipelines``, so a non-member's
row is already invisible and the dependency's resolver 404s BEFORE the handler.
These tests prove that end to end - they are the evidence for the rationale,
and the reason the unit tests (which cover the dependency's own 403 branch and
the in-txn re-check against a session double) are not the whole story.

The member/admin cases do more than assert a status: CLONE is the one endpoint
whose in-txn gate could not simply be taken first. It rides in on
``clone_pipeline``'s ``_on_step_a_committed`` hook (after step (a) has
committed its separate-connection ``FOR SHARE`` read of the source row, before
the copy write), and a 201 here is what proves that placement actually works
against a real database rather than deadlocking or 409ing on every clone.

Runs against the migrated testcontainer with the real auth stack:

* non-member operator -> DENIED as 404 "Resource not found" (RLS-parity: the
  row is invisible, so the team-scope resolver returns no row and the
  dependency 404s BEFORE the membership check),
* member operator -> the request REACHES THE HANDLER and succeeds,
* org admin -> same (admin bypass, RLS parity).

The two 404/2xx outcomes are distinguished by their detail: "Resource not
found" (dependency, row invisible) vs the handler's own response. A member
reading the dependency's detail would mean the gate over-blocks.
"""

from __future__ import annotations

import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
#: Seconds added to ``mutation_row_lock_timeout_ms`` for the HTTP client's own
#: timeout, so the client never gives up before the SERVER answers at any
#: permitted setting (100 ms..30 s).
_CLIENT_MARGIN_SECONDS = 20.0
#: Seconds added to the expected wait for the "the wait was bounded" ceiling -
#: tight enough that an UNBOUNDED wait still fails, loose enough for overhead.
_BOUNDED_MARGIN_SECONDS = 10.0


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
    ``pipeline.create`` / ``pipeline.update`` floor at ``operator``.
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
                "name": f"clone-move {label}",
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
            name=f"clone-move-{label}-{uuid.uuid4().hex[:6]}",
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
                "name": f"clone-move-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _cleanup_pipelines(db_engine: AsyncEngine, *pipeline_ids: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        for pipeline_id in pipeline_ids:
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
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns ``(pipeline_id, member_id, non_member_id)``."""
    member = await _seed_operator_account(db_engine, test_org, "member")
    outsider = await _seed_operator_account(db_engine, test_org, "outsider")
    team_id = await _seed_team(db_engine, test_org, test_user, member_id=member, label="gate")
    pipeline_id = await _insert_team_private_pipeline(db_engine, test_org, test_user, team_id)
    return pipeline_id, member, outsider


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


async def test_clone_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Denied end-to-end: the source row is RLS-invisible, so the dependency 404s."""
    pipeline_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/clone",
            json={},
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup_pipelines(db_engine, pipeline_id)


async def test_clone_member_clones_successfully(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Past BOTH gates: a member's clone lands (201), with no lock hang.

    This is also the placement check for the clone-specific in-txn gate: it
    runs on ``clone_pipeline``'s step-(a) commit hook, so the ``FOR SHARE``
    read on a separate connection and the gate's ``FOR UPDATE`` must not
    collide. A collision would surface here as a hang or a 409 rather than a
    201, so the bounded client timeout is the assertion's clock.
    """
    pipeline_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    cloned_id: uuid.UUID | None = None
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/clone",
            json={},
            headers=_auth_headers(test_org, member, role="operator"),
            timeout=30.0,
        )
        assert resp.status_code == 201, resp.text
        cloned_id = uuid.UUID(resp.json()["id"])
        assert cloned_id != pipeline_id
        # The clone inherits the source's team - the target-side check in
        # _clone_pipeline_into_org must see it as the SAME team, not a foreign one.
        body = resp.json()
        assert body["visibility"] == "team"
        assert body["owner_team_id"] is not None
    finally:
        await _cleanup_pipelines(db_engine, *((cloned_id,) if cloned_id else ()) + (pipeline_id,))


async def test_clone_org_admin_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    pipeline_id, _member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    cloned_id: uuid.UUID | None = None
    try:
        resp = await integration_client.post(
            f"/api/v1/pipelines/{pipeline_id}/clone",
            json={},
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code == 201, resp.text
        cloned_id = uuid.UUID(resp.json()["id"])
    finally:
        await _cleanup_pipelines(db_engine, *((cloned_id,) if cloned_id else ()) + (pipeline_id,))


async def test_contended_clone_degrades_to_a_bounded_409(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A held source-row lock must make the clone DEGRADE, never park unboundedly.

    The clone's step (a) runs on a SEPARATE connection (the torn-read fix) and
    takes ``FOR SHARE`` on the source row. Until the read session got its own
    transaction-scoped ``lock_timeout`` that wait was UNBOUNDED: a clone
    overlapping a graph save (or a second clone of the same source) would park
    its read connection - and the caller's main mutation transaction with it -
    until the holder committed, with no degradation to the 409 the rest of the
    mutation path produces.

    Deterministic here: the holder takes ``FOR UPDATE`` on the source row and
    NEVER releases it before the request returns, so the only thing that can
    end the step-(a) ``FOR SHARE`` wait is the bounded ``lock_timeout``
    (``Settings.mutation_row_lock_timeout_ms``, applied transaction-scoped
    before the select). Both the client timeout and the ceiling derive from the
    setting so the test stays valid across its whole permitted range.
    """
    pipeline_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    expected_seconds = get_settings().mutation_row_lock_timeout_ms / 1000
    client_timeout = expected_seconds + _CLIENT_MARGIN_SECONDS
    bounded_ceiling = expected_seconds + _BOUNDED_MARGIN_SECONDS
    cloned_id: uuid.UUID | None = None
    try:
        # The holder is the container superuser (RLS bypassed), so it really
        # does lock the row; the app's connections run as the non-superuser
        # role and wait on that same row-level lock.
        async with db_engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM pipelines WHERE id = :id FOR UPDATE"),
                {"id": str(pipeline_id)},
            )

            started = time.monotonic()
            resp = await integration_client.post(
                f"/api/v1/pipelines/{pipeline_id}/clone",
                json={},
                headers=_auth_headers(test_org, member, role="operator"),
                timeout=client_timeout,
            )
            elapsed = time.monotonic() - started
            await holder.rollback()

        if resp.status_code == 201:
            # Only reachable if the bound did NOT fire - captured so the
            # failure path still cleans the clone up.
            cloned_id = uuid.UUID(resp.json()["id"])
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "Timed out waiting for a lock" in detail, detail
        # The wait really happened (it did not resolve instantly)...
        assert elapsed >= expected_seconds * 0.5, (
            f"the clone returned after {elapsed:.2f}s - before the {expected_seconds}s lock_timeout could fire"
        )
        # ...and it was really BOUNDED (the holder never released the lock).
        assert elapsed < bounded_ceiling, f"the clone did NOT degrade - {elapsed:.2f}s for a held source-row lock"
    finally:
        ids = [pipeline_id] + ([cloned_id] if cloned_id is not None else [])
        await _cleanup_pipelines(db_engine, *ids)


# ---------------------------------------------------------------------------
# Move to folder
# ---------------------------------------------------------------------------


async def test_move_non_member_never_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Sibling denial path for ``PATCH /{id}/folder`` (same dependency, same 404)."""
    pipeline_id, _member, outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/folder",
            json={"folder_id": None},
            headers=_auth_headers(test_org, outsider, role="operator"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Resource not found"
    finally:
        await _cleanup_pipelines(db_engine, pipeline_id)


async def test_move_member_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Past BOTH gates: a member's move completes (200) and sticks."""
    pipeline_id, member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/folder",
            json={"folder_id": None},
            headers=_auth_headers(test_org, member, role="operator"),
            timeout=30.0,
        )
        assert resp.status_code == 200, resp.text
    finally:
        await _cleanup_pipelines(db_engine, pipeline_id)


async def test_move_org_admin_reaches_the_handler(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    pipeline_id, _member, _outsider = await _seed_scenario(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}/folder",
            json={"folder_id": None},
            headers=_auth_headers(test_org, test_user, role="admin"),
            timeout=30.0,
        )
        assert resp.status_code == 200, resp.text
    finally:
        await _cleanup_pipelines(db_engine, pipeline_id)
