"""Step definitions for lifecycle map BDD features."""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.lifecycle_map.validation import LifecycleMapContentError

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/lifecycle_maps/crud.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/lifecycle_maps/versioning.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/lifecycle_maps/library.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/lifecycle_maps/graduation.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


def _make_lifecycle_map(**kwargs: Any) -> MagicMock:
    m = MagicMock()
    m.id = kwargs.get("id", uuid.uuid4())
    m.organisation_id = kwargs.get("org_id", ORG_ID)
    m.name = kwargs.get("name", "SDLC Workflow")
    m.description = kwargs.get("description")
    m.owner_team_id = kwargs.get("owner_team_id")
    m.visibility = kwargs.get("visibility", "org")
    m.version = kwargs.get("version", 1)
    m.content_json = kwargs.get("content_json", {})
    m.archived_at = kwargs.get("archived_at")
    m.account_id = kwargs.get("account_id", USER_ID)
    m.updated_by = kwargs.get("updated_by")
    m.created_at = kwargs.get("created_at", datetime.now(UTC))
    m.updated_at = kwargs.get("updated_at", datetime.now(UTC))
    return m


@given(parsers.parse('a lifecycle map named "{name}" exists'))
def lifecycle_map_exists(name: str, ctx: dict[str, Any], request: Any) -> None:
    lm = _make_lifecycle_map(name=name)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@given(parsers.parse('a lifecycle map named "{name}" exists with version {version:d}'))
def lifecycle_map_exists_with_version(name: str, version: int, ctx: dict[str, Any], request: Any) -> None:
    lm = _make_lifecycle_map(name=name, version=version)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@given(parsers.parse('a lifecycle map named "{name}" exists with a manual stage "{stage_id}"'))
def lifecycle_map_exists_with_manual_stage(name: str, stage_id: str, ctx: dict[str, Any], request: Any) -> None:
    content_json = {"stages": [{"id": stage_id, "name": stage_id.replace("-", " ").title(), "type": "manual"}]}
    lm = _make_lifecycle_map(name=name, content_json=content_json)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@given(parsers.parse('a lifecycle map named "{name}" exists with a modulo stage "{stage_id}"'))
def lifecycle_map_exists_with_modulo_stage(name: str, stage_id: str, ctx: dict[str, Any], request: Any) -> None:
    content_json = {"stages": [{"id": stage_id, "name": stage_id.replace("-", " ").title(), "type": "modulo"}]}
    lm = _make_lifecycle_map(name=name, content_json=content_json)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@given(parsers.parse('a lifecycle map named "{name}" exists with version {version:d} and a manual stage "{stage_id}"'))
def lifecycle_map_exists_with_version_and_manual_stage(
    name: str, version: int, stage_id: str, ctx: dict[str, Any], request: Any
) -> None:
    content_json = {"stages": [{"id": stage_id, "name": stage_id.replace("-", " ").title(), "type": "manual"}]}
    lm = _make_lifecycle_map(name=name, version=version, content_json=content_json)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@when(parsers.parse('I create a lifecycle map named "{name}" with visibility "{visibility}"'))
def post_create_lifecycle_map(name: str, visibility: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    payload = {"name": name, "visibility": visibility}
    with patch("modulo.api.routes.lifecycle_maps.create_lifecycle_map", new=AsyncMock()) as mock_create:
        mock_lm = _make_lifecycle_map(name=name, visibility=visibility)
        mock_create.return_value = mock_lm
        resp = client.post("/api/v1/lifecycle-maps", json=payload)
    _store_response(request, ctx, resp)
    ctx["created_map"] = mock_lm


@when("I export the lifecycle map")
def get_export_lifecycle_map(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    lm.content_json = {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []}
    with patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = lm
        resp = client.get(f"/api/v1/lifecycle-maps/{lm.id}/export")
    _store_response(request, ctx, resp)


@then("the response is a lifecycle map export envelope")
def response_is_export_envelope(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("primitive_type") == "lifecycle_map", data
    assert data.get("format_version") == "2", data
    assert "content_json" in data, data
    assert "name" in data, data


@then("the export envelope carries the version history")
def export_envelope_carries_version_history(request: Any) -> None:
    """FAR-204: the v2 envelope carries a versions array with per-version graph."""
    resp = request.node._resp
    data = resp.json()
    versions = data.get("versions")
    assert isinstance(versions, list), data
    assert versions, data
    first = versions[0]
    assert isinstance(first, dict), data
    assert "stages" in first, data
    assert "edges" in first, data
    assert first.get("version") is not None, data


@when(parsers.parse('I import a lifecycle map named "{name}" from a v1 envelope'))
def post_import_lifecycle_map_v1(name: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    """FAR-204 backward compat: a format_version 1 envelope still imports."""
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "1",
        "name": name,
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
    }
    with patch("modulo.api.routes.lifecycle_maps.import_lifecycle_map_envelope", new=AsyncMock()) as mock_import:
        mock_lm = _make_lifecycle_map(name=name)
        mock_import.return_value = mock_lm
        resp = client.post("/api/v1/lifecycle-maps/import", json=envelope)
    _store_response(request, ctx, resp)
    ctx["imported_map"] = mock_lm


@when(parsers.parse('I import a lifecycle map named "{name}" with version history'))
def post_import_lifecycle_map_with_versions(name: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    """FAR-204: a v2 envelope carrying a versions array is accepted by the route."""
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "2",
        "name": name,
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
        "versions": [
            {"version": 1, "stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
            {
                "version": 2,
                "stages": [
                    {"id": "s1", "name": "Inbox", "type": "manual"},
                    {"id": "s2", "name": "Review", "type": "manual"},
                ],
                "edges": [{"id": "e1", "source": "s1", "target": "s2"}],
            },
        ],
    }
    with patch("modulo.api.routes.lifecycle_maps.import_lifecycle_map_envelope", new=AsyncMock()) as mock_import:
        mock_lm = _make_lifecycle_map(name=name)
        mock_import.return_value = mock_lm
        resp = client.post("/api/v1/lifecycle-maps/import", json=envelope)
    _store_response(request, ctx, resp)
    ctx["imported_map"] = mock_lm


@when("I import a lifecycle map with a malformed version history")
def post_import_lifecycle_map_malformed_versions(request: Any, client: Any) -> None:
    """FAR-204: a malformed versions entry raises the bundle error → 422."""
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "2",
        "name": "Broken",
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
        "versions": [{"version": "nope", "stages": [], "edges": []}],
    }
    from modulo.core.lifecycle_map.import_export import LifecycleMapBundleError

    bundle_error = LifecycleMapBundleError("Lifecycle map 'versions' entry #0 'version' must be an integer, got 'nope'")
    with patch(
        "modulo.api.routes.lifecycle_maps.import_lifecycle_map_envelope",
        new=AsyncMock(side_effect=bundle_error),
    ):
        resp = client.post("/api/v1/lifecycle-maps/import", json=envelope)
    _store_response(request, ctx={}, resp=resp)


@when(parsers.parse('I contribute a lifecycle map primitive named "{name}"'))
def post_contribute_lifecycle_map(name: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    """FAR-204: lifecycle_map is an accepted community-contribute primitive_type."""
    payload = {
        "primitive_type": "lifecycle_map",
        "name": name,
        "slug": "community-sdlc",
        "description": "shared lifecycle map",
        "tags": [],
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
        "source_url": None,
    }

    def _primitive() -> MagicMock:
        p = MagicMock()
        p.id = uuid.uuid4()
        p.organisation_id = ORG_ID
        p.source = "local"
        p.primitive_type = "lifecycle_map"
        p.name = name
        p.slug = "community-sdlc"
        p.description = "shared lifecycle map"
        p.author = USER_ID.hex
        p.version = "1.0"
        p.tags = []
        p.content_json = payload["content_json"]
        p.source_url = None
        p.forked_from = None
        p.checksum = None
        p.ed25519_signature = None
        p.verified = None
        p.download_count = None
        p.average_rating = None
        p.review_count = None
        p.owner_team_id = None
        p.visibility = "org"
        p.account_id = USER_ID
        p.auto_update = True
        p.tier = "native"
        p.trust_tier = None
        p.created_at = datetime.now(UTC)
        p.updated_at = datetime.now(UTC)
        return p

    with patch("modulo.api.routes.library.contribute_primitive", new=AsyncMock(return_value=_primitive())):
        resp = client.post("/api/v1/libraries/community/contribute", json=payload)
    _store_response(request, ctx, resp)
    ctx["contributed"] = name


@when(parsers.parse('I import a lifecycle map named "{name}"'))
def post_import_lifecycle_map(name: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "1",
        "name": name,
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
    }
    with patch("modulo.api.routes.lifecycle_maps.import_lifecycle_map_envelope", new=AsyncMock()) as mock_import:
        mock_lm = _make_lifecycle_map(name=name)
        mock_import.return_value = mock_lm
        resp = client.post("/api/v1/lifecycle-maps/import", json=envelope)
    _store_response(request, ctx, resp)
    ctx["imported_map"] = mock_lm


@given("a lifecycle map primitive exists")
def lifecycle_map_primitive_exists(ctx: dict[str, Any]) -> None:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.primitive_type = "lifecycle_map"
    p.name = "SDLC Workflow"
    p.description = "shared"
    p.content_json = {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []}
    ctx["primitive"] = p


@given("a non-lifecycle-map primitive exists")
def non_lifecycle_map_primitive_exists(ctx: dict[str, Any]) -> None:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.primitive_type = "workflow"
    p.name = "Some Workflow"
    p.content_json = {}
    ctx["primitive"] = p


@when("I create a lifecycle map from the primitive")
def post_create_lifecycle_map_from_primitive(ctx: dict[str, Any], request: Any, client: Any) -> None:
    prim = ctx.get("primitive") or MagicMock(id=uuid.uuid4(), primitive_type="lifecycle_map")
    from modulo.core.lifecycle_map.import_export import LifecycleMapBundleError
    from modulo.core.lifecycle_map.validation import LifecycleMapContentError

    async def _fake_materialize(session, *, org_id, account_id, primitive, owner_team_id=None, visibility="org"):
        if prim.primitive_type != "lifecycle_map":
            raise LifecycleMapBundleError(f"Primitive type '{prim.primitive_type}' is not 'lifecycle_map'")
        if not isinstance(prim.content_json, dict) or "stages" not in prim.content_json:
            raise LifecycleMapContentError("content_json.stages must be an array")
        return _make_lifecycle_map(name=prim.name)

    with (
        patch("modulo.api.routes.library.get_primitive", new=AsyncMock(return_value=prim)),
        patch("modulo.api.routes.library.materialize_map_from_primitive", new=_fake_materialize),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/create-lifecycle-map")
    _store_response(request, ctx, resp)
    ctx["created_map"] = _make_lifecycle_map(name=prim.name)


@when("I create a lifecycle map from a primitive that conflicts with an existing map's pipeline")
def post_create_lifecycle_map_from_conflicting_primitive(ctx: dict[str, Any], request: Any, client: Any) -> None:
    prim = ctx.get("primitive") or MagicMock(id=uuid.uuid4(), primitive_type="lifecycle_map", name="SDLC Workflow")
    from modulo.core.lifecycle_map.validation import LifecycleMapPipelineConflictError

    async def _fake_materialize(session, *, org_id, account_id, primitive, owner_team_id=None, visibility="org"):
        raise LifecycleMapPipelineConflictError("pipeline(s) already a stage of another active lifecycle map")

    with (
        patch("modulo.api.routes.library.get_primitive", new=AsyncMock(return_value=prim)),
        patch("modulo.api.routes.library.materialize_map_from_primitive", new=_fake_materialize),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/create-lifecycle-map")
    _store_response(request, ctx, resp)


@when("I create a lifecycle map from missing primitive")
def post_create_lifecycle_map_from_missing_primitive(request: Any, client: Any) -> None:
    missing_id = uuid.uuid4()
    with patch("modulo.api.routes.library.get_primitive", new=AsyncMock(return_value=None)):
        resp = client.post(f"/api/v1/libraries/{missing_id}/create-lifecycle-map")
    _store_response(request, ctx={}, resp=resp)


@when("I import a lifecycle map that conflicts with an existing map's pipeline")
def post_import_lifecycle_map_conflict(request: Any, client: Any) -> None:
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "1",
        "name": "Conflicting",
        "content_json": {
            "stages": [{"id": "s1", "name": "Inbox", "type": "modulo", "pipeline_id": str(uuid.uuid4())}],
            "edges": [],
        },
    }
    from modulo.core.lifecycle_map.validation import LifecycleMapPipelineConflictError

    conflict_error = LifecycleMapPipelineConflictError("pipeline(s) already a stage of another active lifecycle map")
    with patch(
        "modulo.api.routes.lifecycle_maps.import_lifecycle_map_envelope",
        new=AsyncMock(side_effect=conflict_error),
    ):
        resp = client.post("/api/v1/lifecycle-maps/import", json=envelope)
    _store_response(request, ctx={}, resp=resp)


@when("I import a lifecycle map with invalid content")
def post_import_lifecycle_map_invalid(ctx: dict[str, Any], request: Any, client: Any) -> None:
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "1",
        "name": "Broken",
        "content_json": {"stages": "not-an-array"},
    }
    from modulo.core.lifecycle_map.validation import LifecycleMapContentError

    with patch(
        "modulo.api.routes.lifecycle_maps.import_lifecycle_map_envelope",
        new=AsyncMock(side_effect=LifecycleMapContentError("content_json.stages must be an array")),
    ):
        resp = client.post("/api/v1/lifecycle-maps/import", json=envelope)
    _store_response(request, ctx, resp)


@when("I list lifecycle maps")
def get_list_lifecycle_maps(ctx: dict[str, Any], request: Any, client: Any) -> None:
    with patch("modulo.api.routes.lifecycle_maps.list_lifecycle_maps", new=AsyncMock()) as mock_list:
        mock_list.return_value = MagicMock(items=[_make_lifecycle_map()], total=1, page=1, page_size=20)
        resp = client.get("/api/v1/lifecycle-maps")
    _store_response(request, ctx, resp)


@when("I get the lifecycle map by id")
def get_lifecycle_map_by_id(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    with patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = lm
        resp = client.get(f"/api/v1/lifecycle-maps/{lm.id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I get lifecycle map by id "{lm_id}"'))
def get_lifecycle_map_by_raw_id(lm_id: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    with patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = None
        resp = client.get(f"/api/v1/lifecycle-maps/{lm_id}")
    _store_response(request, ctx, resp)


@when("I get the deleted lifecycle map by id")
def get_deleted_lifecycle_map_by_id(ctx: dict[str, Any], request: Any, client: Any) -> None:
    with patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = None
        resp = client.get(f"/api/v1/lifecycle-maps/{uuid.uuid4()}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I update the lifecycle map name to "{name}"'))
def put_update_lifecycle_map_name(name: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    with (
        patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get,
        patch("modulo.api.routes.lifecycle_maps.update_lifecycle_map", new=AsyncMock()) as mock_update,
    ):
        mock_get.return_value = current
        updated = _make_lifecycle_map(name=name, version=current.version, content_json=current.content_json)
        mock_update.return_value = updated
        resp = client.put(f"/api/v1/lifecycle-maps/{current.id}", json={"name": name})
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@when(parsers.parse('I update the lifecycle map description to "{description}"'))
def put_update_lifecycle_map_description(description: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    with (
        patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get,
        patch("modulo.api.routes.lifecycle_maps.update_lifecycle_map", new=AsyncMock()) as mock_update,
    ):
        mock_get.return_value = current
        updated = _make_lifecycle_map(
            name=current.name, version=current.version, description=description, content_json=current.content_json
        )
        mock_update.return_value = updated
        resp = client.put(f"/api/v1/lifecycle-maps/{current.id}", json={"description": description})
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@when(parsers.parse("I update the lifecycle map content to include {count:d} stage"))
@when(parsers.parse("I update the lifecycle map content to include {count:d} stages"))
def put_update_lifecycle_map_content(count: int, ctx: dict[str, Any], request: Any, client: Any) -> None:
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [{"id": f"stage-{i}", "name": f"Stage {i}", "type": "modulo"} for i in range(count)]
    content_json = {"stages": stages}
    with (
        patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get,
        patch("modulo.api.routes.lifecycle_maps.update_lifecycle_map", new=AsyncMock()) as mock_update,
    ):
        mock_get.return_value = current
        updated = _make_lifecycle_map(name=current.name, version=current.version + 1, content_json=content_json)
        mock_update.return_value = updated
        resp = client.put(f"/api/v1/lifecycle-maps/{current.id}", json={"content_json": content_json})
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@when("I delete the lifecycle map")
def delete_lifecycle_map(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    deleted_id = str(lm.id)
    with patch("modulo.api.routes.lifecycle_maps.delete_lifecycle_map", new=AsyncMock()) as mock_delete:
        mock_delete.return_value = True
        resp = client.delete(f"/api/v1/lifecycle-maps/{deleted_id}")
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = None


@when(parsers.parse("I save a version of the lifecycle map with {count:d} stage"))
@when(parsers.parse("I save a version of the lifecycle map with {count:d} stages"))
def save_version_of_lifecycle_map(count: int, ctx: dict[str, Any], request: Any, client: Any) -> None:
    """POST /versions — a save publishes a new active version and bumps the counter.

    This step mocks ``save_map_version`` and simply steps the returned version
    forward, so it pins the route contract (each save returns the next version)
    rather than true concurrency. The concurrent-save guarantee — strictly
    increasing unique version numbers under the row lock — is proven by the unit
    SQL-assertion tests and the Postgres integration tests instead.
    """
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [{"id": f"stage-{i}", "name": f"Stage {i}", "type": "modulo"} for i in range(count)]
    with patch("modulo.api.routes.lifecycle_maps.save_map_version", new=AsyncMock()) as mock_save:
        updated = _make_lifecycle_map(name=current.name, version=current.version + 1, content_json={"stages": stages})
        mock_save.return_value = updated
        resp = client.post(
            f"/api/v1/lifecycle-maps/{current.id}/versions",
            json={"stages": stages, "edges": [], "notes": ""},
        )
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@when("I save a version of the lifecycle map with a circular transition")
def save_version_with_circular_transition(ctx: dict[str, Any], request: Any, client: Any) -> None:
    """POST /versions with a cyclic transition — the route maps the
    ``LifecycleMapContentError`` raised by the content validator to a 422."""
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [
        {"id": "stage-0", "name": "Stage 0", "type": "modulo"},
        {"id": "stage-1", "name": "Stage 1", "type": "manual"},
    ]
    edges = [
        {"id": "edge-0", "source": "stage-0", "target": "stage-1"},
        {"id": "edge-1", "source": "stage-1", "target": "stage-0"},
    ]
    with patch("modulo.api.routes.lifecycle_maps.save_map_version", new=AsyncMock()) as mock_save:
        mock_save.side_effect = LifecycleMapContentError(
            "lifecycle-map content: stage transitions form a cycle: stage-0 -> stage-1 -> stage-0"
        )
        resp = client.post(
            f"/api/v1/lifecycle-maps/{current.id}/versions",
            json={"stages": stages, "edges": edges, "notes": ""},
        )
    _store_response(request, ctx, resp)


@when("I save a version of the lifecycle map with a dangling edge")
def save_version_with_dangling_edge(ctx: dict[str, Any], request: Any, client: Any) -> None:
    """POST /versions with an edge referencing an undefined stage — the route
    maps the ``LifecycleMapContentError`` raised by the content validator to a 422."""
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [{"id": "stage-0", "name": "Stage 0", "type": "modulo"}]
    edges = [{"id": "edge-0", "source": "stage-0", "target": "stage-ghost"}]
    with patch("modulo.api.routes.lifecycle_maps.save_map_version", new=AsyncMock()) as mock_save:
        mock_save.side_effect = LifecycleMapContentError(
            "lifecycle-map edge/transition #0 (id 'edge-0'): target stage 'stage-ghost' is not defined in stages"
        )
        resp = client.post(
            f"/api/v1/lifecycle-maps/{current.id}/versions",
            json={"stages": stages, "edges": edges, "notes": ""},
        )
    _store_response(request, ctx, resp)


@when("I save a version of the lifecycle map with duplicate stage ids")
def save_version_with_duplicate_stage_ids(ctx: dict[str, Any], request: Any, client: Any) -> None:
    """POST /versions with two stages sharing an id — the route maps the
    ``LifecycleMapContentError`` raised by the content validator to a 422."""
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [
        {"id": "stage-0", "name": "Stage 0", "type": "modulo"},
        {"id": "stage-0", "name": "Stage 0 again", "type": "manual"},
    ]
    with patch("modulo.api.routes.lifecycle_maps.save_map_version", new=AsyncMock()) as mock_save:
        mock_save.side_effect = LifecycleMapContentError(
            "lifecycle-map stage #1: duplicate stage id 'stage-0' (already used by stage #0)"
        )
        resp = client.post(
            f"/api/v1/lifecycle-maps/{current.id}/versions",
            json={"stages": stages, "edges": [], "notes": ""},
        )
    _store_response(request, ctx, resp)


@when("I get the lifecycle map versions")
def get_lifecycle_map_versions(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    with patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = lm
        resp = client.get(f"/api/v1/lifecycle-maps/{lm.id}/versions")
    _store_response(request, ctx, resp)


@when("I save a version of the lifecycle map")
def save_version_capture_audit(ctx: dict[str, Any], request: Any, client: Any, mock_session: Any) -> None:
    """POST /versions with an audit capture — pins the dedicated
    ``lifecycle_map.version_saved`` event flowing through the real route's
    ``_record_audit`` (FAR-203)."""
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [{"id": "stage-0", "name": "Stage 0", "type": "modulo"}]
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin_nested = MagicMock(return_value=begin_nested_cm)
    with (
        patch("modulo.api.routes.lifecycle_maps.save_map_version", new=AsyncMock()) as mock_save,
        patch("modulo.api.routes.lifecycle_maps.append_audit_event", new=AsyncMock()) as mock_audit,
    ):
        updated = _make_lifecycle_map(name=current.name, version=current.version + 1, content_json={"stages": stages})
        mock_save.return_value = updated
        resp = client.post(
            f"/api/v1/lifecycle-maps/{current.id}/versions",
            json={"stages": stages, "edges": [], "notes": ""},
        )
    _store_response(request, ctx, resp)
    ctx["audit_mock"] = mock_audit
    ctx["lifecycle_map"] = updated


@when("I save a version of the lifecycle map as the current user")
def save_version_as_current_user(ctx: dict[str, Any], request: Any, client: Any) -> None:
    """POST /versions with the returned map stamped with the authenticated
    account as its version actor (updated_by) — pins that the version entry's
    created_by reflects the saving account, not a static null."""
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    stages = [{"id": "stage-0", "name": "Stage 0", "type": "modulo"}]
    with patch("modulo.api.routes.lifecycle_maps.save_map_version", new=AsyncMock()) as mock_save:
        updated = _make_lifecycle_map(
            name=current.name,
            version=current.version + 1,
            content_json={"stages": stages},
            updated_by=USER_ID,
        )
        mock_save.return_value = updated
        resp = client.post(
            f"/api/v1/lifecycle-maps/{current.id}/versions",
            json={"stages": stages, "edges": [], "notes": ""},
        )
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@then("the saved version reports the current user as created_by")
def saved_version_reports_current_user(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("created_by") == str(USER_ID), f"Expected created_by {USER_ID}, got {data.get('created_by')}"


@when("I restore the lifecycle map")
def restore_lifecycle_map(ctx: dict[str, Any], request: Any, client: Any, mock_session: Any) -> None:
    """POST /{id}/restore with an audit capture — pins the ``lifecycle_map.restored``
    event flowing through the real route's ``_record_audit`` (FAR-203)."""
    current = ctx.get("lifecycle_map", _make_lifecycle_map())
    restored = _make_lifecycle_map(name=current.name, content_json=current.content_json)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin_nested = MagicMock(return_value=begin_nested_cm)
    with (
        patch("modulo.api.routes.lifecycle_maps.restore_lifecycle_map", new=AsyncMock()) as mock_restore,
        patch("modulo.api.routes.lifecycle_maps.append_audit_event", new=AsyncMock()) as mock_audit,
    ):
        mock_restore.return_value = restored
        resp = client.post(f"/api/v1/lifecycle-maps/{current.id}/restore")
    _store_response(request, ctx, resp)
    ctx["audit_mock"] = mock_audit
    ctx["lifecycle_map"] = restored


@then(parsers.parse('a "{event_type}" audit event was recorded for the map'))
def lifecycle_map_audit_event_recorded(event_type: str, ctx: dict[str, Any]) -> None:
    mock_audit = ctx.get("audit_mock")
    assert mock_audit is not None, "No audit mock captured — the step did not run"
    assert mock_audit.await_args is not None, f"No audit event was recorded, expected {event_type!r}"
    kwargs = mock_audit.await_args.kwargs
    assert kwargs["event_type"] == event_type, f"Expected {event_type!r}, got {kwargs['event_type']!r}"
    assert kwargs["resource_type"] == "lifecycle_map"


@given("a lifecycle map with dangling legacy content exists")
def lifecycle_map_with_dangling_legacy_content(ctx: dict[str, Any], request: Any) -> None:
    """A pre-FAR-175 stored graph: an edge referencing an undefined stage."""
    content_json = {
        "stages": [{"id": "stage-0", "name": "Inbox", "type": "manual"}],
        "edges": [{"id": "edge-0", "source": "stage-0", "target": "stage-ghost"}],
    }
    lm = _make_lifecycle_map(name="Legacy Map", content_json=content_json)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@when("the legacy content backfill cleans the map")
def legacy_backfill_cleans_map(ctx: dict[str, Any]) -> None:
    from modulo.core.lifecycle_map.validation import clean_legacy_content

    lm = ctx.get("lifecycle_map")
    assert lm is not None, "No lifecycle map in context"
    cleaned, changes = clean_legacy_content(lm.content_json)
    ctx["cleaned_content"] = cleaned
    ctx["backfill_changes"] = changes


@then("the repaired map content is accepted by editor validation")
def repaired_map_content_accepted(ctx: dict[str, Any]) -> None:
    from modulo.core.lifecycle_map.validation import normalize_content

    cleaned = ctx.get("cleaned_content")
    assert cleaned is not None, "The backfill step did not run"
    assert ctx.get("backfill_changes"), "The backfill made no changes to the legacy content"
    normalize_content(cleaned)  # raises LifecycleMapContentError if still invalid
    assert not ctx["cleaned_content"]["edges"], "the dangling edge must be dropped"


@then(parsers.parse("the version list contains exactly {count:d} version at version {version:d}"))
def version_list_exactly(count: int, version: int, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert isinstance(data, list), f"Expected a list of versions, got {type(data).__name__}"
    assert len(data) == count, f"Expected {count} version(s), got {len(data)}"
    assert data[0]["version"] == version, f"Expected version {version}, got {data[0]['version']}"


@given(parsers.parse('a lifecycle map with an external stage "{stage_id}" exists'))
def lifecycle_map_exists_with_external_stage(stage_id: str, ctx: dict[str, Any], request: Any) -> None:
    content_json = {"stages": [{"id": stage_id, "name": stage_id.replace("-", " ").title(), "type": "external"}]}
    lm = _make_lifecycle_map(name="SDLC Workflow", content_json=content_json)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@when(parsers.parse('I self-report "{kind}" "{ref}" with stage_id "{stage_id}"'))
def self_report_with_stage_id(
    kind: str, ref: str, stage_id: str, ctx: dict[str, Any], request: Any, client: Any, mock_session: Any
) -> None:
    """POST .../journeys/self-report naming the completed stage.

    The route resolves ``stage_id`` against the map's current
    ``lifecycle_map_stages`` projection and passes the row to
    ``advance_journeys`` as the explicit stage (external stages have no
    ``pipeline_id``, so the pipeline path can never move the journey). The
    junction lookup is faked on the mock session; the captured explicit stage
    proves the journey would advance into it.
    """
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())

    stage_mock = MagicMock()
    stage_mock.map_id = lm.id
    stage_mock.version = lm.version
    stage_mock.stage_id = stage_id
    stage_mock.stage_name = stage_id.replace("-", " ").title()
    stage_mock.position = 7
    junction_result = AsyncMock()
    junction_result.scalar_one_or_none = MagicMock(return_value=stage_mock)
    mock_session.execute.return_value = junction_result

    async def _fake_advance(
        session: Any,
        organisation_id: Any,
        run_id: Any = None,
        pipeline_id: Any = None,
        refs: Any = None,
        status: str = "complete",
        completed_at: Any = None,
        run_created_at: Any = None,
        explicit_stage: Any = None,
    ) -> int:
        ctx["advance_explicit_stage"] = explicit_stage
        return 1

    with (
        patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock(return_value=lm)),
        patch(
            "modulo.api.routes.lifecycle_maps.confirm_reported_refs",
            new=AsyncMock(return_value=([{"kind": kind, "ref": ref, "source": "reported"}], 0)),
        ),
        patch("modulo.api.routes.lifecycle_maps.advance_journeys", new=_fake_advance),
    ):
        resp = client.post(
            f"/api/v1/lifecycle-maps/{lm.id}/journeys/self-report",
            json={"work_item_refs": [{"kind": kind, "ref": ref}], "stage_id": stage_id},
        )
    _store_response(request, ctx, resp)
    ctx["advance_called"] = True


@then(parsers.parse('the journey is at stage "{stage_id}"'))
def journey_is_at_stage(stage_id: str, ctx: dict[str, Any]) -> None:
    assert ctx.get("advance_called"), "advance_journeys was not called"
    explicit = ctx.get("advance_explicit_stage")
    assert explicit is not None, "expected an explicit_stage resolved from the reported stage_id"
    assert explicit.stage_id == stage_id, f"Expected journey at stage {stage_id!r}, got {explicit.stage_id!r}"


@then(parsers.parse('the response contains a lifecycle map named "{name}"'))
def response_contains_lifecycle_map(name: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    if isinstance(data, list):
        assert any(item.get("name") == name for item in data), f"No lifecycle map named '{name}' in response"
    elif isinstance(data, dict):
        assert data.get("name") == name, f"Expected name '{name}', got '{data.get('name')}'"


@then(parsers.parse("the lifecycle map has version {version:d}"))
def lifecycle_map_version(version: int, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("version") == version, f"Expected version {version}, got {data.get('version')}"


@then(parsers.parse("the response contains {count:d} lifecycle map"))
def response_contains_count(count: int, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    items = data.get("items", [])
    assert len(items) == count, f"Expected {count} item(s), got {len(items)}"


@then(parsers.parse('the stage type is "{stage_type}"'))
def assert_stage_type(stage_type: str, ctx: dict[str, Any]) -> None:
    lm = ctx.get("lifecycle_map")
    assert lm is not None, "No lifecycle map in context"
    stages = lm.content_json.get("stages", [])
    for s in stages:
        assert s.get("type") == stage_type, f"Expected stage type '{stage_type}', got '{s.get('type')}'"
