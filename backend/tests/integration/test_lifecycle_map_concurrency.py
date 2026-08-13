"""Integration tests for lifecycle-map concurrent-save semantics on Postgres.

Two agents saving versions of the same map concurrently must never produce
duplicate version numbers. Every version-bumping write path — POST /versions
(``save_map_version``) and the content-changing PUT /lifecycle-maps/{id}
(``update_lifecycle_map``) — fetches the map row with ``SELECT ... FOR UPDATE``
(``get_lifecycle_map_for_update``), so under Postgres READ COMMITTED the second
write blocks on the row lock, re-reads the first write's committed version and
bumps from there: the active version is last-write-wins and the counter is
strictly increasing with no duplicates. A version-list read running concurrently
with a save always observes one consistent committed snapshot — never a
half-written map.

These assertions FAIL against the pre-fix read-then-increment behaviour, where
both transactions read version 1 and the second commit silently overwrites the
first (final version 2, duplicate number). The PUT route also FAILED because it
pre-fetched the row into the session identity map before the FOR UPDATE re-read,
so the second update bumped from the stale pre-commit version.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.core.lifecycle_map.service import create_lifecycle_map, get_lifecycle_map, save_map_version
from modulo.db.rls import set_rls_org

_VALID_32 = "a" * 32

pytestmark = pytest.mark.integration


async def _create_map(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        lm = await create_lifecycle_map(
            session,
            org_id=org_id,
            name=name,
            account_id=account_id,
            visibility="org",
            version=1,
            content_json={"stages": [], "edges": [], "notes": ""},
        )
        return lm.id


async def _final_state(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    map_id: uuid.UUID,
):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        return await get_lifecycle_map(session, map_id)


class TestConcurrentVersionSaves:
    async def test_concurrent_saves_yield_strictly_increasing_unique_versions(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        map_id = await _create_map(db_engine, test_org, test_user, "Concurrent Map")

        async def _save_one(stage_id: str) -> int:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                lm = await save_map_version(
                    session,
                    map_id,
                    stages=[{"id": stage_id, "name": stage_id, "type": "manual"}],
                    edges=[],
                    notes=stage_id,
                )
                assert lm is not None
                return lm.version

        v_a, v_b = await asyncio.gather(_save_one("a"), _save_one("b"))
        assert sorted((v_a, v_b)) == [2, 3], f"concurrent saves must be unique + increasing, got {v_a}, {v_b}"

        final = await _final_state(db_engine, test_org, map_id)
        assert final is not None
        assert final.version == 3, "no save may be lost under concurrency"
        # Last-write-wins: the active content is exactly one of the two saves.
        assert final.content_json["stages"] in (
            [{"id": "a", "name": "a", "type": "manual"}],
            [{"id": "b", "name": "b", "type": "manual"}],
        )

    async def test_version_list_read_during_save_sees_committed_snapshot(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        map_id = await _create_map(db_engine, test_org, test_user, "Consistent Reads")

        new_stage = {"id": "s1", "name": "Build", "type": "manual"}

        async def _save() -> int:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                lm = await save_map_version(
                    session,
                    map_id,
                    stages=[new_stage],
                    edges=[],
                    notes="v2",
                )
                assert lm is not None
                return lm.version

        async def _read() -> int:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                lm = await get_lifecycle_map(session, map_id)
                assert lm is not None
                # Every snapshot observed must be a complete committed map — the
                # empty v1 stage list or the full single-stage v2 list, never a
                # partial mix.
                assert lm.content_json["stages"] in ([], [new_stage]), "reader saw a partial map"
                return lm.version

        save_v, read_v = await asyncio.gather(_save(), _read())
        assert save_v == 2
        assert read_v in (1, 2), f"reader must see a committed version, got {read_v}"

        final = await _final_state(db_engine, test_org, map_id)
        assert final is not None
        assert final.version == 2
        assert final.content_json["stages"] == [new_stage], "the save must not be lost"


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
    )


def _auth_headers(org_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(org_id, user_id)}"}


@pytest_asyncio.fixture
async def integration_client(db_url: str, db_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI AsyncClient bound to the migrated testcontainer database."""
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: db_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
        yield client

    app.dependency_overrides.clear()


class TestConcurrentPutUpdates:
    async def test_concurrent_put_updates_yield_strictly_increasing_unique_versions(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """Two concurrent PUT /lifecycle-maps/{id} content updates must never
        produce duplicate version numbers.

        The update route's first statement in the transaction is the service's
        ``SELECT ... FOR UPDATE`` (``get_lifecycle_map_for_update``), so under
        Postgres READ COMMITTED the later update blocks on the row lock,
        re-reads the earlier update's committed version and bumps from there.
        This test FAILS against the pre-fix route, which pre-fetched the row
        with a plain ``get_lifecycle_map`` before calling the service: that
        loaded the row into the session identity map, so the re-executed FOR
        UPDATE returned the same unrefreshed object (version still 1) and both
        updates wrote version 2.
        """
        map_id = await _create_map(db_engine, test_org, test_user, "Concurrent PUT Map")
        headers = _auth_headers(test_org, test_user)

        async def _put_one(stage_id: str) -> int:
            resp = await integration_client.put(
                f"/api/v1/lifecycle-maps/{map_id}",
                json={
                    "content_json": {
                        "stages": [{"id": stage_id, "name": stage_id, "type": "manual"}],
                        "edges": [],
                        "notes": stage_id,
                    }
                },
                headers=headers,
            )
            assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"
            return resp.json()["version"]

        v_a, v_b = await asyncio.gather(_put_one("a"), _put_one("b"))
        assert sorted((v_a, v_b)) == [2, 3], f"concurrent PUTs must be unique + increasing, got {v_a}, {v_b}"

        final = await _final_state(db_engine, test_org, map_id)
        assert final is not None
        assert final.version == 3, "no update may be lost under concurrency"
        # Last-write-wins: the active content is exactly one of the two updates.
        assert final.content_json["stages"] in (
            [{"id": "a", "name": "a", "type": "manual"}],
            [{"id": "b", "name": "b", "type": "manual"}],
        )

    async def test_put_missing_map_returns_404(
        self,
        integration_client: AsyncClient,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """The PUT route derives the 404 from ``update_lifecycle_map``'s None
        return (no pre-fetch, no extra round-trip)."""
        missing_id = uuid.uuid4()
        resp = await integration_client.put(
            f"/api/v1/lifecycle-maps/{missing_id}",
            json={"content_json": {"stages": [], "edges": [], "notes": ""}},
            headers=_auth_headers(test_org, test_user),
        )
        assert resp.status_code == 404, f"expected 404 for a missing map, got {resp.status_code}"
