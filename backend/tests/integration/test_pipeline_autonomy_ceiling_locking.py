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

_LEVEL_SQL = "SELECT default_autonomy_level FROM pipelines WHERE id = :id"
_LEVEL_AND_CEILING_SQL = "SELECT default_autonomy_level, max_autonomy_level FROM pipelines WHERE id = :id"


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
