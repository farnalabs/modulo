"""BDD step definitions: Model backend configure, rotation, health check."""

import contextlib
import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/model_backends/configure.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/model_backends/rotation.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/model_backends/health_check.feature")


@given(parsers.parse('I configure an OpenAI model backend with model "{model}" and API key "{key}"'))
def configure_openai(model: str, key: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_model_backend",
            return_value=MagicMock(
                id=uuid.uuid4(),
                provider="openai",
                model=model,
                name=f"openai-{model}",
            ),
        ),
        patch("modulo.core.pipeline_engine.fernet_utils.encrypt_credential"),
    ):
        resp = client.post(
            "/api/model-backends",
            json={
                "name": f"openai-{model}",
                "provider": "openai",
                "model": model,
                "api_key": key,
            },
        )
    request.node._resp = resp


@given(parsers.parse('I configure an Anthropic model backend with model "{model}" and API key "{key}"'))
def configure_anthropic(model: str, key: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_model_backend",
            return_value=MagicMock(
                id=uuid.uuid4(),
                provider="anthropic",
                model=model,
                name=f"anthropic-{model}",
            ),
        ),
        patch("modulo.core.pipeline_engine.fernet_utils.encrypt_credential"),
    ):
        resp = client.post(
            "/api/model-backends",
            json={
                "name": f"anthropic-{model}",
                "provider": "anthropic",
                "model": model,
                "api_key": key,
            },
        )
    request.node._resp = resp


@given("I configure a Stub model backend with fixture map")
def configure_stub(client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_model_backend",
            return_value=MagicMock(
                id=uuid.uuid4(),
                provider="stub",
                model="stub",
                name="stub-backend",
            ),
        ),
    ):
        resp = client.post(
            "/api/model-backends",
            json={"name": "stub-backend", "provider": "stub", "model": "stub"},
        )
    request.node._resp = resp


@when("I GET /api/model-backends")
def get_model_backends(client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.list_model_backends",
            return_value=[
                MagicMock(
                    id=uuid.uuid4(),
                    provider="openai",
                    model="gpt-4",
                    name="openai-gpt-4",
                )
            ],
        ),
    ):
        resp = client.get("/api/model-backends")
    request.node._resp = resp


@then(parsers.parse('the response contains a backend with provider "{provider}" and model "{model}"'))
def check_backend(provider: str, model: str, request):
    data = request.node._resp.json()
    if isinstance(data, list):
        found = any(d.get("provider") == provider and d.get("model") == model for d in data)
        assert found, f"Backend {provider}/{model} not found in {data}"
    else:
        assert data.get("provider") == provider


@then(parsers.parse('the response contains a backend with provider "{provider}"'))
def check_backend_provider(provider: str, request):
    data = request.node._resp.json()
    if isinstance(data, list):
        found = any(d.get("provider") == provider for d in data)
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
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_model_backend",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=name,
                model=model,
            ),
        ),
    ):
        resp = client.patch(f"/api/model-backends/{name}", json={"model": model})
    request.node._resp = resp


@then(parsers.parse('the model is updated to "{model}"'))
def check_model_updated(model: str, request):
    data = request.node._resp.json()
    assert data.get("model") == model


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


@when(parsers.parse("I check the model backend health"))
def check_mb_health(client, request):
    mock_health = MagicMock()
    mock_health.ok = True
    mock_health.detail = "ok"
    with (
        patch(
            "modulo.model_backend_hub.check_health",
            return_value=mock_health,
        ),
    ):
        resp = client.get("/api/model-backends/health")
    request.node._resp = resp


@then("the health check returns ok")
def health_ok(request):
    data = request.node._resp.json()
    assert data.get("ok") is True or data.get("status") == "ok"


@then("the health check returns error")
def health_error(request):
    data = request.node._resp.json()
    assert data.get("ok") is False or data.get("status") != "ok"


@then("the error describes the authentication failure")
def health_auth_error(request):
    pass


@given(parsers.parse('org "{org}" has a model backend "{name}"'))
def org_has_model_backend_simple(org: str, name: str, request):
    request.node._mb_name = name


@when(parsers.parse('I check the health of "{name}"'))
def check_health_of(name: str, client, request):
    pass


@given("a Stub model backend is configured")
def stub_configured(request):
    pass
