"""Unit tests complementing the BDD feature: Schema Inference."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_connector_instance(name: str = "Test Connector") -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.name = name
    ci.organisation_id = _ORG_ID
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
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _base_infer_patches(ci, mb, records, expected_schema, backend_id=None):
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    if backend_id is None:
        backend_id = uuid.uuid4()

    return [
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=records),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ]


def _patch_and_post(client, patches, connector_id, resource="issues", filters=None, limit=5):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(connector_id),
                "sample_query": {
                    "resource": resource,
                    "filters": filters or {},
                    "limit": limit,
                },
            },
        )
    return resp


# --- Scenario 1: Infer returns draft schema ---


def test_infer_returns_draft_schema(client: TestClient) -> None:
    ci = _make_mock_connector_instance("github-issues")
    mb = _make_mock_model_backend()
    records = [{"id": 1, "title": "Fix bug"}, {"id": 2, "title": "Add feature"}]
    expected_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "number", "description": "ID"},
            "title": {"type": "string", "description": "Title"},
        },
        "required": ["id", "title"],
    }
    patches = _base_infer_patches(ci, mb, records, expected_schema)
    resp = _patch_and_post(client, patches, ci.id)

    assert resp.status_code == 200
    data = resp.json()
    assert data["definition_json"] == expected_schema
    assert data["sample_count"] == 2
    assert "github-issues" in data["suggestion_name"]
    assert data["suggestion_description"] is not None


# --- Scenario 2: Field type detection ---


def test_infer_detects_field_types(client: TestClient) -> None:
    ci = _make_mock_connector_instance("mixed-types")
    mb = _make_mock_model_backend()
    records = [
        {"title": "Bug", "priority": 1, "completed": False, "tags": ["bug"]},
        {"title": "Feature", "priority": 2, "completed": True, "tags": ["feature", "enhancement"]},
    ]
    expected_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "priority": {"type": "number"},
            "completed": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title"],
    }
    patches = _base_infer_patches(ci, mb, records, expected_schema)
    resp = _patch_and_post(client, patches, ci.id)

    assert resp.status_code == 200
    definition = resp.json()["definition_json"]
    props = definition["properties"]
    assert props["title"]["type"] == "string"
    assert props["priority"]["type"] == "number"
    assert props["completed"]["type"] == "boolean"
    assert props["tags"]["type"] == "array"


# --- Scenario 3: Enum detection ---


def test_infer_suggests_enum_values(client: TestClient) -> None:
    ci = _make_mock_connector_instance("status-source")
    mb = _make_mock_model_backend()
    records = [
        {"id": 1, "status": "open", "title": "A"},
        {"id": 2, "status": "in_progress", "title": "B"},
        {"id": 3, "status": "open", "title": "C"},
        {"id": 4, "status": "closed", "title": "D"},
        {"id": 5, "status": "in_progress", "title": "E"},
    ]
    expected_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "in_progress", "closed"],
            },
        },
        "required": ["status"],
    }
    patches = _base_infer_patches(ci, mb, records, expected_schema)
    resp = _patch_and_post(client, patches, ci.id)

    assert resp.status_code == 200
    status_field = resp.json()["definition_json"]["properties"]["status"]
    assert "enum" in status_field
    assert sorted(status_field["enum"]) == ["closed", "in_progress", "open"]


# --- Scenario 4: Default limit ---


def test_infer_applies_default_limit(client: TestClient) -> None:
    ci = _make_mock_connector_instance("github-issues")
    mb = _make_mock_model_backend()
    records = [{"id": 1, "title": "Item"}]

    captured_limit = {}

    async def capture_sample(connector_id, resource, filters, limit):
        captured_limit["value"] = limit
        return records

    expected_schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": []}
    backend_id = uuid.uuid4()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", side_effect=capture_sample),
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
                "connector_instance_id": str(ci.id),
                "sample_query": {"resource": "issues", "filters": {}},
            },
        )

    assert resp.status_code == 200
    assert captured_limit["value"] == 10


# --- Scenario 5: Select connector instance ---


def test_infer_targets_specific_connector(client: TestClient) -> None:
    ci = _make_mock_connector_instance("jira-tasks")
    mb = _make_mock_model_backend()
    records = [{"id": "JIRA-1", "summary": "Fix login"}]
    expected_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["id"],
    }
    patches = _base_infer_patches(ci, mb, records, expected_schema)
    resp = _patch_and_post(client, patches, ci.id)

    assert resp.status_code == 200
    assert "jira-tasks" in resp.json()["suggestion_name"].lower()


# --- Scenario 6: Publish inferred schema ---


def test_publish_inferred_schema(client: TestClient) -> None:
    ci = _make_mock_connector_instance("github-issues")
    mb = _make_mock_model_backend()
    records = [{"id": 1, "title": "Issue"}]
    expected_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "number"},
            "title": {"type": "string"},
        },
        "required": ["id", "title"],
    }
    patches = _base_infer_patches(ci, mb, records, expected_schema)
    resp = _patch_and_post(client, patches, ci.id)
    assert resp.status_code == 200
    inferred = resp.json()

    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_schema.organisation_id = _ORG_ID
    mock_schema.name = "inferred-schema"
    mock_schema.description = "Inferred from github-issues"
    mock_schema.abstract_name = None
    mock_schema.created_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_schema.created_at = datetime.now()
    mock_schema.updated_at = datetime.now()

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_schema", return_value=mock_schema),
    ):
        create_resp = client.post(
            "/api/v1/schemas",
            json={"name": "inferred-schema", "description": "Inferred from github-issues"},
        )
    assert create_resp.status_code == 201
    schema_id = create_resp.json()["id"]

    mock_sv = MagicMock()
    mock_sv.id = uuid.uuid4()
    mock_sv.organisation_id = _ORG_ID
    mock_sv.schema_id = uuid.UUID(schema_id)
    mock_sv.version = "1.0"
    mock_sv.version_number = 1
    mock_sv.definition_json = inferred["definition_json"]
    mock_sv.published = True
    mock_sv.created_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_sv.created_at = datetime.now()
    mock_sv.updated_at = datetime.now()

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.get_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=mock_sv),
    ):
        version_resp = client.post(
            f"/api/v1/schemas/{schema_id}/versions",
            json={
                "version": "1.0",
                "version_number": 1,
                "definition_json": inferred["definition_json"],
                "published": True,
            },
        )
    assert version_resp.status_code == 201
    version_data = version_resp.json()
    assert version_data["published"] is True
    assert version_data["version"] == "1.0"
    assert version_data["definition_json"] == inferred["definition_json"]
