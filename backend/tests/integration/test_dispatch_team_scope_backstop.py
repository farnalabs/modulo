"""FAR-1598: the dispatch-time team-scope backstop — real Postgres RLS.

The unit doubles cannot prove this rule: RLS policies do not exist in a
mocked session, and ``rls_team_isolation`` (migrations 0003/0124) is exactly
what decides whether dispatch can SEE the bound profile at all. Under an
org-only session (no ``app.user_id``, no ``app.org_role``, no execution
context) a team-private ``environment_profiles`` row is INVISIBLE — so the
load read must widen to the execution context (otherwise the binding
silently resolves the DEFAULT route and no predicate could ever fire), and
the backstop must refuse the mismatched binding under the real policy.

Three assertions, in the order that makes a pass meaningful:

* **the premise is real** — an org-only session cannot see the team-private
  profile, asserted FIRST, so the refusal below cannot be explained by
  "there was nothing to find" and the widened read is demonstrably
  load-bearing;
* **the refusal fires through the PRODUCTION chain** — executor ctx seeding
  -> the node_runner seam ``_resolve_sandbox_dispatch_route_for_run`` ->
  route resolution -> shared FAR-1558 predicate — typed error, named code,
  no provider selected;
* **the mirror passes** — a same-team binding resolves the e2b route, so the
  backstop does not vacuously refuse every team-private profile.

Runs against the migrated testcontainer with the app-role engine (RLS
enforced), exactly like ``test_environment_profile_scope_rls_guard.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.bundled_runner.runner_dispatch import SandboxDispatchUnboundError
from modulo.core.pipeline_engine import node_runner

pytestmark = pytest.mark.integration


async def _seed_team(db_engine: AsyncEngine, org_id: uuid.UUID, owner_id: uuid.UUID, label: str) -> uuid.UUID:
    """A committed team via the ORM CRUD layer (superuser session)."""
    from modulo.db.crud.team import create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(
            session,
            org_id=org_id,
            name=f"dispatch-scope-{label}-{uuid.uuid4().hex[:6]}",
            account_id=owner_id,
        )
        return team.id


async def _seed_team_private_profile(
    db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID, team_id: uuid.UUID
) -> uuid.UUID:
    """A committed TEAM-PRIVATE environment profile (provider ``e2b`` so no
    hub/provider registration is involved in route resolution)."""
    from modulo.db.crud.environment_profile import create_environment_profile

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        profile = await create_environment_profile(
            session,
            org_id=org_id,
            name=f"dispatch-scope-{uuid.uuid4().hex[:6]}",
            account_id=account_id,
            provider_type="e2b",
            image_ref="python:3.12-slim",
            visibility="team",
            owner_team_id=team_id,
        )
        return profile.id


async def _insert_pipeline_with_binding(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    team_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> uuid.UUID:
    """A committed TEAM-PRIVATE pipeline whose environment-profile binding is
    set directly (the drift surface FAR-1598 backstops — the same-org tenant
    trigger still applies)."""
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, visibility, owner_team_id, "
                "environment_profile_id) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, "
                "'manual_approval', 'team', :tid, :pid)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "tid": str(team_id),
                "pid": str(profile_id),
                "name": f"dispatch-scope-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _org_only_session_sees_profile(app_engine: AsyncEngine, org_id: uuid.UUID, profile_id: uuid.UUID) -> bool:
    """Read the profile under an ORG-ONLY app-role context — the session
    shape dispatch ran with before the execution-context widening."""
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        row = (
            await conn.execute(text("SELECT id FROM environment_profiles WHERE id = :id"), {"id": str(profile_id)})
        ).scalar_one_or_none()
    return row is not None


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID, profile_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        # FK is ON DELETE SET NULL, so this clears even a surviving reference.
        await conn.execute(text("DELETE FROM environment_profiles WHERE id = :id"), {"id": str(profile_id)})


async def test_dispatch_refuses_a_cross_team_profile_under_real_rls(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A team-private profile owned by team A, bound to team B's pipeline:
    refused at dispatch with the typed error — through the production chain."""
    team_a = await _seed_team(db_engine, test_org, test_user, "a")
    team_b = await _seed_team(db_engine, test_org, test_user, "b")
    profile_id = await _seed_team_private_profile(db_engine, test_org, test_user, team_a)
    pipeline_id = await _insert_pipeline_with_binding(db_engine, test_org, test_user, team_b, profile_id)
    # The app-role session factory the executor threads to dispatch.
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    try:
        # Premise: the ORG-ONLY context cannot see the team-private row. If
        # this ever returns True the RLS premise is false and the widening
        # below is dead weight — asserted first for exactly that reason.
        assert not await _org_only_session_sees_profile(app_engine, test_org, profile_id)

        node_runner.set_conformance_ctx(factory, test_org, profile_id, pipeline_id, [], False)
        state = {"_org_id": str(test_org), "_pipeline_id": str(pipeline_id)}
        with pytest.raises(SandboxDispatchUnboundError, match="environment_profile_team_mismatch"):
            await node_runner._resolve_sandbox_dispatch_route_for_run(state, factory)
    finally:
        node_runner._conformance_ctx_cv.set(None)
        await _cleanup(db_engine, pipeline_id, profile_id)


async def test_dispatch_resolves_a_same_team_profile_under_real_rls(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The mirror: a LEGAL same-team binding resolves the route — proving the
    widened read surfaces the team-private row (org-only context cannot) and
    the predicate does not vacuously refuse every team profile."""
    team = await _seed_team(db_engine, test_org, test_user, "same")
    profile_id = await _seed_team_private_profile(db_engine, test_org, test_user, team)
    pipeline_id = await _insert_pipeline_with_binding(db_engine, test_org, test_user, team, profile_id)
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    try:
        assert not await _org_only_session_sees_profile(app_engine, test_org, profile_id)

        node_runner.set_conformance_ctx(factory, test_org, profile_id, pipeline_id, [], False)
        state = {"_org_id": str(test_org), "_pipeline_id": str(pipeline_id)}
        route = await node_runner._resolve_sandbox_dispatch_route_for_run(state, factory)
    finally:
        node_runner._conformance_ctx_cv.set(None)
        await _cleanup(db_engine, pipeline_id, profile_id)

    assert route.provider_type == "e2b"
    assert route.profile is not None
    assert route.profile.id == profile_id
