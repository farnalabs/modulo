"""Tests for audit_viewer feature gating via require_feature('audit_viewer')."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_EVENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _settings_without_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
    )


def _settings_with_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="valid-license-key",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _build_client(settings_fn) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = settings_fn
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def client_no_audit() -> Generator[TestClient, None, None]:
    yield from _build_client(_settings_without_license)


@pytest.fixture()
def client_with_audit() -> Generator[TestClient, None, None]:
    yield from _build_client(_settings_with_license)


# ── Audit endpoints return 402 when feature disabled ──


class TestAuditGating:
    def test_list_events_returns_402_when_disabled(self, client_no_audit: TestClient) -> None:
        resp = client_no_audit.get("/api/v1/admin/audit")
        assert resp.status_code == 402
        assert "audit_viewer" in resp.json()["detail"].lower()

    def test_batch_detail_returns_402_when_disabled(self, client_no_audit: TestClient) -> None:
        resp = client_no_audit.post("/api/v1/admin/audit/batch-detail", json={"event_ids": []})
        assert resp.status_code == 402
        assert "audit_viewer" in resp.json()["detail"].lower()

    def test_verify_chain_returns_402_when_disabled(self, client_no_audit: TestClient) -> None:
        resp = client_no_audit.get("/api/v1/admin/audit/verify")
        assert resp.status_code == 402
        assert "audit_viewer" in resp.json()["detail"].lower()

    def test_export_returns_402_when_disabled(self, client_no_audit: TestClient) -> None:
        resp = client_no_audit.get("/api/v1/admin/audit/export")
        assert resp.status_code == 402
        assert "audit_viewer" in resp.json()["detail"].lower()


# ── Audit endpoints succeed when feature enabled ──


class TestAuditSuccess:
    def test_list_events_succeeds_when_enabled(self, client_with_audit: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.audit.list_audit_events",
                return_value={"items": [], "total": 0, "next_cursor": None, "prev_cursor": None, "limit": 50},
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client_with_audit.get("/api/v1/admin/audit")
        assert resp.status_code == 200

    def test_batch_detail_succeeds_when_enabled(self, client_with_audit: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.get_audit_events_batch", return_value=[]),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client_with_audit.post("/api/v1/admin/audit/batch-detail", json={"event_ids": [str(_EVENT_ID)]})
        assert resp.status_code == 200

    def test_verify_chain_succeeds_when_enabled(self, client_with_audit: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.verify_chain", return_value={"valid": True}),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client_with_audit.get("/api/v1/admin/audit/verify")
        assert resp.status_code == 200

    def test_export_succeeds_when_enabled(self, client_with_audit: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.export_chain", return_value={"items": [], "total": 0}),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client_with_audit.get("/api/v1/admin/audit/export")
        assert resp.status_code == 200


# ── Non-audit endpoints are unaffected ──


class TestNonAuditEndpoints:
    def test_health_returns_404(self, client_no_audit: TestClient) -> None:
        resp = client_no_audit.get("/api/v1/health")
        assert resp.status_code in (200, 404)

    def test_login_returns_422_without_body(self, client_no_audit: TestClient) -> None:
        resp = client_no_audit.post("/api/v1/auth/login", json={})
        assert resp.status_code == 422
