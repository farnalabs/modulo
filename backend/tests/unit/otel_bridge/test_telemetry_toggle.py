"""Unit tests for telemetry enable/disable toggle.

Verifies that MODULO_TELEMETRY_ENABLED controls whether OTel
exporters are registered, enabling data residency compliance.
"""

from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from modulo.core.runtime_config.store import RuntimeConfigStore
from modulo.core.runtime_config.telemetry_bridge import is_telemetry_enabled, toggle_telemetry
from modulo.otel_bridge.export import setup_otel
from modulo.settings import Settings, get_settings
from tests.unit.otel_bridge.conftest import span_processors

# Minimum env a Settings() instance accepts — hoisted so the settings-based
# tests below do not each restate the same four required values.
_SETTINGS_KWARGS = {
    "database_url": "postgresql+asyncpg://localhost/test",
    "secret_key": "a" * 32,
    "fernet_key": "a" * 32,
    "modulo_admin_password": "testpass",
}


def _settings(**overrides):
    return Settings(**_SETTINGS_KWARGS, **overrides)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the Settings env every ``is_telemetry_enabled`` test depends on.

    ``get_settings`` is an lru_cache, so it is cleared here and again on
    teardown — otherwise a test that reads Settings would poison the next one.
    """
    monkeypatch.setenv("DATABASE_URL", _SETTINGS_KWARGS["database_url"])
    monkeypatch.setenv("SECRET_KEY", _SETTINGS_KWARGS["secret_key"])
    monkeypatch.setenv("FERNET_KEY", _SETTINGS_KWARGS["fernet_key"])
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestTelemetryDefaults:
    """Tests that telemetry is disabled by default."""

    def test_telemetry_disabled_registers_no_exporters(self):
        """telemetry_enabled=False should register no span processors."""
        setup_otel(telemetry_enabled=False)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert not span_processors(provider)

    def test_disabled_stdout_exporter_not_used(self):
        """When disabled, the ConsoleSpanExporter must not be instantiated."""
        with patch("modulo.otel_bridge.export.ConsoleSpanExporter") as mock_console:
            setup_otel(telemetry_enabled=False)
        mock_console.assert_not_called()

    def test_disabled_otlp_exporter_not_used(self, monkeypatch: pytest.MonkeyPatch):
        """When disabled, OTLP must not be configured even with endpoint set."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_otlp:
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

    def test_enabled_registers_stdout_exporter(self, monkeypatch: pytest.MonkeyPatch):
        """telemetry_enabled=True should register a stdout span processor."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert len(span_processors(provider)) == 1

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

    def test_otlp_not_configured_without_env(self, monkeypatch: pytest.MonkeyPatch):
        """OTLP exporter should not be configured when env var is absent."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_otlp:
            setup_otel(telemetry_enabled=True)
        mock_otlp.assert_not_called()

    def test_otlp_configured_with_env(self, monkeypatch: pytest.MonkeyPatch):
        """OTLP exporter should be constructed when endpoint env var is set."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_otlp:
            setup_otel(telemetry_enabled=True)
        mock_otlp.assert_called_once()

    def test_otlp_configured_with_env_keeps_stdout(self, monkeypatch: pytest.MonkeyPatch):
        """Both stdout and OTLP processors should be active when endpoint is set."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert len(span_processors(provider)) == 2


class TestSettingsIntegration:
    """Tests that the Settings model integration with telemetry."""

    def test_settings_defaults_to_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """Settings.modulo_telemetry_enabled should default to False."""
        # Isolate from any ambient MODULO_TELEMETRY_ENABLED in the runner env.
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)
        assert _settings().modulo_telemetry_enabled is False

    def test_settings_can_enable(self, monkeypatch: pytest.MonkeyPatch):
        """Settings.modulo_telemetry_enabled can be set to True."""
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)
        assert _settings(modulo_telemetry_enabled=True).modulo_telemetry_enabled is True

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch):
        """MODULO_TELEMETRY_ENABLED env var should override the default."""
        monkeypatch.setenv("MODULO_TELEMETRY_ENABLED", "true")
        assert _settings().modulo_telemetry_enabled is True


class TestIsTelemetryEnabled:
    """Tests for the is_telemetry_enabled() bridge function (FAR-1131)."""

    def test_falls_back_to_settings_when_store_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings_env: None,
    ):
        """When the runtime config store is not initialised, falls back to Settings."""
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)

        with patch("modulo.core.runtime_config.store.get_runtime_config_store", side_effect=RuntimeError("not init")):
            assert is_telemetry_enabled() is False

    def test_non_runtime_error_propagates(self):
        """Non-RuntimeError exceptions from the store must propagate (not be swallowed)."""
        with (
            patch(
                "modulo.core.runtime_config.store.get_runtime_config_store",
                side_effect=ValueError("bad config"),
            ),
            pytest.raises(ValueError, match="bad config"),
        ):
            is_telemetry_enabled()

    def test_reads_from_store_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings_env: None,
    ):
        """When the store has an override, is_telemetry_enabled reads it."""
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)
        store = RuntimeConfigStore()
        store.set_override("MODULO_TELEMETRY_ENABLED", "true")
        with patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store):
            assert is_telemetry_enabled() is True

    def test_store_false_overrides_env_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings_env: None,
    ):
        """When env is true but store override is false, store wins."""
        monkeypatch.setenv("MODULO_TELEMETRY_ENABLED", "true")
        store = RuntimeConfigStore()
        store.set_override("MODULO_TELEMETRY_ENABLED", "false")
        with patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store):
            assert is_telemetry_enabled() is False

    def test_store_clear_falls_back_to_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings_env: None,
    ):
        """When the store override is cleared, falls back to env/Settings."""
        monkeypatch.setenv("MODULO_TELEMETRY_ENABLED", "true")
        store = RuntimeConfigStore()
        store.set_override("MODULO_TELEMETRY_ENABLED", "true")
        store.clear_override("MODULO_TELEMETRY_ENABLED")
        with patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store):
            assert is_telemetry_enabled() is True

    def test_store_none_value_falls_back_to_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings_env: None,
    ):
        """When the store returns None for the key, fall back to Settings.

        The MODULO_TELEMETRY_ENABLED default is "false" rather than None, so
        this defends the None arm of the store-first check for stores that
        were reset/torn down for test isolation.
        """
        monkeypatch.setenv("MODULO_TELEMETRY_ENABLED", "true")
        store = RuntimeConfigStore()
        with (
            patch.object(store, "get", return_value=None),
            patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store),
        ):
            assert is_telemetry_enabled() is True


class TestToggleTelemetry:
    """Tests for the toggle_telemetry() function (FAR-1131 C2 fix)."""

    @pytest.mark.parametrize(
        ("enabled", "expected_override"),
        [
            pytest.param(True, "true", id="enable-configures-otel"),
            pytest.param(False, "false", id="disable-tears-down-otel"),
        ],
    )
    def test_toggle_writes_override_and_reconfigures_otel(
        self, monkeypatch: pytest.MonkeyPatch, enabled: bool, expected_override: str
    ):
        """The toggle must persist its value AND reconfigure OTel to match it.

        Enable and disable share one code path, so both arms are pinned by one
        parameterised test rather than two copies of the same body.
        """
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        store = RuntimeConfigStore()
        with (
            patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store),
            patch("modulo.otel_bridge.export.setup_otel") as mock_setup,
            patch("modulo.settings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.modulo_otel_service_name = "test-svc"
            toggle_telemetry(enabled)
        assert store.get("MODULO_TELEMETRY_ENABLED") == expected_override
        mock_setup.assert_called_once_with(service_name="test-svc", telemetry_enabled=enabled)

    def test_toggle_is_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        """Calling toggle_telemetry with the same value twice is safe."""
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)

        store = RuntimeConfigStore()
        with (
            patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store),
            patch("modulo.otel_bridge.export.setup_otel"),
            patch("modulo.settings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.modulo_otel_service_name = "test-svc"
            toggle_telemetry(True)
            toggle_telemetry(True)
        assert store.get("MODULO_TELEMETRY_ENABLED") == "true"
