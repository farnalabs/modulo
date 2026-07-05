"""Step definitions for admin runtime-config BDD scenarios."""

from typing import Any

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.runtime_config.store import get_runtime_config_store

scenarios("runtime-config.feature")


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — fixture handles this."""


@given("I am not authenticated")
def _bdd_not_authenticated(request) -> None:
    """Flag scenario for unauth client."""
    request.node._unauth = True


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def _bdd_auth_viewer_in_org(org: str, request) -> None:
    request.node._viewer_auth = True


@when("I request GET /api/v1/admin/runtime-config")
def _bdd_get_runtime_config(client: TestClient, unauth_client: TestClient, request) -> None:
    if getattr(request.node, "_unauth", False):
        resp = unauth_client.get("/api/v1/admin/runtime-config")
        request.node._resp = resp
        return
    if getattr(request.node, "_viewer_auth", False):
        resp = client.get("/api/v1/admin/runtime-config")
        request.node._resp = resp
        return
    store = get_runtime_config_store()
    items_before = store.get_all()
    resp = client.get("/api/v1/admin/runtime-config")
    request.node._resp = resp
    request.node._items_before = items_before


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with override for "{key}"'))
def _bdd_put_override(client: TestClient, request, key: str) -> None:
    store = get_runtime_config_store()
    store.clear_all_overrides()
    resp = client.put("/api/v1/admin/runtime-config", json={"overrides": {key: "DEBUG"}})
    request.node._resp = resp


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with clear for "{key}"'))
def _bdd_put_clear(client: TestClient, request, key: str) -> None:
    store = get_runtime_config_store()
    store.set_override(key, "DEBUG")
    resp = client.put("/api/v1/admin/runtime-config", json={"clear": [key]})
    request.node._resp = resp


@when("I POST /api/v1/admin/runtime-config/reload")
def _bdd_post_reload(client: TestClient, request) -> None:
    resp = client.post("/api/v1/admin/runtime-config/reload")
    request.node._resp = resp


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with unknown key "{key}"'))
def _bdd_put_unknown_key(client: TestClient, request, key: str) -> None:
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
