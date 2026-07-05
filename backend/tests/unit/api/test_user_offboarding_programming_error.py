"""Unit tests for user offboarding routes — error handling and validation."""
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_OTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _execute_side_effect(*args: object, **kwargs: object) -> None:
    raise ProgrammingError("mock", {}, "")


@pytest.fixture()
def broken_session() -> AsyncMock:
    mock_session = AsyncMock()
    mock_session.execute.side_effect = _execute_side_effect
    mock_session.begin = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(side_effect=_execute_side_effect),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    return mock_session


@pytest.fixture()
def client_admin(broken_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield broken_session

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


class TestAdminUserDeactivateProgrammingError:
    URL = "/api/v1/admin/users"

    def test_deactivate_returns_501(self, client_admin: TestClient) -> None:
        target_id = str(_OTHER_USER_ID)
        resp = client_admin.post(f"{self.URL}/{target_id}/deactivate")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_reactivate_returns_501(self, client_admin: TestClient) -> None:
        target_id = str(_OTHER_USER_ID)
        resp = client_admin.post(f"{self.URL}/{target_id}/reactivate")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_deactivate_malformed_uuid_returns_422(self, client_admin: TestClient) -> None:
        resp = client_admin.post(f"{self.URL}/not-a-uuid/deactivate")
        assert resp.status_code == 422

    def test_reactivate_malformed_uuid_returns_422(self, client_admin: TestClient) -> None:
        resp = client_admin.post(f"{self.URL}/not-a-uuid/reactivate")
        assert resp.status_code == 422
