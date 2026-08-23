"""Step definitions for the monitor-config BDD feature."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.db.models.system_config import SystemConfig

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

scenarios("../../bdd/features/observability/monitor_config.feature")


@pytest.fixture(autouse=True)
def _prevent_identity_db_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent ``_verify_identity`` from connecting to a real database.

    ``get_current_tenant_user`` verifies the account/org against Postgres
    before returning the principal. Patch it out so BDD scenarios run
    against the mocked DB session (mirrors tests/unit/api/conftest.py).
    """
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))


_URL = "/api/v1/admin/monitor-config"

_DEFAULT_CONFIG = {
    "backends": ["builtin"],
    "sentry": None,
    "datadog_rum": None,
    "grafana_faro": None,
}


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — the ``client`` fixture already provides an admin principal."""


@given("no monitoring configuration is stored")
def _bdd_no_config_stored() -> None:
    """No-op — GET handler falls back to defaults when nothing is stored."""


@given(parsers.parse('a monitoring configuration stores "{backends}" backends'))
def _bdd_config_stored(backends: str, request) -> None:
    request.node._monitor_stored_backends = [b.strip().strip('"') for b in backends.split("and")]


@given("the system_config table does not exist")
def _bdd_config_table_missing(request) -> None:
    request.node._monitor_db_error = ProgrammingError("stmt", "params", Exception("table missing"))


@given("the database is unavailable")
def _bdd_db_unavailable(request) -> None:
    request.node._monitor_db_error = SQLAlchemyError("connection lost")


def _set_monitor_auth(request) -> None:
    """Gate the monitor-config principal on the route's system-admin permission.

    ``admin_monitor_config`` routes now use ``require_system_permission``
    (``system.config.manage``), which checks ``get_current_user`` and requires
    ``is_system_admin``. The shared ``client`` fixture only provides an org
    admin here, so we override ``get_current_user`` to a system admin; the
    viewer scenario overrides it to a viewer so the 403 path is still covered.
    """
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    is_viewer = getattr(request.node, "_viewer_auth", False)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer" if is_viewer else "testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="viewer" if is_viewer else "admin",
        is_system_admin=not is_viewer,
    )


@when("I request GET /api/v1/admin/monitor-config")
def _bdd_get_monitor_config(client: TestClient, request) -> None:
    _set_monitor_auth(request)
    db_error = getattr(request.node, "_monitor_db_error", None)
    stored = getattr(request.node, "_monitor_stored_backends", None)
    if getattr(request.node, "_viewer_auth", False):
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.get(_URL)
    elif db_error is not None:
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            side_effect=db_error,
        ):
            resp = client.get(_URL)
    elif stored is not None:
        stored_config = dict(_DEFAULT_CONFIG)
        stored_config["backends"] = stored
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            return_value=SystemConfig(key="monitor_backends", value=stored_config),
        ):
            resp = client.get(_URL)
    else:
        with patch(
            "modulo.api.routes.admin_monitor_config.get_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.get(_URL)
    request.node._resp = resp


@when(parsers.parse("I PUT /api/v1/admin/monitor-config with backends [{backends}]"))
def _bdd_put_monitor_config(client: TestClient, request, backends: str) -> None:
    _set_monitor_auth(request)
    import json

    parsed = json.loads(f"[{backends}]")
    with patch(
        "modulo.api.routes.admin_monitor_config.update_config",
        new_callable=AsyncMock,
        side_effect=_fake_set_config,
    ):
        resp = client.put(_URL, json={"backends": parsed})
    request.node._resp = resp


@when(parsers.parse("I PUT /api/v1/admin/monitor-config with backends [{backends}] and a clientToken"))
def _bdd_put_monitor_config_with_token(client: TestClient, request, backends: str) -> None:
    _set_monitor_auth(request)
    import json

    parsed = json.loads(f"[{backends}]")
    payload: dict = {"backends": parsed}
    if "datadog_rum" in parsed:
        payload["datadog_rum"] = {"clientToken": "pub123456", "site": "datadoghq.com"}
    with patch(
        "modulo.api.routes.admin_monitor_config.update_config",
        new_callable=AsyncMock,
        side_effect=_fake_set_config,
    ):
        resp = client.put(_URL, json=payload)
    request.node._resp = resp


@when(parsers.parse('I PUT /api/v1/admin/monitor-config enabling "{backend}" without its required fields'))
def _bdd_put_monitor_config_missing_fields(client: TestClient, request, backend: str) -> None:
    _set_monitor_auth(request)
    resp = client.put(_URL, json={"backends": [backend], backend: {}})
    request.node._resp = resp


@when("I PUT /api/v1/admin/monitor-config with backends []")
def _bdd_put_monitor_config_empty(client: TestClient, request) -> None:
    _set_monitor_auth(request)
    resp = client.put(_URL, json={"backends": []})
    request.node._resp = resp


def _fake_set_config(session, key, value, updated_by=None):
    return SystemConfig(key=key, value=value, updated_by=updated_by)


@then('the monitor config defaults to the "builtin" backend')
def _bdd_check_default_config(request) -> None:
    data = request.node._resp.json()
    assert data["backends"] == ["builtin"], f"Expected [builtin], got {data['backends']}"


@then(parsers.parse('the monitor config includes the "{backend}" backend'))
def _bdd_check_config_includes(backend: str, request) -> None:
    data = request.node._resp.json()
    assert backend in data["backends"], f"Expected {backend} in {data['backends']}"


@then('the error mentions "admin"')
def _bdd_check_error_admin(request) -> None:
    assert "admin" in request.node._resp.json()["detail"].lower()


@then('the error mentions "migration"')
def _bdd_check_error_migration(request) -> None:
    assert "migration" in request.node._resp.json()["detail"].lower()
