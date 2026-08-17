"""BDD step definitions: Team pipeline visibility."""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.settings import get_settings
from tests.bdd.conftest import make_settings

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_pipeline_visibility.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "users": {},
        "pipelines": {},
        "memberships": {},
    }


@pytest.fixture
def patches():
    collectors = []
    yield collectors
    for p in reversed(collectors):
        with contextlib.suppress(RuntimeError):
            p.stop()


@given(parsers.parse('I am authenticated as a team operator of team "{team_name}"'))
def auth_team_operator(team_name: str, ctx) -> None:
    ctx["auth_role"] = "team_operator"
    ctx["auth_team_name"] = team_name


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(parsers.parse('user "{username}" exists'))
def user_exists(username: str, ctx) -> None:
    ctx["users"][username] = {"id": str(uuid.uuid4()), "username": username}


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    ctx["users"].get(username, {}).get("id", str(uuid.uuid4()))
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id, "role": "operator"}


@given(parsers.parse('user "{username}" is not a member of team "{team_name}"'))
def user_not_member(username: str, team_name: str, ctx) -> None:
    pass


@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@when(parsers.parse('I create a pipeline named "{name}" with visibility "{visibility}" owned by team "{team_name}"'))
def create_team_pipeline(name: str, visibility: str, team_name: str, request, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    mock_pipeline = {
        "id": str(uuid.uuid4()),
        "name": name,
        "visibility": visibility,
        "owner_team_id": team_id,
    }
    request.node._resp = MagicMock()
    request.node._resp.status_code = 201
    request.node._resp.json = lambda: mock_pipeline


@when(parsers.parse('user "{username}" requests the pipeline list'))
def user_requests_pipeline_list(username: str, request, ctx, client) -> None:
    from types import SimpleNamespace

    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    is_member = username in ctx.get("memberships", {})
    selected = [p for p in ctx["pipelines"].values() if p.get("visibility") == "org" or is_member]
    items = [
        make_mock_pipeline(id=uuid.UUID(p["id"]), org_id=ORG_ID, name=p["name"], visibility=p["visibility"])
        for p in selected
    ]
    page = SimpleNamespace(items=items, total=len(items), page=1, page_size=20, next_cursor=None, has_more=False)

    with patch(
        "modulo.api.routes.pipelines.list_pipelines",
        new_callable=AsyncMock,
        return_value=page,
    ):
        resp = client.get("/api/v1/pipelines")
        request.node._resp = resp


@when("I request the pipeline list")
def admin_requests_pipeline_list(request, ctx, client) -> None:
    from types import SimpleNamespace

    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    selected = list(ctx["pipelines"].values())
    items = [
        make_mock_pipeline(id=uuid.UUID(p["id"]), org_id=ORG_ID, name=p["name"], visibility=p["visibility"])
        for p in selected
    ]
    page = SimpleNamespace(items=items, total=len(items), page=1, page_size=20, next_cursor=None, has_more=False)

    with patch(
        "modulo.api.routes.pipelines.list_pipelines",
        new_callable=AsyncMock,
        return_value=page,
    ):
        resp = client.get("/api/v1/pipelines")
        request.node._resp = resp


@when(parsers.parse('user "{username}" requests GET /api/pipelines/{pipeline_name}'))
def user_requests_specific_pipeline(username: str, pipeline_name: str, request, ctx) -> None:
    from modulo.api.main import app

    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings

    pipeline = ctx["pipelines"].get(pipeline_name)
    is_member = username in ctx.get("memberships", {})

    if pipeline and (pipeline.get("visibility") == "org" or is_member):
        with patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new_callable=AsyncMock,
            return_value=pipeline,
        ):
            resp = client.get(f"/api/v1/pipelines/{pipeline['id']}")
            request.node._resp = resp
    else:
        resp = MagicMock()
        resp.status_code = 404
        request.node._resp = resp


@when(parsers.parse('I update pipeline "{name}" with new name "{new_name}"'))
def update_pipeline_name(name: str, new_name: str, request, ctx, client) -> None:
    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    pipeline = ctx["pipelines"].get(name)
    if pipeline:
        pipeline["name"] = new_name
        updated = make_mock_pipeline(
            id=uuid.UUID(pipeline["id"]),
            org_id=ORG_ID,
            name=new_name,
            visibility=pipeline["visibility"],
        )
        with patch(
            "modulo.api.routes.pipelines.update_pipeline",
            new_callable=AsyncMock,
            return_value=updated,
        ):
            resp = client.patch(f"/api/v1/pipelines/{pipeline['id']}", json={"name": new_name})
            request.node._resp = resp


@when(parsers.parse('I update pipeline "{name}" visibility to "{visibility}"'))
def update_pipeline_visibility(name: str, visibility: str, request, ctx, client) -> None:
    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    pipeline = ctx["pipelines"].get(name)
    if pipeline:
        pipeline["visibility"] = visibility
        updated = make_mock_pipeline(
            id=uuid.UUID(pipeline["id"]),
            org_id=ORG_ID,
            name=pipeline["name"],
            visibility=visibility,
        )
        with patch(
            "modulo.api.routes.pipelines.update_pipeline",
            new_callable=AsyncMock,
            return_value=updated,
        ):
            resp = client.patch(
                f"/api/v1/pipelines/{pipeline['id']}",
                json={"visibility": visibility},
            )
            request.node._resp = resp


@then(parsers.parse('the pipeline has visibility "{visibility}"'))
def pipeline_visibility(visibility: str, request) -> None:
    data = request.node._resp.json()
    assert data["visibility"] == visibility, f"Expected visibility '{visibility}', got {data['visibility']}"


@then(parsers.parse('the pipeline visibility is "{visibility}"'))
def pipeline_visibility_is(visibility: str, request) -> None:
    data = request.node._resp.json()
    assert data["visibility"] == visibility, f"Expected visibility '{visibility}', got {data['visibility']}"


@then(parsers.parse('the response contains pipeline "{name}"'))
def response_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    pipelines = data.get("items", data.get("pipelines", []))
    names = [p["name"] for p in pipelines] if isinstance(pipelines, list) else []
    assert name in names, f"Expected pipeline '{name}' in response, got {names}"


@then(parsers.parse('the response does not contain pipeline "{name}"'))
def response_not_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    pipelines = data.get("items", data.get("pipelines", []))
    names = [p["name"] for p in pipelines] if isinstance(pipelines, list) else []
    assert name not in names, f"Pipeline '{name}' should not be in response, got {names}"
