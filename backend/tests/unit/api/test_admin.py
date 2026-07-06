"""Unit tests for /api/v1/admin endpoints (org deletion flow)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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
_NOW = datetime(2025, 6, 1, tzinfo=UTC)
_TOKEN = "test-deletion-token-1234567890abcdef"
_TOKEN_EXPIRES = "2025-06-02T00:00:00+00:00"
_EXPORT = {
    "organisation": [
        {
            "id": str(_ORG_ID),
            "name": "Test Org",
            "slug": "test-org",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    ],
    "memberships": [{"id": str(_USER_ID), "email": "admin@test.com"}],
    "pipelines": [],
    "runs": [],
    "audit_events": [],
    "library_primitives": [],
    "connector_instances": [],
    "model_backends": [],
    "exported_at": "2025-06-01T12:00:00+00:00",
}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDeletionRequest:
    URL = "/api/v1/admin/org/deletion-request"

    def test_admin_requests_deletion_returns_202(self, client: TestClient) -> None:
        crud_result = {
            "token": _TOKEN,
            "token_expires_at": _TOKEN_EXPIRES,
            "export": _EXPORT,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                return_value=crud_result,
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 202
        data = resp.json()
        assert data["token"] == _TOKEN
        assert data["token_expires_at"] == _TOKEN_EXPIRES
        assert data["export_summary"]["organisation"] == "Test Org"
        assert data["export_summary"]["user_count"] == 1

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL)
        assert resp.status_code in (401, 403)

    def test_already_deleted_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation is already deleted"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 409

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 409


class TestDeletionConfirm:
    URL = "/api/v1/admin/org/deletion-confirm"

    def test_admin_confirms_deletion_returns_200(self, client: TestClient) -> None:
        crud_result = {
            "deleted_organisation_id": str(_ORG_ID),
            "hard_deleted_runs": 5,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                return_value=crud_result,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 5
        assert "permanently deleted" in data["message"]

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code in (401, 403)

    def test_invalid_token_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Invalid deletion token"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": "wrong"})
        assert resp.status_code == 409

    def test_expired_token_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Deletion token has expired"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 409

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 409


class TestOrgExport:
    URL = "/api/v1/admin/org/export"

    def test_admin_exports_org_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.export_org_data",
                return_value=_EXPORT,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["organisation"]["name"] == "Test Org"
        assert data["organisation"]["status"] == "active"
        assert data["exported_at"] == "2025-06-01T12:00:00+00:00"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_org_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.export_org_data",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 404


class TestDeleteOrgImmediate:
    URL = "/api/v1/admin/org"

    def test_admin_deletes_org_returns_200(self, client: TestClient) -> None:
        request_result = {
            "token": _TOKEN,
            "token_expires_at": _TOKEN_EXPIRES,
            "export": _EXPORT,
        }
        confirm_result = {
            "deleted_organisation_id": str(_ORG_ID),
            "hard_deleted_runs": 0,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                return_value=request_result,
            ),
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                return_value=confirm_result,
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 0
        assert "permanently deleted" in data["message"]

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.delete(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete(self.URL)
        assert resp.status_code in (401, 403)

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 409

    def test_already_deleted_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation is already deleted"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 409
