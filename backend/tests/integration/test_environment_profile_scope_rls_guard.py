"""FAR-1558 M1: the profile scope-change guard must read team-BLIND — real Postgres.

``PUT /api/v1/environment-profiles/{id}`` re-scopes a profile only after
``_assert_scope_change_keeps_bindings_eligible`` proves no bound pipeline would
be stranded. The route gate is ``environment_profile.update`` at min org role
``operator``, so a perfectly ordinary caller is a member of ONE team — and under
``rls_team_isolation`` (migration 0124) their own session context cannot see
another team's ``visibility='team'`` pipeline. A guard that scanned bound
pipelines in the caller's context would therefore read ``[]`` for exactly the
row that matters, pass vacuously, and let a team-private re-scope strand a
foreign-team binding. That is the FAR-1515 CRITICAL-1 defect class on a new
surface, and it is invisible to the mocked unit session (no RLS policy there).

This is the proof the unit tests cannot give — real policy, real session
context, non-admin caller:

* **the blindness is real** — the seeded binding EXISTS (superuser read) and is
  INVISIBLE to the caller's own RLS context (app-role read), asserted first, so
  a passing guard below cannot be explained by "there was nothing to find";
* **the change is refused** — 422 ``environment_profile_binding_team_mismatch``,
  with the stored visibility untouched (the refusal precedes the write);
* **the mirror direction still passes** — a caller who IS a member of the
  binding's team gets 200, so the team-blind scan does not vacuously block
  every re-scope.

Runs against the migrated testcontainer with the real auth stack, exactly like
``test_pipeline_team_gate_parity.py``.
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


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID, role: str = "operator") -> dict[str, str]:
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

    The shared ``test_user`` holds ``admin``, which both the org-role bypass in
    the RLS policy and the route's team gates neutralise — the guard under test
    needs a caller whose team visibility is genuinely restricted.
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
                "name": f"env-scope-guard {label}",
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
            name=f"scope-{label}-{uuid.uuid4().hex[:6]}",
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
    """A committed TEAM-PRIVATE pipeline with an EMPTY graph (RLS-hidden from other teams)."""
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
                "name": f"scope-bound-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _seed_org_visible_profile(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """A committed ORG-VISIBLE environment profile (a caller of any team may re-scope it)."""
    from modulo.db.crud.environment_profile import create_environment_profile

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        profile = await create_environment_profile(
            session,
            org_id=org_id,
            name=f"scope-guard-{uuid.uuid4().hex[:6]}",
            account_id=account_id,
            provider_type="local_docker",
            image_ref="python:3.12-slim",
            visibility="org",
        )
        return profile.id


async def _bind_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> None:
    """Point the pipeline's binding at the profile (commits; same-org tenant trigger applies)."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipelines SET environment_profile_id = :pid WHERE id = :id AND organisation_id = :oid"),
            {"pid": str(profile_id), "id": str(pipeline_id), "oid": str(org_id)},
        )


async def _pipeline_exists(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> bool:
    """Superuser read — the row demonstrably exists, RLS aside."""
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT id FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        ).scalar_one_or_none()
    return row is not None


async def _caller_can_see_pipeline(
    app_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> bool:
    """Read the pipeline under the CALLER'S own RLS context (app role, non-admin).

    This is the read the guard would have made before M1: if it returns True
    here the blindness premise is false and the test proves nothing, so it is
    asserted first.
    """
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await conn.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(account_id)})
        await conn.execute(text("SELECT set_config('app.org_role', 'operator', true)"))
        row = (
            await conn.execute(text("SELECT id FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        ).scalar_one_or_none()
    return row is not None


async def _read_profile_visibility(db_engine: AsyncEngine, profile_id: uuid.UUID) -> str:
    async with db_engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT visibility FROM environment_profiles WHERE id = :id"), {"id": str(profile_id)}
                )
            ).scalar_one()
        )


async def _cleanup(
    db_engine: AsyncEngine,
    pipeline_id: uuid.UUID | None,
    profile_id: uuid.UUID,
) -> None:
    async with db_engine.begin() as conn:
        if pipeline_id is not None:
            await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        # The FK is ON DELETE SET NULL, so this clears even a surviving reference.
        await conn.execute(text("DELETE FROM environment_profiles WHERE id = :id"), {"id": str(profile_id)})


async def test_plain_field_update_succeeds_without_consulting_bindings(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Control: a rename PUT (NO scope change) must never reach the guard.

    Pins two things at once: the guard's early return (no ``visibility`` /
    ``owner_team_id`` in the payload means no bound-pipelines query at all),
    and the happy path of the route itself — the response is built AFTER the
    mutation transaction commits, so the row's server-computed ``updated_at``
    must have been refreshed in-transaction or the 200 becomes a 500.
    """
    profile_id = await _seed_org_visible_profile(db_engine, test_org, test_user)
    renamed = f"renamed-{uuid.uuid4().hex[:6]}"
    try:
        resp = await integration_client.put(
            f"/api/v1/environment-profiles/{profile_id}",
            json={"name": renamed},
            headers=_auth_headers(test_org, test_user, role="admin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == renamed

        # The sibling write path (create) builds its response the same way, so
        # pin it too: a 500 here would mean the in-transaction refresh is
        # missing on CREATE as well.
        created = await integration_client.post(
            "/api/v1/environment-profiles",
            json={
                "name": f"created-{uuid.uuid4().hex[:6]}",
                "provider_type": "local_docker",
                "image_ref": "python:3.12-slim",
            },
            headers=_auth_headers(test_org, test_user, role="admin"),
        )
        assert created.status_code == 201, created.text
        created_id = uuid.UUID(str(created.json()["id"]))
        await _cleanup(db_engine, None, created_id)
    finally:
        await _cleanup(db_engine, None, profile_id)


async def test_scope_change_stranding_a_foreign_team_binding_is_refused(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A Team-A operator re-scoping an org profile to Team A must be REFUSED.

    The bound pipeline belongs to Team B, so re-scoping would strand it. The
    caller's own RLS context cannot see that pipeline — asserted explicitly
    below — so the refusal can only come from the team-blind scan.
    """
    member = await _seed_operator_account(db_engine, test_org, "team-a")
    team_a = await _seed_team(db_engine, test_org, test_user, member_id=member, label="a")
    team_b = await _seed_team(db_engine, test_org, test_user, member_id=None, label="b")
    pipeline_id = await _insert_team_private_pipeline(db_engine, test_org, test_user, team_b)
    profile_id = await _seed_org_visible_profile(db_engine, test_org, test_user)
    await _bind_pipeline(db_engine, test_org, pipeline_id, profile_id)
    try:
        # (1) The bound pipeline EXISTS...
        assert await _pipeline_exists(db_engine, pipeline_id)
        # ...and (2) is INVISIBLE to this caller's own RLS context. Without
        # this, a guard reading in the caller's context would also "work" by
        # finding nothing to refuse.
        assert not await _caller_can_see_pipeline(app_engine, test_org, member, pipeline_id)

        # (3) The re-scope that would strand Team B's binding is refused.
        resp = await integration_client.put(
            f"/api/v1/environment-profiles/{profile_id}",
            json={"visibility": "team", "owner_team_id": str(team_a)},
            headers=_auth_headers(test_org, member, role="operator"),
        )
        assert resp.status_code == 422, resp.text
        detail = str(resp.json()["detail"])
        assert detail.startswith("environment_profile_binding_team_mismatch"), detail

        # (4) Fail closed BEFORE the write: the stored scope is untouched.
        assert await _read_profile_visibility(db_engine, profile_id) == "org"
    finally:
        await _cleanup(db_engine, pipeline_id, profile_id)


async def test_scope_change_matching_the_binding_team_is_allowed(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The mirror: a member of the BINDING's team may re-scope to that team.

    Proves the team-blind scan does not vacuously block every re-scope — the
    widened read finds the same pipeline and the shared predicate judges it
    eligible.
    """
    member_b = await _seed_operator_account(db_engine, test_org, "team-b")
    team_b = await _seed_team(db_engine, test_org, test_user, member_id=member_b, label="match")
    other_team = await _seed_team(db_engine, test_org, test_user, member_id=None, label="other")
    pipeline_id = await _insert_team_private_pipeline(db_engine, test_org, test_user, team_b)
    profile_id = await _seed_org_visible_profile(db_engine, test_org, test_user)
    await _bind_pipeline(db_engine, test_org, pipeline_id, profile_id)
    try:
        # The caller CAN see their own team's pipeline (control on the control).
        assert await _caller_can_see_pipeline(app_engine, test_org, member_b, pipeline_id)

        resp = await integration_client.put(
            f"/api/v1/environment-profiles/{profile_id}",
            json={"visibility": "team", "owner_team_id": str(team_b)},
            headers=_auth_headers(test_org, member_b, role="operator"),
        )
        assert resp.status_code == 200, resp.text
        assert await _read_profile_visibility(db_engine, profile_id) == "team"

        # Cross-check: re-scoping AWAY (to a third team) now strands the
        # binding and must be refused — the same guard, the other direction.
        stray = await integration_client.put(
            f"/api/v1/environment-profiles/{profile_id}",
            json={"owner_team_id": str(other_team)},
            headers=_auth_headers(test_org, member_b, role="operator"),
        )
        assert stray.status_code == 422, stray.text
        assert str(stray.json()["detail"]).startswith("environment_profile_binding_team_mismatch"), stray.text
    finally:
        await _cleanup(db_engine, pipeline_id, profile_id)
