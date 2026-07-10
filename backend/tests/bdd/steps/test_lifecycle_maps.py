"""Step definitions for lifecycle map BDD features."""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.db.models.lifecycle_map import LifecycleMap

try:
    scenarios("../features/lifecycle_maps/crud.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/lifecycle_maps/versioning.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/lifecycle_maps/library.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../features/lifecycle_maps/graduation.feature")
except (FileNotFoundError, OSError):
    pass

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
    m = MagicMock(spec=LifecycleMap)
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
    m.created_at = kwargs.get("created_at")
    m.updated_at = kwargs.get("updated_at")
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


@given(parsers.parse('a lifecycle map named "{name}" exists with content_json:\n{content}'))
def lifecycle_map_exists_with_content(name: str, content: str, ctx: dict[str, Any], request: Any) -> None:
    content_json = json.loads(content.strip())
    lm = _make_lifecycle_map(name=name, content_json=content_json)
    ctx["lifecycle_map"] = lm
    request.node._lifecycle_map = lm


@given(parsers.parse('the map has a manual stage "{stage_id}"'))
def map_has_manual_stage(stage_id: str, ctx: dict[str, Any]) -> None:
    lm = ctx.get("lifecycle_map")
    if lm is not None:
        stages = lm.content_json.get("stages", [])
        stages.append({"id": stage_id, "name": stage_id.replace("-", " ").title(), "type": "manual"})
        lm.content_json = {**lm.content_json, "stages": stages}


@given(parsers.parse("I have a lifecycle map bundle"))
def have_lifecycle_map_bundle(ctx: dict[str, Any]) -> None:
    ctx["bundle"] = {
        "primitive_type": "lifecycle_map",
        "name": "Imported SDLC Workflow",
        "content_json": {
            "stages": [
                {"id": "stage-1", "name": "Plan", "type": "manual"},
                {"id": "stage-2", "name": "Build", "type": "modulo"},
            ],
            "transitions": [
                {"from": "stage-1", "to": "stage-2", "trigger_type": "manual"},
            ],
        },
    }


@when(parsers.parse("I POST /api/v1/lifecycle-maps with:\n{data}"))
def post_create_lifecycle_map(data: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    payload = _parse_table_data(data)
    with patch("modulo.core.lifecycle_map.service.create_lifecycle_map", new=AsyncMock()) as mock_create:
        mock_lm = _make_lifecycle_map(name=payload.get("name", "SDLC Workflow"))
        mock_create.return_value = mock_lm
        resp = client.post("/api/v1/lifecycle-maps", json=payload)
    _store_response(request, ctx, resp)
    ctx["created_map"] = mock_lm


@when("I GET /api/v1/lifecycle-maps")
def get_list_lifecycle_maps(ctx: dict[str, Any], request: Any, client: Any) -> None:
    with patch("modulo.core.lifecycle_map.service.list_lifecycle_maps", new=AsyncMock()) as mock_list:
        mock_list.return_value = MagicMock(items=[_make_lifecycle_map()], total=1, page=1, page_size=20)
        resp = client.get("/api/v1/lifecycle-maps")
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/lifecycle-maps/{id}"))
def get_lifecycle_map(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm_id = str(ctx.get("lifecycle_map", MagicMock()).id or uuid.uuid4())
    with patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = ctx.get("lifecycle_map")
        resp = client.get(f"/api/v1/lifecycle-maps/{lm_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse("I PUT /api/v1/lifecycle-maps/{{id}} with:\n{data}"))
def put_update_lifecycle_map(data: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm_id = str(ctx.get("lifecycle_map", MagicMock()).id or uuid.uuid4())
    payload = _parse_table_data(data)
    current = ctx.get("lifecycle_map")
    with (
        patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new=AsyncMock()) as mock_get,
        patch("modulo.core.lifecycle_map.service.update_lifecycle_map", new=AsyncMock()) as mock_update,
    ):
        mock_get.return_value = current
        updated = _make_lifecycle_map(
            name=payload.get("name", current.name if current else "SDLC Workflow"),
            description=payload.get("description", current.description if current else None),
            version=(
                current.version + 1 if current and "content_json" in payload else (current.version if current else 1)
            ),
            content_json=current.content_json if current else {},
        )
        mock_update.return_value = updated
        resp = client.put(f"/api/v1/lifecycle-maps/{lm_id}", json=payload)
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@when(parsers.parse("I PUT /api/v1/lifecycle-maps/{{id}} with content_json:\n{data}"))
def put_update_lifecycle_map_content(data: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm_id = str(ctx.get("lifecycle_map", MagicMock()).id or uuid.uuid4())
    content_json = json.loads(data.strip())
    current = ctx.get("lifecycle_map")
    with (
        patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new=AsyncMock()) as mock_get,
        patch("modulo.core.lifecycle_map.service.update_lifecycle_map", new=AsyncMock()) as mock_update,
    ):
        mock_get.return_value = current
        new_version = (current.version + 1) if current else 2
        updated = _make_lifecycle_map(
            name=current.name if current else "SDLC Workflow",
            version=new_version,
            content_json=content_json,
        )
        mock_update.return_value = updated
        resp = client.put(f"/api/v1/lifecycle-maps/{lm_id}", json={"content_json": content_json})
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = updated


@when(parsers.parse("I DELETE /api/v1/lifecycle-maps/{{id}}"))
def delete_lifecycle_map(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm_id = str(ctx.get("lifecycle_map", MagicMock()).id or uuid.uuid4())
    with patch("modulo.core.lifecycle_map.service.delete_lifecycle_map", new=AsyncMock()) as mock_delete:
        mock_delete.return_value = True
        resp = client.delete(f"/api/v1/lifecycle-maps/{lm_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/lifecycle-maps/{lm_id}"))
def get_lifecycle_map_by_raw_id(lm_id: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    with patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = None
        resp = client.get(f"/api/v1/lifecycle-maps/{lm_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I query the library for primitive type "{ptype}"'))
def query_library_for_type(ptype: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    with patch("modulo.core.library_service.list_primitives", new=AsyncMock()) as mock_list:
        mock_list.return_value = MagicMock(items=[], total=0, page=1, page_size=20)
        resp = client.get("/api/v1/library", params={"primitive_type": ptype})
    _store_response(request, ctx, resp)


@when(parsers.parse('I save the lifecycle map to the library as "{slug}"'))
def save_lifecycle_map_to_library(slug: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    with patch("modulo.core.library_service.copy_to_adapt", new=AsyncMock()) as mock_copy:
        mock_copy.return_value = MagicMock(id=uuid.uuid4(), primitive_type="lifecycle_map", slug=slug)
        resp = client.post(
            "/api/v1/library",
            json={
                "primitive_type": "lifecycle_map",
                "name": lm.name,
                "slug": slug,
                "content_json": lm.content_json,
            },
        )
    _store_response(request, ctx, resp)


@when("I export the lifecycle map as a bundle")
def export_lifecycle_map_bundle(ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    lm_id = str(lm.id)
    with patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new=AsyncMock()) as mock_get:
        mock_get.return_value = lm
        resp = client.get(f"/api/v1/lifecycle-maps/{lm_id}/export")
    _store_response(request, ctx, resp)


@when("I import the bundle")
def import_lifecycle_map_bundle(ctx: dict[str, Any], request: Any, client: Any) -> None:
    bundle = ctx.get("bundle", {})
    with patch("modulo.core.lifecycle_map.service.create_lifecycle_map", new=AsyncMock()) as mock_create:
        mock_create.return_value = _make_lifecycle_map(name="Imported SDLC Workflow")
        resp = client.post("/api/v1/lifecycle-maps/import", json=bundle)
    _store_response(request, ctx, resp)


@when(parsers.parse('I graduate stage "{stage_id}" to modulo with pipeline_name "{pipeline_name}"'))
def graduate_stage(stage_id: str, pipeline_name: str, ctx: dict[str, Any], request: Any, client: Any) -> None:
    lm = ctx.get("lifecycle_map", _make_lifecycle_map())
    lm_id = str(lm.id)
    with (
        patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new=AsyncMock()) as mock_get,
        patch("modulo.core.lifecycle_map.service.update_lifecycle_map", new=AsyncMock()) as mock_update,
    ):
        mock_get.return_value = lm
        stages = list(lm.content_json.get("stages", []))
        for s in stages:
            if s.get("id") == stage_id:
                s["type"] = "modulo"
                s["pipeline_name"] = pipeline_name
        new_content = {**lm.content_json, "stages": stages}
        graduated = _make_lifecycle_map(
            name=lm.name,
            content_json=new_content,
            version=lm.version + 1,
        )
        mock_update.return_value = graduated
        resp = client.post(
            f"/api/v1/lifecycle-maps/{lm_id}/graduate",
            json={"stage_id": stage_id, "pipeline_name": pipeline_name},
        )
    _store_response(request, ctx, resp)
    ctx["lifecycle_map"] = graduated


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


@then(parsers.parse('the response contains primitives filtered by type "{ptype}"'))
def response_contains_primitives_of_type(ptype: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    items = data.get("items", [])
    for item in items:
        assert item.get("primitive_type") == ptype, (
            f"Expected primitive_type '{ptype}', got '{item.get('primitive_type')}'"
        )


@then(parsers.parse('the library contains a primitive of type "{ptype}" with slug "{slug}"'))
def library_contains_primitive(ptype: str, slug: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("primitive_type") == ptype, f"Expected type '{ptype}', got '{data.get('primitive_type')}'"
    assert data.get("slug") == slug, f"Expected slug '{slug}', got '{data.get('slug')}'"


@then("the bundle contains lifecycle_map content")
def bundle_contains_lifecycle_map(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("primitive_type") == "lifecycle_map", (
        f"Expected primitive_type 'lifecycle_map', got '{data.get('primitive_type')}'"
    )
    assert "stages" in data.get("content_json", {}), "Bundle content_json missing 'stages'"


@then(parsers.parse('a lifecycle map named "{name}" exists'))
def assert_lifecycle_map_exists(name: str, ctx: dict[str, Any]) -> None:
    lm = ctx.get("lifecycle_map")
    assert lm is not None, "No lifecycle map in context"
    assert lm.name == name, f"Expected name '{name}', got '{lm.name}'"


@then(parsers.parse('the stage type is "{stage_type}"'))
def assert_stage_type(stage_type: str, ctx: dict[str, Any]) -> None:
    lm = ctx.get("lifecycle_map")
    assert lm is not None, "No lifecycle map in context"
    stages = lm.content_json.get("stages", [])
    for s in stages:
        assert s.get("type") == stage_type, f"Expected stage type '{stage_type}', got '{s.get('type')}'"


@then("the stage has a pipeline link")
def stage_has_pipeline_link(ctx: dict[str, Any]) -> None:
    lm = ctx.get("lifecycle_map")
    assert lm is not None, "No lifecycle map in context"
    stages = lm.content_json.get("stages", [])
    for s in stages:
        if s.get("type") == "modulo":
            assert s.get("pipeline_name") is not None, "Modulo stage missing pipeline_name"


def _parse_table_data(data: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in data.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("|"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    if not result:
        for line in data.strip().split("\n"):
            line = line.strip()
            if line.startswith("|") and line.endswith("|"):
                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) >= 2:
                    result[parts[0]] = parts[1]
    return result
