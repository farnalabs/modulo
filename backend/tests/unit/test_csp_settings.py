"""Tests for CSP monitor domains parsing — custom validator that rejects semicolons."""

import pytest

from modulo.settings import Settings


class TestCspSettings:
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

    def test_rejects_semicolons_to_prevent_csp_injection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv(
            "MODULO_MONITOR_DOMAINS",
            "https://evil.com; script-src 'none'",
        )
        with pytest.raises(ValueError, match="semicolons"):
            Settings()
