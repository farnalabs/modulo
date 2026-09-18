"""Unit tests for GET /api/v1/model-backends/{id}/pipeline-references endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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


def _make_backend() -> MagicMock:
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb.organisation_id = _ORG_ID
    mb.name = "Test Backend"
    mb.display_name = "GPT-4"
    mb.provider = "openai"
    mb.model_id = "gpt-4"
    mb.credentials_ciphertext = b"encrypted"
    mb.default_params = {}
    mb.visibility = "org"
    mb.owner_team_id = None
    mb.tier = "native"
    mb.fallback_backend_ids = None
    mb.account_id = uuid.uuid4()
    mb.created_at = _NOW
    mb.updated_at = _NOW
    return mb


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)
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
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
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


def test_pipeline_references_404_for_nonexistent_backend(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=None),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}/pipeline-references")
    assert resp.status_code == 404


def test_pipeline_references_empty_state(client: TestClient) -> None:
    page_result = MagicMock(items=[], total=0, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch("modulo.api.routes.model_backends.list_pipeline_references_for_backend", return_value=page_result),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
    assert resp.status_code == 200
    body = resp.json()
    assert not body["items"]
    assert body["total"] == 0


def test_pipeline_references_pagination(client: TestClient) -> None:
    ref = {
        "pipeline_id": str(uuid.uuid4()),
        "pipeline_name": "my-pipeline",
        "agent_name": None,
        "agent_id": None,
        "reference_type": "direct_node",
    }
    page_result = MagicMock(items=[ref], total=5, page=2, page_size=1)
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch(
            "modulo.api.routes.model_backends.list_pipeline_references_for_backend", return_value=page_result
        ) as mock_refs,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(
            f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references",
            params={"page": 2, "page_size": 1},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["page"] == 2
    assert body["page_size"] == 1
    assert len(body["items"]) == 1
    mock_refs.assert_awaited_once()
    call_kwargs = mock_refs.await_args.kwargs
    assert call_kwargs["page"] == 2
    assert call_kwargs["page_size"] == 1


def test_pipeline_references_returns_both_types(client: TestClient) -> None:
    refs = [
        {
            "pipeline_id": str(uuid.uuid4()),
            "pipeline_name": "p1",
            "agent_name": None,
            "agent_id": None,
            "reference_type": "direct_node",
        },
        {
            "pipeline_id": str(uuid.uuid4()),
            "pipeline_name": "p2",
            "agent_name": "my-agent",
            "agent_id": str(uuid.uuid4()),
            "reference_type": "agent",
        },
    ]
    page_result = MagicMock(items=refs, total=2, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch("modulo.api.routes.model_backends.list_pipeline_references_for_backend", return_value=page_result),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["items"][0]["reference_type"] == "direct_node"
    assert body["items"][1]["reference_type"] == "agent"


def test_pipeline_references_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
    assert resp.status_code in (401, 403)


def test_pipeline_references_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as ProgrammingError_

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch(
            "modulo.api.routes.model_backends.list_pipeline_references_for_backend",
            side_effect=ProgrammingError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_pipeline_references_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch(
            "modulo.api.routes.model_backends.list_pipeline_references_for_backend",
            side_effect=SQLAlchemyError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
    assert resp.status_code == 503


def test_pipeline_references_exception_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch(
            "modulo.api.routes.model_backends.list_pipeline_references_for_backend",
            side_effect=TypeError("unexpected None"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
    assert resp.status_code == 500
