"""Unit tests for telemetry enable/disable toggle.

Verifies that MODULO_TELEMETRY_ENABLED controls whether OTel
exporters are registered, enabling data residency compliance.
"""

import os
from collections.abc import Generator
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

from modulo.otel_bridge.export import setup_otel, shutdown_otel


def _reset_global_provider() -> None:
    """Replace the global TracerProvider.

    OTel allows set_tracer_provider() to be called only once per process, so
    the internal Once guard must be reset before swapping the provider. The
    provider is assigned directly (not via set_tracer_provider) so the fresh
    guard stays unconsumed, letting each test's setup_otel() call take effect.
    """
    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = TracerProvider()  # type: ignore[attr-defined]


def _span_processors(provider: TracerProvider) -> tuple:
    return provider._active_span_processor._span_processors  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def reset_otel() -> Generator[None, None, None]:
    """Reset global OTel state between tests to avoid cross-test pollution."""
    _reset_global_provider()
    yield
    shutdown_otel()
    _reset_global_provider()


class TestTelemetryDefaults:
    """Tests that telemetry is disabled by default."""

    def test_telemetry_disabled_registers_no_exporters(self):
        """telemetry_enabled=False should register no span processors."""
        setup_otel(telemetry_enabled=False)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert _span_processors(provider) == ()

    def test_disabled_stdout_exporter_not_used(self):
        """When disabled, the ConsoleSpanExporter must not be instantiated."""
        with patch("modulo.otel_bridge.export.ConsoleSpanExporter") as mock_console:
            setup_otel(telemetry_enabled=False)
        mock_console.assert_not_called()

    def test_disabled_otlp_exporter_not_used(self):
        """When disabled, OTLP must not be configured even with endpoint set."""
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}), patch(
            "modulo.otel_bridge.export.OTLPSpanExporter"
        ) as mock_otlp:
            setup_otel(telemetry_enabled=False)
        mock_otlp.assert_not_called()

    def test_disabled_tracer_still_works(self):
        """Span creation must work with telemetry disabled."""
        setup_otel(telemetry_enabled=False)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test") as span:
            span.set_attribute("test", True)
        assert span.name == "test"


class TestTelemetryEnabled:
    """Tests that telemetry is properly configured when enabled."""

    def test_enabled_registers_stdout_exporter(self):
        """telemetry_enabled=True should register a stdout span processor."""
        setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert len(_span_processors(provider)) == 1

    def test_enabled_instantiates_console_exporter(self):
        """When enabled, the ConsoleSpanExporter should be constructed."""
        with patch("modulo.otel_bridge.export.ConsoleSpanExporter") as mock_console:
            setup_otel(telemetry_enabled=True)
        mock_console.assert_called_once()

    def test_tracer_works_when_enabled(self):
        """Creating spans should work when telemetry is enabled."""
        setup_otel(telemetry_enabled=True)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test-enabled") as span:
            span.set_attribute("key", "value")
        assert span.name == "test-enabled"

    def test_otlp_not_configured_without_env(self):
        """OTLP exporter should not be configured when env var is absent."""
        with patch.dict(os.environ, {}, clear=True), patch(
            "modulo.otel_bridge.export.OTLPSpanExporter"
        ) as mock_otlp:
            setup_otel(telemetry_enabled=True)
        mock_otlp.assert_not_called()

    def test_otlp_configured_with_env(self):
        """OTLP exporter should be constructed when endpoint env var is set."""
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}), patch(
            "modulo.otel_bridge.export.OTLPSpanExporter"
        ) as mock_otlp:
            setup_otel(telemetry_enabled=True)
        mock_otlp.assert_called_once()

    def test_otlp_configured_with_env_keeps_stdout(self):
        """Both stdout and OTLP processors should be active when endpoint is set."""
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}):
            setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert len(_span_processors(provider)) == 2


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
