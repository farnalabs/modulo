"""BDD step definitions: Product Analytics Transparency (FAR-351).

Each scenario drives the real ``GET /api/v1/product-analytics/transparency``
route through a TestClient, patching only the ``get_config`` CRUD helper it
depends on so the SystemConfig rows are controlled per scenario. The
``is_system_admin`` auth gate is exercised explicitly: only a system admin may
read the transparency page, org admins get 403, and unauthenticated requests
get 401. Error mapping (ProgrammingError → 501, SQLAlchemyError → 503) comes
from the route's ``handle_db_errors`` decorator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from tests.bdd.conftest import ORG_ID, USER_ID, _active_client

scenarios("transparency.feature")

TRANS_URL = "/api/v1/product-analytics/transparency"


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _set_auth_override(is_system_admin: bool) -> None:
    """Point ``get_current_user`` at a principal with the wanted system-admin flag."""
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="sysadmin" if is_system_admin else "testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
    )


def _config_row(value: Any) -> MagicMock:
    row = MagicMock()
    row.value = value
    return row


def _get_transparency(client: Any, ctx: dict[str, Any]) -> Any:
    """GET the real route with only ``get_config`` mocked, stash the response."""
    config: dict[str, Any] = ctx.get("config", {})
    config_exc: Exception | None = ctx.get("config_exc")

    import modulo.api.routes.product_analytics_transparency as transparency_module

    async def fake_get_config(session: Any, key: str) -> MagicMock | None:
        if config_exc is not None:
            raise config_exc
        value = config.get(key)
        return _config_row(value) if value is not None else None

    with patch.object(
        transparency_module,
        "get_config",
        new_callable=AsyncMock,
        side_effect=fake_get_config,
    ):
        return client.get(TRANS_URL)


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am authenticated as a system admin")
def _given_system_admin(ctx) -> None:
    ctx["is_system_admin"] = True


@given("I am authenticated as an org admin")
def _given_org_admin(ctx) -> None:
    ctx["is_system_admin"] = False


@given(parsers.parse('the system config row "{key}" has value "{value}"'))
def _given_config_row(key: str, value: str, ctx) -> None:
    ctx.setdefault("config", {})[key] = value


@given(parsers.parse('the system config row "{key}" was "{days}" days ago'))
def _given_config_row_days_ago(key: str, days: str, ctx) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=int(days))).isoformat()
    ctx.setdefault("config", {})[key] = stamp


@given("the transparency config lookup fails with a programming error")
def _given_programming_error(ctx) -> None:
    ctx["config_exc"] = ProgrammingError("SELECT * FROM system_config", {}, Exception("no such table"))


@given("the transparency config lookup fails with a database error")
def _given_db_error(ctx) -> None:
    ctx["config_exc"] = SQLAlchemyError("connection lost")


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("I GET /api/v1/product-analytics/transparency")
def _when_get_transparency(request, ctx) -> None:
    client = _active_client(request)
    _set_auth_override(ctx.get("is_system_admin", True))
    request.node._resp = _get_transparency(client, ctx)


@when("I GET /api/v1/product-analytics/transparency without authentication")
def _when_get_transparency_unauth(request) -> None:
    from modulo.auth.dependencies import get_current_user

    unauth_client = request.getfixturevalue("unauth_client")
    unauth_client.app.dependency_overrides.pop(get_current_user, None)
    request.node._resp = unauth_client.get(TRANS_URL)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


def _body(request: Any) -> dict:
    resp = request.node._resp
    return resp.json()


@then(parsers.parse('the transparency consent level is "{expected}"'))
def _then_consent_level(expected: str, request) -> None:
    assert _body(request)["consent_level"] == expected


@then("the transparency instance is enabled")
def _then_instance_enabled(request) -> None:
    assert _body(request)["instance_enabled"] is True


@then("the transparency enforcement is enabled")
def _then_enforcement_enabled(request) -> None:
    assert _body(request)["enforcement_enabled"] is True


@then(parsers.parse("the transparency dump count is {expected:d}"))
def _then_dump_count(expected: int, request) -> None:
    assert _body(request)["dump_count_total"] == expected


@then("the transparency last dump matches the configured value")
def _then_last_dump_matches(request, ctx) -> None:
    expected = ctx["config"]["product_analytics_last_dump_at"]
    assert _body(request)["last_successful_dump_at"] == expected


@then("the transparency response returns the default state")
def _then_default_state(request) -> None:
    body = _body(request)
    assert body["consent_level"] == "off"
    assert body["instance_enabled"] is False
    assert body["enforcement_enabled"] is False
    assert body["dump_count_total"] == 0
    assert body["last_successful_dump_at"] is None
    assert body["warning"] is None


@then(parsers.parse('the transparency response warns "{expected}"'))
def _then_warns(expected: str, request) -> None:
    assert _body(request)["warning"] == expected


@then("the transparency response has no warning")
def _then_no_warning(request) -> None:
    assert _body(request)["warning"] is None
