"""Unit tests: ProgrammingError on publisher admin routes returns 501."""
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


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("mock", {}, "table not found"))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

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


class TestListPublishersProgrammingError:
    def test_list_publishers_returns_501(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/publishers")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestCreatePublisherProgrammingError:
    def test_create_publisher_returns_501(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/publishers",
            json={
                "name": "Test Pub",
                "contact_email": "pub@test.com",
                "public_key_hex": "ab" * 32,
                "trust_tier": "green",
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestUpdatePublisherProgrammingError:
    def test_update_publisher_returns_501(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/admin/publishers/{uuid.uuid4()}",
            json={"name": "Updated Name"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestDeletePublisherProgrammingError:
    def test_delete_publisher_returns_501(self, client: TestClient) -> None:
        resp = client.delete(f"/api/v1/admin/publishers/{uuid.uuid4()}")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()
