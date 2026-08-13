"""Integration tests for lifecycle-map concurrent-save semantics on Postgres.

Two agents saving versions of the same map concurrently must never produce
duplicate version numbers. The save path fetches the map row with
``SELECT ... FOR UPDATE`` (``get_lifecycle_map_for_update``), so under Postgres
READ COMMITTED the second save blocks on the row lock, re-reads the first
save's committed version and bumps from there: the active version is
last-write-wins and the counter is strictly increasing with no duplicates. A
version-list read running concurrently with a save always observes one
consistent committed snapshot — never a half-written map.

These assertions FAIL against the pre-fix read-then-increment behaviour, where
both transactions read version 1 and the second commit silently overwrites the
first (final version 2, duplicate number).
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.lifecycle_map.service import create_lifecycle_map, get_lifecycle_map, save_map_version
from modulo.db.rls import set_rls_org

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
