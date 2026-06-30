"""Unit tests for observability route resilience.

Tests the in-memory cache, degraded response fallback, and timeout/error
handling added to prevent the GET /api/v1/settings/observability endpoint
from hanging when the database is unreachable.
"""

import uuid

import pytest

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

_ORG_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear module-level cache before and after each test."""
    _config_cache.clear()
    _config_cache_ts.clear()
    yield
    _config_cache.clear()
    _config_cache_ts.clear()


class TestObservabilityCache:
    def test_cache_miss_returns_none(self) -> None:
        assert _cached_config(_ORG_ID) is None

    def test_cache_hit_after_update(self) -> None:
        config = {"otlp_endpoint": "http://collector:4318"}
        _update_cache(_ORG_ID, config)
        cached = _cached_config(_ORG_ID)
        assert cached is not None
        assert cached["otlp_endpoint"] == "http://collector:4318"

    def test_cache_returns_copy_not_reference(self) -> None:
        config = {"otlp_endpoint": "http://collector:4318"}
        _update_cache(_ORG_ID, config)
        cached = _cached_config(_ORG_ID)
        cached["otlp_endpoint"] = "http://other:4318"
        # Original cache entry should be unchanged
        second = _cached_config(_ORG_ID)
        assert second["otlp_endpoint"] == "http://collector:4318"

    def test_invalidate_cache_clears_entry(self) -> None:
        _update_cache(_ORG_ID, {"otlp_endpoint": "http://collector:4318"})
        _invalidate_cache(_ORG_ID)
        assert _cached_config(_ORG_ID) is None

    def test_invalidate_unknown_org_does_not_raise(self) -> None:
        _invalidate_cache("nonexistent-org")  # should not raise

    def test_cache_uses_org_id_isolation(self) -> None:
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        _update_cache(org_a, {"otlp_endpoint": "http://a:4318"})
        _update_cache(org_b, {"otlp_endpoint": "http://b:4318"})
        assert _cached_config(org_a)["otlp_endpoint"] == "http://a:4318"
        assert _cached_config(org_b)["otlp_endpoint"] == "http://b:4318"


class TestDegradedResponse:
    def test_degraded_with_no_cache_uses_defaults(self) -> None:
        resp = _build_degraded_response(_ORG_ID)
        assert resp.otlp_endpoint == ""
        assert resp.export_interval_seconds == 10
        assert resp.langsmith_enabled is False
        assert resp.has_langsmith_api_key is False

    def test_degraded_with_stale_cache_returns_cached(self) -> None:
        _update_cache(_ORG_ID, {"otlp_endpoint": "http://cached:4318", "langsmith_enabled": True})
        resp = _build_degraded_response(_ORG_ID)
        assert resp.otlp_endpoint == "http://cached:4318"
        assert resp.langsmith_enabled is True

    def test_degraded_response_always_returns_200_fields(self) -> None:
        resp = _build_degraded_response(_ORG_ID)
        assert resp.otlp_endpoint is not None
        assert resp.otlp_headers is not None
        assert resp.effective_otlp_endpoint is not None
        assert isinstance(resp.env_override_active, bool)

    def test_degraded_response_catches_missing_keys(self) -> None:
        _update_cache(_ORG_ID, {})  # empty config
        resp = _build_degraded_response(_ORG_ID)
        # Should fall through to defaults for missing keys
        assert resp.export_interval_seconds == 10
        assert resp.langsmith_enabled is False


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
        assert _DEFAULT_OTEL_CONFIG["otlp_endpoint"] == ""
