"""BDD step definitions: Admin override of team restrictions."""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_anonymous_plan_context, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import get_settings
from tests.bdd.conftest import _make_mock_pipeline_full, _shared_state, make_mock_session, make_settings

scenarios("../features/teams/admin_override.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_MISSING = object()


class _AllFeatures:
    """Plan context standing in for the shared ``client`` fixture's override."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


async def _all_features_plan_context() -> _AllFeatures:
    return _AllFeatures()


@contextmanager
def _session_client(role: str, shaper: Callable[[MagicMock], None] | None = None) -> Iterator[TestClient]:
    """A TestClient driving the REAL routes with a stubbed session.

    ``require_permission`` / ``require_team_membership_or_admin`` and the route
    bodies run unpatched; only the DB seams are stubbed. Overrides installed
    here are snapshotted and restored so the shared ``client`` fixture keeps
    working after the step.
    """
    mock_session: MagicMock = make_mock_session()  # type: ignore[assignment]
    if shaper is not None:
        shaper(mock_session)

    async def override_session() -> AsyncMock:
        yield mock_session

    principal_kwargs = {
        "username": role,
        "organisation_id": _ORG_ID,
        "account_id": uuid.uuid4(),
        "org_role": role,
    }
    overrides = {
        get_settings: make_settings,  # type: ignore[dict-item]
        get_db_session: override_session,
        _get_engine: lambda: MagicMock(),
        get_current_user: lambda: AuthenticatedPrincipal(**principal_kwargs),
        get_current_tenant_user: lambda: TenantPrincipal(**principal_kwargs),
        get_plan_context: _all_features_plan_context,
        get_anonymous_plan_context: _all_features_plan_context,
    }
    saved = {key: app.dependency_overrides.get(key, _MISSING) for key in overrides}
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        for key, value in saved.items():
            if value is _MISSING:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value


def _caller_role(request: pytest.FixtureRequest) -> str:
    """Org role for 'I ...' steps: viewer when the viewer-auth given ran, else admin."""
    return _shared_state(request).get("org_role", "admin")


def _pipeline_row(pdata: dict | None) -> MagicMock:
    """A Pipeline ORM double matching PipelineResponse validation for a ctx entry."""
    if pdata is None:
        return None
    row = _make_mock_pipeline_full(
        name=pdata["name"],
        visibility=pdata.get("visibility", "org"),
    )
    row.id = uuid.UUID(pdata["id"]) if pdata.get("id") else row.id
    row.owner_team_id = uuid.UUID(pdata["owner_team_id"]) if pdata.get("owner_team_id") else None
    row.graph_nodes_json = []
    return row


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "pipelines": {},
        "connectors": {},
    }


@given(parsers.parse('team "{name}" exists'))
def team_exists(name: str, ctx) -> None:
    ctx["teams"][name] = {"id": str(uuid.uuid4()), "name": name}


@given(parsers.parse('pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@then(parsers.parse('the pipeline owner is team "{team_name}"'))
def pipeline_owner_is_team(team_name: str, request, ctx) -> None:
    resp = request.node._resp
    data = resp.json() if hasattr(resp, "json") else {}
    team_id = ctx["teams"].get(team_name, {}).get("id")
    assert data.get("owner_team_id") == team_id, f"Expected owner_team_id {team_id}, got {data.get('owner_team_id')}"


@given(parsers.parse('connector "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def connector_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["connectors"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@when(parsers.parse("I request the pipeline list"))
def request_pipeline_list(request, ctx) -> None:
    rows = [_pipeline_row(pdata) for pdata in ctx.get("pipelines", {}).values()]
    with (
        _session_client(_caller_role(request)) as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.pipelines.list_pipelines",
            new_callable=AsyncMock,
            return_value=PageResult(items=rows, total=len(rows), page=1, page_size=20),
        ),
    ):
        resp = client.get("/api/v1/pipelines")
    request.node._resp = resp


@when(parsers.parse("I request GET /api/connectors/{connector_name}"))
def request_connector(connector_name: str, request, ctx) -> None:
    """GET the connector route for real; the row double carries the owner/visibility."""
    pdata = ctx.get("connectors", {}).get(connector_name)
    ci = MagicMock()
    ci.id = uuid.UUID(pdata["id"]) if pdata else uuid.uuid4()
    ci.organisation_id = _ORG_ID
    ci.name = connector_name
    ci.connector_type_id = "rest"
    ci.credentials_ciphertext = b"gAAAAAB"
    ci.config_json = {}
    ci.allowed_operations = []
    ci.status = "active"
    ci.visibility = (pdata or {}).get("visibility", "org")
    ci.owner_team_id = uuid.UUID(pdata["owner_team_id"]) if pdata and pdata.get("owner_team_id") else None
    ci.tier = "native"
    now = datetime.now(UTC)
    ci.created_at = now
    ci.updated_at = now
    ci.last_skip_error = None
    ci.validation_level = "standard"
    ci.degraded_at = None

    with (
        _session_client(_caller_role(request)) as client,
        patch("modulo.api.routes.connectors.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.connectors.get_connector_instance", new_callable=AsyncMock, return_value=ci),
    ):
        resp = client.get(f"/api/v1/connectors/{ci.id}")
    request.node._resp = resp


@when(parsers.parse('I delete pipeline "{name}"'))
def delete_pipeline(name: str, request, ctx) -> None:
    pdata = ctx.get("pipelines", {}).get(name)
    pipeline_id = uuid.UUID(pdata["id"]) if pdata else uuid.uuid4()
    with (
        _session_client(_caller_role(request)) as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.pipelines.soft_delete_pipeline",
            new_callable=AsyncMock,
            return_value=pdata is not None,
        ),
    ):
        resp = client.delete(f"/api/v1/pipelines/{pipeline_id}")
    request.node._resp = resp


@when(parsers.parse('I reassign pipeline "{pipeline_name}" to team "{team_name}"'))
def reassign_pipeline(pipeline_name: str, team_name: str, request, ctx) -> None:
    pdata = ctx.get("pipelines", {}).get(pipeline_name)
    team = ctx.get("teams", {}).get(team_name)
    if team is None:
        raise AssertionError(f"team '{team_name}' was not given; the real route cannot be driven")
    new_team_id = team["id"]
    updated = _pipeline_row(
        {"name": pipeline_name, "owner_team_id": new_team_id, "visibility": (pdata or {}).get("visibility", "team")}
    )
    with (
        _session_client(_caller_role(request)) as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=_pipeline_row(pdata)),
        patch("modulo.api.routes.pipelines.update_pipeline", new_callable=AsyncMock, return_value=updated),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{uuid.UUID(pdata['id']) if pdata else uuid.uuid4()}",
            json={"owner_team_id": new_team_id},
        )
    request.node._resp = resp


@when(parsers.parse('I bulk reassign all resources from team "{team_name}" to org-wide'))
def bulk_reassign(team_name: str, request, ctx) -> None:
    """POST the real bulk-reassign route; the CRUD seam mutates the ctx rows.

    The seam stub reproduces ``reassign_team_resources_to_org``'s row-write
    contract (org-wide the owned rows), and the Then below observes that
    post-write state from the real JSON body contract.
    """
    team = ctx.get("teams", {}).get(team_name)
    if team is None:
        raise AssertionError(f"team '{team_name}' was not given; the real route cannot be driven")

    def _reassign(
        _session: object,
        *,
        org_id: uuid.UUID,
        team_id: uuid.UUID,
    ) -> tuple[int, list[str]]:
        reassigned = 0
        for pdata in ctx.get("pipelines", {}).values():
            if pdata.get("owner_team_id") == str(team_id):
                pdata["owner_team_id"] = None
                pdata["visibility"] = "org"
                reassigned += 1
        return reassigned, ["pipeline"] if reassigned else []

    team_row = MagicMock()
    team_row.id = uuid.UUID(team["id"])
    team_row.organisation_id = _ORG_ID
    with (
        _session_client(_caller_role(request)) as client,
        patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.admin.get_team", new_callable=AsyncMock, return_value=team_row),
        patch(
            "modulo.api.routes.admin.reassign_team_resources_to_org",
            new_callable=AsyncMock,
            side_effect=_reassign,
        ),
    ):
        resp = client.post(f"/api/v1/admin/teams/{team['id']}/reassign-all")
    request.node._resp = resp


@when(parsers.parse("I request GET /api/pipelines/{pipeline_name}"))
def request_pipeline(pipeline_name: str, request, ctx) -> None:
    """GET the single-pipeline route for real.

    The admin case returns the row double (200); the viewer case the
    team-scope resolver already hides the row under real RLS, so the
    resolution seam is stubbed to None — the route's own not-found branch
    (404) then runs.
    """
    role = _caller_role(request)
    pdata = ctx.get("pipelines", {}).get(pipeline_name)
    pipeline_id = uuid.UUID(pdata["id"]) if pdata else uuid.uuid4()
    with (
        _session_client(role) as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new_callable=AsyncMock,
            return_value=_pipeline_row(pdata) if role == "admin" else None,
        ),
    ):
        resp = client.get(f"/api/v1/pipelines/{pipeline_id}")
    request.node._resp = resp


@then(parsers.parse('the response contains pipeline "{name}"'))
def response_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", [])
    names = [p["name"] for p in items] if isinstance(items, list) else []
    assert name in names, f"Expected pipeline '{name}' in response, got {names}"


@then(parsers.parse('pipeline "{name}" has owner_team_id null'))
def pipeline_owner_team_id_null(name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(name)
    assert pipeline is not None
    assert pipeline.get("owner_team_id") is None
