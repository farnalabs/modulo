"""Unit tests for /api/v1/model-backends endpoints.

Credentials must NEVER appear in responses — only `has_credentials: true/false`.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_BACKEND_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_backend(credentials_ciphertext: bytes = b"encrypted") -> MagicMock:
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb.organisation_id = _ORG_ID
    mb.name = "Test Backend"
    mb.display_name = "GPT-4"
    mb.provider = "openai"
    mb.model_id = "gpt-4"
    mb.credentials_ciphertext = credentials_ciphertext
    mb.default_params = {}
    mb.visibility = "org"
    mb.tier = "native"
    mb.fallback_backend_ids = None
    mb.account_id = uuid.uuid4()
    mb.created_at = _NOW
    mb.updated_at = _NOW
    return mb


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Default: no duplicate found for name check
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)
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
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


_CREATE_BODY = {
    "name": "Test Backend",
    "display_name": "GPT-4",
    "provider": "openai",
    "model_id": "gpt-4",
    "api_key": "sk-test",
}


def test_list_model_backends_returns_200(client: TestClient) -> None:
    page_result = MagicMock(items=[_make_backend()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_create_model_backend_does_not_expose_credentials(client: TestClient) -> None:
    backend = _make_backend(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json=_CREATE_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert "api_key" not in body
    assert body["has_credentials"] is True


def test_create_model_backend_with_fallback_ids(client: TestClient) -> None:
    """Verify fallback_backend_ids are passed to create_model_backend."""
    fallback_id = uuid.uuid4()
    captured: list[list[str] | None] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs.get("fallback_backend_ids"))
        backend = _make_backend()
        backend.fallback_backend_ids = kwargs.get("fallback_backend_ids")
        return backend  # type: ignore[return-value]

    with (
        patch("modulo.api.routes.model_backends.create_model_backend", new=fake_create),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        body = {**_CREATE_BODY, "fallback_backend_ids": [str(fallback_id)]}
        resp = client.post("/api/v1/model-backends", json=body)

    assert resp.status_code == 201
    assert captured == [[str(fallback_id)]]
    assert resp.json()["fallback_backend_ids"] == [str(fallback_id)]


def test_create_model_backend_encrypts_api_key(client: TestClient) -> None:
    captured: list[bytes] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs["credentials_ciphertext"])  # type: ignore[arg-type]
        return _make_backend(credentials_ciphertext=kwargs["credentials_ciphertext"])  # type: ignore[arg-type]

    with (
        patch("modulo.api.routes.model_backends.create_model_backend", new=fake_create),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        client.post("/api/v1/model-backends", json=_CREATE_BODY)

    assert captured, "create_model_backend was not called"
    ciphertext = captured[0]
    assert isinstance(ciphertext, bytes)
    # Decrypt to verify the api_key was stored
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    assert decrypted == "sk-test"
    assert b"sk-test" not in ciphertext


def test_get_model_backend_returns_200_without_credentials(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert body["has_credentials"] is True
    assert body["fallback_backend_ids"] is None


def test_get_model_backend_with_fallback_ids_in_response(client: TestClient) -> None:
    """Response includes fallback_backend_ids when set."""
    fallback_id = uuid.uuid4()
    backend = _make_backend()
    backend.fallback_backend_ids = [str(fallback_id)]
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 200
    assert resp.json()["fallback_backend_ids"] == [str(fallback_id)]


def test_get_model_backend_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=None),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_update_model_backend_returns_200(client: TestClient) -> None:
    backend = _make_backend()
    backend.name = "Updated"
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"name": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


def test_update_model_backend_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=None),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_model_backend_returns_204(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=True),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 204


def test_delete_model_backend_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=False),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_model_backend_no_credentials_shows_false(client: TestClient) -> None:
    backend = _make_backend(credentials_ciphertext=b"")
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.json()["has_credentials"] is False


def test_list_model_backends_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/model-backends")
    assert resp.status_code in (401, 403)


def test_list_model_backends_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as PE
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", side_effect=PE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_create_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as PE
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", side_effect=PE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json={
            "name": "x", "display_name": "x", "provider": "openai",
            "model_id": "gpt-4", "api_key": "sk-test",
        })
    assert resp.status_code == 501


def test_get_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as PE
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", side_effect=PE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 501


def test_update_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as PE
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", side_effect=PE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 501


def test_delete_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as PE
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", side_effect=PE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 501


def test_create_model_backend_duplicate_name_returns_409(client: TestClient) -> None:
    """Duplicate backend name within same org should return 409."""
    mock_session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = MagicMock()  # existing row found
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    client.app.dependency_overrides[get_db_session] = override_session

    with (
        patch("modulo.api.routes.model_backends.create_model_backend"),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        body = {**_CREATE_BODY, "name": "duplicate"}
        resp = client.post("/api/v1/model-backends", json=body)

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_create_model_backend_invalid_provider_returns_422(client: TestClient) -> None:
    """Creating a backend with an unsupported provider returns 422."""
    body = {**_CREATE_BODY, "provider": "nonexistent_provider"}
    resp = client.post("/api/v1/model-backends", json=body)
    assert resp.status_code == 422
    detail_str = resp.text
    assert "nonexistent_provider" in detail_str


def test_create_model_backend_invalid_provider_returns_422_via_plugins(client: TestClient) -> None:
    """Provider that fails plugin registry check also returns 422."""
    from modulo.api.routes.model_backends import _VALID_PROVIDERS
    saved = dict.fromkeys(_VALID_PROVIDERS, True)
    try:
        _VALID_PROVIDERS.clear()
        with patch("modulo.api.routes.model_backends.get_plugin_registry") as mock_reg:
            mock_reg.return_value.has_model_backend.return_value = False
            body = {**_CREATE_BODY, "provider": "unknown"}
            resp = client.post("/api/v1/model-backends", json=body)
        assert resp.status_code == 422
    finally:
        _VALID_PROVIDERS.update(saved)


def test_create_azure_openai_model_backend_round_trips(client: TestClient) -> None:
    """Creating an azure_openai backend preserves provider and model_id in response."""
    azure_body = {**_CREATE_BODY, "provider": "azure_openai"}
    backend = _make_backend(credentials_ciphertext=b"encrypted_bytes")
    backend.provider = "azure_openai"
    backend.model_id = "gpt-4-deployment"
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json=azure_body)

    assert resp.status_code == 201
    body = resp.json()
    assert body["provider"] == "azure_openai"
    assert "credentials_ciphertext" not in body
    assert "api_key" not in body
    assert body["has_credentials"] is True


def test_list_model_backends_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SAE
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", side_effect=SAE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 503


def test_create_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SAE
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", side_effect=SAE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json={
            "name": "x", "display_name": "x", "provider": "openai",
            "model_id": "gpt-4", "api_key": "sk-test",
        })
    assert resp.status_code == 503


def test_get_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SAE
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", side_effect=SAE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 503


def test_update_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SAE
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", side_effect=SAE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 503


def test_delete_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SAE
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", side_effect=SAE("mock", "mock", "mock")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 503
