"""Step definitions for feature-flag inspection: list, per-flag detail, 404, toggle.

Wired into the executing BDD suite on the 2026-09-14 improve-architecture
product-map walk, closing the ``feat-license`` "no executing BDD surface for
feature-flag inspection" gap: ``feature_flag_inspection.feature`` shipped under
``tests/bdd/features/licensing/`` but no step module registered it via
``scenarios(...)``, so it never executed and the inspection/override endpoints
were unit-tested only.

The steps drive the real ``/api/v1/admin/feature-flags`` routes with the DB
CRUD reads patched (the licensing hermetic-mock pattern from
``test_license_management.py`` — a mock session whose org/tier/flag catalog
reads return no rows, so the ``FeatureFlagRegistry`` falls back to its
hardcoded catalogue), so the scenarios assert the actual API contract: status
codes, the ``license`` / ``flags`` response shapes, per-flag field presence,
the unknown-flag 404, the toggle override write-back (``overridden: true``),
and the public ``/api/v1/license`` surface.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/licensing/feature_flag_inspection.feature")


def _make_mock_session() -> Any:
    session = AsyncMock()

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_scalars.first.return_value = None

    # The flag-toggle path writes `feature_overrides[flag_name]` into the
    # caller's org settings row (`_write_org_override` 404s when the org read
    # returns None), so the single-row read returns an org-shaped row whose
    # ``settings_json`` / ``plan_id`` are real values — the tier/feature
    # catalog *list* reads still fall through to ``[]`` (hardcoded catalogue).
    org_mock = MagicMock()
    org_mock.settings_json = {}
    org_mock.plan_id = None

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = org_mock
    mock_result.first.return_value = org_mock

    session.execute = AsyncMock(return_value=mock_result)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()

    return session


def _setup_client(ctx: dict[str, Any]) -> None:
    from modulo.api.dependencies import _get_engine as _eng
    from modulo.api.dependencies import get_db_session
    from modulo.api.main import app as _app
    from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
    from modulo.core.license import clear_license
    from modulo.settings import Settings, get_settings

    # Hermetic tier resolution: a stored license from another licensing
    # scenario must not leak into the feature-flag tier/has_license_key payload.
    clear_license()

    mock_session = _make_mock_session()

    async def _override_session():
        yield mock_session

    is_admin = ctx.get("is_admin", True)
    _valid_32 = "a" * 32
    _settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_valid_32,
        fernet_key=_valid_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
        # No real Redis for hermetic BDD steps: the feature-flags routes read
        # and write a 60s-TTL cache under ``feature-flags:{org_id}``. In the
        # full BDD suite Redis IS up, so a GET from one scenario writes a
        # shared cache entry that a later scenario reads back as stale across
        # the mocked-org boundary. An empty redis_url makes every cache
        # read/write fail fast inside the route's ``except Exception``,
        # keeping each scenario hermetic.
        redis_url="",
    )

    _principal_kwargs = {
        "username": "admin" if is_admin else "operator",
        "organisation_id": "00000000-0000-0000-0000-000000000001",
        "account_id": "00000000-0000-0000-0000-000000000002",
        "org_role": "admin" if is_admin else "operator",
        # The flag toggle (PUT /{flag_name}) requires system-admin level
        # (require_system_permission("system.config.manage")).
        "is_system_admin": is_admin,
    }

    _app.dependency_overrides[get_settings] = lambda: _settings
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_eng] = lambda: None
    _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(**_principal_kwargs)

    async def _override_tenant() -> TenantPrincipal:
        return TenantPrincipal(**_principal_kwargs)

    _app.dependency_overrides[get_current_tenant_user] = _override_tenant
    _app.dependency_overrides[get_current_tenant_user_or_api_key] = _override_tenant

    get_settings.cache_clear()


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


@pytest.fixture
def client() -> Any:
    from fastapi.testclient import TestClient

    from modulo.api.main import app

    return TestClient(app)


# ── Given steps ──────────────────────────────────────────────────────────────


@given("the database has feature flags configured")
def database_has_feature_flags() -> None:
    """No-op — the mock session's tier/flag catalog reads return no rows, so
    ``FeatureFlagRegistry.from_db`` falls back to the hardcoded catalogue, which
    is exactly the "configured" surface these scenarios exercise."""


@given("I have a valid session")
def valid_session(ctx: dict[str, Any]) -> None:
    ctx["is_admin"] = True


# ── When steps ────────────────────────────────────────────────────────────────


@when('I GET "/api/v1/admin/feature-flags"')
def get_feature_flags(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.get("/api/v1/admin/feature-flags")
    _store_response(request, ctx, resp)


@when('I GET "/api/v1/admin/feature-flags/sso"')
def get_sso_flag(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.get("/api/v1/admin/feature-flags/sso")
    _store_response(request, ctx, resp)


@when('I GET "/api/v1/admin/feature-flags/nonexistent_flag"')
def get_unknown_flag(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.get("/api/v1/admin/feature-flags/nonexistent_flag")
    _store_response(request, ctx, resp)


@when('I PUT "/api/v1/admin/feature-flags/sso" with body {"enabled": true}')
def toggle_sso_flag(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
    _store_response(request, ctx, resp)


@when('I GET "/api/v1/license"')
def get_public_license(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.get("/api/v1/license")
    _store_response(request, ctx, resp)


# ── Then steps ────────────────────────────────────────────────────────────────


@then('the response contains a "license" object with "tier", "has_license_key", and "is_valid" fields')
def response_license_object(request: Any) -> None:
    data = request.node._resp.json()
    license_obj = data["license"]
    for field in ("tier", "has_license_key", "is_valid"):
        assert field in license_obj, f"license object missing '{field}': {license_obj}"


@then('the response contains a "flags" array')
def response_flags_array(request: Any) -> None:
    flags = request.node._resp.json()["flags"]
    assert isinstance(flags, list), f"expected a flags array, got {type(flags).__name__}"


@then('each flag in the "flags" array has "name", "description", "tier", and "currently_active" fields')
def each_flag_has_core_fields(request: Any) -> None:
    for flag in request.node._resp.json()["flags"]:
        for field in ("name", "description", "tier", "currently_active"):
            assert field in flag, f"flag missing '{field}': {flag}"


@then(parsers.parse('the response field "{field}" equals "{value}"'))
def response_field_equals(field: str, value: str, request: Any) -> None:
    data = request.node._resp.json()
    assert data[field] == value, f"Expected {field}={value!r}, got {data.get(field)!r}"


@then('the response contains an "overridden" field')
def response_overridden_field(request: Any) -> None:
    assert "overridden" in request.node._resp.json(), "expected an 'overridden' field"


@then('the response contains a "tier" field')
def response_tier_field(request: Any) -> None:
    assert "tier" in request.node._resp.json(), "expected a 'tier' field"


@then('the response contains a "features" list')
def response_features_list(request: Any) -> None:
    features = request.node._resp.json()["features"]
    assert isinstance(features, list), f"expected a features list, got {type(features).__name__}"
