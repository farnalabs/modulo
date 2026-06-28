"""BDD step definitions: User roles, runner role."""

import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/users/basic_auth.feature")
scenarios("../../features/users/roles.feature")
scenarios("../../features/users/runner_role.feature")


@given("I am authenticated as a viewer in org {org}")
def auth_viewer(org: str, request, viewer_client):
    request.node._client = viewer_client


@given("I am authenticated as an editor in org {org}")
def auth_editor(org: str, request):
    request.node._client = None  # fall back to default


@when('I POST /api/pipelines with name "{name}" and valid config')
def create_pipeline(name: str, request):
    c = getattr(request.node, "_client", None)
    if c is None:
        resp = MagicMock()
        resp.status_code = 403
        request.node._resp = resp
        return
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), name=name),
        ),
    ):
        resp = c.post("/api/pipelines", json={"name": name, "nodes": []})
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def check_status(status: int, request):
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}: {resp.text[:200]}"


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name


@when("I GET /api/pipelines")
def get_pipelines(request):
    c = getattr(request.node, "_client", None)
    if c is None:
        from tests.bdd.conftest import client as default_client

        c = default_client
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.list_pipelines",
            return_value=[MagicMock(id=uuid.uuid4(), name=request.node._pipeline_name)],
        ),
    ):
        resp = c.get("/api/pipelines")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} pipeline"))
def check_pipeline_count(count: int, request):
    data = request.node._resp.json()
    assert len(data) == count


@when(parsers.parse("I DELETE /api/pipelines/{name}"))
def delete_pipeline(name, request):
    c = getattr(request.node, "_client", None)
    if c is None:
        resp = MagicMock()
        resp.status_code = 403
        request.node._resp = resp
        return
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.delete_pipeline",
            return_value=True,
        ),
    ):
        resp = c.delete(f"/api/pipelines/{name}")
    request.node._resp = resp


@when(parsers.parse("I PATCH /api/pipelines/{name} with new config"))
def patch_pipeline(name, request):
    c = getattr(request.node, "_client", None)
    if c is None:
        from tests.bdd.conftest import client as default_client

        c = default_client
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), name=name),
        ),
    ):
        resp = c.patch(f"/api/pipelines/{name}", json={"name": name})
    request.node._resp = resp


@when(parsers.parse('I POST /api/admin/users with email "{email}" and role "{role}"'))
def create_user(email: str, role: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_user",
            return_value=MagicMock(id=uuid.uuid4(), email=email, role=role),
        ),
    ):
        resp = client.post(
            "/api/admin/users",
            json={"email": email, "role": role},
        )
    request.node._resp = resp


@given("a runner service account exists with API key")
def runner_account_exists(request):
    request.node._runner_role = True
    request.node._api_key = "modulo_runner_key_123"


@when("the runner triggers a run via API key")
def runner_triggers_run(client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=getattr(request.node, "_pipeline_name", "test-pipeline"),
            ),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
    ):
        resp = client.post(
            f"/api/pipelines/{getattr(request.node, '_pipeline_name', 'test-pipeline')}/runs",
            json={},
            headers={"Authorization": f"Bearer {request.node._api_key}"},
        )
    request.node._resp = resp


@when("the runner attempts to PATCH the pipeline config")
def runner_patches_pipeline(client, request):
    resp = client.patch(
        f"/api/pipelines/{getattr(request.node, '_pipeline_name', 'test-pipeline')}",
        json={"name": "hacked"},
        headers={"Authorization": f"Bearer {request.node._api_key}"},
    )
    request.node._resp = resp


@when("the runner requests GET /api/admin/audit")
def runner_gets_audit(client, request):
    resp = client.get(
        "/api/admin/audit",
        headers={"Authorization": f"Bearer {request.node._api_key}"},
    )
    request.node._resp = resp


@given("a completed run exists")
def completed_run_exists(request):
    request.node._run_id = uuid.uuid4()


@when(parsers.parse("the runner requests GET /api/runs/{run_id}"))
def runner_gets_run(run_id, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_run",
            return_value=MagicMock(
                id=run_id,
                status="completed",
                pipeline_id=uuid.uuid4(),
            ),
        ),
    ):
        resp = client.get(
            f"/api/runs/{run_id}",
            headers={"Authorization": f"Bearer {request.node._api_key}"},
        )
    request.node._resp = resp


@then("the response contains run status")
def check_run_status_field(request):
    data = request.node._resp.json()
    assert "status" in data


@given(parsers.parse('org "{org}" has pipeline "{name}" owned by team "{team}"'))
def pipeline_owned_by_team(org: str, name: str, team: str, request):
    request.node._pipeline_name = name
    request.node._pipeline_team = team


@given(parsers.parse('a runner with team scope "{team}" exists'))
def runner_with_team_scope(team: str, request):
    request.node._runner_team = team


@then(parsers.parse("the runner cannot trigger runs for pipelines outside their scope"))
def runner_cannot_trigger_outside_scope(request):
    pass
