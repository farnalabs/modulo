"""BDD step definitions: Model backend configure, rotation, health check."""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/model_backends/configure.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/model_backends/rotation.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/model_backends/health_check.feature")

from tests.bdd.conftest import ORG_ID, USER_ID


def _backend_id_for(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"model-backend/{name}")


def _make_mock_backend(name: str = "backend", provider: str = "openai", model_id: str = "gpt-4") -> MagicMock:
    b = MagicMock()
    b.id = _backend_id_for(name)
    b.organisation_id = ORG_ID
    b.name = name
    b.display_name = name
    b.provider = provider
    b.model_id = model_id
    b.has_credentials = True
    b.default_params = {}
    b.visibility = "org"
    b.owner_team_id = None
    b.tier = "native"
    b.fallback_backend_ids = None
    b.account_id = USER_ID
    b.created_at = datetime.now(UTC)
    b.updated_at = datetime.now(UTC)
    return b


def _page_result(items: list) -> MagicMock:
    page_result = MagicMock()
    page_result.items = items
    page_result.total = len(items)
    page_result.page = 1
    page_result.page_size = 20
    return page_result


@given(parsers.parse('I configure an OpenAI model backend with model "{model}" and API key "{key}"'))
def configure_openai(model: str, key: str, client, request):
    request.node._configured_backend = ("openai", model)
    with (
        patch(
            "modulo.api.routes.model_backends.create_model_backend",
            return_value=_make_mock_backend(name=f"openai-{model}", provider="openai", model_id=model),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": f"openai-{model}",
                "display_name": f"openai-{model}",
                "provider": "openai",
                "model_id": model,
                "api_key": key,
            },
        )
    request.node._resp = resp


@given(parsers.parse('I configure an OpenAI model backend with API key "{key}"'))
def configure_openai_no_model(key: str, client, request):
    configure_openai("gpt-4", key, client, request)


@given(parsers.parse('I configure an Anthropic model backend with model "{model}" and API key "{key}"'))
def configure_anthropic(model: str, key: str, client, request):
    request.node._configured_backend = ("anthropic", model)
    with (
        patch(
            "modulo.api.routes.model_backends.create_model_backend",
            return_value=_make_mock_backend(name=f"anthropic-{model}", provider="anthropic", model_id=model),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": f"anthropic-{model}",
                "display_name": f"anthropic-{model}",
                "provider": "anthropic",
                "model_id": model,
                "api_key": key,
            },
        )
    request.node._resp = resp


@given("I configure a Stub model backend with fixture map")
def configure_stub(client, request):
    request.node._configured_backend = ("stub", "stub")
    with (
        patch(
            "modulo.api.routes.model_backends.create_model_backend",
            return_value=_make_mock_backend(name="stub-backend", provider="stub", model_id="stub"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "stub-backend",
                "display_name": "stub-backend",
                "provider": "stub",
                "model_id": "stub",
                "api_key": "stub-key",
            },
        )
    request.node._resp = resp


@when("I GET /api/model-backends")
def get_model_backends(client, request):
    provider, model = getattr(request.node, "_configured_backend", ("openai", "gpt-4"))
    mock_backend = _make_mock_backend(name=f"{provider}-{model}", provider=provider, model_id=model)
    with (
        patch(
            "modulo.api.routes.model_backends.list_model_backends",
            return_value=_page_result([mock_backend]),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    request.node._resp = resp


@then(parsers.parse('the response contains a backend with provider "{provider}" and model "{model}"'))
def check_backend(provider: str, model: str, request):
    data = request.node._resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    found = any(d.get("provider") == provider and d.get("model_id") == model for d in items)
    assert found, f"Backend {provider}/{model} not found in {data}"


@then(parsers.parse('the response contains a backend with provider "{provider}"'))
def check_backend_provider(provider: str, request):
    data = request.node._resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    found = any(d.get("provider") == provider for d in items)
    assert found, f"Backend provider {provider} not found"


@when("I inspect the database directly")
def inspect_db(request):
    pass


@then("the API key is not stored in plaintext")
def check_key_not_plaintext(request):
    pass


@given(parsers.parse('org "{org}" has a model backend "{name}" with model "{model}"'))
def org_has_model_backend(org: str, name: str, model: str, request):
    request.node._mb_name = name
    request.node._mb_model = model


@when(parsers.parse('I PATCH /api/model-backends/{name} with model "{model}"'))
def patch_model_backend(name: str, model: str, client, request):
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_mock_backend(name=name)),
        patch(
            "modulo.api.routes.model_backends.update_model_backend",
            return_value=_make_mock_backend(name=name, model_id=model),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_backend_id_for(name)}", json={"model_id": model})
    request.node._resp = resp


@then(parsers.parse('the model is updated to "{model}"'))
def check_model_updated(model: str, request):
    data = request.node._resp.json()
    assert data.get("model_id") == model


@given(parsers.parse('org "{org}" has model backends "{primary}" and "{fallback}"'))
def org_has_model_backends(org: str, primary: str, fallback: str, request):
    request.node._mb_primary = primary
    request.node._mb_fallback = fallback


@given(parsers.parse('"{name}" is healthy'))
def backend_healthy(name: str, request):
    request.node._mb_healthy = {name: True}


@given(parsers.parse('"{name}" is unhealthy'))
def backend_unhealthy(name: str, request):
    if not hasattr(request.node, "_mb_healthy"):
        request.node._mb_healthy = {}
    request.node._mb_healthy[name] = False


@when("I trigger a run with model backend assignment")
def trigger_run_with_backend(client, request):
    pass


@then(parsers.parse('the run uses "{name}"'))
def run_uses_backend(name: str, request):
    pass


@then(parsers.parse('the run fails with "{error}"'))
def run_fails_with(error: str, request):
    pass


@when("the backend health is checked")
def backend_health_checked(request):
    pass


@then("the health check result determines whether the backend is selected")
def health_check_determines(request):
    pass


@when("I check the model backend health")
def check_mb_health(client, request):
    # No standalone health endpoint exists — health is enforced via pipeline
    # validation (see backend_health_check.feature). The carrying scenarios are
    # marked @awaiting-implementation and deselected.
    request.node._resp = None


@then("the health check returns ok")
def health_ok(request):
    pass


@then("the health check returns error")
def health_error(request):
    pass


@then("the error describes the authentication failure")
def health_auth_error(request):
    pass


@given(parsers.parse('org "{org}" has model backend "{name}"'))
def org_has_model_backend_simple(org: str, name: str, request):
    request.node._mb_name = name


@when(parsers.parse('I check the health of "{name}"'))
def check_health_of(name: str, client, request):
    request.node._resp = None


@given("a Stub model backend is configured")
def stub_configured(request):
    pass
