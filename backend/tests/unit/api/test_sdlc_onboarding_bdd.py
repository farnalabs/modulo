"""Unit tests for SDLC Onboarding Path (PRD §8.16) BDD scenarios."""

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

_SDLC_STEPS = [
    {"id": "connect_tools", "label": "Connect Tooling", "order": 1},
    {"id": "run_inference", "label": "Run Schema Inference", "order": 2},
    {"id": "review_schemas", "label": "Review and Publish Schemas", "order": 3},
    {"id": "browse_library", "label": "Browse Community Library", "order": 4},
    {"id": "wire_pipeline", "label": "Wire Pipeline", "order": 5},
]


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
def _patch_sdlc_steps() -> Generator[None, None, None]:
    import modulo.api.routes.onboarding as onboarding_mod

    original_steps = onboarding_mod._ONBOARDING_STEPS
    onboarding_mod._ONBOARDING_STEPS = list(_SDLC_STEPS)
    yield
    onboarding_mod._ONBOARDING_STEPS = original_steps


@pytest.fixture(autouse=True)
def _patch_onboarding_persistence() -> Generator[None, None, None]:
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


def _mock_no_pipelines(mock_session: AsyncMock) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result


# ===========================================================================
# Scenario 1: Full SDLC onboarding flow
# ===========================================================================


class TestFullSdlcOnboardingFlow:
    """Complete 5-step SDLC wizard walkthrough."""

    def test_get_status_first_run(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        resp = client.get("/api/v1/onboarding/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_first_run"] is True
        assert data["completed_steps"] == []
        assert data["current_step"] == 1
        assert data["total_steps"] == 5

    def test_full_onboarding_walkthrough(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        for step_id in [s["id"] for s in _SDLC_STEPS]:
            resp = client.post("/api/v1/onboarding/step", json={"step_id": step_id})
            assert resp.status_code == 200, f"Failed marking {step_id}: {resp.text}"

        resp = client.get("/api/v1/onboarding/status")
        data = resp.json()
        assert data["is_first_run"] is False
        assert len(data["completed_steps"]) == 5
        assert data["current_step"] is None


# ===========================================================================
# Scenario 2: Connect tools step shows available connectors
# ===========================================================================


class TestConnectToolsStep:
    """Step 1 connector discovery."""

    def test_step_connect_tools_data(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        resp = client.get("/api/v1/onboarding/step/connect_tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["step_id"] == "connect_tools"
        assert data["order"] == 1
        assert "connectors" in data["data"]
        connector_ids = [c["id"] for c in data["data"]["connectors"]]
        assert "github" in connector_ids
        assert "jira" in connector_ids
        assert "linear" in connector_ids

    def test_mark_connect_tools_advances_step(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
        assert resp.status_code == 200

        resp = client.get("/api/v1/onboarding/status")
        assert resp.json()["current_step"] == 2


# ===========================================================================
# Scenario 3: Run inference
# ===========================================================================


class TestRunInferenceStep:
    """Step 2: schema inference from connected connector."""

    def test_mark_inference_completed(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
        assert resp.status_code == 200

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "run_inference"})
        assert resp.status_code == 200
        assert "run_inference" in resp.json()["completed_steps"]

        resp = client.get("/api/v1/onboarding/status")
        assert resp.json()["current_step"] == 3


# ===========================================================================
# Scenario 4: Review and publish inferred schemas
# ===========================================================================


class TestReviewSchemasStep:
    """Step 3: review and publish."""

    def test_mark_review_schemas_completed(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        for step_id in ["connect_tools", "run_inference"]:
            client.post("/api/v1/onboarding/step", json={"step_id": step_id})

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "review_schemas"})
        assert resp.status_code == 200
        assert "review_schemas" in resp.json()["completed_steps"]

        resp = client.get("/api/v1/onboarding/status")
        assert resp.json()["current_step"] == 4


# ===========================================================================
# Scenario 5: Browse library
# ===========================================================================


class TestBrowseLibraryStep:
    """Step 4: browse filtered library."""

    def test_mark_browse_library_completed(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        for step_id in ["connect_tools", "run_inference", "review_schemas"]:
            client.post("/api/v1/onboarding/step", json={"step_id": step_id})

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "browse_library"})
        assert resp.status_code == 200

        resp = client.get("/api/v1/onboarding/status")
        assert resp.json()["current_step"] == 5


# ===========================================================================
# Scenario 6: Wire pipeline completes onboarding
# ===========================================================================


class TestWirePipelineStep:
    """Step 5: final step marks onboarding complete."""

    def test_wire_pipeline_ends_onboarding(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        for step_id in [s["id"] for s in _SDLC_STEPS]:
            resp = client.post("/api/v1/onboarding/step", json={"step_id": step_id})
            assert resp.status_code == 200

        resp = client.get("/api/v1/onboarding/status")
        data = resp.json()
        assert data["is_first_run"] is False
        assert len(data["completed_steps"]) == 5
        assert data["current_step"] is None


# ===========================================================================
# Scenario 7: Re-run inference (state is tracked independently)
# ===========================================================================


class TestReRunInference:
    """Re-running inference doesn't reset onboarding progress."""

    def test_re_run_inference_preserves_progress(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "run_inference"})
        assert resp.status_code == 200
        first_data = resp.json()

        resp = client.post("/api/v1/onboarding/step", json={"step_id": "run_inference"})
        assert resp.status_code == 200
        second_data = resp.json()

        assert len(second_data["completed_steps"]) == len(first_data["completed_steps"])
        assert second_data["completed_steps"] == first_data["completed_steps"]


# ===========================================================================
# Scenario 8: Onboarding state is persisted across sessions
# ===========================================================================


class TestOnboardingStatePersistence:
    """State survives across HTTP requests via file-backed persistence."""

    def test_state_persists_across_requests(self, client: TestClient, mock_session: AsyncMock) -> None:
        _mock_no_pipelines(mock_session)

        resp1 = client.get("/api/v1/onboarding/status")
        assert resp1.json()["completed_steps"] == []

        client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
        client.post("/api/v1/onboarding/step", json={"step_id": "run_inference"})

        resp2 = client.get("/api/v1/onboarding/status")
        data = resp2.json()
        assert len(data["completed_steps"]) == 2
        assert data["current_step"] == 3

    def test_incomplete_onboarding_returns_correct_step(self, client, mock_session):
        _mock_no_pipelines(mock_session)

        client.post("/api/v1/onboarding/step", json={"step_id": "connect_tools"})
        client.post("/api/v1/onboarding/step", json={"step_id": "run_inference"})
        client.post("/api/v1/onboarding/step", json={"step_id": "review_schemas"})

        resp = client.get("/api/v1/onboarding/status")
        data = resp.json()
        assert data["current_step"] == 4
        assert data["is_first_run"] is True

    def test_auth_required(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/onboarding/status")
        assert resp.status_code in (401, 403)
