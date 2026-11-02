"""Integration tests: System Maps version-aware read model (FAR-2288).

These drive the real ``SystemMapRepository`` against a migrated Postgres
(testcontainers), because the defect class is SQL and a mocked session cannot
see it:

* ``publish_revision`` archives the superseded revision by stamping its
  ``effective_to`` and setting ``status='archived'``. A by-time lookup must
  still find that archived revision inside its historical window.
* The interval is half-open: ``effective_from <= at < effective_to``.
* A draft revision has a NULL ``effective_from`` and must never match.

The first assertion below fails against the pre-fix query (which filtered on
``status == 'published'``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.system_map import SystemMapRepository
from modulo.db.rls import set_rls_user_context

pytestmark = pytest.mark.integration


async def _unused_session_factory() -> AsyncGenerator[AsyncSession, None]:
    """The repository is always passed an explicit session in these tests."""
    raise RuntimeError("SystemMapRepository integration test passes an explicit session")
    yield  # pragma: no cover


@pytest.fixture
def repo() -> SystemMapRepository:
    return SystemMapRepository(session_factory=_unused_session_factory)


async def _publish_two_revisions(
    repo: SystemMapRepository,
    session: AsyncSession,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, datetime, datetime]:
    """Create a map with two revisions, publish both, return (map, rev1, rev2, t1, t2)."""
    map_obj = await repo.create_map(
        session,
        organisation_id=org_id,
        name=f"Version Aware {uuid.uuid4().hex[:8]}",
        account_id=account_id,
    )
    rev1 = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=1,
    )
    rev2 = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=2,
    )

    published1 = await repo.publish_revision(
        session, organisation_id=org_id, system_map_id=map_obj.id, revision_id=rev1.id
    )
    assert published1 is not None
    t1 = published1.effective_from
    assert t1 is not None

    published2 = await repo.publish_revision(
        session, organisation_id=org_id, system_map_id=map_obj.id, revision_id=rev2.id
    )
    assert published2 is not None
    t2 = published2.effective_from
    assert t2 is not None
    # Publish timestamps must be monotonic or the intervals would collapse.
    assert t2 > t1

    return map_obj.id, rev1.id, rev2.id, t1, t2


async def test_by_time_finds_superseded_archived_revision(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A re-published (archived) revision is still found inside its window."""
    await set_rls_user_context(rls_session, test_user, "admin")
    map_id, rev1_id, rev2_id, t1, t2 = await _publish_two_revisions(repo, rls_session, test_org, test_user)

    superseded = await repo.get_revision_by_id(rls_session, organisation_id=test_org, revision_id=rev1_id)
    assert superseded is not None
    await rls_session.refresh(superseded)
    assert superseded.status == "archived"
    assert superseded.effective_to == t2, "publishing rev2 must close rev1's effective window"

    # Mid-window: only rev1 covers this instant. Pre-fix this returned None
    # because the query filtered on status == 'published'.
    midpoint = t1 + (t2 - t1) / 2
    inside = await repo.get_revision_by_time(rls_session, organisation_id=test_org, system_map_id=map_id, at=midpoint)
    assert inside is not None
    assert inside.id == rev1_id

    # The current revision owns the open-ended window.
    current = await repo.get_revision_by_time(
        rls_session, organisation_id=test_org, system_map_id=map_id, at=datetime.now(UTC)
    )
    assert current is not None
    assert current.id == rev2_id


async def test_by_time_interval_is_half_open(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """effective_from is inclusive; effective_to is exclusive."""
    await set_rls_user_context(rls_session, test_user, "admin")
    map_id, rev1_id, rev2_id, t1, t2 = await _publish_two_revisions(repo, rls_session, test_org, test_user)

    at_from = await repo.get_revision_by_time(rls_session, organisation_id=test_org, system_map_id=map_id, at=t1)
    assert at_from is not None
    assert at_from.id == rev1_id, "effective_from must be inclusive"

    at_to = await repo.get_revision_by_time(rls_session, organisation_id=test_org, system_map_id=map_id, at=t2)
    assert at_to is not None
    assert at_to.id == rev2_id, "effective_to must be exclusive (the successor starts here)"

    before_first = await repo.get_revision_by_time(
        rls_session,
        organisation_id=test_org,
        system_map_id=map_id,
        at=t1 - timedelta(seconds=1),
    )
    assert before_first is None


async def test_draft_revision_never_matches_by_time(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A draft has no effective_from, so it is invisible to a by-time read."""
    await set_rls_user_context(rls_session, test_user, "admin")
    map_id, _rev1_id, rev2_id, _t1, _t2 = await _publish_two_revisions(repo, rls_session, test_org, test_user)

    draft = await repo.create_revision(
        rls_session,
        organisation_id=test_org,
        system_map_id=map_id,
        revision_number=3,
    )
    assert draft.effective_from is None

    found = await repo.get_revision_by_time(
        rls_session, organisation_id=test_org, system_map_id=map_id, at=datetime.now(UTC)
    )
    assert found is not None
    assert found.id == rev2_id
