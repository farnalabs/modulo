"""Unit tests for POST /api/v1/schemas/generate endpoint."""

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
from modulo.core.schema_registry import SchemaGenerationError
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
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
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate", return_value=expected_schema),
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


def test_generate_schema_threads_session_into_model_backend(client: TestClient) -> None:
    """Regression (FAR-519): the model-backend path (``_generate_schema`` ->

    ``_resolve_model_backend``) must build the secrets backend with the DB
    session. The default FernetSecretsBackend raises
    ``RuntimeError('no DB session')`` inside ``get_secret`` before the
    ``credentials_ciphertext`` fallback applies, and ``ModelBackendHub.initialise``
    only catches ``TimeoutError``/``KeyError`` — so without a session every model
    backend init becomes a blanket 502 ("Failed to initialise model backend for
    generation") and ``POST /api/v1/schemas/generate`` cannot complete end-to-end."""
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    captured_backends: list[object] = []

    def fake_create_backend(*args: object, **kwargs: object) -> MagicMock:
        obj = MagicMock()
        obj._session = kwargs.get("session")
        return obj

    async def spy_initialise(*args: object, **kwargs: object) -> None:
        captured_backends.append(kwargs.get("secrets_backend"))

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise", side_effect=spy_initialise),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate", return_value={"type": "object"}),
        patch("modulo.api.routes.schemas.create_secrets_backend", fake_create_backend),
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
    # The model-backend path (``_generate_schema`` -> ``_resolve_model_backend``)
    # must build the secrets backend with the DB session — this is the exact
    # regression the PR-Reviewer MAJOR flagged for schemas.py:1073.
    assert captured_backends, "ModelBackendHub.initialise was never called"
    assert all(b is not None and getattr(b, "_session", None) is not None for b in captured_backends), (
        "model-backend secrets backend must carry the DB session"
    )


def test_generate_schema_threads_session_into_model_backend_decrypt(client: TestClient) -> None:
    """Regression (FAR-522): the ModelBackendHub decrypt path in
    ``_generate_schema`` must build its secrets backend with the session and
    re-assert the org scope in the SAME transaction, or model-backend
    credentials never decrypt and generation 502s (silent skip / degraded)."""
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    backend_sessions: list[object] = []
    events: list[tuple[str, object]] = []

    def fake_create_backend(*args: object, **kwargs: object) -> object:
        backend = MagicMock()
        backend._session = kwargs.get("session")
        events.append(("create_backend", backend._session))  # type: ignore[arg-type]
        return backend

    async def fake_hub_init(self: object, instances: object, secrets_backend: object) -> None:
        backend_sessions.append(secrets_backend._session)  # type: ignore[attr-defined]

    def spy_set_rls_org(session: object, org_id: object) -> object:
        events.append(("rls", org_id))

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org", side_effect=spy_set_rls_org) as mock_rls,
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise", fake_hub_init),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch(
            "modulo.api.routes.schemas.SchemaGenerationService.generate",
            return_value={"type": "object", "properties": {}},
        ),
        patch("modulo.api.routes.schemas.create_secrets_backend", fake_create_backend),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "A user profile with name and email"},
        )

    assert resp.status_code == 200
    assert backend_sessions, "ModelBackendHub must receive a secrets backend for the model-backend decrypt"
    assert all(s is not None for s in backend_sessions), (
        "model-backend decrypt secrets backend must carry the DB session, or credentials never decrypt"
    )
    # ORG-SCOPE AT DECRYPT (MAJOR regression): the decrypt is the only secrets
    # backend built by the generate path. It must re-assert the org scope AFTER
    # building that backend (the endpoint-context loader scope is transaction-
    # local and gone once it commits). A missing re-assert leaves no set_rls_org
    # call after the final backend build.
    last_backend_build = next(i for i, (kind, _) in reversed(list(enumerate(events))) if kind == "create_backend")
    assert any(kind == "rls" and org == _ORG_ID for i, (kind, org) in enumerate(events) if i > last_backend_build), (
        "model-backend decrypt must re-assert the org scope (set_rls_org) in the same "
        "transaction as the credential decrypt"
    )
    # Sanity: the endpoint-context loader re-assert happens before the build.
    assert mock_rls.call_count >= 2


def test_generate_schema_no_examples_returns_200(client: TestClient) -> None:
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()

    expected_schema = {"type": "object", "properties": {}}

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate", return_value=expected_schema),
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
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch(
            "modulo.api.routes.schemas.SchemaGenerationService.generate",
            side_effect=SchemaGenerationError("LLM returned garbage"),
        ),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.core.audit_logger.append_audit_event", return_value=None),
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


def test_generate_schema_forwards_examples_to_generation_service(client: TestClient) -> None:
    """The request ``examples`` field must reach SchemaGenerationService.generate."""
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    expected_schema = {"type": "object", "properties": {}}
    examples = [{"name": "Alice", "email": "alice@example.com"}]

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch(
            "modulo.api.routes.schemas.SchemaGenerationService.generate",
            return_value=expected_schema,
        ) as mock_generate,
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "A profile", "examples": examples},
        )

    assert resp.status_code == 200
    mock_generate.assert_awaited_once()
    assert mock_generate.await_args.kwargs["examples"] == examples


def test_generate_schema_extra_fields_accepted(client: TestClient) -> None:
    mb = _make_mock_model_backend()
    page_result = MagicMock(items=[mb], total=1, page=1, page_size=1)
    backend_id = uuid.uuid4()
    expected_schema = {"type": "object", "properties": {}}

    with (
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.SchemaGenerationService.generate", return_value=expected_schema),
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


def test_generate_schema_programming_error_returns_501(client: TestClient) -> None:
    """Missing DB table (migration not applied) must surface as 501."""
    with (
        patch(
            "modulo.api.routes.schemas.list_model_backends",
            side_effect=ProgrammingError("stmt", {}, Exception("table does not exist")),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "Issues tracker schema", "examples": []},
        )
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_generate_schema_sqlalchemy_error_returns_503(client: TestClient) -> None:
    """Connection/deadlock failures must surface as 503, not 500."""
    with (
        patch(
            "modulo.api.routes.schemas.list_model_backends",
            side_effect=SQLAlchemyError("connection reset"),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/generate",
            json={"description": "Issues tracker schema", "examples": []},
        )
    assert resp.status_code == 503
