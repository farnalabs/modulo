"""Step definitions for admin runtime-config BDD scenarios.

The ``admin_runtime_config`` routes gate on ``require_system_permission``
(``system.config.manage``), which only passes for ``is_system_admin``
principals. The shared ``client`` fixture only provides an org admin, and the
shared ``app``'s ``dependency_overrides`` are mutated by the whole BDD suite
(cross-file global state pollution), so this module builds a fresh, isolated
``FastAPI`` app per request with its own overrides — the same approach the
email-settings scenarios use. That makes the admin (200/400), viewer (403) and
unauthenticated (401) outcomes deterministic regardless of suite ordering.
"""

import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_config.store import get_runtime_config_store

scenarios("runtime-config.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _build_app(request: Any) -> FastAPI:
    """Return a fresh app for a single request.

    The requested principal depends on the auth Given step that ran:
    admin scenarios (no flag stashed) get a system admin, viewer scenarios get
    a viewer, and unauth scenarios get no ``get_current_user`` override so the
    real auth dependency returns 401.
    """
    from modulo.api.dependencies import get_plan_context
    from modulo.api.routes.admin_runtime_config import router as runtime_config_router
    from modulo.auth.dependencies import get_current_user

    class _AllFeatures:
        def feature_enabled(self, name: str) -> bool:
            return True

        def list_enabled_features(self) -> list:
            return []

        def tier(self) -> str:
            return "team"

        def has_license_key(self) -> bool:
            return True

    app = FastAPI()
    app.include_router(runtime_config_router)
    app.dependency_overrides[get_plan_context] = lambda: _AllFeatures()

    is_viewer = bool(getattr(request.node, "_viewer_auth", False))
    is_unauth = bool(getattr(request.node, "_unauth", False))
    principal: AuthenticatedPrincipal | None
    if is_viewer:
        principal = AuthenticatedPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="viewer",
            is_system_admin=False,
        )
    elif is_unauth:
        principal = None
    else:
        principal = AuthenticatedPrincipal(
            username="testuser",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
            is_system_admin=True,
        )

    if principal is not None:
        app.dependency_overrides[get_current_user] = lambda: principal
    return app


def _call(request: Any, method: str, path: str, json: dict[str, Any] | None = None) -> None:
    app = _build_app(request)
    client = TestClient(app)
    resp = client.request(method, path, json=json)
    request.node._resp = resp


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — ``_build_app`` supplies a system admin for admin scenarios."""


@given("I am not authenticated")
def _bdd_not_authenticated(request) -> None:
    """Flag the unauth scenario so ``_build_app`` leaves auth un-overridden."""
    request.node._unauth = True


@when("I request GET /api/v1/admin/runtime-config")
def _bdd_get_runtime_config(request) -> None:
    store = get_runtime_config_store()
    items_before = store.get_all()
    _call(request, "GET", "/api/v1/admin/runtime-config")
    request.node._items_before = items_before


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with override for "{key}"'))
def _bdd_put_override(request, key: str) -> None:
    store = get_runtime_config_store()
    store.clear_all_overrides()
    _call(request, "PUT", "/api/v1/admin/runtime-config", json={"overrides": {key: "DEBUG"}})


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with clear for "{key}"'))
def _bdd_put_clear(request, key: str) -> None:
    store = get_runtime_config_store()
    store.set_override(key, "DEBUG")
    _call(request, "PUT", "/api/v1/admin/runtime-config", json={"clear": [key]})


@when("I POST /api/v1/admin/runtime-config/reload")
def _bdd_post_reload(request) -> None:
    _call(request, "POST", "/api/v1/admin/runtime-config/reload")


@when(parsers.parse('I PUT /api/v1/admin/runtime-config with unknown key "{key}"'))
def _bdd_put_unknown_key(request, key: str) -> None:
    _call(request, "PUT", "/api/v1/admin/runtime-config", json={"overrides": {key: "value"}})


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
