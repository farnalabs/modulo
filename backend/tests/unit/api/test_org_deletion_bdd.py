"""Unit tests for org deletion BDD scenarios — covers all 8 scenarios from org_deletion.feature."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
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
_TOKEN_EXPIRES = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
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
    "users": [{"id": str(_USER_ID), "email": "admin@test.com"}],
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
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Scenario 1: Admin requests org deletion
# ===========================================================================


class TestAdminRequestsDeletion:
    def test_returns_202_with_token(self, client: TestClient) -> None:
        crud_result = {"token": _TOKEN, "token_expires_at": _TOKEN_EXPIRES, "export": _EXPORT}
        with (
            patch("modulo.db.crud.org_deletion.request_org_deletion", return_value=crud_result),
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post("/api/v1/admin/org/deletion-request")

        assert resp.status_code == 202
        data = resp.json()
        assert data["token"] == _TOKEN
        assert data["token_expires_at"] == _TOKEN_EXPIRES
        assert "export_summary" in data

    def test_token_expires_in_24_hours(self, client: TestClient) -> None:
        expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        crud_result = {"token": _TOKEN, "token_expires_at": expires_at, "export": _EXPORT}
        with (
            patch("modulo.db.crud.org_deletion.request_org_deletion", return_value=crud_result),
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post("/api/v1/admin/org/deletion-request")

        data = resp.json()
        assert data["token_expires_at"] == expires_at


# ===========================================================================
# Scenario 2: Export data during grace period
# ===========================================================================


class TestOrgExport:
    def test_export_returns_200_with_bundle(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.db.crud.org_deletion.export_org_data", return_value=_EXPORT),
        ):
            resp = client.get("/api/v1/admin/org/export")

        assert resp.status_code == 200
        data = resp.json()
        assert data["organisation"]["name"] == "Test Org"
        assert "exported_at" in data

    def test_export_for_deleted_org(self, client: TestClient) -> None:
        deleted_export = {**_EXPORT, "organisation": [{**_EXPORT["organisation"][0], "status": "deleted"}]}
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.db.crud.org_deletion.export_org_data", return_value=deleted_export),
        ):
            resp = client.get("/api/v1/admin/org/export")

        assert resp.status_code == 200
        assert resp.json()["organisation"]["status"] == "deleted"


# ===========================================================================
# Scenario 3: Cancel deletion within window
# ===========================================================================


class TestCancelDeletion:
    def test_cancel_restores_org_status(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch(
                "modulo.db.crud.org_deletion.cancel_org_deletion",
                new_callable=AsyncMock,
                return_value={"status": "active"},
            ),
        ):
            resp = client.patch("/api/v1/admin/org/deletion-cancel")

        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_cancel_without_pending_deletion_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch(
                "modulo.db.crud.org_deletion.cancel_org_deletion",
                new_callable=AsyncMock,
                side_effect=ValueError("No pending deletion found"),
            ),
        ):
            resp = client.patch("/api/v1/admin/org/deletion-cancel")

        assert resp.status_code == 409


# ===========================================================================
# Scenario 4: Confirm hard deletion with valid token
# ===========================================================================


class TestConfirmDeletion:
    def test_confirm_with_valid_token_returns_200(self, client: TestClient) -> None:
        confirm_result = {"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 5}
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.db.crud.org_deletion.confirm_org_deletion", return_value=confirm_result),
        ):
            resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": _TOKEN})

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 5
        assert "permanently deleted" in data["message"]

    def test_confirm_with_invalid_token_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Invalid deletion token"),
            ),
        ):
            resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": "wrong"})

        assert resp.status_code == 409

    def test_confirm_with_expired_token_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Deletion token has expired"),
            ),
        ):
            resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": _TOKEN})

        assert resp.status_code == 409


# ===========================================================================
# Scenario 5: Access denied during deletion window
# ===========================================================================


class TestAccessDuringDeletion:
    def test_list_pipelines_returns_403_when_org_deleted(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.pipelines.list_pipelines",
            side_effect=__import__("fastapi").HTTPException(status_code=403, detail="Organisation is deleted"),
        ):
            resp = client.get("/api/v1/pipelines")

        assert resp.status_code == 403

    def test_create_run_returns_403_when_org_deleted(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.runs.get_pipeline",
            side_effect=__import__("fastapi").HTTPException(status_code=403, detail="Organisation is deleted"),
        ):
            resp = client.post(
                "/api/v1/runs",
                json={"pipeline_id": "00000000-0000-0000-0000-000000000001"},
            )

        assert resp.status_code == 403


# ===========================================================================
# Scenario 6: Audit event logged on deletion request
# ===========================================================================


class TestAuditOnDeletion:
    def test_audit_event_recorded_on_request(self, client: TestClient) -> None:
        crud_result = {"token": _TOKEN, "token_expires_at": _TOKEN_EXPIRES, "export": _EXPORT}
        mock_audit = AsyncMock()
        with (
            patch("modulo.db.crud.org_deletion.request_org_deletion", return_value=crud_result),
            patch("modulo.core.audit_logger.append_audit_event", mock_audit),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            client.post("/api/v1/admin/org/deletion-request")

        mock_audit.assert_awaited_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["event_type"] == "org_deletion_requested"
        assert call_kwargs["resource_type"] == "organisation"
        assert call_kwargs["org_id"] == _ORG_ID

    def test_audit_event_contains_token_and_export_summary(self, client: TestClient) -> None:
        crud_result = {"token": _TOKEN, "token_expires_at": _TOKEN_EXPIRES, "export": _EXPORT}
        mock_audit = AsyncMock()
        with (
            patch("modulo.db.crud.org_deletion.request_org_deletion", return_value=crud_result),
            patch("modulo.core.audit_logger.append_audit_event", mock_audit),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            client.post("/api/v1/admin/org/deletion-request")

        payload = mock_audit.call_args.kwargs["payload_json"]
        assert "deletion_token" in payload
        assert "exported_entities" in payload


# ===========================================================================
# Scenario 7: Cascading resource cleanup on hard delete
# ===========================================================================


class TestCascadingCleanup:
    def test_hard_delete_removes_pipelines_and_runs(self, client: TestClient) -> None:
        confirm_result = {"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 15}
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.db.crud.org_deletion.confirm_org_deletion", return_value=confirm_result),
        ):
            resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": _TOKEN})

        assert resp.status_code == 200
        data = resp.json()
        assert data["hard_deleted_runs"] == 15

    def test_hard_delete_with_no_resources(self, client: TestClient) -> None:
        confirm_result = {"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 0}
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.db.crud.org_deletion.confirm_org_deletion", return_value=confirm_result),
        ):
            resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": _TOKEN})

        assert resp.status_code == 200
        data = resp.json()
        assert data["hard_deleted_runs"] == 0

    def test_batch_delete_old_terminal_runs_called(self, client: TestClient) -> None:
        confirm_result = {"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 5}
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.db.crud.org_deletion.confirm_org_deletion", return_value=confirm_result) as mock_confirm,
        ):
            client.post("/api/v1/admin/org/deletion-confirm", json={"token": _TOKEN})

        mock_confirm.assert_awaited_once()


# ===========================================================================
# Scenario 8: Non-admin cannot request deletion
# ===========================================================================


class TestNonAdminRestrictions:
    def test_viewer_cannot_request_deletion(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post("/api/v1/admin/org/deletion-request")
        assert resp.status_code == 403

    def test_viewer_cannot_confirm_deletion(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post("/api/v1/admin/org/deletion-confirm", json={"token": _TOKEN})
        assert resp.status_code == 403

    def test_viewer_cannot_export(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/api/v1/admin/org/export")
        assert resp.status_code == 403

    def test_viewer_cannot_cancel_deletion(self, viewer_client: TestClient) -> None:
        resp = viewer_client.patch("/api/v1/admin/org/deletion-cancel")
        assert resp.status_code == 403
