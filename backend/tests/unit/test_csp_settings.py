"""Tests for CSP monitor domains parsing — custom validator that rejects semicolons."""

import pytest

from modulo.settings import Settings


def _make_settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
    monkeypatch.setenv("SECRET_KEY", "a" * 32)
    monkeypatch.setenv("FERNET_KEY", "a" * 32)
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)


class TestCspSettings:
    def test_custom_monitor_domains_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(
            monkeypatch,
            MODULO_MONITOR_DOMAINS="https://faro.mycompany.com https://sentry.mycompany.com",
        )
        settings = Settings()
        assert "https://faro.mycompany.com" in settings.modulo_monitor_domains
        assert "https://sentry.mycompany.com" in settings.modulo_monitor_domains

    def test_rejects_semicolons_to_prevent_csp_injection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(
            monkeypatch,
            MODULO_MONITOR_DOMAINS="https://evil.com; script-src 'none'",
        )
        with pytest.raises(ValueError, match="semicolons"):
            Settings()

    def test_empty_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(monkeypatch)
        monkeypatch.delenv("MODULO_MONITOR_DOMAINS", raising=False)
        assert Settings().modulo_monitor_domains == ""

    def test_empty_string_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(monkeypatch, MODULO_MONITOR_DOMAINS="")
        assert Settings().modulo_monitor_domains == ""

    def test_single_domain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(monkeypatch, MODULO_MONITOR_DOMAINS="https://sentry.mycompany.com")
        assert Settings().modulo_monitor_domains == "https://sentry.mycompany.com"

    def test_trailing_whitespace_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(monkeypatch, MODULO_MONITOR_DOMAINS="  https://faro.mycompany.com  ")
        assert Settings().modulo_monitor_domains == "  https://faro.mycompany.com  "

    def test_domain_with_path_and_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(monkeypatch, MODULO_MONITOR_DOMAINS="https://faro.mycompany.com/collect?d=1")
        assert "faro.mycompany.com" in Settings().modulo_monitor_domains

    def test_https_and_wss_mixed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_settings(monkeypatch, MODULO_MONITOR_DOMAINS="https://a.example.com wss://b.example.com")
        settings = Settings()
        assert "https://a.example.com" in settings.modulo_monitor_domains
        assert "wss://b.example.com" in settings.modulo_monitor_domains
