"""Smoke test: SecurityHeadersMiddleware CSP construction.

Verifies that the SecurityHeadersMiddleware correctly builds the
Content-Security-Policy header string, including default
connect-src sources and custom monitoring domains from settings.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI

from modulo.api.middleware.security_headers import SecurityHeadersMiddleware
from modulo.settings import Settings


class TestSecurityHeadersMiddleware:
    def test_default_csp_contains_expected_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        settings = Settings()
        with patch("modulo.api.middleware.security_headers.get_settings", return_value=settings):
            app = FastAPI()
            middleware = SecurityHeadersMiddleware(app)
            assert "*.ingest.sentry.io" in middleware._csp
            assert "ws:" in middleware._csp
            assert "connect-src" in middleware._csp

    def test_custom_domains_included_in_csp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv(
            "MODULO_MONITOR_DOMAINS",
            "https://faro.mycompany.com",
        )
        settings = Settings()
        with patch("modulo.api.middleware.security_headers.get_settings", return_value=settings):
            app = FastAPI()
            middleware = SecurityHeadersMiddleware(app)
            assert "https://faro.mycompany.com" in middleware._csp
            assert "*.ingest.sentry.io" in middleware._csp
