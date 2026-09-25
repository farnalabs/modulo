"""FAR-1222: the ceiling/default merge must validate against the LOCKED row.

``update_pipeline_endpoint`` merges ``default_autonomy_level`` and
``max_autonomy_level`` (a PATCH may set only one of the pair) and rejects
``ceiling < default``. Before FAR-1222 the merge read the row with an
UNLOCKED SELECT, then the in-txn team gate re-read it ``FOR UPDATE`` — two
concurrent PATCHes could each validate against their own stale snapshot and
commit an inverted pair.

FAR-1222 moved the merge onto the locked row, but SQLAlchemy's identity map
makes that fix a NO-OP unless the re-select carries
``populate_existing=True``: the endpoint's unlocked read parks the ``Pipeline``
instance in the session, and a re-select of the same row WITHOUT that option
returns the SAME instance with its ORIGINAL (pre-lock) attribute values.

These tests run against real Postgres (testcontainer) and cover the REAL path:

1. ``test_locked_reselect_refreshes_the_identity_mapped_row`` drives the two
   reads in ONE session — the unlocked read, a concurrent committed change to
   ``default_autonomy_level``, then the helper's ``FOR UPDATE`` re-select — and
   asserts the helper hands back the FRESH value. The control half proves the
   same re-select WITHOUT ``populate_existing`` keeps the stale value, so the
   assertion fails if the execution option is ever dropped.
2. ``test_patch_rejects_ceiling_made_inverted_by_a_concurrent_commit`` drives
   the actual endpoint: a hook fires immediately after the endpoint's unlocked
   read and commits a raise of the default in a concurrent transaction (the
   exact TOCTOU window), then asserts the PATCH is rejected with 422 and the
   row is left untouched. Without the refresh the endpoint validates against
   the stale default and answers 200.
3. ``test_patch_to_the_pre_change_owner_is_blocked_for_a_non_member`` covers
   the AUTHZ half of the same refresh. ``_assert_team_transition_allowed``
   compares the payload's target team against ``current.owner_team_id``, and
   the endpoint computes ``ownership_changed`` from the same attribute - both
   read POST-lock values ONLY because the helper's ``populate_existing=True``
   refreshes the identity-mapped instance. The test flips the pipeline's
   ``owner_team_id`` in the unlocked-read -> lock window and PATCHes it back
   to the pre-change owner from a NON-member: the refreshed row makes that a
   reassignment (403), while the stale row makes it look like a no-op (200).
4. ``test_patch_reports_rebind_compared_against_the_post_lock_owner`` is the
   succeeding sibling: a caller who IS a member of both teams gets 200 with
   ``connector_rebind_required`` true, which only holds when
   ``ownership_changed`` compared the payload against the POST-lock owner.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes import pipelines as pipelines_route
from modulo.auth.jwt import TenantPrincipal
from modulo.core.run_context.autonomy import validate_autonomy_ceiling
from modulo.db.models.pipeline import Pipeline

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_MANUAL = "manual_approval"
_FULL = "fully_autonomous"
_CEILING_BELOW_FULL = "notify_on_complete"

_LEVEL_AND_CEILING_SQL = "SELECT default_autonomy_level, max_autonomy_level FROM pipelines WHERE id = :id"
_SCOPE_SQL = "SELECT visibility, owner_team_id FROM pipelines WHERE id = :id"


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID, role: str = "admin") -> dict[str, str]:
    from modulo.auth.jwt import create_access_token

    token = create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role=role,
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


async def _insert_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed pipelines row (own row — never the shared fixture).

    ``pipelines.account_id`` is a NOT NULL FK to ``accounts`` — the
    session-scoped ``test_user`` fixture supplies a valid one.
    """
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, max_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, :default, NULL)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "default": _MANUAL,
                "name": f"ceiling-lock-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _set_default_committed(db_engine: AsyncEngine, pipeline_id: uuid.UUID, level: str) -> None:
    """A CONCURRENT transaction raises the default and COMMITs.

    Runs on its own connection so it is a genuine second transaction, not part
    of the session under test — exactly the interleaving the TOCTOU closes.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipelines SET default_autonomy_level = :level WHERE id = :id"),
            {"level": level, "id": str(pipeline_id)},
        )


async def _read_levels(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> tuple[str, Any]:
    async with db_engine.connect() as conn:
        row = (await conn.execute(text(_LEVEL_AND_CEILING_SQL), {"id": str(pipeline_id)})).one()
    return str(row[0]), row[1]


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
        await conn.execute(
            text("DELETE FROM pipelines WHERE id = :id"),
            {"id": str(pipeline_id)},
        )


async def test_locked_reselect_refreshes_the_identity_mapped_row(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The helper's ``FOR UPDATE`` re-select refreshes the shared instance.

    Step (a) is the REAL path: the endpoint's unlocked read parks the row in
    the session identity map, a concurrent transaction commits a raise of the
    default, and the helper's re-select must hand back the FRESH value — the
    whole FAR-1222 fix is that one refresh.

    Step (b) is the control: the identical re-select WITHOUT
    ``populate_existing`` keeps the stale pre-lock value on the SAME instance.
    That is the no-op FAR-1222 would be without the execution option, so this
    test fails if the option is ever dropped.
    """
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    principal = TenantPrincipal(
        username="integration-test",
        organisation_id=test_org,
        account_id=test_user,
        org_role="admin",
    )
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
    try:
        # (a) Real helper, real session — refreshed by populate_existing.
        async with factory() as session, session.begin():
            unlocked = await pipelines_route._get_pipeline_or_404(session, pipeline_id)
            assert unlocked.default_autonomy_level == _MANUAL

            await _set_default_committed(db_engine, pipeline_id, _FULL)

            locked = await pipelines_route._reapply_team_gate_inside_mutation_txn(
                session,
                principal,
                pipeline_id,
            )
            # Identity map hands back the SAME Python object for the row...
            assert locked is unlocked
            # ...whose attributes must now reflect the LOCKED row, not the
            # pre-lock snapshot the unlocked read captured.
            assert locked.default_autonomy_level == _FULL, (
                "the FOR UPDATE re-select returned the stale identity-mapped values"
            )
            # The endpoint's ceiling merge now validates against the fresh
            # default and must reject the lower ceiling.
            with pytest.raises(ValueError, match="must be >="):
                validate_autonomy_ceiling(
                    locked.default_autonomy_level,
                    _CEILING_BELOW_FULL,
                    lenient=True,
                )

        # (b) Control — same sequence, plain re-select (no populate_existing).
        await _set_default_committed(db_engine, pipeline_id, _MANUAL)
        async with factory() as session, session.begin():
            first = await pipelines_route._get_pipeline_or_404(session, pipeline_id)
            assert first.default_autonomy_level == _MANUAL

            await _set_default_committed(db_engine, pipeline_id, _FULL)

            result = await session.execute(
                select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update(),
            )
            stale = result.scalar_one()
            assert stale is first
            # WITHOUT populate_existing the freshly-fetched row is discarded
            # and the pre-lock values remain — the identity-map trap.
            assert stale.default_autonomy_level == _MANUAL, (
                "expected the un-refreshed re-select to keep the stale value "
                "(the control would no longer discriminate the fix)"
            )
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_patch_rejects_ceiling_made_inverted_by_a_concurrent_commit(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The real PATCH endpoint rejects a ceiling the locked row makes inverted.

    A hook fires immediately AFTER the endpoint's unlocked read and commits a
    raise of ``default_autonomy_level`` in a concurrent transaction — the exact
    window FAR-1222 closes. The endpoint must then validate against the LOCKED
    row and answer 422; validating against the stale unlocked read would answer
    200 and store ceiling < default.
    """
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    original = pipelines_route._get_pipeline_or_404
    raced = False

    async def _unlocked_read_then_race(session: AsyncSession, pid: uuid.UUID) -> Pipeline:
        nonlocal raced
        row = await original(session, pid)
        if not raced:
            raced = True
            await _set_default_committed(db_engine, pid, _FULL)
        return row

    try:
        with patch.object(pipelines_route, "_get_pipeline_or_404", new=_unlocked_read_then_race):
            resp = await integration_client.patch(
                f"/api/v1/pipelines/{pipeline_id}",
                json={"max_autonomy_level": _CEILING_BELOW_FULL},
                headers=_auth_headers(test_org, test_user),
            )

        assert raced, "the endpoint never performed its unlocked read"
        assert resp.status_code == 422, resp.text
        assert "must be >=" in resp.json()["detail"]

        # The rejected PATCH must not have been applied: the concurrent raise
        # survives, the ceiling the client tried to set does not.
        default_level, ceiling = await _read_levels(db_engine, pipeline_id)
        assert default_level == _FULL
        assert ceiling is None
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_patch_accepts_a_ceiling_that_the_locked_row_still_allows(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Control for the 422 above: with no concurrent change the same PATCH lands.

    Guards against the 422 being produced by something other than the ceiling
    check (a broken hook, an auth failure, a schema rejection).
    """
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    try:
        resp = await integration_client.patch(
            f"/api/v1/pipelines/{pipeline_id}",
            json={"max_autonomy_level": _CEILING_BELOW_FULL},
            headers=_auth_headers(test_org, test_user),
        )
        assert resp.status_code == 200, resp.text

        default_level, ceiling = await _read_levels(db_engine, pipeline_id)
        assert default_level == _MANUAL
        assert ceiling == _CEILING_BELOW_FULL
    finally:
        await _cleanup(db_engine, pipeline_id)


# ---------------------------------------------------------------------------
# FAR-1222 authz half: the SAME refresh must drive the team-transition gate
# ---------------------------------------------------------------------------


async def _seed_operator_account(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    """A NON-admin ``operator`` account in ``org_id`` (committed).

    The shared ``test_user`` fixture holds the ``admin`` org role, and BOTH
    ``_reapply_team_gate_inside_mutation_txn`` and
    ``_assert_team_transition_allowed`` return early for admins - so the authz
    half needs its own principal. ``pipeline.update`` floors at ``operator``.
    """
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(account_id),
                "email": f"far1222-{account_id.hex[:12]}@example.com",
                "name": "FAR-1222 operator",
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
    """A committed team, plus an optional membership row, via the ORM CRUD layer.

    Mirrors the seeding pattern the other team-gate integration tests use
    (``create_team`` needs the org RLS GUC set on the session; the raw
    ``teams`` INSERT would have to hand-supply every NOT NULL column).
    """
    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(
            session,
            org_id=org_id,
            name=f"far1222-{label}-{uuid.uuid4().hex[:6]}",
            account_id=owner_id,
        )
        if member_id is not None:
            await add_team_member(session, org_id=org_id, team_id=team.id, account_id=member_id, role="operator")
        return team.id


async def _set_scope_committed(
    db_engine: AsyncEngine,
    pipeline_id: uuid.UUID,
    *,
    visibility: str,
    owner_team_id: uuid.UUID,
) -> None:
    """A CONCURRENT transaction re-scopes the pipeline and COMMITs.

    Same shape as ``_set_default_committed``: its own connection, so it is a
    genuine second transaction rather than part of the session under test.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipelines SET visibility = :vis, owner_team_id = :tid WHERE id = :id"),
            {"vis": visibility, "tid": str(owner_team_id), "id": str(pipeline_id)},
        )


async def _read_scope(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> tuple[str, str]:
    async with db_engine.connect() as conn:
        row = (await conn.execute(text(_SCOPE_SQL), {"id": str(pipeline_id)})).one()
    return str(row[0]), str(row[1])


async def test_patch_to_the_pre_change_owner_is_blocked_for_a_non_member(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The refreshed POST-lock row drives the new-team membership gate (403).

    ``_assert_team_transition_allowed`` only re-checks membership of the
    payload's target team when ``_is_owner_reassignment(new, current)`` says the
    payload DIFFERS from the current owner. Both ``_get_pipeline_or_404``
    (unlocked) and the helper's ``FOR UPDATE`` re-select resolve to the SAME
    identity-mapped instance, so without the helper's ``populate_existing=True``
    the gate compares against the PRE-change owner: the payload then looks like
    a no-op, the membership check is skipped, and a NON-member could hand the
    pipeline back to a team they do not belong to.

    Sequence (the same hook pattern as the ceiling race above):

    1. the row starts ``visibility='org'`` under a team the caller is NOT a
       member of, so the request-time team dependency passes (org-visible
       rows are not team-gated) while the caller still cannot join that team;
    2. the hook commits ``visibility='team', owner=<the caller's own team>``
       inside the unlocked-read -> lock window;
    3. the caller PATCHes ``owner_team_id`` back to the pre-change team.

    With the refresh the locked row is (team, caller's team): the current-team
    gate passes (member) and the new-team gate sees a reassignment to a team
    the caller does NOT belong to -> 403. Without the refresh the gate reads
    (org, pre-change team): nothing is team-private and the reassignment
    comparison is False, so the PATCH would commit -> 200.
    """
    operator = await _seed_operator_account(db_engine, test_org)
    pre_team = await _seed_team(db_engine, test_org, test_user, member_id=None, label="pre")
    locked_team = await _seed_team(db_engine, test_org, test_user, member_id=operator, label="locked")
    pipeline_id = await _insert_pipeline(db_engine, test_org, operator)
    await _set_scope_committed(db_engine, pipeline_id, visibility="org", owner_team_id=pre_team)

    original = pipelines_route._get_pipeline_or_404
    raced = False

    async def _unlocked_read_then_rescope(session: AsyncSession, pid: uuid.UUID) -> Pipeline:
        nonlocal raced
        row = await original(session, pid)
        if not raced:
            raced = True
            await _set_scope_committed(db_engine, pid, visibility="team", owner_team_id=locked_team)
        return row

    try:
        with patch.object(pipelines_route, "_get_pipeline_or_404", new=_unlocked_read_then_rescope):
            resp = await integration_client.patch(
                f"/api/v1/pipelines/{pipeline_id}",
                json={"owner_team_id": str(pre_team)},
                headers=_auth_headers(test_org, operator, role="operator"),
            )

        assert raced, "the endpoint never performed its unlocked read"
        assert resp.status_code == 403, resp.text
        assert "Cannot reassign a pipeline to a team you are not a member of" in resp.json()["detail"]

        # The rejected PATCH left the concurrent flip intact.
        visibility, owner = await _read_scope(db_engine, pipeline_id)
        assert visibility == "team"
        assert owner == str(locked_team)
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_patch_reports_rebind_compared_against_the_post_lock_owner(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """``ownership_changed`` (-> ``connector_rebind_required``) reads the POST-lock owner.

    Same race as the 403 above, but the caller is a member of BOTH teams, so
    the refreshed gate lets the PATCH through. The payload restores the
    pre-change owner, which the PRE-lock row already held: comparing against
    the stale value would report ``ownership_changed = False``, while the
    refreshed comparison (pre-change team != the team the lock just read)
    reports True. A regression that drops ``populate_existing=True`` therefore
    flips this response field to false.
    """
    operator = await _seed_operator_account(db_engine, test_org)
    pre_team = await _seed_team(db_engine, test_org, test_user, member_id=operator, label="pre")
    locked_team = await _seed_team(db_engine, test_org, test_user, member_id=operator, label="locked")
    pipeline_id = await _insert_pipeline(db_engine, test_org, operator)
    await _set_scope_committed(db_engine, pipeline_id, visibility="org", owner_team_id=pre_team)

    original = pipelines_route._get_pipeline_or_404
    raced = False

    async def _unlocked_read_then_rescope(session: AsyncSession, pid: uuid.UUID) -> Pipeline:
        nonlocal raced
        row = await original(session, pid)
        if not raced:
            raced = True
            await _set_scope_committed(db_engine, pid, visibility="team", owner_team_id=locked_team)
        return row

    try:
        with patch.object(pipelines_route, "_get_pipeline_or_404", new=_unlocked_read_then_rescope):
            resp = await integration_client.patch(
                f"/api/v1/pipelines/{pipeline_id}",
                json={"owner_team_id": str(pre_team)},
                headers=_auth_headers(test_org, operator, role="operator"),
            )

        assert raced, "the endpoint never performed its unlocked read"
        assert resp.status_code == 200, resp.text
        assert resp.json()["connector_rebind_required"] is True, (
            "ownership_changed was computed from the pre-lock owner, not the locked row"
        )

        visibility, owner = await _read_scope(db_engine, pipeline_id)
        assert visibility == "team"
        assert owner == str(pre_team)
    finally:
        await _cleanup(db_engine, pipeline_id)
