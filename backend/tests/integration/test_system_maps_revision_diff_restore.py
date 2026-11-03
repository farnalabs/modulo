"""Integration tests: System Maps revision diff and restore-against-Postgres.

Both behaviours are pure graph operations inside ``SystemMapRepository``, so a
mocked session cannot see them: the diff walks the rows of two revisions and
the restore copies nodes/relationships/loop configs into a brand-new revision.
Driving the real repository against a migrated Postgres is the only way to
prove the copy actually lands and the source revision is left untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.pipeline import Pipeline
from modulo.db.models.system_map import SystemMap, SystemMapRevision
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


async def _create_pipeline(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """A committed-adjacent pipeline row for the loop config's FK."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=f"Action {uuid.uuid4().hex[:8]}",
        account_id=account_id,
        max_concurrent_runs=1,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline.id


async def _seed_graph(
    repo: SystemMapRepository, session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[SystemMap, SystemMapRevision, SystemMapRevision]:
    """Create a map with two revisions whose graphs differ in every diff axis."""
    map_obj = await repo.create_map(
        session,
        organisation_id=org_id,
        name=f"Diff {uuid.uuid4().hex[:8]}",
        account_id=account_id,
    )
    action_pipeline_id = await _create_pipeline(session, org_id, account_id)

    base = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=1,
        content_json={"notes": "base"},
    )
    await repo.create_node(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        node_key="build",
        node_type="pipeline",
        display_name="Build",
    )
    await repo.create_node(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        node_key="iterate",
        node_type="improvement_loop",
        display_name="Iterate",
    )
    await repo.create_relationship(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        source_node_key="build",
        target_node_key="iterate",
        relationship_type="flow",
    )
    await repo.create_loop_config(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        node_key="iterate",
        target_signal="health",
        observation_window_seconds=3600,
        action_pipeline_id=action_pipeline_id,
        budget_limit_usd=Decimal("10.000000"),
        cooldown_seconds=60,
        outcome_metric="score",
    )

    target = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=2,
        content_json={"notes": "target"},
    )
    await repo.create_node(
        session,
        organisation_id=org_id,
        revision_id=target.id,
        node_key="build",
        node_type="pipeline",
        display_name="Build v2",
    )
    await repo.create_node(
        session,
        organisation_id=org_id,
        revision_id=target.id,
        node_key="deploy",
        node_type="pipeline",
        display_name="Deploy",
    )
    await repo.create_relationship(
        session,
        organisation_id=org_id,
        revision_id=target.id,
        source_node_key="build",
        target_node_key="deploy",
        relationship_type="flow",
    )
    return map_obj, base, target


async def test_diff_revisions_reports_every_change_axis(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    await set_rls_user_context(rls_session, test_user, "admin")
    map_obj, base, target = await _seed_graph(repo, rls_session, test_org, test_user)

    diff = await repo.diff_revisions(
        rls_session,
        organisation_id=test_org,
        base_revision_id=base.id,
        target_revision_id=target.id,
    )

    assert diff is not None
    assert diff["base_revision_id"] == base.id
    assert diff["target_revision_id"] == target.id
    assert diff["nodes_added"] == ["deploy"]
    assert diff["nodes_removed"] == ["iterate"]
    assert diff["nodes_modified"] == ["build"]
    assert diff["relationships_added"] == ["build->deploy:flow"]
    assert diff["relationships_removed"] == ["build->iterate:flow"]
    assert not diff["relationships_modified"]
    assert not diff["loop_configs_added"]
    assert diff["loop_configs_removed"] == ["iterate"]
    assert not diff["loop_configs_modified"]
    assert diff["content_json_changed"] is True

    reverse = await repo.diff_revisions(
        rls_session,
        organisation_id=test_org,
        base_revision_id=target.id,
        target_revision_id=base.id,
    )

    assert reverse is not None
    assert reverse["nodes_added"] == ["iterate"]
    assert reverse["nodes_removed"] == ["deploy"]

    # Sanity: the map is genuinely the one seeded, not a fixture collision.
    assert map_obj.id == base.system_map_id


async def test_diff_revisions_is_organisation_scoped(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    await set_rls_user_context(rls_session, test_user, "admin")
    _map_obj, base, target = await _seed_graph(repo, rls_session, test_org, test_user)

    diff = await repo.diff_revisions(
        rls_session,
        organisation_id=uuid.uuid4(),
        base_revision_id=base.id,
        target_revision_id=target.id,
    )

    assert diff is None


async def test_restore_published_revision_copies_the_graph_into_a_new_draft(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    await set_rls_user_context(rls_session, test_user, "admin")
    map_obj, base, _target = await _seed_graph(repo, rls_session, test_org, test_user)
    published = await repo.publish_revision(
        rls_session,
        organisation_id=test_org,
        system_map_id=map_obj.id,
        revision_id=base.id,
    )
    assert published is not None

    restored = await repo.restore_revision_as_new_draft(
        rls_session,
        organisation_id=test_org,
        system_map_id=map_obj.id,
        source_revision_id=base.id,
        notes="Restored from revision 1",
    )

    assert restored is not None
    assert restored.system_map_id == map_obj.id
    assert restored.status == "draft"
    assert restored.revision_number == 3
    assert restored.effective_from is None
    assert restored.content_json == {"notes": "Restored from revision 1"}

    source_graph = await repo.get_full_revision_graph(rls_session, organisation_id=test_org, revision_id=base.id)
    restored_graph = await repo.get_full_revision_graph(rls_session, organisation_id=test_org, revision_id=restored.id)

    assert source_graph is not None
    assert restored_graph is not None
    assert [(n.node_key, n.node_type, n.display_name) for n in restored_graph["nodes"]] == [
        (n.node_key, n.node_type, n.display_name) for n in source_graph["nodes"]
    ]
    assert [(r.source_node_key, r.target_node_key, r.relationship_type) for r in restored_graph["relationships"]] == [
        (r.source_node_key, r.target_node_key, r.relationship_type) for r in source_graph["relationships"]
    ]
    assert [(lc.node_key, lc.target_signal, lc.outcome_metric) for lc in restored_graph["loop_configs"]] == [
        (lc.node_key, lc.target_signal, lc.outcome_metric) for lc in source_graph["loop_configs"]
    ]

    # Restoring never mutates or supersedes the source revision: it stays the
    # published revision until the new draft is itself published.
    refetched = await repo.get_revision_by_id(rls_session, organisation_id=test_org, revision_id=base.id)
    assert refetched is not None
    assert refetched.status == "published"
    current = await repo.get_published_revision(rls_session, organisation_id=test_org, system_map_id=map_obj.id)
    assert current is not None
    assert current.id == base.id


async def test_restore_rejects_a_draft_source(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    await set_rls_user_context(rls_session, test_user, "admin")
    map_obj, base, _target = await _seed_graph(repo, rls_session, test_org, test_user)

    restored = await repo.restore_revision_as_new_draft(
        rls_session,
        organisation_id=test_org,
        system_map_id=map_obj.id,
        source_revision_id=base.id,
        notes="Not restorable",
    )

    assert restored is None


async def test_restore_rejects_a_source_from_another_map(
    repo: SystemMapRepository,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    await set_rls_user_context(rls_session, test_user, "admin")
    _map_obj, base, _target = await _seed_graph(repo, rls_session, test_org, test_user)
    other_map = await repo.create_map(
        rls_session,
        organisation_id=test_org,
        name=f"Other {uuid.uuid4().hex[:8]}",
        account_id=test_user,
    )
    published = await repo.publish_revision(
        rls_session,
        organisation_id=test_org,
        system_map_id=base.system_map_id,
        revision_id=base.id,
    )
    assert published is not None

    restored = await repo.restore_revision_as_new_draft(
        rls_session,
        organisation_id=test_org,
        system_map_id=other_map.id,
        source_revision_id=base.id,
        notes="Wrong map",
    )

    assert restored is None
