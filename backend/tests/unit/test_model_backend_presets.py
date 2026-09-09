"""Unit tests for model backend presets module and /presets endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.model_backend_presets import MODEL_BACKEND_PRESETS, ModelBackendPreset
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_FERNET_KEY = "a" * 32
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


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


def _client_for_session(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    yield from _client_for_session(_make_mock_session())


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Presets module tests
# ---------------------------------------------------------------------------


class TestModelBackendPresets:
    def test_presets_is_nonempty_list(self) -> None:
        assert isinstance(MODEL_BACKEND_PRESETS, list)
        assert len(MODEL_BACKEND_PRESETS) > 0

    def test_each_preset_is_pydantic_model(self) -> None:
        for preset in MODEL_BACKEND_PRESETS:
            assert isinstance(preset, ModelBackendPreset)

    def test_each_preset_has_required_fields(self) -> None:
        for preset in MODEL_BACKEND_PRESETS:
            assert preset.id, "preset id must be non-empty"
            assert preset.provider, "provider must be non-empty"
            assert preset.display_name, "display_name must be non-empty"
            assert preset.default_model_id, "default_model_id must be non-empty"
            assert preset.description, "description must be non-empty"
            assert preset.api_key_docs_url.startswith("http"), "api_key_docs_url must be a URL"

    def test_one_preset_per_required_provider(self) -> None:
        required = {"openai", "anthropic", "gemini", "deepseek", "groq"}
        providers = {p.provider for p in MODEL_BACKEND_PRESETS}
        assert required.issubset(providers), f"Missing providers: {required - providers}"

    def test_preset_provider_matches_id(self) -> None:
        for preset in MODEL_BACKEND_PRESETS:
            assert preset.id == preset.provider, f"Preset id {preset.id!r} should match provider {preset.provider!r}"

    def test_no_duplicate_providers(self) -> None:
        providers = [p.provider for p in MODEL_BACKEND_PRESETS]
        assert len(providers) == len(set(providers)), "Duplicate provider presets found"

    def test_default_model_ids_are_plausible(self) -> None:
        """Spot-check that model ids follow known provider naming conventions."""
        openai = next(p for p in MODEL_BACKEND_PRESETS if p.provider == "openai")
        assert "gpt" in openai.default_model_id.lower()

        anthropic = next(p for p in MODEL_BACKEND_PRESETS if p.provider == "anthropic")
        assert "claude" in anthropic.default_model_id.lower()

        gemini = next(p for p in MODEL_BACKEND_PRESETS if p.provider == "gemini")
        assert "gemini" in gemini.default_model_id.lower()

        deepseek = next(p for p in MODEL_BACKEND_PRESETS if p.provider == "deepseek")
        assert "deepseek" in deepseek.default_model_id.lower()

        groq = next(p for p in MODEL_BACKEND_PRESETS if p.provider == "groq")
        assert "llama" in groq.default_model_id.lower() or "mixtral" in groq.default_model_id.lower()


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


def test_presets_endpoint_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/model-backends/presets")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_presets_endpoint_returns_one_entry_per_provider(client: TestClient) -> None:
    resp = client.get("/api/v1/model-backends/presets")
    assert resp.status_code == 200
    items = resp.json()["items"]
    providers = [item["provider"] for item in items]
    assert len(providers) == len(set(providers)), "Duplicate providers in response"


def test_presets_endpoint_response_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/model-backends/presets")
    assert resp.status_code == 200
    items = resp.json()["items"]
    required_fields = {"id", "provider", "display_name", "default_model_id", "description", "api_key_docs_url"}
    for item in items:
        assert required_fields.issubset(item.keys()), f"Missing fields: {required_fields - item.keys()}"


def test_presets_endpoint_requires_auth(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/model-backends/presets")
    assert resp.status_code in (401, 403)
