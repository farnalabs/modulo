"""Unit tests for the admin telemetry opt-in/opt-out endpoints (FAR-1131).

Exercises the REAL ``GET``/``PUT /api/v1/admin/telemetry`` routes against the
real ``RuntimeConfigStore`` (only the OTel reconfiguration is stubbed) so the
wire contract is validated end-to-end:

* ``system.config.manage`` permission gate (403 for non-system-admins,
  401 unauthenticated),
* ``200`` round-trip of the ``{"enabled": bool}`` payload,
* ``PUT`` persists the runtime-config override.
"""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_config.store import RuntimeConfigStore
from modulo.settings import Settings, get_settings

_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


async def _override_session() -> AsyncGenerator[MagicMock, None]:
    yield MagicMock()


@pytest.fixture(autouse=True)
def runtime_config_store() -> Generator[RuntimeConfigStore, None, None]:
    """Give each test a fresh runtime-config store, mirroring production wiring."""
    store = RuntimeConfigStore()
    with patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store):
        yield store


@pytest.fixture(autouse=True)
def _no_otel_reconfigure() -> Generator[None, None, None]:
    """Keep PUT from mutating global OTel state while still persisting the override."""
    with (
        patch("modulo.otel_bridge.export.setup_otel"),
        patch("modulo.settings.get_settings", return_value=_make_settings()),
    ):
        yield


def _client(principal: AuthenticatedPrincipal | None) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    if principal is not None:
        app.dependency_overrides[get_current_user] = lambda: principal
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    """System admin — passes the ``require_system_permission`` gate."""
    yield from _client(
        AuthenticatedPrincipal(
            username="sysadmin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
            is_system_admin=True,
        )
    )


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    """Org viewer without the system-admin flag — must be denied."""
    yield from _client(
        AuthenticatedPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="viewer",
        )
    )


@pytest.fixture
def org_admin_client() -> Generator[TestClient, None, None]:
    """Org admin WITHOUT the system-admin flag — no org-role fall-through."""
    yield from _client(
        AuthenticatedPrincipal(
            username="orgadmin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
    )


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    yield from _client(None)


class TestTelemetryPermissionGate:
    def test_get_403_for_viewer(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/api/v1/admin/telemetry")
        assert resp.status_code == 403

    def test_get_403_for_org_admin_without_system_flag(self, org_admin_client: TestClient) -> None:
        resp = org_admin_client.get("/api/v1/admin/telemetry")
        assert resp.status_code == 403
        assert "system.config.manage" in resp.json()["detail"]

    def test_put_403_for_non_system_admin(self, viewer_client: TestClient) -> None:
        resp = viewer_client.put("/api/v1/admin/telemetry", json={"enabled": True})
        assert resp.status_code == 403

    def test_get_401_for_unauthenticated(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/telemetry")
        assert resp.status_code == 401


class TestTelemetryRoundTrip:
    def test_get_returns_enabled_boolean(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/admin/telemetry")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"enabled"}
        assert isinstance(body["enabled"], bool)

    def test_put_enable_round_trips(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/telemetry", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True}

    def test_put_disable_round_trips(self, admin_client: TestClient) -> None:
        admin_client.put("/api/v1/admin/telemetry", json={"enabled": True})
        resp = admin_client.put("/api/v1/admin/telemetry", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}

    def test_put_persists_override_in_store(
        self, admin_client: TestClient, runtime_config_store: RuntimeConfigStore
    ) -> None:
        resp = admin_client.put("/api/v1/admin/telemetry", json={"enabled": True})
        assert resp.status_code == 200
        assert runtime_config_store.get("MODULO_TELEMETRY_ENABLED") == "true"
        # A subsequent GET reflects the persisted override.
        assert admin_client.get("/api/v1/admin/telemetry").json() == {"enabled": True}

    def test_put_rejects_non_boolean_payload(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/telemetry", json={"enabled": "yes"})
        assert resp.status_code == 422

    def test_put_missing_field_is_422(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/telemetry", json={})
        assert resp.status_code == 422
