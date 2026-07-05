"""Unit tests for /api/v1/notifications endpoints.

Verifies that secrets are never exposed in responses — only stored encrypted.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ENDPOINT_ID = uuid.uuid4()
_FERNET_KEY = Fernet.generate_key().decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_endpoint(**overrides: object) -> MagicMock:
    ep = MagicMock()
    ep.id = overrides.get("id", _ENDPOINT_ID)
    ep.organisation_id = _ORG_ID
    ep.url = overrides.get("url", "https://hooks.example.com/notify")
    ep.secret_ciphertext = overrides.get("secret_ciphertext")
    ep.events = overrides.get("events", '["hitl.review_required"]')
    ep.description = overrides.get("description", "Test endpoint")
    ep.auto_disabled = overrides.get("auto_disabled", False)
    ep.consecutive_dead_letter_count = overrides.get("consecutive_dead_letter_count", 0)
    ep.created_by = _USER_ID
    return ep


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_endpoints_returns_200(client: TestClient) -> None:
    ep = _make_mock_endpoint()
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        result_mock = MagicMock()
        result_mock.scalars.return_value = [ep]
        session.execute = AsyncMock(return_value=result_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/notifications")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["url"] == "https://hooks.example.com/notify"
    assert "secret" not in body[0]


def test_list_endpoints_empty_returns_200(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        result_mock = MagicMock()
        result_mock.scalars.return_value = []
        session.execute = AsyncMock(return_value=result_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get("/api/v1/notifications")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    assert resp.json() == []


def test_create_endpoint_returns_201(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            "/api/v1/notifications",
            json={
                "url": "https://hooks.example.com/notify",
                "events": ["hitl.review_required"],
                "description": "My endpoint",
            },
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == "https://hooks.example.com/notify"
    assert "secret" not in body
    assert body["events"] == ["hitl.review_required"]


def test_create_endpoint_with_secret_encrypts_it(client: TestClient) -> None:
    captured_ciphertext: list[bytes] = []

    import cryptography.fernet

    orig_encrypt = cryptography.fernet.Fernet.encrypt

    def tracking_encrypt(self: object, data: bytes) -> bytes:
        result = orig_encrypt(self, data)
        captured_ciphertext.append(result)
        return result

    with (
        patch.object(cryptography.fernet.Fernet, "encrypt", tracking_encrypt),
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            "/api/v1/notifications",
            json={
                "url": "https://hooks.example.com/notify",
                "secret": "my-webhook-secret",
                "events": ["hitl.review_required"],
            },
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 201
    body = resp.json()
    assert "secret" not in body

    assert len(captured_ciphertext) == 1
    decrypted = cryptography.fernet.Fernet(_FERNET_KEY.encode()).decrypt(captured_ciphertext[0]).decode()
    assert decrypted == "my-webhook-secret"
    assert b"my-webhook-secret" not in captured_ciphertext[0]


def test_create_endpoint_invalid_url_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/notifications",
        json={
            "url": "ftp://bad.example.com",
            "events": [],
        },
    )
    assert resp.status_code == 422


def test_get_endpoint_returns_200(client: TestClient) -> None:
    ep = _make_mock_endpoint()
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/notifications/{_ENDPOINT_ID}")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    assert resp.json()["id"] == str(_ENDPOINT_ID)


def test_get_endpoint_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/notifications/{uuid.uuid4()}")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


def test_get_endpoint_wrong_org_returns_404(client: TestClient) -> None:
    ep = _make_mock_endpoint()
    ep.organisation_id = uuid.UUID("00000000-0000-0000-0000-00000000ffff")
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.get(f"/api/v1/notifications/{_ENDPOINT_ID}")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


def test_update_endpoint_returns_200(client: TestClient) -> None:
    ep = _make_mock_endpoint()
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/notifications/{_ENDPOINT_ID}",
            json={"url": "https://updated.example.com/hook", "description": "Updated"},
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://updated.example.com/hook"


def test_update_endpoint_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.put(
            f"/api/v1/notifications/{uuid.uuid4()}",
            json={"url": "https://updated.example.com/hook"},
        )
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


def test_delete_endpoint_returns_204(client: TestClient) -> None:
    ep = _make_mock_endpoint()
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=ep)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/notifications/{_ENDPOINT_ID}")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 204


def test_delete_endpoint_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.notifications.set_rls_org"),
    ):
        session = _make_mock_session()
        session.get = AsyncMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"/api/v1/notifications/{uuid.uuid4()}")
        client.app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    assert resp.status_code == 404


def test_notifications_unauthenticated_returns_4xx(client: TestClient) -> None:
    client.app.dependency_overrides.pop(get_current_user, None)
    resp = client.get("/api/v1/notifications")
    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    assert resp.status_code in (401, 403)
