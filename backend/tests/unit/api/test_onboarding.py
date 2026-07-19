"""Unit tests for /api/v1/onboarding endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.onboarding_progress import OnboardingProgress
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture()
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_progress(
    completed: list[str] | None = None,
    skipped: list[str] | None = None,
    dismissed: bool = False,
) -> OnboardingProgress:
    p = OnboardingProgress(
        organisation_id=_ORG_ID,
        completed_actions=completed or [],
        skipped_actions=skipped or [],
        dismissed=dismissed,
    )
    p.id = uuid.uuid4()
    return p


# ---------------------------------------------------------------------------
# GET /api/v1/onboarding/status
# ---------------------------------------------------------------------------


def test_get_status_first_run(client: TestClient, mock_session: AsyncMock) -> None:
    with (
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
        patch("modulo.api.routes.onboarding._check_auto_completion") as mock_auto,
    ):
        mock_get_progress.return_value = _make_progress()
        mock_auto.return_value = set()

        resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_first_run"] is True
    assert data["completed_actions"] == []
    assert data["skipped_actions"] == []
    assert data["dismissed"] is False
    assert data["progress_pct"] == 0.0
    assert len(data["actions"]) == 6
    assert data["actions"][0]["id"] == "login"
    assert data["actions"][0]["completed"] is False


def test_get_status_with_completed_actions(client: TestClient, mock_session: AsyncMock) -> None:
    with (
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
        patch("modulo.api.routes.onboarding._check_auto_completion") as mock_auto,
    ):
        mock_get_progress.return_value = _make_progress(completed=["login", "add_ai_model"])
        mock_auto.return_value = set()

        resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_first_run"] is False
    assert "login" in data["completed_actions"]
    assert "add_ai_model" in data["completed_actions"]
    assert data["progress_pct"] == pytest.approx(33.3, rel=0.1)


def test_get_status_with_auto_detection(client: TestClient, mock_session: AsyncMock) -> None:
    with (
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
        patch("modulo.api.routes.onboarding._check_auto_completion") as mock_auto,
    ):
        mock_get_progress.return_value = _make_progress()
        mock_auto.return_value = {"login", "has_pipelines"}

        resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert "login" in data["completed_actions"]
    assert data["actions"][0]["completed"] is True


def test_get_status_dismissed(client: TestClient, mock_session: AsyncMock) -> None:
    with (
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
        patch("modulo.api.routes.onboarding._check_auto_completion") as mock_auto,
    ):
        mock_get_progress.return_value = _make_progress(completed=["login"], dismissed=True)
        mock_auto.return_value = set()

        resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["dismissed"] is True
    assert data["is_first_run"] is False


def test_get_status_skipped_actions(client: TestClient, mock_session: AsyncMock) -> None:
    with (
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
        patch("modulo.api.routes.onboarding._check_auto_completion") as mock_auto,
    ):
        mock_get_progress.return_value = _make_progress(skipped=["add_ai_model", "create_first_agent"])
        mock_auto.return_value = set()

        resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert "add_ai_model" in data["skipped_actions"]
    skipped_action = next(a for a in data["actions"] if a["id"] == "add_ai_model")
    assert skipped_action["skipped"] is True


def test_get_status_requires_auth(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/onboarding/status")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /api/v1/onboarding/actions/{action_id}/complete
# ---------------------------------------------------------------------------


def test_mark_action_complete_valid(client: TestClient, mock_session: AsyncMock) -> None:
    with patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress:
        progress = _make_progress()
        mock_get_progress.return_value = progress

        resp = client.post("/api/v1/onboarding/actions/login/complete")

    assert resp.status_code == 200
    data = resp.json()
    assert data["action_id"] == "login"
    assert data["completed"] is True
    assert "login" in progress.completed_actions


def test_mark_action_complete_already_done(client: TestClient, mock_session: AsyncMock) -> None:
    with patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress:
        progress = _make_progress(completed=["login"])
        mock_get_progress.return_value = progress

        resp = client.post("/api/v1/onboarding/actions/login/complete")

    assert resp.status_code == 200
    data = resp.json()
    assert data["action_id"] == "login"
    assert data["completed"] is True


def test_mark_action_complete_invalid(client: TestClient) -> None:
    resp = client.post("/api/v1/onboarding/actions/nonexistent/complete")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/onboarding/actions/{action_id}/skip
# ---------------------------------------------------------------------------


def test_mark_action_skip_valid(client: TestClient, mock_session: AsyncMock) -> None:
    with patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress:
        progress = _make_progress()
        mock_get_progress.return_value = progress

        resp = client.post("/api/v1/onboarding/actions/add_ai_model/skip")

    assert resp.status_code == 200
    data = resp.json()
    assert data["action_id"] == "add_ai_model"
    assert data["skipped"] is True
    assert "add_ai_model" in progress.skipped_actions


def test_mark_action_skip_invalid(client: TestClient) -> None:
    resp = client.post("/api/v1/onboarding/actions/nonexistent/skip")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/onboarding/dismiss
# ---------------------------------------------------------------------------


def test_dismiss_onboarding(client: TestClient, mock_session: AsyncMock) -> None:
    with patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress:
        progress = _make_progress()
        mock_get_progress.return_value = progress

        resp = client.post("/api/v1/onboarding/dismiss")

    assert resp.status_code == 200
    data = resp.json()
    assert data["dismissed"] is True
    assert progress.dismissed is True


# ---------------------------------------------------------------------------
# POST /api/v1/onboarding/seed-examples
# ---------------------------------------------------------------------------


def test_seed_examples(client: TestClient, mock_session: AsyncMock) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()

    mock_agent = MagicMock()
    mock_agent.id = uuid.uuid4()

    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()

    mock_model_backend = MagicMock()
    mock_model_backend.id = uuid.uuid4()

    with (
        patch("modulo.api.routes.onboarding.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.onboarding.create_schema_version") as mock_create_sv,
        patch("modulo.api.routes.onboarding.create_agent", return_value=mock_agent),
        patch("modulo.api.routes.onboarding.create_pipeline", return_value=mock_pipeline),
        patch("modulo.api.routes.onboarding.replace_pipeline_graph"),
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
    ):
        progress = _make_progress()
        mock_get_progress.return_value = progress

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model_backend
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.post("/api/v1/onboarding/seed-examples")

    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_id"] == str(mock_agent.id)
    assert data["schema_id"] == str(mock_schema.id)
    assert data["pipeline_id"] == str(mock_pipeline.id)
    assert mock_create_sv.call_count == 2
    assert "create_first_schema" in progress.completed_actions
    assert "create_first_agent" in progress.completed_actions
    assert "create_first_pipeline" in progress.completed_actions


def test_seed_examples_no_model_backend(client: TestClient, mock_session: AsyncMock) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()

    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()

    with (
        patch("modulo.api.routes.onboarding.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.onboarding.create_schema_version"),
        patch("modulo.api.routes.onboarding.create_pipeline", return_value=mock_pipeline),
        patch("modulo.api.routes.onboarding._get_or_create_progress") as mock_get_progress,
    ):
        progress = _make_progress()
        mock_get_progress.return_value = progress

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.post("/api/v1/onboarding/seed-examples")

    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_id"] is None
    assert data["schema_id"] == str(mock_schema.id)
    assert data["pipeline_id"] == str(mock_pipeline.id)
    assert "create_first_schema" in progress.completed_actions
    assert "create_first_pipeline" in progress.completed_actions


# ---------------------------------------------------------------------------
# POST /api/v1/onboarding/starter-pipeline
# ---------------------------------------------------------------------------


def test_create_starter_pipeline(client: TestClient, mock_session: AsyncMock) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()

    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    mock_pipeline.name = "SDLC Starter Pipeline"

    with (
        patch("modulo.api.routes.onboarding.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.onboarding.create_pipeline", return_value=mock_pipeline),
        patch("modulo.api.routes.onboarding.replace_pipeline_graph"),
    ):
        resp = client.post("/api/v1/onboarding/starter-pipeline")

    assert resp.status_code == 201
    data = resp.json()
    assert data["pipeline_id"] == str(mock_pipeline.id)
    assert data["name"] == "SDLC Starter Pipeline"
