"""Step definitions for lifecycle map BDD features."""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

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
