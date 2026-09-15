"""BDD step definitions: Deployment metadata endpoint."""

from pytest_bdd import parsers, scenarios, then, when

scenarios("../features/deployment/metadata.feature")


@when("I GET /api/v1/deployment")
def get_deployment_info(client, request):
    # The deployment metadata endpoint is gated with ``system.config.manage``
    # (FAR-880 RBAC sweep), which authenticates the principal (not just the org
    # role). The shared BDD ``client`` fixture provides an org admin without the
    # system-admin claim, so override ``get_current_user`` here — mirroring the
    # pattern in tests/bdd/steps/test_monitor_config.py.
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="sysadmin@test",
        organisation_id=None,
        account_id=None,
        org_role=None,
        is_system_admin=True,
    )
    resp = client.get("/api/v1/deployment")
    request.node._resp = resp
    request.node._body = resp.json()


@then("the response contains deployment metadata fields")
def check_required_fields(request):
    body = request.node._body
    assert "version" in body
    assert "uptime_seconds" in body
    assert "started_at" in body
    assert "python_version" in body
    assert "hostname" in body
    assert "environment" in body
    assert "git_sha" in body
    assert "git_branch" in body
    assert "git_commit_timestamp" in body
    assert "git_commit_message" in body
    assert "build_timestamp" in body
    assert "ci_job_url" in body


@then(parsers.parse('the "{field}" field is a non-empty string'))
def check_non_empty_string(field: str, request):
    body = request.node._body
    assert isinstance(body[field], str)
    assert body[field]


@then(parsers.parse('the "{field}" field is a non-negative integer'))
def check_non_negative_int(field: str, request):
    body = request.node._body
    assert isinstance(body[field], int)
    assert body[field] >= 0


@then(parsers.parse('the "{field}" field is "{value}"'))
def check_field_value(field: str, value: str, request):
    body = request.node._body
    assert body[field] == value


@then("build metadata fields are strings")
def check_build_metadata_types(request):
    body = request.node._body
    for field in (
        "git_sha",
        "git_branch",
        "git_commit_timestamp",
        "git_commit_message",
        "build_timestamp",
        "ci_job_url",
    ):
        assert isinstance(body[field], str), f"{field} should be a string"


@then("git_sha is empty and ci_job_url is empty")
def check_fallback_empty(request):
    body = request.node._body
    assert not body["git_sha"]
    assert not body["ci_job_url"]
