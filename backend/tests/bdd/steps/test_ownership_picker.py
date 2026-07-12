"""Step definitions for ownership picker feature — team-scoped resource visibility."""

import contextlib
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/ownership_picker.feature")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_url(url: str) -> str:
    return url.replace("/api/", "/api/v1/")


def _make_mock_stage(**kwargs: Any) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", uuid.uuid4())
    s.organisation_id = kwargs.get("org_id", ORG_ID)
    s.name = kwargs.get("name", "Test Stage")
    s.description = kwargs.get("description")
    s.position = kwargs.get("position", 0)
    s.owner_team_id = kwargs.get("owner_team_id")
    s.visibility = kwargs.get("visibility", "org")
    s.created_by = kwargs.get("created_by", uuid.uuid4())
    s.created_at = kwargs.get("created_at", datetime.now())
    s.updated_at = kwargs.get("updated_at", datetime.now())
    return s


def _store_response(request: pytest.FixtureRequest, resp: Any) -> None:
    request.node._resp = resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        with contextlib.suppress(RuntimeError):
            p.stop()


@pytest.fixture
def ctx():
    return {}


# ---------------------------------------------------------------------------
# GIVEN
# ---------------------------------------------------------------------------


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["team_id"] = str(uuid.uuid4())
    ctx["team_name"] = team_name


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    ctx["member_user_id"] = str(uuid.uuid4())
    ctx["member_username"] = username


@given(parsers.parse('user "{username}" is not a member of team "{team_name}"'))
def user_not_member(username: str, team_name: str, ctx) -> None:
    ctx["non_member_user_id"] = str(uuid.uuid4())
    ctx["non_member_username"] = username


@given(parsers.parse('stage "{name}" is owned by team "{team_name}"'))
def stage_owned_by_team(name: str, team_name: str, ctx, request: pytest.FixtureRequest) -> None:
    team_id = uuid.UUID(ctx.get("team_id", str(uuid.uuid4())))
    mock_stage = _make_mock_stage(name=name, owner_team_id=team_id, visibility="team")
    request.node._mock_stage = mock_stage
    ctx["stage_id"] = str(mock_stage.id)
    ctx["stage_name"] = name


# ---------------------------------------------------------------------------
# WHEN
# ---------------------------------------------------------------------------


@when(parsers.parse('I POST /api/stages with name "{name}" and org visibility'))
def create_stage_org_visibility(name: str, request: pytest.FixtureRequest, client, patches) -> None:
    actual_url = _map_url("/api/stages")

    patcher = patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)

    mock_stage = _make_mock_stage(name=name, visibility="org", owner_team_id=None)
    patcher = patch(
        "modulo.api.routes.stages.create_stage",
        new_callable=AsyncMock,
        return_value=mock_stage,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.post(actual_url, json={"name": name, "visibility": "org"})
    _store_response(request, resp)
    request.node._created_stage = mock_stage


@when(parsers.parse('I POST /api/stages with name "{name}" owned by team "{team_name}"'))
def create_stage_team_visibility(
    name: str,
    team_name: str,
    request: pytest.FixtureRequest,
    client,
    patches,
    ctx,
) -> None:
    actual_url = _map_url("/api/stages")
    team_id = uuid.UUID(ctx["team_id"])

    patcher = patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)

    mock_stage = _make_mock_stage(name=name, owner_team_id=team_id, visibility="team")
    patcher = patch(
        "modulo.api.routes.stages.create_stage",
        new_callable=AsyncMock,
        return_value=mock_stage,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.post(actual_url, json={"name": name, "owner_team_id": str(team_id), "visibility": "team"})
    _store_response(request, resp)
    request.node._created_stage = mock_stage


@when(parsers.parse('I POST /api/stages with name "{name}" and no visibility'))
def create_stage_no_visibility(name: str, request: pytest.FixtureRequest, client, patches) -> None:
    actual_url = _map_url("/api/stages")

    patcher = patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)

    mock_stage = _make_mock_stage(name=name, visibility="org", owner_team_id=None)
    patcher = patch(
        "modulo.api.routes.stages.create_stage",
        new_callable=AsyncMock,
        return_value=mock_stage,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.post(actual_url, json={"name": name})
    _store_response(request, resp)
    request.node._created_stage = mock_stage


@when(parsers.parse("I GET /api/stages/{name}"))
def get_stage_by_name(name: str, request: pytest.FixtureRequest, client, patches, ctx) -> None:
    mock_stage = getattr(request.node, "_mock_stage", None) or getattr(request.node, "_created_stage", None)
    stage_id = ctx.get("stage_id") or (str(mock_stage.id) if mock_stage else name)
    actual_url = _map_url(f"/api/stages/{stage_id}")

    patcher = patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)
    if mock_stage is not None:
        patcher = patch(
            "modulo.api.routes.stages.get_stage",
            new_callable=AsyncMock,
            return_value=mock_stage,
        )
        patcher.start()
        patches.append(patcher)
        resp = client.get(actual_url)
    else:
        patcher = patch(
            "modulo.api.routes.stages.get_stage",
            new_callable=AsyncMock,
            return_value=None,
        )
        patcher.start()
        patches.append(patcher)
        resp = client.get(actual_url)

    _store_response(request, resp)


@when(parsers.parse('user "{username}" requests GET /api/stages/{stage_name}'))
def user_requests_get_stage(username: str, stage_name: str, request: pytest.FixtureRequest, ctx) -> None:
    from fastapi.testclient import TestClient

    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings
    from tests.bdd.conftest import make_mock_session, make_settings

    mock_session = make_mock_session()

    async def override_session():
        yield mock_session

    stage_id = ctx.get("stage_id", str(uuid.uuid4()))
    team_id = uuid.UUID(ctx.get("team_id", str(uuid.uuid4())))
    mock_stage = _make_mock_stage(
        id=uuid.UUID(stage_id),
        name=ctx.get("stage_name", stage_name),
        owner_team_id=team_id,
        visibility="team",
    )

    # Determine if this user is a member or not
    is_member = username == ctx.get("member_username", "")
    user_id = (
        uuid.UUID(ctx.get("member_user_id", str(uuid.uuid4())))
        if is_member
        else uuid.UUID(ctx.get("non_member_user_id", str(uuid.uuid4())))
    )

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=username,
        organisation_id=ORG_ID,
        account_id=user_id,
        org_role="operator",
    )

    client = TestClient(app)

    with (
        patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
    ):
        if is_member:
            with patch(
                "modulo.api.routes.stages.get_stage",
                new_callable=AsyncMock,
                return_value=mock_stage,
            ):
                resp = client.get(f"/api/v1/stages/{stage_id}")
        else:
            with patch(
                "modulo.api.routes.stages.get_stage",
                new_callable=AsyncMock,
                return_value=None,
            ):
                resp = client.get(f"/api/v1/stages/{stage_id}")

    app.dependency_overrides.clear()
    _store_response(request, resp)


@when(parsers.parse('I am authenticated as an admin in org "{org}"'))
def admin_auth_in_org(org: str) -> None:
    pass


# ---------------------------------------------------------------------------
# THEN
# ---------------------------------------------------------------------------


@then('the response visibility is "org"')
def response_visibility_org(request) -> None:
    body = request.node._resp.json()
    assert body.get("visibility") == "org", f"Expected visibility=org, got {body}"


@then('the response visibility is "team"')
def response_visibility_team(request) -> None:
    body = request.node._resp.json()
    assert body.get("visibility") == "team", f"Expected visibility=team, got {body}"


@then("the response owner_team_id is null")
def response_owner_team_id_null(request) -> None:
    body = request.node._resp.json()
    assert body.get("owner_team_id") is None, f"Expected owner_team_id=null, got {body}"


@then("the response owner_team_id is set")
def response_owner_team_id_set(request) -> None:
    body = request.node._resp.json()
    assert body.get("owner_team_id") is not None, f"Expected owner_team_id to be non-null, got {body}"


@then("the response owner_team_id matches the owning team")
def response_owner_team_id_matches(request, ctx) -> None:
    body = request.node._resp.json()
    expected = ctx.get("team_id")
    assert body.get("owner_team_id") == expected, f"Expected owner_team_id={expected}, got {body.get('owner_team_id')}"
