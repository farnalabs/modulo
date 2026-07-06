"""Unit tests for /api/v1/onboarding endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
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


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
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


@pytest.fixture(autouse=True)
def _clean_onboarding_state() -> Generator[None, None, None]:
    """Replace file-based persistence with in-memory for tests."""
    import modulo.api.routes.onboarding as onboarding_mod

    original_load = onboarding_mod._load_onboarding_json
    original_save = onboarding_mod._save_onboarding_state

    state_store: dict[str, Any] | None = None

    def fake_load() -> dict[str, Any] | None:
        return state_store

    def fake_save(state: Any) -> None:
        nonlocal state_store
        state_store = {"is_first_run": state.is_first_run, "completed_steps": state.completed_steps}

    onboarding_mod._load_onboarding_json = fake_load
    onboarding_mod._save_onboarding_state = fake_save

    yield

    onboarding_mod._load_onboarding_json = original_load
    onboarding_mod._save_onboarding_state = original_save


# ---------------------------------------------------------------------------
# GET /api/v1/onboarding/status
# ---------------------------------------------------------------------------


def _mock_no_pipelines(mock_session: AsyncMock) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result


def test_get_status_first_run(client: TestClient, mock_session: AsyncMock) -> None:
    _mock_no_pipelines(mock_session)
    resp = client.get("/api/v1/onboarding/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_first_run"] is True
    assert data["completed_steps"] == []
    assert data["current_step"] == 1
    assert data["total_steps"] == 4


def test_get_status_with_completed_steps(client: TestClient, mock_session: AsyncMock) -> None:
    from modulo.api.routes.onboarding import _OnboardingState, _save_onboarding_state

    _save_onboarding_state(
        _OnboardingState(
            is_first_run=True,
            completed_steps=["connect_tools", "select_template"],
        )
    )

    _mock_no_pipelines(mock_session)

    resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_first_run"] is True
    assert data["completed_steps"] == ["connect_tools", "select_template"]
    assert data["current_step"] == 3


def test_get_status_not_first_run(client: TestClient, mock_session: AsyncMock) -> None:
    mock_pipeline_obj = MagicMock()
    mock_pipeline_obj.id = uuid.uuid4()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_pipeline_obj
    mock_session.execute.return_value = mock_result

    resp = client.get("/api/v1/onboarding/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_first_run"] is False
    assert data["completed_steps"] == []


def test_get_status_requires_auth(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/onboarding/status")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /api/v1/onboarding/step
# ---------------------------------------------------------------------------


def test_mark_step_valid(client: TestClient, mock_session: AsyncMock) -> None:
    _mock_no_pipelines(mock_session)

    resp = client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["step_id"] == "connect_tools"
    assert data["completed"] is True
    assert "connect_tools" in data["completed_steps"]


def test_mark_step_invalid(client: TestClient) -> None:
    resp = client.post("/api/v1/onboarding/step", json={"step_id": "invalid_step"})
    assert resp.status_code == 422


def test_mark_step_already_completed(client: TestClient, mock_session: AsyncMock) -> None:
    _mock_no_pipelines(mock_session)

    resp1 = client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
    assert resp1.status_code == 200

    resp2 = client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
    assert resp2.status_code == 200
    assert len(resp2.json()["completed_steps"]) == 1


def test_mark_step_all_completed(client: TestClient, mock_session: AsyncMock) -> None:
    _mock_no_pipelines(mock_session)

    for step_id in ["connect_tools", "select_template", "configure_agent", "run_demo"]:
        resp = client.post("/api/v1/onboarding/step", json={"step_id": step_id})
        assert resp.status_code == 200

    resp = client.get("/api/v1/onboarding/status")
    data = resp.json()
    assert data["is_first_run"] is False
    assert len(data["completed_steps"]) == 4
    assert data["current_step"] is None


# ---------------------------------------------------------------------------
# GET /api/v1/onboarding/step/{step_id}
# ---------------------------------------------------------------------------


def test_get_step_data_valid(client: TestClient, mock_session: AsyncMock) -> None:
    _mock_no_pipelines(mock_session)

    resp = client.get("/api/v1/onboarding/step/connect_tools")
    assert resp.status_code == 200
    data = resp.json()
    assert data["step_id"] == "connect_tools"
    assert data["label"] == "Connect Tooling"
    assert data["order"] == 1
    assert "connectors" in data["data"]


def test_get_step_data_not_found(client: TestClient) -> None:
    resp = client.get("/api/v1/onboarding/step/nonexistent")
    assert resp.status_code == 404
