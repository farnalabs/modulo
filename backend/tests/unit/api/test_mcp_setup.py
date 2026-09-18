"""Contract + API tests for the MCP setup handoff endpoint.

The endpoint completes model-backend setup with a one-time browser token and
stores an encrypted API key. Credentials must never leak into responses, and
every failure mode (invalid token, mismatch, already-configured, missing or
invalid encryption) must map to a precise HTTP status.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.api.routes.mcp_setup import router
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_FERNET_KEY = Fernet.generate_key().decode()
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_BACKEND_ID = uuid.uuid4()


def _make_settings(*, fernet_key: str = _FERNET_KEY) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=fernet_key,
        modulo_admin_password="testpass",
        modulo_public_url="https://app.example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _pending_backend() -> MagicMock:
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb.name = "Test Backend"
    mb.status = "pending_setup"
    mb.organisation_id = _ORG_ID
    return mb


def _consumed_token(resource_id: uuid.UUID = _BACKEND_ID) -> SimpleNamespace:
    return SimpleNamespace(
        resource_id=resource_id,
        completed_at=datetime.now(UTC),
    )


def test_complete_setup_accepts_unwrapped_request_body():
    app_schema = FastAPI()
    app_schema.include_router(router)

    operation = app_schema.openapi()["paths"]["/api/v1/model-backends/{backend_id}/complete-setup"]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert body_schema == {"$ref": "#/components/schemas/CompleteSetupRequest"}
    request_schema = app_schema.openapi()["components"]["schemas"]["CompleteSetupRequest"]
    assert set(request_schema["properties"]) == {"token", "api_key"}


class TestCompleteModelBackendSetup:
    def test_success_encrypts_api_key_and_activates(self, client: TestClient) -> None:
        captured_updates: dict = {}

        async def fake_update(session: object, backend_id: object, updates: dict) -> MagicMock:
            captured_updates.update(updates)
            updated = MagicMock()
            updated.id = _BACKEND_ID
            updated.name = "Test Backend"
            return updated

        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=_pending_backend()),
            patch("modulo.api.routes.mcp_setup.update_model_backend", new=fake_update),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "one-time-token", "api_key": "sk-super-secret"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "ok", "backend_id": str(_BACKEND_ID), "name": "Test Backend"}
        assert captured_updates["status"] == "active"
        # the stored value must be the encrypted ciphertext, never the raw key
        ciphertext = captured_updates["credentials_ciphertext"]
        assert isinstance(ciphertext, bytes)
        assert b"sk-super-secret" not in ciphertext
        assert Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode() == "sk-super-secret"

    def test_invalid_token_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch("modulo.api.routes.mcp_setup.consume_handoff", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "wrong-token", "api_key": "sk-x"},
            )

        assert resp.status_code == 404
        assert "Token not found, expired, or already used" in resp.json()["detail"]

    def test_token_resource_mismatch_returns_400(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(resource_id=uuid.uuid4()),
            ),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "other-backend-token", "api_key": "sk-x"},
            )

        assert resp.status_code == 400
        assert "Token does not match" in resp.json()["detail"]

    def test_backend_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=None),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 404
        assert "backend_not_found" in resp.json()["detail"]

    def test_complete_setup_foreign_org_returns_404(self, client: TestClient) -> None:
        """IDOR regression: completing setup for a model backend owned by a
        foreign org must be denied with 404 (backend_not_found), not succeed or
        surface a 500. The endpoint already has a None-branch test; this proves
        the ownership (organisation_id) branch too."""
        foreign_backend = _pending_backend()
        foreign_backend.organisation_id = uuid.uuid4()
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=foreign_backend),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 404
        assert "backend_not_found" in resp.json()["detail"]

    def test_already_configured_backend_returns_400(self, client: TestClient) -> None:
        backend = _pending_backend()
        backend.status = "active"
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=backend),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 400
        assert "already configured" in resp.json()["detail"].lower()

    def test_missing_fernet_key_returns_500(self, client: TestClient) -> None:
        empty_key_settings = MagicMock()
        empty_key_settings.fernet_key = ""
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=empty_key_settings),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=_pending_backend()),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 500
        assert "Encryption is not configured" in resp.json()["detail"]

    def test_invalid_fernet_key_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings(fernet_key="x" * 44)),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=_pending_backend()),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 500
        assert "Failed to initialise encryption" in resp.json()["detail"]

    def test_update_failure_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                return_value=_consumed_token(),
            ),
            patch("modulo.api.routes.mcp_setup.get_model_backend", return_value=_pending_backend()),
            patch("modulo.api.routes.mcp_setup.update_model_backend", return_value=None),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 500
        assert "Failed to update model backend" in resp.json()["detail"]

    def test_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                side_effect=SQLAlchemyError_("mock", "mock", "mock"),
            ),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()

    def test_unexpected_error_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_setup.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.routes.mcp_setup.consume_handoff",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch("modulo.api.routes.mcp_setup.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )

        assert resp.status_code == 500
        assert "unexpected" in resp.json()["detail"].lower()

    def test_unauthenticated_returns_4xx(self) -> None:
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        try:
            resp = TestClient(app).post(
                f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup",
                json={"token": "token", "api_key": "sk-x"},
            )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code in (401, 403)

    def test_missing_request_fields_returns_422(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/model-backends/{_BACKEND_ID}/complete-setup", json={})
        assert resp.status_code == 422
