"""Unit tests for observability route resilience.

Tests the in-memory cache, degraded response fallback, and timeout/error
handling added to prevent the GET /api/v1/settings/observability endpoint
from hanging when the database is unreachable.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.observability import (
    _DEFAULT_OTEL_CONFIG,
    _build_degraded_response,
    _cached_config,
    _config_cache,
    _config_cache_ts,
    _config_to_response,
    _invalidate_cache,
    _update_cache,
)
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = str(uuid.uuid4())

_VALID_32 = "a" * 32
_FERNET_KEY = "KuV0vzf5ha7CJ3n4Dg_aqO6S4wBNJ31Q1fahdEYHHCo="
_ORG_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_UUID = uuid.UUID("00000000-0000-0000-0000-000000000002")


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
    authz_result = MagicMock()
    authz_result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=authz_result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear module-level cache before and after each test."""
    _config_cache.clear()
    _config_cache_ts.clear()
    yield
    _config_cache.clear()
    _config_cache_ts.clear()


# ── Test client fixtures for route-level integration tests ──────────────────


@pytest.fixture
def free_client() -> Generator[TestClient, None, None]:
    """Client with no license — observability is team-gated."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_UUID,
        account_id=_USER_UUID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_UUID, account_id=_USER_UUID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    with patch("modulo.api.routes.observability.validate_outbound_url_async", new=AsyncMock()):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    """Client with a viewer role — observability config manage is operator-gated."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_UUID,
        account_id=_USER_UUID,
        org_role="viewer",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="viewer", organisation_id=_ORG_UUID, account_id=_USER_UUID, org_role="viewer"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestObservabilityCache:
    def test_cache_returns_copy_not_reference(self) -> None:
        config = {"otlp_endpoint": "http://collector:4318"}
        _update_cache(_ORG_ID, config)
        cached = _cached_config(_ORG_ID)
        assert cached is not None
        cached["otlp_endpoint"] = "http://other:4318"
        second = _cached_config(_ORG_ID)
        assert second is not None
        assert second["otlp_endpoint"] == "http://collector:4318"

    def test_invalidate_cache_clears_entry(self) -> None:
        _update_cache(_ORG_ID, {"otlp_endpoint": "http://collector:4318"})
        _invalidate_cache(_ORG_ID)
        assert _cached_config(_ORG_ID) is None

    def test_invalidate_unknown_org_does_not_raise(self) -> None:
        _update_cache(_ORG_ID, {"otlp_endpoint": "http://collector:4318"})
        _invalidate_cache("nonexistent-org")
        assert _cached_config("nonexistent-org") is None
        assert _cached_config(_ORG_ID) is not None

    def test_cache_uses_org_id_isolation(self) -> None:
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        _update_cache(org_a, {"otlp_endpoint": "http://a:4318"})
        _update_cache(org_b, {"otlp_endpoint": "http://b:4318"})
        a_config = _cached_config(org_a)
        b_config = _cached_config(org_b)
        assert a_config is not None
        assert b_config is not None
        assert a_config["otlp_endpoint"] == "http://a:4318"
        assert b_config["otlp_endpoint"] == "http://b:4318"


class TestDegradedResponse:
    def test_degraded_response_always_returns_200_fields(self) -> None:
        resp = _build_degraded_response(_ORG_ID)
        assert resp.otlp_endpoint is not None
        assert resp.otlp_headers is not None
        assert resp.effective_otlp_endpoint is not None
        assert isinstance(resp.env_override_active, bool)


class TestConfigToResponse:
    def test_no_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        config = {"otlp_endpoint": "http://db:4318", "otlp_headers": {"Authorization": "secret123"}}
        resp = _config_to_response(config)
        assert resp.otlp_endpoint == "http://db:4318"
        assert resp.effective_otlp_endpoint == "http://db:4318"
        assert resp.env_override_active is False

    def test_env_override_takes_effect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env:4318")
        config = {"otlp_endpoint": "http://db:4318"}
        resp = _config_to_response(config)
        assert resp.otlp_endpoint == "http://db:4318"
        assert resp.effective_otlp_endpoint == "http://env:4318"
        assert resp.env_override_active is True

    def test_has_langsmith_api_key_true_when_ciphertext(self) -> None:
        config = {"langsmith_api_key_ciphertext": "encrypted-value"}
        resp = _config_to_response(config)
        assert resp.has_langsmith_api_key is True

    def test_has_langsmith_api_key_false_when_none(self) -> None:
        config = {"langsmith_api_key_ciphertext": None}
        resp = _config_to_response(config)
        assert resp.has_langsmith_api_key is False

    def test_sensitive_headers_are_masked(self) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        config = {
            "otlp_headers": {
                "Authorization": "Bearer tok",
                "x-api-key": "key123",
                "X-Otlp-Token": "tok456",
                "safe-header": "visible",
            }
        }
        resp = _config_to_response(config)
        assert resp.otlp_headers["Authorization"] == SENSITIVE_VALUE_MASK
        assert resp.otlp_headers["x-api-key"] == SENSITIVE_VALUE_MASK
        assert resp.otlp_headers["X-Otlp-Token"] == SENSITIVE_VALUE_MASK
        assert resp.otlp_headers["safe-header"] == "visible"


class TestDefaultConfig:
    def test_defaults_have_all_required_fields(self) -> None:
        assert "otlp_endpoint" in _DEFAULT_OTEL_CONFIG
        assert "otlp_headers" in _DEFAULT_OTEL_CONFIG
        assert "export_interval_seconds" in _DEFAULT_OTEL_CONFIG
        assert "langsmith_enabled" in _DEFAULT_OTEL_CONFIG
        assert "langsmith_api_key_ciphertext" in _DEFAULT_OTEL_CONFIG

    def test_default_endpoint_is_empty(self) -> None:
        assert not _DEFAULT_OTEL_CONFIG["otlp_endpoint"]


# ── GET /api/v1/settings/observability — timeout / error resilience ────────


class TestObservabilityGetEndpoint:
    """Test GET endpoint's timeout and error fallback to degraded response."""

    def test_get_returns_501_on_programming_error(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability._fetch_and_cache",
                side_effect=ProgrammingError("stmt", {}, "table not found"),
            ),
        ):
            resp = free_client.get("/api/v1/settings/observability")
        assert resp.status_code == 501
        body = resp.json()
        assert "migration" in body["detail"].lower()

    def test_get_returns_degraded_on_db_timeout(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=TimeoutError("db timeout")),
        ):
            resp = free_client.get("/api/v1/settings/observability")
        assert resp.status_code == 200
        body = resp.json()
        assert not body["otlp_endpoint"]
        assert body["export_interval_seconds"] == 10
        assert body["langsmith_enabled"] is False
        assert body["env_override_active"] is False

    def test_get_returns_degraded_on_generic_error(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=RuntimeError("unexpected")),
        ):
            resp = free_client.get("/api/v1/settings/observability")
        assert resp.status_code == 200
        body = resp.json()
        assert not body["otlp_endpoint"]
        assert body["has_langsmith_api_key"] is False

    def test_get_uses_stale_cache_on_timeout(self, free_client: TestClient) -> None:
        _update_cache(str(_ORG_UUID), {"otlp_endpoint": "http://cached:4318", "langsmith_enabled": True})
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=TimeoutError("db timeout")),
        ):
            resp = free_client.get("/api/v1/settings/observability")
        assert resp.status_code == 200
        body = resp.json()
        assert body["otlp_endpoint"] == "http://cached:4318"
        assert body["langsmith_enabled"] is True

    def test_get_uses_stale_cache_on_generic_error(self, free_client: TestClient) -> None:
        _update_cache(str(_ORG_UUID), {"otlp_endpoint": "http://cached:4318"})
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=RuntimeError("unexpected")),
        ):
            resp = free_client.get("/api/v1/settings/observability")
        assert resp.status_code == 200
        body = resp.json()
        assert body["otlp_endpoint"] == "http://cached:4318"


# ── GET /api/v1/settings/observability/preview — timeout / error resilience ──


class TestObservabilityPreviewEndpoint:
    """Test preview endpoint's timeout and error fallback."""

    def test_preview_returns_501_on_programming_error(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability._fetch_and_cache",
                side_effect=ProgrammingError("stmt", {}, "table not found"),
            ),
        ):
            resp = free_client.get("/api/v1/settings/observability/preview")
        assert resp.status_code == 501
        body = resp.json()
        assert "migration" in body["detail"].lower()

    def test_preview_returns_defaults_on_db_timeout(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=TimeoutError("db timeout")),
        ):
            resp = free_client.get("/api/v1/settings/observability/preview")
        assert resp.status_code == 200
        body = resp.json()
        assert "sample_span" in body
        assert "config_used" in body
        assert not body["config_used"]["otlp_endpoint"]
        assert body["config_used"]["export_interval_seconds"] == 10
        assert body["config_used"]["langsmith_enabled"] is False

    def test_preview_uses_stale_cache_on_timeout(self, free_client: TestClient) -> None:
        _update_cache(str(_ORG_UUID), {"otlp_endpoint": "http://cached:4318"})
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=TimeoutError("db timeout")),
        ):
            resp = free_client.get("/api/v1/settings/observability/preview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config_used"]["otlp_endpoint"] == "http://cached:4318"

    def test_preview_returns_defaults_on_generic_error(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.observability._fetch_and_cache", side_effect=RuntimeError("unexpected")),
        ):
            resp = free_client.get("/api/v1/settings/observability/preview")
        assert resp.status_code == 200
        body = resp.json()
        assert not body["config_used"]["otlp_endpoint"]


# ── PUT /api/v1/settings/observability — timeout / error resilience ────────


class TestObservabilityPutEndpoint:
    """Test PUT endpoint re-raises TimeoutError and generic errors."""

    def test_put_viewer_gets_403(self, viewer_client: TestClient) -> None:
        """A viewer cannot update observability config (observability.manage floor)."""
        resp = viewer_client.put(
            "/api/v1/settings/observability",
            json={"otlp_endpoint": "http://e:4318"},
        )
        assert resp.status_code == 403

    def test_put_returns_501_on_programming_error(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability.update_otel_config",
                side_effect=ProgrammingError("stmt", {}, "table not found"),
            ),
        ):
            resp = free_client.put("/api/v1/settings/observability", json={"otlp_endpoint": "http://e:4318"})
        assert resp.status_code == 501
        body = resp.json()
        assert "migration" in body["detail"].lower()

    def test_put_returns_503_on_sqlalchemy_error(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability.update_otel_config",
                side_effect=SQLAlchemyError("connection refused"),
            ),
        ):
            resp = free_client.put("/api/v1/settings/observability", json={"otlp_endpoint": "http://e:4318"})
        assert resp.status_code == 503
        body = resp.json()
        assert "temporarily unavailable" in body["detail"].lower()

    def test_put_reraises_on_db_timeout(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.observability.update_otel_config", side_effect=TimeoutError("db timeout")),
        ):
            resp = free_client.put("/api/v1/settings/observability", json={"otlp_endpoint": "http://e:4318"})
        assert resp.status_code == 504
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:gateway_timeout"
        assert body["title"] == "Gateway Timeout"
        assert body["status"] == 504

    def test_put_reraises_on_generic_error(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.observability.update_otel_config", side_effect=RuntimeError("unexpected")),
        ):
            resp = free_client.put("/api/v1/settings/observability", json={"otlp_endpoint": "http://e:4318"})
        assert resp.status_code == 500

    def test_put_success_returns_200(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability.update_otel_config",
                return_value={**dict(_DEFAULT_OTEL_CONFIG), "otlp_endpoint": "http://e:4318"},
            ),
        ):
            resp = free_client.put("/api/v1/settings/observability", json={"otlp_endpoint": "http://e:4318"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["otlp_endpoint"] == "http://e:4318"

    def test_put_updates_langsmith_key(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability.update_otel_config",
                return_value={
                    **dict(_DEFAULT_OTEL_CONFIG),
                    "langsmith_enabled": True,
                    "langsmith_api_key_ciphertext": "encrypted-value",
                },
            ),
        ):
            resp = free_client.put(
                "/api/v1/settings/observability",
                json={"langsmith_enabled": True, "langsmith_api_key": "my-api-key"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["langsmith_enabled"] is True
        assert body["has_langsmith_api_key"] is True

    def test_put_clears_langsmith_key_with_empty_string(self, free_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.observability.update_otel_config",
                return_value={**dict(_DEFAULT_OTEL_CONFIG), "langsmith_api_key_ciphertext": None},
            ),
        ):
            resp = free_client.put(
                "/api/v1/settings/observability",
                json={"langsmith_api_key": ""},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_langsmith_api_key"] is False


# ── POST /api/v1/settings/observability/test — error resilience ────────────


class TestObservabilityTestEndpoint:
    """Test connection endpoint's httpx error handling."""

    def test_test_requires_endpoint(self, free_client: TestClient) -> None:
        resp = free_client.post("/api/v1/settings/observability/test", json={"otlp_endpoint": ""})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "required" in body["message"].lower()

    def test_test_rejects_ssrf_rebind_on_pinned_client(self, free_client: TestClient) -> None:
        """FAR-517: the OTLP test connection must be built through pinned_async_client,
        so a tenant-supplied endpoint whose host re-resolves to a blocked internal
        address (169.254.169.254) is rejected (fail closed) rather than connected
        with a plain unpinned client."""

        async def _fake_pinned(_url: str) -> httpx.AsyncClient:
            raise ValueError(
                "URL hostname collector.example.com resolves to a private/internal "
                "address (169.254.169.254). Add its CIDR to SSRF_ALLOW_PRIVATE_RANGES "
                "to allow this target, or use a public URL."
            )

        with (
            patch("modulo.api.routes.observability.pinned_async_client", new=_fake_pinned),
        ):
            resp = free_client.post(
                "/api/v1/settings/observability/test",
                json={"otlp_endpoint": "http://collector.example.com:4318"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "Rejected" in body["message"]
        assert "169.254.169.254" in body["message"]

    def test_test_handles_timeout(self, free_client: TestClient) -> None:
        with (
            patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timed out")),
        ):
            resp = free_client.post(
                "/api/v1/settings/observability/test",
                json={"otlp_endpoint": "http://collector:4318"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "timed out" in body["message"].lower()

    def test_test_handles_connect_error(self, free_client: TestClient) -> None:
        with (
            patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("connection refused")),
        ):
            resp = free_client.post(
                "/api/v1/settings/observability/test",
                json={"otlp_endpoint": "http://collector:4318"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "connection refused" in body["message"].lower()

    def test_test_handles_server_error(self, free_client: TestClient) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        with (
            patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)),
        ):
            resp = free_client.post(
                "/api/v1/settings/observability/test",
                json={"otlp_endpoint": "http://collector:4318"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "503" in body["message"]

    def test_test_handles_success(self, free_client: TestClient) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        with (
            patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)),
        ):
            resp = free_client.post(
                "/api/v1/settings/observability/test",
                json={"otlp_endpoint": "http://collector:4318"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
