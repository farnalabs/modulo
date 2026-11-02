"""Unit tests for System Maps modulo apply support (FAR-2288)."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from modulo.cli.apply.models import (
    SystemMapEntity,
    SystemMapLoopConfigEntity,
    SystemMapNodeEntity,
    SystemMapRelationshipEntity,
    SystemMapRevisionEntity,
)
from modulo.cli.apply.system_map_apply import (
    _apply_one_system_map,
    _create_payload,
    _patch_payload,
    _plan_system_map_view,
    _revision_payload,
    apply_system_maps,
    build_desired_views,
)

# ─── Test constants ───

_ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_MAP_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_PIPELINE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _map_row(
    *,
    map_id: uuid.UUID = _MAP_ID,
    name: str = "Test System Map",
    description: str | None = "A test system map",
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
    default_map: bool = False,
) -> dict[str, Any]:
    return {
        "id": str(map_id),
        "name": name,
        "description": description,
        "owner_team_id": str(owner_team_id) if owner_team_id else None,
        "visibility": visibility,
        "default_map": default_map,
    }


def _system_map_entity(
    name: str = "Test System Map",
    description: str | None = "A test system map",
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
    default_map: bool = False,
    revision: SystemMapRevisionEntity | None = None,
) -> SystemMapEntity:
    return SystemMapEntity(
        name=name,
        description=description,
        owner_team_id=owner_team_id,
        visibility=visibility,
        default_map=default_map,
        revision=revision,
    )


def _revision_entity(
    revision_number: int | None = None,
    status: str = "draft",
    nodes: list[SystemMapNodeEntity] | None = None,
    relationships: list[SystemMapRelationshipEntity] | None = None,
    loop_configs: list[SystemMapLoopConfigEntity] | None = None,
    notes: str = "",
) -> SystemMapRevisionEntity:
    return SystemMapRevisionEntity(
        revision_number=revision_number,
        status=status,
        nodes=nodes or [],
        relationships=relationships or [],
        loop_configs=loop_configs or [],
        notes=notes,
    )


def _node_entity(
    node_key: str = "node-1",
    node_type: str = "pipeline",
    display_name: str = "Node 1",
    pipeline_id: uuid.UUID | None = _PIPELINE_ID,
) -> SystemMapNodeEntity:
    return SystemMapNodeEntity(
        node_key=node_key,
        node_type=node_type,
        display_name=display_name,
        pipeline_id=pipeline_id,
    )


def _loop_node_entity(
    node_key: str = "loop-1",
    display_name: str = "Loop 1",
) -> SystemMapNodeEntity:
    return SystemMapNodeEntity(
        node_key=node_key,
        node_type="improvement_loop",
        display_name=display_name,
    )


def _relationship_entity(
    source_node_key: str = "node-1",
    target_node_key: str = "node-2",
    relationship_type: str = "flow",
) -> SystemMapRelationshipEntity:
    return SystemMapRelationshipEntity(
        source_node_key=source_node_key,
        target_node_key=target_node_key,
        relationship_type=relationship_type,
    )


def _loop_config_entity(
    node_key: str = "loop-1",
    target_signal: str = "error_rate",
    observation_window_seconds: int = 300,
    action_pipeline_id: uuid.UUID = _PIPELINE_ID,
    budget_limit_usd: float = 10.0,
    cooldown_seconds: int = 3600,
    outcome_metric: str = "success_rate",
) -> SystemMapLoopConfigEntity:
    return SystemMapLoopConfigEntity(
        node_key=node_key,
        target_signal=target_signal,
        observation_window_seconds=observation_window_seconds,
        action_pipeline_id=action_pipeline_id,
        budget_limit_usd=budget_limit_usd,
        cooldown_seconds=cooldown_seconds,
        outcome_metric=outcome_metric,
    )


# ─── Model tests ───


def test_system_map_node_entity_validates_node_type() -> None:
    for node_type in ("pipeline", "agent", "connector", "improvement_loop", "data_store", "external_system"):
        node = SystemMapNodeEntity(node_key="test", node_type=node_type, display_name="Test")
        assert node.node_type == node_type


def test_system_map_node_entity_rejects_invalid_node_type() -> None:
    with pytest.raises(ValidationError):
        SystemMapNodeEntity(node_key="test", node_type="invalid_type", display_name="Test")


def test_system_map_relationship_entity_validates_relationship_type() -> None:
    for rel_type in ("flow", "data", "trigger", "loop_back"):
        rel = SystemMapRelationshipEntity(source_node_key="a", target_node_key="b", relationship_type=rel_type)
        assert rel.relationship_type == rel_type


def test_system_map_revision_entity_validates_graph_references() -> None:
    node = _loop_node_entity(node_key="loop-1")
    loop = _loop_config_entity(node_key="loop-1")
    rev = _revision_entity(nodes=[node], loop_configs=[loop])
    assert len(rev.nodes) == 1
    assert len(rev.loop_configs) == 1


def test_system_map_revision_entity_rejects_duplicate_node_keys() -> None:
    node = _node_entity(node_key="node-1")
    with pytest.raises(ValidationError):
        _revision_entity(nodes=[node, node])


def test_system_map_revision_entity_rejects_dangling_relationship() -> None:
    node = _node_entity(node_key="node-1")
    rel = _relationship_entity(source_node_key="ghost", target_node_key="node-1")
    with pytest.raises(ValidationError):
        _revision_entity(nodes=[node], relationships=[rel])


def test_system_map_revision_entity_rejects_loop_config_for_non_loop_node() -> None:
    node = _node_entity(node_key="node-1", node_type="pipeline")
    loop = _loop_config_entity(node_key="node-1")
    with pytest.raises(ValidationError):
        _revision_entity(nodes=[node], loop_configs=[loop])


def test_system_map_entity_managed_view() -> None:
    entity = _system_map_entity(name="Test Map", visibility="org")
    view = entity.managed_view()
    # name is the identity key, not part of managed view
    assert view["visibility"] == "org"
    assert view["default_map"] is False


def test_system_map_entity_revision_managed_view() -> None:
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop], notes="Test revision")
    entity = _system_map_entity(revision=rev)
    view = entity.managed_view()
    assert "revision" in view
    assert view["revision"]["status"] == "draft"
    assert len(view["revision"]["nodes"]) == 1
    assert len(view["revision"]["loop_configs"]) == 1
    assert view["revision"]["notes"] == "Test revision"


def test_system_map_entity_create_payload() -> None:
    entity = _system_map_entity(name="Test Map", visibility="team", owner_team_id=_USER_ID)
    payload = entity.create_payload()
    assert payload["name"] == "Test Map"
    assert payload["visibility"] == "team"
    assert payload["owner_team_id"] == str(_USER_ID)


def test_system_map_entity_update_payload() -> None:
    entity = _system_map_entity(name="Test Map", visibility="team", owner_team_id=_USER_ID)
    payload = entity.update_payload()
    assert payload["name"] == "Test Map"
    assert payload["visibility"] == "team"
    assert payload["owner_team_id"] == str(_USER_ID)


def test_system_map_entity_revision_payload() -> None:
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop], notes="Test revision")
    entity = _system_map_entity(revision=rev)
    payload = _revision_payload(entity)
    assert payload is not None
    assert len(payload["nodes"]) == 1
    assert payload["nodes"][0]["node_key"] == "loop-1"
    assert len(payload["loop_configs"]) == 1
    assert payload["notes"] == "Test revision"


def test_system_map_entity_no_revision_payload() -> None:
    entity = _system_map_entity()
    payload = _revision_payload(entity)
    assert payload is None


# ─── System Map Apply tests ───


class _MockExecutor:
    """Mock executor for testing."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, payload))
        return {"id": str(_MAP_ID)}

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.patches.append((path, payload))
        return {}


class _MockReport:
    """Mock report dict with status tracking."""

    def __init__(self) -> None:
        self.data: dict[str, list[dict[str, Any]]] = {
            "created": [],
            "updated": [],
            "unchanged": [],
            "blocked": [],
            "failed": [],
        }

    def __getitem__(self, key: str) -> list[dict[str, Any]]:
        return self.data[key]

    def __setitem__(self, key: str, value: list[dict[str, Any]]) -> None:
        self.data[key] = value

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def setdefault(self, key: str, default=None):
        return self.data.setdefault(key, default)


def test_create_payload() -> None:
    entity = _system_map_entity(name="Test Map", visibility="team", owner_team_id=_USER_ID)
    payload = _create_payload(entity)
    assert payload["name"] == "Test Map"
    assert payload["visibility"] == "team"
    assert payload["owner_team_id"] == str(_USER_ID)


def test_patch_payload() -> None:
    entity = _system_map_entity(name="Test Map", visibility="team", owner_team_id=_USER_ID)
    payload = _patch_payload(entity)
    assert payload["name"] == "Test Map"
    assert payload["visibility"] == "team"
    assert payload["owner_team_id"] == str(_USER_ID)


def test_revision_payload() -> None:
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop], notes="Test revision")
    entity = _system_map_entity(revision=rev)
    payload = _revision_payload(entity)
    assert payload is not None
    assert len(payload["nodes"]) == 1
    assert payload["nodes"][0]["node_key"] == "loop-1"
    assert len(payload["loop_configs"]) == 1
    assert payload["notes"] == "Test revision"


def test_revision_payload_none_when_no_revision() -> None:
    entity = _system_map_entity()
    payload = _revision_payload(entity)
    assert payload is None


def test_plan_system_map_view() -> None:
    entity = _system_map_entity(name="Test Map")
    current_entities: dict[str, Any] = {}
    result = _plan_system_map_view(entity, current_entities)
    assert isinstance(result, tuple)
    view, revision = result
    # name is the identity key, not part of managed view
    assert view["visibility"] == "org"
    assert revision is None


def test_plan_system_map_view_with_revision() -> None:
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop])
    entity = _system_map_entity(revision=rev)
    current_entities: dict[str, Any] = {}
    view, revision = _plan_system_map_view(entity, current_entities)
    assert "revision" in view
    assert revision == view["revision"]


def test_apply_one_system_map_create() -> None:
    executor = _MockExecutor()
    entity = _system_map_entity(name="New Map")
    current_system_maps: dict[str, dict[str, Any]] = {}
    report = _MockReport()
    report["created"] = [{"kind": "system_map", "name": "New Map"}]
    map_ids: dict[str, str] = {}

    map_id = _apply_one_system_map(
        executor,
        entity,
        "created",
        report=report,
        current_system_maps=current_system_maps,
        map_ids=map_ids,
    )

    assert map_id == str(_MAP_ID)
    assert len(executor.posts) == 1
    assert executor.posts[0][0] == "/api/v1/system-maps"
    assert executor.posts[0][1]["name"] == "New Map"
    assert map_ids["New Map"] == str(_MAP_ID)


def test_apply_one_system_map_create_with_revision() -> None:
    executor = _MockExecutor()
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop])
    entity = _system_map_entity(name="New Map", revision=rev)
    current_system_maps: dict[str, dict[str, Any]] = {}
    report = _MockReport()
    report["created"] = [{"kind": "system_map", "name": "New Map"}]
    map_ids: dict[str, str] = {}

    map_id = _apply_one_system_map(
        executor,
        entity,
        "created",
        report=report,
        current_system_maps=current_system_maps,
        map_ids=map_ids,
    )

    assert map_id == str(_MAP_ID)
    assert len(executor.posts) == 2
    assert executor.posts[0][0] == "/api/v1/system-maps"
    assert executor.posts[1][0] == f"/api/v1/system-maps/{_MAP_ID}/revisions"
    assert "nodes" in executor.posts[1][1]
    assert "loop_configs" in executor.posts[1][1]
    assert map_ids["New Map"] == str(_MAP_ID)


def test_apply_one_system_map_update() -> None:
    executor = _MockExecutor()
    entity = _system_map_entity(name="Existing Map", description="Updated")
    current_system_maps: dict[str, dict[str, Any]] = {
        "Existing Map": {"id": str(_MAP_ID), "name": "Existing Map", "description": "Original"}
    }
    report = _MockReport()
    report["updated"] = [{"kind": "system_map", "name": "Existing Map"}]
    map_ids: dict[str, str] = {"Existing Map": str(_MAP_ID)}

    map_id = _apply_one_system_map(
        executor,
        entity,
        "updated",
        report=report,
        current_system_maps=current_system_maps,
        map_ids=map_ids,
    )

    assert map_id == str(_MAP_ID)
    assert len(executor.patches) == 1
    assert executor.patches[0][0] == f"/api/v1/system-maps/{_MAP_ID}"
    assert executor.patches[0][1]["description"] == "Updated"


def test_build_desired_views() -> None:
    from modulo.cli.apply.models import EntitySet

    entity = _system_map_entity(name="Test Map")
    entity_set = EntitySet(system_maps=[entity])
    current_entities: dict[str, dict[str, dict[str, Any]]] = {}
    desired: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "system_map": [],
    }
    blocked: list[tuple[str, str, str]] = []
    blocked_keys: set[tuple[str, str]] = set()

    desired, blocked = build_desired_views(entity_set, current_entities, desired, blocked, blocked_keys)

    assert len(desired["system_map"]) == 1
    assert desired["system_map"][0][0] == "Test Map"


def test_build_desired_views_ambiguous_name_blocked() -> None:
    from modulo.cli.apply.models import EntitySet

    entity = _system_map_entity(name="Ambiguous Map")
    entity_set = EntitySet(system_maps=[entity])
    current_entities: dict[str, dict[str, dict[str, Any]]] = {}
    desired: dict[str, list[tuple[str, dict[str, Any]]]] = {"system_map": []}
    blocked: list[tuple[str, str, str]] = []
    blocked_keys: set[tuple[str, str]] = set()
    ambiguous_maps = {"Ambiguous Map": 2}
    current_entities["ambiguous_system_map_names"] = ambiguous_maps

    desired, blocked = build_desired_views(entity_set, current_entities, desired, blocked, blocked_keys)

    assert len(desired["system_map"]) == 0
    assert len(blocked) == 1
    assert blocked[0][0] == "system_map"
    assert "ambiguous name" in blocked[0][2]


def test_apply_system_maps_create() -> None:
    from modulo.cli.apply.models import EntitySet

    executor = _MockExecutor()
    entity = _system_map_entity(name="New Map")
    EntitySet(system_maps=[entity])
    entities = {"system_map": [("New Map", entity)]}
    current_entities: dict[str, dict[str, Any]] = {"system_map": {}}
    report = _MockReport()
    report["created"] = [{"kind": "system_map", "name": "New Map"}]

    map_ids = apply_system_maps(executor, entities, current_entities, report)

    assert map_ids["New Map"] == str(_MAP_ID)
    assert len(executor.posts) == 1


def test_apply_system_maps_update() -> None:
    from modulo.cli.apply.models import EntitySet

    executor = _MockExecutor()
    entity = _system_map_entity(name="Existing Map", description="Updated")
    EntitySet(system_maps=[entity])
    entities = {"system_map": [("Existing Map", entity)]}
    current_entities = {
        "system_map": {"Existing Map": {"id": str(_MAP_ID), "name": "Existing Map", "description": "Original"}}
    }
    report = _MockReport()
    report["updated"] = [{"kind": "system_map", "name": "Existing Map"}]

    map_ids = apply_system_maps(executor, entities, current_entities, report)

    assert map_ids["Existing Map"] == str(_MAP_ID)
    assert len(executor.patches) == 1


def test_apply_system_maps_create_with_revision() -> None:
    from modulo.cli.apply.models import EntitySet

    executor = _MockExecutor()
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop])
    entity = _system_map_entity(name="New Map", revision=rev)
    EntitySet(system_maps=[entity])
    entities = {"system_map": [("New Map", entity)]}
    current_entities = {"system_map": {}}
    report = _MockReport()
    report["created"] = [{"kind": "system_map", "name": "New Map"}]

    map_ids = apply_system_maps(executor, entities, current_entities, report)

    assert map_ids["New Map"] == str(_MAP_ID)
    assert len(executor.posts) == 2


def test_apply_system_maps_failure_isolated() -> None:
    from modulo.cli.apply.models import EntitySet

    class _FailingExecutor(_MockExecutor):
        def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "/system-maps" in path and "revisions" not in path:
                return super()._post(path, payload)
            raise ValueError("Revision create failed")

    executor = _FailingExecutor()
    node = _loop_node_entity()
    loop = _loop_config_entity()
    rev = _revision_entity(nodes=[node], loop_configs=[loop])
    entity = _system_map_entity(name="New Map", revision=rev)
    EntitySet(system_maps=[entity])
    entities = {"system_map": [("New Map", entity)]}
    current_entities = {"system_map": {}}
    report = _MockReport()
    report["created"] = [{"kind": "system_map", "name": "New Map"}]

    map_ids = apply_system_maps(executor, entities, current_entities, report)

    # The map was created but revision failed - map should still be in map_ids from the first POST
    # but the report should have a failed entry
    assert "New Map" in map_ids
    assert len(report["failed"]) == 1
    assert report["failed"][0]["kind"] == "system_map"
    assert "Revision create failed" in report["failed"][0]["error"]
