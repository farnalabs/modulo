"""Unit tests for POST /api/v1/schemas/infer endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.schema_registry import SchemaInferenceError
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
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


def test_infer_schema_forwards_filters_to_sampling(client: TestClient) -> None:
    """The request ``sample_query.filters`` must reach the connector sampling call."""
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    expected_schema = {"type": "object", "properties": {}}
    filters = {"state": "open", "labels": ["bug"]}

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch(
            "modulo.api.routes.schemas.ConnectorHub.sample",
            return_value=[{"id": "1"}],
        ) as mock_sample,
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
                    "filters": filters,
                    "limit": 5,
                },
            },
        )

    assert resp.status_code == 200
    mock_sample.assert_awaited_once()
    assert mock_sample.await_args.kwargs["filters"] == filters
    assert mock_sample.await_args.kwargs["limit"] == 5


def test_infer_schema_passes_connector_type_to_service(client: TestClient) -> None:
    """The inference service must be constructed with the connector's type so
    the prompt applies connector-type-aware field-extraction guidance."""
    ci = _make_mock_connector_instance()
    ci.connector_type_id = "jira"
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=[{"summary": "x"}]),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.SchemaInferenceService", autospec=True) as mock_service_cls,
    ):
        mock_service_cls.return_value.infer = AsyncMock(
            return_value={"type": "object", "properties": {"summary": {"type": "string"}}}
        )
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 5},
            },
        )

    assert resp.status_code == 200
    assert mock_service_cls.call_args.kwargs["connector_type"] == "jira"


def test_infer_schema_default_limit_is_200(client: TestClient) -> None:
    """When the client omits ``limit``, the default sample size must be 200
    per PRD §8.16 (previously 10)."""
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch(
            "modulo.api.routes.schemas.ConnectorHub.sample",
            return_value=[{"id": "1"}],
        ) as mock_sample,
        patch(
            "modulo.api.routes.schemas.SchemaInferenceService.infer",
            return_value={"type": "object", "properties": {}},
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
    mock_sample.assert_awaited_once()
    assert mock_sample.await_args.kwargs["limit"] == 200
    assert resp.json()["sample_count"] == 1


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


def test_infer_schema_sampling_timeout_returns_504_problem(client: TestClient) -> None:
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", side_effect=TimeoutError),
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

    assert resp.status_code == 504
    body = resp.json()
    assert body["type"] == "urn:problem:modulo:gateway_timeout"
    assert body["title"] == "Gateway Timeout"
    assert body["status"] == 504
    assert "timed out" in body["detail"].lower()


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


def test_infer_schema_emits_audit_event_with_tool_source_and_model(client: TestClient) -> None:
    """Successful inference records an audit event carrying the tool source,
    connector type, resource, sample count, and model backend used."""
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=[{"id": "1", "title": "Test"}]),
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
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock) as mock_append,
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 5},
            },
        )

    assert resp.status_code == 200
    mock_append.assert_awaited_once()
    call = mock_append.await_args
    assert call.kwargs["event_type"] == "schema_inference_completed"
    assert call.kwargs["resource_type"] == "connector_instance"
    assert call.kwargs["resource_id"] == _CONNECTOR_ID
    payload = call.kwargs["payload"]
    assert payload["connector_name"] == "Test Connector"
    assert payload["connector_type"] == "github"
    assert payload["resource"] == "issues"
    assert payload["sample_count"] == 1
    assert payload["model_backend_id"] == str(backend_id)


def test_infer_schema_response_does_not_contain_or_persist_sample_records(client: TestClient) -> None:
    """Sampled data must never be persisted or echoed back — the response only
    carries the inferred definition and metadata, never the raw sample records."""
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    secret_ish = "sample-secret-value-2f8c1a"
    samples = [{"id": "1", "api_token": secret_ish, "title": "internal note"}]
    expected_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=samples),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 5},
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {
        "definition_json",
        "sample_count",
        "suggestion_name",
        "suggestion_description",
        "rare_fields",
    }
    assert secret_ish not in resp.text
    assert data["definition_json"] == expected_schema
    assert not data["rare_fields"]


def test_infer_schema_flags_rare_fields_in_response(client: TestClient) -> None:
    """Fields present in fewer than 10% of the sampled records must be listed
    in the response so the operator sees what the draft excludes by default."""
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    samples = [{"id": str(i), "title": "t"} for i in range(11)]
    samples[0]["story_points"] = 5
    expected_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=samples),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 11},
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["rare_fields"] == ["story_points"]


def test_infer_schema_rare_fields_empty_when_all_fields_common(client: TestClient) -> None:
    """A sample set where every field appears in at least 10% of records must
    produce an empty rare_fields list."""
    ci = _make_mock_connector_instance()
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    samples = [{"id": str(i), "title": f"t{i}"} for i in range(11)]
    expected_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=samples),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 11},
            },
        )

    assert resp.status_code == 200
    assert not resp.json()["rare_fields"]


def test_infer_schema_programming_error_returns_501(client: TestClient) -> None:
    """Missing DB table (migration not applied) must surface as 501."""
    with (
        patch(
            "modulo.api.routes.schemas.get_connector_instance",
            side_effect=ProgrammingError("stmt", {}, Exception("table does not exist")),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues"},
            },
        )
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_infer_schema_sqlalchemy_error_returns_503(client: TestClient) -> None:
    """Connection/deadlock failures must surface as 503, not 500."""
    with (
        patch(
            "modulo.api.routes.schemas.get_connector_instance",
            side_effect=SQLAlchemyError("connection reset"),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(_CONNECTOR_ID),
                "sample_query": {"resource": "issues"},
            },
        )
    assert resp.status_code == 503
