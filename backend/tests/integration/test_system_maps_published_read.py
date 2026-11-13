"""Integration tests: the viewer published-read contract (FAR-2363).

These drive the real ``SystemMapRepository`` against a migrated Postgres
(testcontainers). The defect class is SQL: the viewer read path depends on the
``published_only`` predicates actually filtering unpublished maps and draft
revisions, which a mocked session cannot prove.

Covered:
* ``list_maps_by_org(published_only=True)`` omits a map with no published
  revision.
* ``list_revisions_history(published_only=True)`` omits drafts.
* ``get_revision_by_time(published_only=True)`` never returns a draft.
* The filter is organisation-scoped: another org's published map is invisible.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from farna.db.repositories.system_map import SystemMapRepository
from farna.db.rls import set_rls_user_context

pytestmark = pytest.mark.integration


async def _unused_session_factory() -> AsyncGenerator[AsyncSession, None]:
    """The repository is always passed an explicit session in these tests."""
    raise RuntimeError("SystemMapRepository integration test passes an explicit session")
    yield  # pragma: no cover


@pytest.fixture
def repo() -> SystemMapRepository:
    return SystemMapRepository(session_factory=_unused_session_factory)


async def _seed_map_with_draft_and_published(
    repo: SystemMapRepository,
    session: AsyncSession,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (map_id, published_revision_id, draft_revision_id)."""
    map_obj = await repo.create_map(
        session,
        organisation_id=org_id,
        name=f"{name} {uuid.uuid4().hex[:8]}",
        account_id=account_id,
    )
    published = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=1,
    )
    draft = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=2,
    )
    published = await repo.publish_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_id=published.id,
    )
    assert published is not None
    return map_obj.id, published.id, draft.id


async def test_published_only_list_excludes_unpublished_maps(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A map with no published revision never reaches the viewer list."""
    await set_rls_user_context(rls_session, test_user, "admin")
    published_map_id, _published_rev, _draft = await _seed_map_with_draft_and_published(repo, rls_session, test_org, test_user, name="Published read")
    unpublished = await repo.create_map(
        rls_session,
        organisation_id=test_org,
        name=f"Unpublished {uuid.uuid4().hex[:8]}",
        account_id=test_user,
    )

    visible = await repo.list_maps_by_org(rls_session, organisation_id=test_org, published_only=True)
    visible_ids = {row.id for row in visible}

    assert published_map_id in visible_ids
    assert unpublished.id not in visible_ids


async def test_published_only_history_excludes_drafts(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The viewer history contains published/archived revisions only."""
    await set_rls_user_context(rls_session, test_user, "admin")
    map_id, published_rev, draft_rev = await _seed_map_with_draft_and_published(repo, rls_session, test_org, test_user, name="History read")

    history = await repo.list_revisions_history(rls_session, organisation_id=test_org, system_map_id=map_id, published_only=True)
    history_ids = {row.id for row in history}

    assert published_rev in history_ids
    assert draft_rev not in history_ids


async def test_published_only_by_time_never_returns_a_draft(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A draft has no effective window, so the viewer by-time read cannot match it."""
    await set_rls_user_context(rls_session, test_user, "admin")
    map_id, published_rev, draft_rev = await _seed_map_with_draft_and_published(repo, rls_session, test_org, test_user, name="By-time read")
    map_obj = await repo.get_map_by_id(rls_session, organisation_id=test_org, map_id=map_id)
    assert map_obj is not None
    published = await repo.get_revision_by_id(rls_session, organisation_id=test_org, revision_id=published_rev)
    assert published is not None
    assert published.effective_from is not None

    matched = await repo.get_revision_by_time(
        rls_session,
        organisation_id=test_org,
        system_map_id=map_id,
        at=published.effective_from,
        published_only=True,
    )

    assert matched is not None
    assert matched.id == published_rev
    assert matched.id != draft_rev


async def test_published_only_read_is_organisation_scoped(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A query bound to another organisation sees none of this org's published maps."""
    await set_rls_user_context(rls_session, test_user, "admin")
    map_id, _published_rev, _draft = await _seed_map_with_draft_and_published(repo, rls_session, test_org, test_user, name="Scoped read")

    visible = await repo.list_maps_by_org(rls_session, organisation_id=uuid.uuid4(), published_only=True)

    assert map_id not in {row.id for row in visible}
    assert not visible
