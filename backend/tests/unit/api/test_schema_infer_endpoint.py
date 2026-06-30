"""Unit tests for POST /api/v1/schemas/infer endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.schema_registry import SchemaInferenceError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",  # nosec — test-only value
    )


def _make_mock_connector_instance() -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.name = "Test Connector"
    ci.connector_type_id = "github"
    ci.config_json = {}
    ci.credentials_ciphertext = b"encrypted"
    ci.visibility = "org"
    ci.allowed_operations = None
    return ci


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
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_infer_schema_returns_200(client: TestClient) -> None:
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)

    expected_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Unique identifier"},
            "title": {"type": "string", "description": "Issue title"},
        },
        "required": ["id", "title"],
    }

    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=[{"id": "1", "title": "Test"}]),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {
                    "resource": "issues",
                    "filters": {},
                    "limit": 5,
                },
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["definition_json"] == expected_schema
    assert data["sample_count"] == 1
    assert "Inferred from" in data["suggestion_name"]


def test_infer_schema_connector_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(uuid.uuid4()),
                "sample_query": {"resource": "issues"},
            },
        )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_infer_schema_no_backends_returns_400(client: TestClient) -> None:
    ci = _make_mock_connector_instance()
    empty_result = MagicMock(items=[], total=0, page=1, page_size=1)

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=empty_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues"},
            },
        )

    assert resp.status_code == 400
    assert "no model backends" in resp.json()["detail"].lower()


def test_infer_schema_sampling_failure_returns_502(client: TestClient) -> None:
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", side_effect=RuntimeError("Connection refused")),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues"},
            },
        )

    assert resp.status_code == 502
    assert "failed to sample" in resp.json()["detail"].lower()


def test_infer_schema_inference_failure_returns_502(client: TestClient) -> None:
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=[{"id": "1"}]),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch(
            "modulo.api.routes.schemas.SchemaInferenceService.infer",
            side_effect=SchemaInferenceError("LLM returned garbage"),
        ),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues"},
            },
        )

    assert resp.status_code == 502
    assert "schema inference failed" in resp.json()["detail"].lower()


def test_infer_schema_empty_resource_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/infer",
        json={
            "connector_instance_id": str(_CONNECTOR_ID),
            "sample_query": {"resource": ""},
        },
    )
    assert resp.status_code == 422


def test_infer_schema_defaults_filters_and_limit(client: TestClient) -> None:
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=[{"id": "1"}]),
        patch(
            "modulo.api.routes.schemas.SchemaInferenceService.infer", return_value={"type": "object", "properties": {}}
        ),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues"},
            },
        )

    assert resp.status_code == 200
    assert resp.json()["sample_count"] == 1


def test_infer_schema_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.post(
        "/api/v1/schemas/infer",
        json={
            "connector_instance_id": str(_CONNECTOR_ID),
            "sample_query": {"resource": "issues"},
        },
    )
    assert resp.status_code in (401, 403)
