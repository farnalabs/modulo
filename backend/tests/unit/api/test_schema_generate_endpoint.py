"""Unit tests for POST /api/v1/schemas/generate endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.schema_registry import SchemaGenerationError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_model_backend() -> MagicMock:
    mb = MagicMock()
    mb.id = uuid.uuid4()
    mb.provider = "anthropic"
    mb.model_id = "claude-sonnet-4-20250514"
    mb.credentials_ciphertext = b"encrypted"
    mb.default_params = {}
    return mb


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
        username="testuser",
        organisation_id=_ORG_ID,
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_generate_schema_returns_200(client: TestClient) -> None:
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    expected_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "User's full name"},
            "email": {"type": "string", "description": "Email address"},
        },
        "required": ["name", "email"],
    }

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.backend_ids",
              new_callable=PropertyMock(return_value=frozenset({backend_id}))),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate",
              return_value=expected_schema),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={
                "description": "A user profile with name and email",
                "examples": [
                    {"name": "Alice", "email": "alice@example.com"},
                ],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["definition_json"] == expected_schema


def test_generate_schema_no_examples_returns_200(client: TestClient) -> None:
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    expected_schema = {"type": "object", "properties": {}}

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.backend_ids",
              new_callable=PropertyMock(return_value=frozenset({backend_id}))),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate",
              return_value=expected_schema),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "An empty schema"},
        )

    assert resp.status_code == 200
    assert resp.json()["definition_json"] == expected_schema


def test_generate_schema_no_backends_returns_400(client: TestClient) -> None:
    empty_result = MagicMock(items=[], total=0, page=1, page_size=1)

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=empty_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "A user profile"},
        )

    assert resp.status_code == 400
    assert "no model backends" in resp.json()["detail"].lower()


def test_generate_schema_generation_failure_returns_502(client: TestClient) -> None:
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.backend_ids",
              new_callable=PropertyMock(return_value=frozenset({backend_id}))),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate",
              side_effect=SchemaGenerationError("LLM returned garbage")),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "A user profile"},
        )

    assert resp.status_code == 502
    assert "schema generation failed" in resp.json()["detail"].lower()


def test_generate_schema_empty_description_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/generate",
        json={"description": ""},
    )
    assert resp.status_code == 422


def test_generate_schema_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.post(
        "/api/v1/schemas/generate",
        json={"description": "A user profile"},
    )
    assert resp.status_code in (401, 403)


def test_generate_schema_null_description_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/generate",
        json={"description": None},
    )
    assert resp.status_code == 422


def test_generate_schema_missing_description_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/generate",
        json={"examples": [{"name": "Alice"}]},
    )
    assert resp.status_code == 422


def test_generate_schema_invalid_examples_type_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/generate",
        json={"description": "A profile", "examples": "not a list"},
    )
    assert resp.status_code == 422


def test_generate_schema_extra_fields_accepted(client: TestClient) -> None:
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    expected_schema = {"type": "object", "properties": {}}

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.backend_ids",
              new_callable=PropertyMock(return_value=frozenset({backend_id}))),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate",
              return_value=expected_schema),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={
                "description": "A profile",
                "examples": [{"name": "Alice"}],
                "extra_field": "should be ignored",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["definition_json"] == expected_schema
