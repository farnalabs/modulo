"""Integration tests for System Maps discovery persistence."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.system_map_persistence import (
    _DISCOVERY_NAMESPACE,
    SystemMapDiscoveryRepository,
    SystemMapNameConflictError,
)
from modulo.core.system_maps_discovery import DiscoverySource, discover_draft
from modulo.db.models.system_map import SystemMap, SystemMapNode, SystemMapRelationship, SystemMapRevision
from modulo.db.rls import set_rls_org, set_rls_user_context

pytestmark = pytest.mark.integration

_REPO = SystemMapDiscoveryRepository()


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


def _content(*, node_keys: list[str], fingerprint: str = "fingerprint") -> dict[str, object]:
    return {
        "nodes": [
            {
                "node_key": node_key,
                "node_type": "pipeline",
                "display_name": node_key.upper(),
                "config_json": {},
            }
            for node_key in node_keys
        ],
        "relationships": [],
        "discovery_fingerprint": fingerprint,
    }


async def test_get_or_create_draft_creates_a_system_map(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Discovery Draft {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        draft = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )

    assert isinstance(draft, SystemMap)
    assert draft.organisation_id == test_org
    assert draft.name == name
    assert draft.description == "draft"
    assert draft.visibility == "org"
    assert draft.default_map is False


async def test_get_or_create_draft_rejects_a_live_name_collision(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Collision {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        created = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="original",
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
                description="replacement",
            )

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        survived = (await session.execute(select(SystemMap).where(SystemMap.id == created_id))).scalar_one()

    assert survived.description == "original"


async def test_save_discovery_revision_persists_current_system_map_entities(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Discovery Revision {uuid.uuid4().hex[:8]}"
    content = _content(node_keys=["build"], fingerprint="fingerprint-1")
    content["relationships"] = [
        {
            "source_node_key": "build",
            "target_node_key": "build",
            "relationship_type": "flow",
            "config_json": {"suggested": True},
        }
    ]

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        draft = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )
        saved_map, revision_id, revision_number = await _REPO.save_discovery_revision(
            session,
            organisation_id=test_org,
            map_id=draft.id,
            content=content,
            updated_by=test_user,
        )

        revision = (
            await session.execute(select(SystemMapRevision).where(SystemMapRevision.id == revision_id))
        ).scalar_one()
        nodes = list(
            (await session.execute(select(SystemMapNode).where(SystemMapNode.revision_id == revision_id))).scalars()
        )
        relationships = list(
            (
                await session.execute(
                    select(SystemMapRelationship).where(SystemMapRelationship.revision_id == revision_id)
                )
            ).scalars()
        )

    assert saved_map.updated_by == test_user
    assert revision_number == 1
    assert revision.system_map_id == draft.id
    assert revision.content_json == content
    assert [(node.node_key, node.node_type) for node in nodes] == [("build", "pipeline")]
    assert [(edge.source_node_key, edge.target_node_key, edge.relationship_type) for edge in relationships] == [
        ("build", "build", "flow")
    ]


async def test_save_identical_discovery_reuses_revision_without_duplicate_children(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Stable Revision {uuid.uuid4().hex[:8]}"
    content = _content(node_keys=["build"], fingerprint="fingerprint-stable")

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        draft = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )
        first_map, first_revision_id, first_number = await _REPO.save_discovery_revision(
            session,
            organisation_id=test_org,
            map_id=draft.id,
            content=content,
            updated_by=test_user,
        )
        second_map, second_revision_id, second_number = await _REPO.save_discovery_revision(
            session,
            organisation_id=test_org,
            map_id=draft.id,
            content=content,
            updated_by=test_user,
        )
        revisions = list(
            (
                await session.execute(select(SystemMapRevision).where(SystemMapRevision.system_map_id == draft.id))
            ).scalars()
        )
        nodes = list(
            (
                await session.execute(select(SystemMapNode).where(SystemMapNode.revision_id == first_revision_id))
            ).scalars()
        )

    expected_revision_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{draft.id}:fingerprint-stable")
    assert first_map is second_map
    assert first_revision_id == second_revision_id
    assert second_revision_id == expected_revision_id
    assert first_number == 1
    assert second_number == 1
    assert len(revisions) == 1
    assert len(nodes) == 1


async def test_save_discovery_revision_is_organisation_scoped(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    name = f"Discovery Scope {uuid.uuid4().hex[:8]}"

    async with _factory(db_engine)() as session, session.begin():
        await _scoped_session(session, test_org, test_user)
        draft = await _REPO.get_or_create_draft(
            session,
            organisation_id=test_org,
            account_id=test_user,
            name=name,
            description="draft",
        )

    other_org = uuid.uuid4()
    async with _factory(app_engine)() as session, session.begin():
        await _scoped_session(session, other_org, test_user)
        with pytest.raises(ValueError, match="system map discovery draft not found"):
            await _REPO.save_discovery_revision(
                session,
                organisation_id=other_org,
                map_id=draft.id,
                content=_content(node_keys=["build"]),
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
