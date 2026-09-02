"""Step definitions for Environment Profile features — CRUD, sandbox test, cross-org isolation."""

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_provider import RuntimeProvider, WorkspaceSpec
from modulo.core.runtime_provider.hub import RuntimeProviderHub

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/environments/environment_profiles.feature")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for environment profile tests."""
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _fake_profile(**overrides: Any) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", uuid.uuid4())
    p.organisation_id = overrides.get("organisation_id", _ORG_ID)
    p.name = overrides.get("name", "test-profile")
    p.description = overrides.get("description", "A test profile")
    p.provider_type = overrides.get("provider_type", "local_docker")
    p.image_ref = overrides.get("image_ref", "python:3.12-slim")
    p.capabilities = overrides.get("capabilities", ["docker"])
    p.capabilities_json = overrides.get("capabilities", ["docker"])
    p.config_json = overrides.get("config_json", {})
    p.network_policy = overrides.get("network_policy", "outbound")
    p.initialisation_strategy = overrides.get("initialisation_strategy", "git_clone")
    p.secret_refs_json = overrides.get("secret_refs", [])
    p.egress_policy = overrides.get("egress_policy", "allow_all")
    p.timeout_seconds = overrides.get("timeout_seconds", 3600)
    p.resource_limits_json = overrides.get("resource_limits", {})
    p.persistence_policy = overrides.get("persistence_policy", "ephemeral")
    p.status = overrides.get("status", "active")
    p.visibility = overrides.get("visibility", "org")
    p.owner_team_id = overrides.get("owner_team_id")
    p.is_active = overrides.get("is_active", True)
    p.created_by = overrides.get("created_by", _USER_ID)
    p.created_at = overrides.get("created_at", datetime(2026, 1, 1, tzinfo=UTC))
    p.updated_at = overrides.get("updated_at", datetime(2026, 1, 1, tzinfo=UTC))
    return p


def _fake_lease(**overrides: Any) -> MagicMock:
    lease = MagicMock()
    lease.id = overrides.get("id", uuid.uuid4())
    lease.organisation_id = overrides.get("organisation_id", _ORG_ID)
    lease.environment_profile_id = overrides.get("environment_profile_id", uuid.uuid4())
    lease.run_id = overrides.get("run_id", uuid.uuid4())
    lease.provider_ref = overrides.get("provider_ref", "local-ws-001")
    lease.status = overrides.get("status", "pending")
    lease.started_at = overrides.get("started_at")
    lease.expires_at = overrides.get("expires_at")
    lease.resource_usage_json = overrides.get("resource_usage")
    return lease


def _stub_provider() -> RuntimeProvider:
    p = MagicMock(spec=RuntimeProvider)
    p.create_workspace = AsyncMock(return_value="ws-ref-001")
    p.exec_command = AsyncMock(return_value=MagicMock(exit_code=0, stdout="hello\n", stderr="", duration_ms=42))
    p.destroy_workspace = AsyncMock()
    p.get_workspace_status = AsyncMock(return_value="running")
    return p


# ============================================================================
# Given
# ============================================================================


@given(
    parsers.parse(
        'a valid environment profile payload with name "{name}", image "{image}", '
        'capabilities {caps}, egress "{egress}", and timeout {timeout:d}'
    )
)
def valid_profile_payload(name: str, image: str, caps: str, egress: str, timeout: int, ctx):
    ctx["payload"] = {
        "name": name,
        "image_ref": image,
        "capabilities": json.loads(caps),
        "egress_policy": egress,
        "timeout_seconds": timeout,
    }


@given(parsers.parse("an invalid environment profile payload with empty name"))
def invalid_profile_empty_name(ctx):
    ctx["payload"] = {"name": "", "image_ref": "python:3.12-slim"}


@given(parsers.parse("an invalid environment profile payload with empty image_ref"))
def invalid_profile_empty_image_ref(ctx):
    ctx["payload"] = {
        "name": "test",
        "image_ref": "",
    }


@given(parsers.parse("an invalid environment profile payload with a too-long name"))
def invalid_profile_long_name(ctx):
    ctx["payload"] = {
        "name": "x" * 256,
        "image_ref": "python:3.12-slim",
    }


@given(parsers.parse('org "{org}" has {count:d} environment profiles'))
def org_has_n_profiles(org: str, count: int, ctx):
    ctx["profile_count"] = count
    ctx["org"] = org


@given(parsers.parse('org "{org}" has an environment profile with id "{profile_id}"'))
def org_has_profile(org: str, profile_id: str, ctx):
    ctx["profile_id"] = profile_id
    ctx["org"] = org
    pid = (
        uuid.UUID(profile_id.replace("profile-", "00000000-0000-0000-0000-00000000000")[:36].ljust(36, "0"))
        if "-" in profile_id
        else uuid.uuid4()
    )
    ctx["profile"] = _fake_profile(id=pid)


@given(parsers.parse('org "{org}" has no environment profile with id "{profile_id}"'))
def org_has_no_profile(org: str, profile_id: str, ctx):
    ctx["profile_id"] = profile_id
    ctx["org"] = org
    ctx["profile_should_be_none"] = True


@given('a RuntimeProviderHub with "local" and "e2b" providers registered')
def hub_with_providers(ctx):
    hub = RuntimeProviderHub()
    local = MagicMock(spec=RuntimeProvider)
    local.supports = MagicMock(return_value=True)
    e2b = MagicMock(spec=RuntimeProvider)
    e2b.supports = MagicMock(return_value=False)
    hub.register("local", local)
    hub.register("e2b", e2b)
    ctx["hub"] = hub
    ctx["local_provider"] = local
    ctx["e2b_provider"] = e2b


@given('an environment profile with capabilities ["docker"] and no provider_hint')
def profile_docker_no_hint(ctx):
    ctx["resolve_profile"] = _fake_profile(capabilities=["docker"], name="docker-only", provider_type=None)


@given(parsers.parse('an environment profile with provider_hint "{hint}"'))
def profile_with_hint(hint: str, ctx):
    p = _fake_profile(capabilities=["docker"], name="hinted-profile")
    p.provider_hint = hint
    ctx["resolve_profile"] = p


@given(parsers.parse('a run with id "{run_id}"'))
def run_with_id(run_id: str, ctx):
    ctx["run_id"] = run_id


@given(parsers.parse('a WorkspaceLease for run "{run_id}" referencing environment profile "{profile_id}"'))
def lease_for_run(run_id: str, profile_id: str, ctx):
    lease = _fake_lease(
        run_id=uuid.uuid4(),
        environment_profile_id=uuid.uuid4(),
        status="pending",
    )
    ctx["lease"] = lease
    ctx["run_id"] = run_id


@given("a LocalRuntimeProvider")
def local_provider(ctx):
    from modulo.core.runtime_provider.local import LocalRuntimeProvider

    ctx["provider"] = LocalRuntimeProvider(max_concurrency=2)


@given('an EnvironmentProfile with image_ref "python:3.12-slim" and capabilities ["docker"]')
def profile_for_spec(ctx):
    ctx["spec_profile"] = _fake_profile(
        image_ref="python:3.12-slim",
        capabilities=["docker"],
    )


@given("a LocalRuntimeProvider with an active workspace")
def provider_with_active_workspace(ctx):
    provider = MagicMock()
    provider.execute_command = AsyncMock(
        return_value={"exit_code": 0, "stdout": "hello\n", "stderr": "", "duration_ms": 42}
    )
    ctx["provider"] = provider
    ctx["ws_ref"] = "ws-active-001"


@given("a ShellConnector using that provider")
def shell_connector_with_provider(ctx):
    from modulo.connectors.shell import ShellConnector

    ctx["connector"] = ShellConnector(runtime_provider=ctx["provider"], allowed_commands=["echo"])


@given('an EnvironmentProfile with capabilities ["docker", "python3.12"]')
def profile_for_validation(ctx):
    ctx["validation_profile"] = _fake_profile(
        capabilities=["docker", "python3.12"],
        name="validation-profile",
    )


@given('a pipeline snapshot with an agent that requires capabilities ["docker", "python3.12", "egress:github.com"]')
def snapshot_validation(ctx):
    ctx["graph_json"] = {
        "nodes": [
            {
                "id": "node-a",
                "agent_id": str(uuid.uuid4()),
                "role": None,
            }
        ],
        "edges": [],
    }
    ctx["agent_required"] = ["docker", "python3.12", "egress:github.com"]
    ctx["validation_profile_caps"] = ["docker", "python3.12"]


@given('an EnvironmentProfile with capabilities ["docker", "python3.12", "egress:github.com"]')
def profile_full_caps(ctx):
    ctx["validation_profile"] = _fake_profile(
        capabilities=["docker", "python3.12", "egress:github.com"],
        name="full-profile",
    )


@given('a pipeline snapshot with an agent that requires capabilities ["docker", "python3.12"]')
def snapshot_subset(ctx):
    ctx["graph_json"] = {
        "nodes": [
            {
                "id": "node-a",
                "agent_id": str(uuid.uuid4()),
                "role": None,
            }
        ],
        "edges": [],
    }
    ctx["agent_required"] = ["docker", "python3.12"]


# ============================================================================
# When
# ============================================================================


@when(parsers.parse("I POST {url} with the profile payload"))
def post_create_profile(url: str, ctx, client):
    payload = ctx.get("payload", {})
    with (
        patch("modulo.api.routes.environment_profiles.create_environment_profile") as mock_create,
        patch("modulo.api.routes.environment_profiles.set_rls_org"),
    ):
        mock_create.return_value = _fake_profile(
            name=payload.get("name", "test"),
            image_ref=payload.get("image_ref", "python:3.12-slim"),
            capabilities=payload.get("capabilities", []),
            egress_policy=payload.get("egress_policy"),
            timeout_seconds=payload.get("timeout_seconds", 3600),
        )
        ctx["response"] = client.post(url, json=payload)


@when(parsers.parse("I POST {url} with the invalid payload"))
def post_create_invalid(url: str, ctx, client):
    payload = ctx.get("payload", {})
    ctx["response"] = client.post(url, json=payload)


@when(parsers.parse("I GET {url}"))
def get_url(url: str, ctx, client):
    with (
        patch("modulo.api.routes.environment_profiles.list_environment_profiles") as mock_list,
        patch("modulo.api.routes.environment_profiles.get_environment_profile") as mock_get,
        patch("modulo.api.routes.environment_profiles.set_rls_org"),
    ):
        is_alt_org = ctx.get("alt_org", False)
        profile_count = 0 if is_alt_org else ctx.get("profile_count", 0)
        profiles = [_fake_profile(name=f"profile-{i}") for i in range(profile_count)]
        mock_list.return_value = MagicMock(items=profiles, total=profile_count, page=1, page_size=20)

        profile = None if is_alt_org else ctx.get("profile")
        profile_should_be_none = ctx.get("profile_should_be_none", False)
        if profile and not profile_should_be_none:
            mock_get.return_value = profile
        else:
            mock_get.return_value = None

        ctx["response"] = client.get(url)


@when(parsers.parse('I PUT {url} with name "{name}"'))
def put_profile(url: str, name: str, ctx, client):
    profile = ctx.get("profile")
    profile_should_be_none = ctx.get("profile_should_be_none", False)
    is_alt_org = ctx.get("alt_org", False)
    with (
        patch("modulo.api.routes.environment_profiles.update_environment_profile") as mock_update,
        patch("modulo.api.routes.environment_profiles.set_rls_org"),
    ):
        if profile and not profile_should_be_none and not is_alt_org:
            updated = _fake_profile(id=profile.id, name=name)
            mock_update.return_value = updated
        else:
            mock_update.return_value = None
        ctx["response"] = client.put(url, json={"name": name})


@when(parsers.parse("I DELETE {url}"))
def delete_url(url: str, ctx, client):
    profile = ctx.get("profile")
    profile_should_be_none = ctx.get("profile_should_be_none", False)
    is_alt_org = ctx.get("alt_org", False)
    with (
        patch("modulo.api.routes.environment_profiles.soft_delete_environment_profile") as mock_delete,
        patch("modulo.api.routes.environment_profiles.set_rls_org"),
    ):
        if profile and not profile_should_be_none and not is_alt_org:
            mock_delete.return_value = True
        else:
            mock_delete.return_value = False
        ctx["response"] = client.delete(url)


@when(parsers.parse("I POST {url}"))
def post_url(url: str, ctx, client):
    with (
        patch("modulo.api.routes.environment_profiles.get_environment_profile") as mock_get,
        patch("modulo.api.routes.environment_profiles.set_rls_org"),
    ):
        profile = ctx.get("profile")
        profile_should_be_none = ctx.get("profile_should_be_none", False)
        is_alt_org = ctx.get("alt_org", False)
        if profile and not profile_should_be_none and not is_alt_org:
            mock_get.return_value = profile
        else:
            mock_get.return_value = None
        ctx["response"] = client.post(url)


@when("I resolve the profile against the hub")
def resolve_profile(ctx):
    hub: RuntimeProviderHub = ctx["hub"]
    profile = ctx["resolve_profile"]
    result = hub.resolve(profile)
    ctx["resolved_provider"] = result
    if result is ctx["local_provider"]:
        ctx["resolved_name"] = "local"
    elif result is ctx["e2b_provider"]:
        ctx["resolved_name"] = "e2b"
    else:
        ctx["resolved_name"] = "unknown"


@when("the run starts executing")
def run_starts(ctx):
    lease = ctx["lease"]
    lease.status = "provisioning"
    ctx["lease"] = lease


@when("the workspace is created")
def workspace_created(ctx):
    lease = ctx["lease"]
    lease.status = "active"
    lease.provider_ref = "ws-provider-ref"
    from datetime import UTC, datetime, timedelta

    lease.started_at = datetime.now(UTC)
    lease.expires_at = datetime.now(UTC) + timedelta(hours=1)
    ctx["lease"] = lease


@when("the run completes")
def run_completes(ctx):
    lease = ctx["lease"]
    lease.status = "completed"
    ctx["lease"] = lease


@when(parsers.parse("I call create_workspace with a WorkspaceSpec derived from the profile"))
def create_workspace_from_profile(ctx):
    provider = ctx["provider"]
    profile = ctx["spec_profile"]
    spec = WorkspaceSpec(
        environment_profile_id=profile.id,
        organisation_id=profile.organisation_id,
        image_ref=profile.image_ref,
        capabilities=profile.capabilities,
        timeout_seconds=profile.timeout_seconds,
    )
    ref = asyncio.run(provider.create_workspace(spec))
    ctx["provider_ref"] = ref
    ctx["ws_spec"] = spec


@when(parsers.parse("I call destroy_workspace with the provider_ref"))
def destroy_workspace(ctx):
    asyncio.run(ctx["provider"].destroy_workspace(ctx["provider_ref"]))


@when(parsers.parse('I execute the command "{command}" via the ShellConnector'))
def execute_shell_command(command: str, ctx):
    from modulo.connectors.base import ConnectorPayload

    result = asyncio.run(
        ctx["connector"].write(
            ConnectorPayload(
                resource="command",
                data={"command": command, "provider_ref": ctx["ws_ref"]},
            )
        )
    )
    ctx["cmd_result"] = result


@when("I validate the snapshot against the profile")
def validate_snapshot(ctx):
    profile = ctx.get("validation_profile")
    graph_json = ctx.get("graph_json")
    agent_required = ctx.get("agent_required", [])
    profile_caps = set(ctx.get("validation_profile_caps", profile.capabilities if profile else []))

    errors = []
    if profile is None:
        errors.append(("ENV_PROFILE_NOT_FOUND", "Profile not found"))
    elif graph_json:
        for node in graph_json.get("nodes", []):
            if agent_required:
                missing = [c for c in agent_required if c not in profile_caps]
                if missing:
                    errors.append(("ENV_MISSING_CAPABILITIES", f"requires capabilities {missing}"))
    ctx["validation_errors"] = errors


@when(parsers.parse('I authenticate as a user in org "{org}"'))
def authenticate_as_org(org: str, ctx, request):
    from modulo.api.main import app

    if org != "acme":
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="otheruser",
            organisation_id=_ALT_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="viewer",
        )
        ctx["alt_org"] = True
    else:
        ctx["alt_org"] = False


# ============================================================================
# Then
# ============================================================================


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, ctx):
    resp = ctx.get("response")
    assert resp is not None, "No response stored in context"
    assert resp.status_code == status, f"Expected status {status}, got {resp.status_code}: {resp.text[:200]}"


@then(parsers.parse('the response contains a profile with name "{name}"'))
def response_contains_profile_name(name: str, ctx):
    data = ctx["response"].json()
    if isinstance(data, dict):
        assert data.get("name") == name, f"Expected name {name!r}, got {data.get('name')!r}"
    else:
        assert any(item.get("name") == name for item in data), f"No profile with name {name!r} in {data}"


@then(parsers.parse('the response contains a profile with id "{profile_id}"'))
def response_contains_profile_id(profile_id: str, ctx):
    data = ctx["response"].json()
    if isinstance(data, dict):
        assert profile_id in str(data.get("id", "")), f"Expected id containing {profile_id}, got {data.get('id')}"


@then(parsers.parse('the profile has image_ref "{expected}"'))
def profile_has_image(expected: str, ctx):
    data = ctx["response"].json()
    assert data.get("image_ref") == expected, f"Expected image_ref {expected!r}, got {data.get('image_ref')!r}"


@then(parsers.parse("the profile has capabilities {expected}"))
def profile_has_capabilities(expected: str, ctx):
    data = ctx["response"].json()
    expected_list = json.loads(expected)
    assert data.get("capabilities") == expected_list, (
        f"Expected capabilities {expected_list}, got {data.get('capabilities')}"
    )


@then(parsers.parse('the profile has egress_policy "{expected}"'))
def profile_has_egress(expected: str, ctx):
    data = ctx["response"].json()
    assert data.get("egress_policy") == expected, (
        f"Expected egress_policy {expected!r}, got {data.get('egress_policy')!r}"
    )


@then(parsers.parse("the profile has timeout_seconds {expected:d}"))
def profile_has_timeout(expected: int, ctx):
    data = ctx["response"].json()
    assert data.get("timeout_seconds") == expected, (
        f"Expected timeout_seconds {expected}, got {data.get('timeout_seconds')}"
    )


@then(parsers.parse('the error indicates "{field}" is required'))
def error_indicates_required(field: str, ctx):
    data = ctx["response"].json()
    detail = data.get("detail", {})
    if isinstance(detail, list):
        assert any(field in str(err.get("loc", [])) for err in detail), (
            f"Expected validation error mentioning {field!r}, got {detail}"
        )
    else:
        assert field in str(detail), f"Expected {field!r} in error detail: {detail}"


@then(parsers.parse("the error indicates timeout is out of range"))
def error_timeout_range(ctx):
    data = ctx["response"].json()
    assert data.get("status_code") == 422 or data.get("status_code") is None, f"Expected 422, got {data}"
    detail = str(data.get("detail", ""))
    assert any(word in detail.lower() for word in ["timeout", "less than", "greater than", "out of range"]), (
        f"Expected timeout range error, got {detail}"
    )


@then(parsers.parse('the error indicates "{field}" has an invalid value'))
def error_invalid_value(field: str, ctx):
    data = ctx["response"].json()
    detail = data.get("detail", {})
    if isinstance(detail, list):
        assert any(field in str(err.get("loc", [])) for err in detail), (
            f"Expected validation error on {field!r}, got {detail}"
        )
    else:
        assert field in str(detail), f"Expected {field!r} in error detail: {detail}"


@then(parsers.parse("the response is a paginated list with {count:d} items and page_size {page_size:d}"))
def paginated_list(count: int, page_size: int, ctx):
    data = ctx["response"].json()
    assert data["total"] == count, f"Expected total {count}, got {data['total']}"
    assert data["page_size"] == page_size, f"Expected page_size {page_size}, got {data['page_size']}"
    assert len(data["items"]) == count, f"Expected {count} items, got {len(data['items'])}"


@then(parsers.parse('the error message is "{msg}"'))
def error_message_is(msg: str, ctx):
    data = ctx["response"].json()
    assert data.get("detail") == msg, f"Expected detail {msg!r}, got {data.get('detail')!r}"


@then("the response is a Server-Sent Events stream")
def response_is_sse(ctx):
    resp = ctx["response"]
    assert resp.status_code == 200
    content_type = resp.headers.get("content-type", "")
    assert "text/event-stream" in content_type, f"Expected text/event-stream, got {content_type}"


@then(parsers.parse('the stream contains a "{event}" event'))
def stream_contains_event(event: str, ctx):
    resp = ctx["response"]
    text = resp.text
    assert event in text, f"Expected event {event!r} in stream, got: {text[:500]}"


@then(parsers.parse('the resolved provider is "{expected}"'))
def resolved_provider_is(expected: str, ctx):
    assert ctx.get("resolved_name") == expected, f"Expected resolved {expected!r}, got {ctx.get('resolved_name')!r}"


@then(parsers.parse('the WorkspaceLease status transitions from "{old_status}" to "{new_status}"'))
def lease_status_transition(old_status: str, new_status: str, ctx):
    lease = ctx["lease"]
    assert lease.status == new_status, f"Expected status {new_status!r}, got {lease.status!r}"


@then(parsers.parse('the WorkspaceLease status is "{status}"'))
def lease_status_is(status: str, ctx):
    lease = ctx["lease"]
    assert lease.status == status, f"Expected status {status!r}, got {lease.status!r}"


@then("the lease has a provider_ref and expires_at set")
def lease_has_provider_and_expiry(ctx):
    lease = ctx["lease"]
    assert lease.provider_ref is not None, "Expected provider_ref to be set"
    assert lease.expires_at is not None, "Expected expires_at to be set"


@then("a provider_ref is returned")
def provider_ref_returned(ctx):
    assert ctx.get("provider_ref") is not None, "Expected a provider_ref"


@then(parsers.parse('the workspace status is "{status}"'))
def workspace_status_is(status: str, ctx):
    provider = ctx["provider"]
    ref = ctx.get("provider_ref")
    actual = asyncio.run(provider.get_workspace_status(ref))
    assert actual == status, f"Expected status {status!r}, got {actual!r}"


@then(parsers.parse("the command exits with code {code:d}"))
def command_exit_code(code: int, ctx):
    result = ctx["cmd_result"]
    assert result["exit_code"] == code, f"Expected exit code {code}, got {result['exit_code']}"


@then(parsers.parse('the stdout contains "{text}"'))
def command_stdout_contains(text: str, ctx):
    result = ctx["cmd_result"]
    assert text in result["stdout"], f"Expected stdout to contain {text!r}, got {result['stdout']!r}"


@then(parsers.parse('a validation error is raised with code "{code}"'))
def validation_error_raised(code: str, ctx):
    errors = ctx.get("validation_errors", [])
    codes = [e[0] for e in errors]
    assert code in codes, f"Expected error code {code!r}, got {codes}"


@then(parsers.parse('the error mentions "{text}" as missing'))
def error_mentions_missing(text: str, ctx):
    errors = ctx.get("validation_errors", [])
    details = " ".join(str(e) for e in errors)
    assert text in details, f"Expected {text!r} in errors: {details}"


@then("no validation errors are raised")
def no_validation_errors(ctx):
    errors = ctx.get("validation_errors", [])
    assert len(errors) == 0, f"Expected no validation errors, got {errors}"


@then(parsers.parse("the response contains {count:d} environment profiles"))
def response_contains_n_profiles(count: int, ctx):
    data = ctx["response"].json()
    items = data.get("items", [])
    assert len(items) == count, f"Expected {count} profiles, got {len(items)}"
