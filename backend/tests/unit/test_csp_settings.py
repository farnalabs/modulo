"""Smoke test: Settings model CSP monitoring domains parsing.

Verifies that the Settings model correctly handles the
MODULO_MONITOR_DOMAINS env var for custom monitoring domains
in the Content-Security-Policy connect-src directive.
"""

import pytest

from modulo.settings import Settings


class TestCspSettings:
    def test_default_monitor_domains_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        settings = Settings()
        assert settings.modulo_monitor_domains == ""

    def test_monitor_domains_field_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        settings = Settings()
        assert hasattr(settings, "modulo_monitor_domains")

    def test_custom_monitor_domains_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv(
            "MODULO_MONITOR_DOMAINS",
            "https://faro.mycompany.com https://sentry.mycompany.com",
        )
        settings = Settings()
        assert "https://faro.mycompany.com" in settings.modulo_monitor_domains
        assert "https://sentry.mycompany.com" in settings.modulo_monitor_domains

    def test_empty_monitor_domains_no_extra_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_MONITOR_DOMAINS", "")
        settings = Settings()
        assert settings.modulo_monitor_domains == ""
