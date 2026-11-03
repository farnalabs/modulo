"""Integration tests: System Maps revision diff and restore-against-Postgres.

Both behaviours are pure graph operations inside ``SystemMapRepository``, so a
mocked session cannot see them: the diff walks the rows of two revisions and
the restore copies nodes/relationships/loop configs into a brand-new revision.
Driving the real repository against a migrated Postgres is the only way to
prove the copy actually lands and the source revision is left untouched.

Every compared field gets its own seeded node/relationship/loop config that
differs in exactly that one axis. A shared "does anything differ" assertion is
not enough: if a single comparison is dropped from ``diff_revisions`` or a
single field is dropped from the restore copy, the aggregate answer can still
look right. One axis per key makes a dropped comparison remove that key from
the result, which the exact-list assertions catch.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.eval import Eval
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.system_map import SystemMap, SystemMapRevision
from modulo.db.models.team import Team
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


# ─── Seeded graph ───
#
# Each tuple below names one change axis. The corresponding shared node (or
# loop config) is identical in both revisions except for that single field, so
# dropping its comparison removes exactly that key from the diff result.
_AXIS_NODE_KEYS = (
    "axis_node_type",
    "axis_display_name",
    "axis_config_json",
    "axis_pipeline_id",
    "axis_connector_instance_id",
    "axis_owner_team_id",
    "axis_eval_id",
    "axis_position_x",
    "axis_position_y",
)

_LOOP_AXIS_KEYS = (
    "loop_target_signal",
    "loop_observation_window",
    "loop_action_pipeline",
    "loop_budget_limit",
    "loop_cooldown",
    "loop_outcome_metric",
)


@dataclass(frozen=True)
class _Seed:
    """FK targets whose values are seeded onto the graph's nodes and loops."""

    action_pipeline_id: uuid.UUID
    target_action_pipeline_id: uuid.UUID
    pipeline_id: uuid.UUID
    target_pipeline_id: uuid.UUID
    connector_instance_id: uuid.UUID
    target_connector_instance_id: uuid.UUID
    team_id: uuid.UUID
    target_team_id: uuid.UUID
    eval_id: uuid.UUID
    target_eval_id: uuid.UUID


async def _create_pipeline(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID, label: str) -> uuid.UUID:
    """A committed-adjacent pipeline row for the node/loop FK."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=f"{label} {uuid.uuid4().hex[:8]}",
        account_id=account_id,
        max_concurrent_runs=1,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline.id


async def _create_connector_instance(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    instance = ConnectorInstance(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=f"Connector {uuid.uuid4().hex[:8]}",
        connector_type_id="github",
        account_id=account_id,
        credentials_ciphertext=b"ciphertext",
    )
    session.add(instance)
    await session.flush()
    return instance.id


async def _create_team(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    team = Team(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=f"Team {uuid.uuid4().hex[:8]}",
        account_id=account_id,
    )
    session.add(team)
    await session.flush()
    return team.id


async def _create_eval(
    session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID, pipeline_id: uuid.UUID
) -> uuid.UUID:
    eval_def = Eval(
        id=uuid.uuid4(),
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        name=f"Eval {uuid.uuid4().hex[:8]}",
        eval_type="regex",
        config_json={"pattern": "x"},
        account_id=account_id,
    )
    session.add(eval_def)
    await session.flush()
    return eval_def.id


async def _seed_fk_targets(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> _Seed:
    """Create distinct FK targets for every comparison axis that carries one."""
    action_pipeline_id = await _create_pipeline(session, org_id, account_id, "Action")
    target_action_pipeline_id = await _create_pipeline(session, org_id, account_id, "Action v2")
    pipeline_id = await _create_pipeline(session, org_id, account_id, "Node")
    target_pipeline_id = await _create_pipeline(session, org_id, account_id, "Node v2")
    return _Seed(
        action_pipeline_id=action_pipeline_id,
        target_action_pipeline_id=target_action_pipeline_id,
        pipeline_id=pipeline_id,
        target_pipeline_id=target_pipeline_id,
        connector_instance_id=await _create_connector_instance(session, org_id, account_id),
        target_connector_instance_id=await _create_connector_instance(session, org_id, account_id),
        team_id=await _create_team(session, org_id, account_id),
        target_team_id=await _create_team(session, org_id, account_id),
        eval_id=await _create_eval(session, org_id, account_id, pipeline_id),
        target_eval_id=await _create_eval(session, org_id, account_id, pipeline_id),
    )


def _node_fields(seed: _Seed, node_key: str, *, node_type: str = "pipeline") -> dict[str, Any]:
    """Every persisted node field, deliberately non-default where a default exists."""
    return {
        "node_key": node_key,
        "node_type": node_type,
        "display_name": f"Node {node_key}",
        "config_json": {"axis": node_key},
        "pipeline_id": seed.pipeline_id,
        "connector_instance_id": seed.connector_instance_id,
        "owner_team_id": seed.team_id,
        "eval_id": seed.eval_id,
        "position_x": Decimal("1.00"),
        "position_y": Decimal("2.00"),
    }


def _axis_node_changes(seed: _Seed) -> dict[str, dict[str, Any]]:
    """The single-field mutation applied to each axis node in the target revision."""
    return {
        "axis_node_type": {"node_type": "agent"},
        "axis_display_name": {"display_name": "Renamed axis node"},
        "axis_config_json": {"config_json": {"axis": "changed"}},
        "axis_pipeline_id": {"pipeline_id": seed.target_pipeline_id},
        "axis_connector_instance_id": {"connector_instance_id": seed.target_connector_instance_id},
        "axis_owner_team_id": {"owner_team_id": seed.target_team_id},
        "axis_eval_id": {"eval_id": seed.target_eval_id},
        "axis_position_x": {"position_x": Decimal("9.00")},
        "axis_position_y": {"position_y": Decimal("8.00")},
    }


def _loop_config_fields(seed: _Seed, node_key: str) -> dict[str, Any]:
    """Every persisted loop-config field, deliberately non-default."""
    return {
        "node_key": node_key,
        "target_signal": "health",
        "observation_window_seconds": 3600,
        "action_pipeline_id": seed.action_pipeline_id,
        "budget_limit_usd": Decimal("10.000000"),
        "cooldown_seconds": 60,
        "outcome_metric": "score",
    }


def _loop_axis_changes(seed: _Seed) -> dict[str, dict[str, Any]]:
    """The single-field mutation applied to each loop config in the target revision."""
    return {
        "loop_target_signal": {"target_signal": "latency"},
        "loop_observation_window": {"observation_window_seconds": 7200},
        "loop_action_pipeline": {"action_pipeline_id": seed.target_action_pipeline_id},
        "loop_budget_limit": {"budget_limit_usd": Decimal("20.000000")},
        "loop_cooldown": {"cooldown_seconds": 120},
        "loop_outcome_metric": {"outcome_metric": "cost"},
    }


async def _create_node(
    repo: SystemMapRepository,
    session: AsyncSession,
    org_id: uuid.UUID,
    revision_id: uuid.UUID,
    fields: dict[str, Any],
) -> None:
    await repo.create_node(session, organisation_id=org_id, revision_id=revision_id, **fields)


async def _create_shared_nodes(
    repo: SystemMapRepository,
    session: AsyncSession,
    org_id: uuid.UUID,
    seed: _Seed,
    revision_id: uuid.UUID,
    *,
    mutate: bool,
) -> None:
    """Seed every node that exists in BOTH revisions, one change axis at a time."""
    await _create_node(repo, session, org_id, revision_id, _node_fields(seed, "build"))
    await _create_node(repo, session, org_id, revision_id, _node_fields(seed, "shared"))
    # One identical improvement-loop node per loop-config axis, so the node
    # itself never appears in nodes_modified and only the loop config does.
    for loop_key in _LOOP_AXIS_KEYS:
        await _create_node(
            repo,
            session,
            org_id,
            revision_id,
            _node_fields(seed, loop_key, node_type="improvement_loop")
            | {"position_x": Decimal("3.00"), "position_y": Decimal("4.00"), "config_json": {}},
        )

    changes = _axis_node_changes(seed)
    for axis_key in _AXIS_NODE_KEYS:
        fields = _node_fields(seed, axis_key)
        if mutate:
            fields |= changes[axis_key]
        await _create_node(repo, session, org_id, revision_id, fields)


async def _create_loop_configs(
    repo: SystemMapRepository,
    session: AsyncSession,
    org_id: uuid.UUID,
    seed: _Seed,
    revision_id: uuid.UUID,
    *,
    mutate: bool,
) -> None:
    changes = _loop_axis_changes(seed)
    for loop_key in _LOOP_AXIS_KEYS:
        fields = _loop_config_fields(seed, loop_key)
        if mutate:
            fields |= changes[loop_key]
        await repo.create_loop_config(session, organisation_id=org_id, revision_id=revision_id, **fields)


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
    seed = await _seed_fk_targets(session, org_id, account_id)

    base = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=1,
        content_json={"notes": "base"},
    )
    await _create_shared_nodes(repo, session, org_id, seed, base.id, mutate=False)
    await _create_node(
        repo,
        session,
        org_id,
        base.id,
        _node_fields(seed, "iterate", node_type="improvement_loop"),
    )
    await repo.create_relationship(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        source_node_key="build",
        target_node_key="shared",
        relationship_type="flow",
        config_json={"weight": 1},
    )
    await repo.create_relationship(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        source_node_key="shared",
        target_node_key="iterate",
        relationship_type="data",
        config_json={"suggested": True},
    )
    await _create_loop_configs(repo, session, org_id, seed, base.id, mutate=False)
    await repo.create_loop_config(
        session,
        organisation_id=org_id,
        revision_id=base.id,
        **_loop_config_fields(seed, "iterate"),
    )

    target = await repo.create_revision(
        session,
        organisation_id=org_id,
        system_map_id=map_obj.id,
        revision_number=2,
        content_json={"notes": "target"},
    )
    await _create_shared_nodes(repo, session, org_id, seed, target.id, mutate=True)
    await _create_node(repo, session, org_id, target.id, _node_fields(seed, "deploy"))
    await repo.create_relationship(
        session,
        organisation_id=org_id,
        revision_id=target.id,
        source_node_key="build",
        target_node_key="shared",
        relationship_type="flow",
        config_json={"weight": 2},
    )
    await repo.create_relationship(
        session,
        organisation_id=org_id,
        revision_id=target.id,
        source_node_key="shared",
        target_node_key="deploy",
        relationship_type="trigger",
        config_json={"suggested": True},
    )
    await _create_loop_configs(repo, session, org_id, seed, target.id, mutate=True)
    return map_obj, base, target


def _node_tuple(node: Any) -> tuple[Any, ...]:
    """Every persisted node field the restore must copy."""
    return (
        node.node_key,
        node.node_type,
        node.display_name,
        node.config_json,
        node.pipeline_id,
        node.connector_instance_id,
        node.owner_team_id,
        node.eval_id,
        node.position_x,
        node.position_y,
    )


def _relationship_tuple(rel: Any) -> tuple[Any, ...]:
    """Every persisted relationship field the restore must copy."""
    return (rel.source_node_key, rel.target_node_key, rel.relationship_type, rel.config_json)


def _loop_config_tuple(loop: Any) -> tuple[Any, ...]:
    """Every persisted loop-config field the restore must copy."""
    return (
        loop.node_key,
        loop.target_signal,
        loop.observation_window_seconds,
        loop.action_pipeline_id,
        loop.budget_limit_usd,
        loop.cooldown_seconds,
        loop.outcome_metric,
    )


def _sorted(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return sorted(rows, key=lambda row: tuple(str(part) for part in row))


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
    # One node per axis: dropping any single comparison removes its key here.
    assert diff["nodes_modified"] == sorted(_AXIS_NODE_KEYS)
    assert diff["relationships_added"] == ["shared->deploy:trigger"]
    assert diff["relationships_removed"] == ["shared->iterate:data"]
    assert diff["relationships_modified"] == ["build->shared:flow"]
    assert not diff["loop_configs_added"]
    assert diff["loop_configs_removed"] == ["iterate"]
    # One loop config per axis: dropping any single comparison removes its key.
    assert diff["loop_configs_modified"] == sorted(_LOOP_AXIS_KEYS)
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
    assert reverse["nodes_modified"] == sorted(_AXIS_NODE_KEYS)
    assert reverse["relationships_added"] == ["shared->iterate:data"]
    assert reverse["relationships_removed"] == ["shared->deploy:trigger"]
    assert reverse["relationships_modified"] == ["build->shared:flow"]
    assert reverse["loop_configs_added"] == ["iterate"]
    assert not reverse["loop_configs_removed"]
    assert reverse["loop_configs_modified"] == sorted(_LOOP_AXIS_KEYS)

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

    # Prove the seed can actually observe a dropped copy: every nullable FK,
    # config_json, and non-zero position carries a non-default value.
    source_nodes = source_graph["nodes"]
    assert any(node.pipeline_id is not None for node in source_nodes)
    assert any(node.connector_instance_id is not None for node in source_nodes)
    assert any(node.owner_team_id is not None for node in source_nodes)
    assert any(node.eval_id is not None for node in source_nodes)
    assert any(node.config_json for node in source_nodes)
    assert any(node.position_x != 0 for node in source_nodes)
    assert any(node.position_y != 0 for node in source_nodes)
    assert any(rel.config_json for rel in source_graph["relationships"])

    assert _sorted([_node_tuple(node) for node in restored_graph["nodes"]]) == _sorted(
        [_node_tuple(node) for node in source_nodes]
    )
    assert _sorted([_relationship_tuple(rel) for rel in restored_graph["relationships"]]) == _sorted(
        [_relationship_tuple(rel) for rel in source_graph["relationships"]]
    )
    assert _sorted([_loop_config_tuple(loop) for loop in restored_graph["loop_configs"]]) == _sorted(
        [_loop_config_tuple(loop) for loop in source_graph["loop_configs"]]
    )

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
