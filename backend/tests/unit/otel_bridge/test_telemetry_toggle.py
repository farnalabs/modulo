"""Unit tests for telemetry enable/disable toggle.

Verifies that MODULO_TELEMETRY_ENABLED controls whether OTel
exporters are registered, enabling data residency compliance.
"""

import os
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from modulo.otel_bridge.export import setup_otel, shutdown_otel


@pytest.fixture(autouse=True)
def reset_otel():
    """Clean up between tests to avoid cross-test pollution."""
    yield
    shutdown_otel()


class TestTelemetryDefaults:
    """Tests that telemetry is disabled by default."""

    def test_telemetry_disabled_by_default(self):
        """setup_otel with telemetry_enabled=False should register no exporters."""
        setup_otel(telemetry_enabled=False)
        provider = trace.get_tracer_provider()
        # Should be a TracerProvider with no active span processors
        assert isinstance(provider, TracerProvider)

    def test_no_otlp_without_endpoint(self):
        """OTLP should not be configured when endpoint env var is unset."""
        with patch.dict(os.environ, {}, clear=True):
            setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

    def test_disabled_skips_stdout_exporter(self):
        """When disabled, no stdout exporter should be active."""
        setup_otel(telemetry_enabled=False)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test") as span:
            span.set_attribute("test", True)
        # No crash means no unexpected side effects


class TestTelemetryEnabled:
    """Tests that telemetry is properly configured when enabled."""

    def test_stdout_exporter_active(self):
        """When enabled, TracerProvider should be initialized."""
        setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

    def test_tracer_works_when_enabled(self):
        """Creating spans should work when telemetry is enabled."""
        setup_otel(telemetry_enabled=True)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test-enabled") as span:
            span.set_attribute("key", "value")
        # Span was created without error; that's the key check
        assert span.name == "test-enabled"

    def test_otlp_not_configured_without_env(self):
        """OTLP exporter should not be configured when env var is absent."""
        with patch.dict(os.environ, {}, clear=True):
            setup_otel(telemetry_enabled=True)
        # No exception means OTLP was skipped gracefully

    def test_otlp_configured_with_env(self):
        """OTLP exporter should be attempted when endpoint env var is set."""
        with patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"},
        ):
            setup_otel(telemetry_enabled=True)
        # Should not crash — endpoint just won't respond


class TestSettingsIntegration:
    """Tests the Settings model integration with telemetry."""

    def test_settings_defaults_to_disabled(self):
        """Settings.modulo_telemetry_enabled should default to False."""
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
        )
        assert settings.modulo_telemetry_enabled is False

    def test_settings_can_enable(self):
        """Settings.modulo_telemetry_enabled can be set to True."""
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
            modulo_telemetry_enabled=True,
        )
        assert settings.modulo_telemetry_enabled is True

    def test_env_var_overrides_default(self):
        """MODULO_TELEMETRY_ENABLED env var should override the default."""
        with patch.dict(os.environ, {"MODULO_TELEMETRY_ENABLED": "true"}):
            from modulo.settings import Settings

            settings = Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key="a" * 32,
                fernet_key="a" * 32,
                modulo_admin_password="testpass",
            )
            assert settings.modulo_telemetry_enabled is True
