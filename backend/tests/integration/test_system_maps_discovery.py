"""Integration tests for System Maps discovery persistence.

The adapter stores discovery drafts in the existing organisation-scoped
``lifecycle_maps`` document until the dedicated System Maps schema lands. These
tests exercise that compatibility contract against a real Postgres
(testcontainers): idempotent draft creation, discovery ownership scoping, the
revision-id/version contract, the derived stage projection and the RLS
boundary.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.lifecycle_map.service import create_lifecycle_map
from modulo.core.system_map_persistence import _DISCOVERY_MARKER, SystemMapNameConflictError, SystemMapRepository
from modulo.core.system_maps_discovery import DiscoverySource, discover_draft
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.rls import set_rls_org, set_rls_user_context

pytestmark = pytest.mark.integration

_REPO = SystemMapRepository()


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _scoped_session(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "admin",
) -> None:
    await set_rls_org(session, org_id)
    await set_rls_user_context(session, user_id, role)


def _content(*, stage_ids: list[str], fingerprint: str = "fingerprint") -> dict:
    return {
        "stages": [{"id": stage_id, "name": stage_id.upper(), "type": "manual"} for stage_id in stage_ids],
        "edges": [],
        "discovery_fingerprint": fingerprint,
    }


async def test_get_or_create_draft_is_idempotent_and_discovery_owned(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Discovery Idempotent {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        first = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )
        first_id = first.id

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        second = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )

    assert second.id == first_id
    assert second.version == 1
    assert second.content_json[_DISCOVERY_MARKER] is True


async def test_get_or_create_draft_rejects_a_user_authored_name_collision(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"User Authored {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        created = await create_lifecycle_map(
            session,
            org_id=test_org,
            name=name,
            account_id=test_user,
            description="user map",
            content_json={"stages": [], "edges": []},
        )
        created_id = created.id

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        with pytest.raises(SystemMapNameConflictError):
            await _REPO.get_or_create_draft(
                session,
                organisation_id=test_org,
                account_id=test_user,
                name=name,
                description="draft",
            )

    # The user map must survive intact: never adopted, never overwritten.
    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        survived = await session.get(LifecycleMap, created_id)
    assert survived is not None
    assert survived.content_json == {"stages": [], "edges": []}


async def test_save_discovery_revision_is_stable_bumps_version_and_projects_stages(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Discovery Revision {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        draft = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )
        map_id = draft.id
        saved, first_revision = await _REPO.save_discovery_revision(
            session,
            organisation_id=test_org,
            map_id=map_id,
            content=_content(stage_ids=["build"]),
            updated_by=test_user,
        )
        first_version = saved.version
        first_revision_id = str(first_revision)

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        saved, second_revision = await _REPO.save_discovery_revision(
            session,
            organisation_id=test_org,
            map_id=map_id,
            content=_content(stage_ids=["build"]),
            updated_by=test_user,
        )
        second_version = saved.version

    # Same fingerprint => same synthetic revision id, but every save advances
    # the version counter under the row lock.
    assert str(second_revision) == first_revision_id
    assert (first_version, second_version) == (2, 3)
    assert saved.content_json[_DISCOVERY_MARKER] is True
    assert saved.content_json["discovery_revision_id"] == first_revision_id

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        stages = list(
            (await session.execute(select(LifecycleMapStage).where(LifecycleMapStage.map_id == map_id))).scalars()
        )
    assert [(stage.stage_id, stage.version) for stage in stages] == [("build", 3)]


async def test_save_discovery_revision_is_rls_scoped(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Discovery RLS {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        draft = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )
        map_id = draft.id

    other_org = uuid.uuid4()
    async with _factory(app_engine)() as session, session.begin():
        await _scoped_session(session, other_org, test_user)
        with pytest.raises(ValueError, match="discovery draft map not found"):
            await _REPO.save_discovery_revision(
                session,
                organisation_id=other_org,
                map_id=map_id,
                content=_content(stage_ids=["build"]),
                updated_by=test_user,
            )


def test_discovery_draft_contains_only_suggested_edges() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="pipeline", identifier="p", name="build"),
            DiscoverySource(kind="trigger", identifier="t", name="webhook", pipeline_id="p"),
        ]
    )
    assert all(edge["config_json"].get("suggested") is True for edge in draft.relationships)
