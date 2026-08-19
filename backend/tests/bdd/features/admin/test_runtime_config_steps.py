"""Step definitions for admin runtime-config BDD scenarios."""

import uuid
from typing import Any

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.runtime_config.store import get_runtime_config_store

scenarios("runtime-config.feature")


def _set_runtime_auth(request: Any) -> None:
    """Auth the admin runtime-config steps as a system admin.

    ``admin_runtime_config`` routes now use ``require_system_permission``
    (``system.config.manage``), which checks ``get_current_user`` and requires
    ``is_system_admin``. The shared ``client`` fixture only provides an org
    admin, so we override ``get_current_user`` to a system admin for the admin
    scenarios. The viewer and unauth scenarios stash their own (non-admin)
    client via ``request.node._client`` and keep the natural 403/401 paths.
    """
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    if getattr(request.node, "_viewer_auth", False):
        return
    if getattr(request.node, "_client", None) is not None:
        return
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        account_id=uuid.uuid4(),
        org_role="admin",
        is_system_admin=True,
    )


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — fixture handles this."""


@given("I am not authenticated")
def _bdd_not_authenticated(request, unauth_client) -> None:
    """Stash the unauth client for _active_client."""
    request.node._client = unauth_client


@when("I request GET /api/v1/admin/runtime-config")
def _bdd_get_runtime_config(request) -> None:
    from tests.bdd.conftest import _active_client

    active = _active_client(request)
    _set_runtime_auth(request)
    store = get_runtime_config_store()
    items_before = store.get_all()
    resp = active.get("/api/v1/admin/runtime-config")
    request.node._resp = resp
    request.node._items_before = items_before


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with override for "{key}"'))
def _bdd_put_override(client: TestClient, request, key: str) -> None:
    _set_runtime_auth(request)
    store = get_runtime_config_store()
    store.clear_all_overrides()
    resp = client.put("/api/v1/admin/runtime-config", json={"overrides": {key: "DEBUG"}})
    request.node._resp = resp


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with clear for "{key}"'))
def _bdd_put_clear(client: TestClient, request, key: str) -> None:
    _set_runtime_auth(request)
    store = get_runtime_config_store()
    store.set_override(key, "DEBUG")
    resp = client.put("/api/v1/admin/runtime-config", json={"clear": [key]})
    request.node._resp = resp


@when("I POST /api/v1/admin/runtime-config/reload")
def _bdd_post_reload(client: TestClient, request) -> None:
    _set_runtime_auth(request)
    resp = client.post("/api/v1/admin/runtime-config/reload")
    request.node._resp = resp


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with unknown key "{key}"'))
def _bdd_put_unknown_key(client: TestClient, request, key: str) -> None:
    _set_runtime_auth(request)
    resp = client.put("/api/v1/admin/runtime-config", json={"overrides": {key: "value"}})
    request.node._resp = resp


@then("the response contains items array and has_drift flag")
def _bdd_check_response_shape(request) -> None:
    data = request.node._resp.json()
    assert "items" in data, "Response missing 'items'"
    assert "has_drift" in data, "Response missing 'has_drift'"
    assert isinstance(data["items"], list)
    assert isinstance(data["has_drift"], bool)


@then("the response includes the updated config items")
def _bdd_check_updated_items(request) -> None:
    data = request.node._resp.json()
    assert "items" in data
