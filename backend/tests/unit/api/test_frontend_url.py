"""Regression tests for the shared ``resolve_frontend_url`` helper.

FAR-837: SSO post-login redirect was derived from the first CORS origin,
which is wrong on any deployment where the marketing site leads the CORS
list.  These tests prove the redirect now uses the explicit frontend URL
settings, independent of CORS ordering.
"""

from __future__ import annotations

from modulo.api.frontend_url import resolve_frontend_url
from modulo.api.routes.sso import _frontend_url, _redirect_to_frontend
from modulo.settings import Settings

_VALID_32 = "a" * 32


def _base_settings(**overrides: str) -> Settings:
    """Minimal Settings instance; callers override only the fields they need."""
    defaults = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_admin_password": "testpass",
    }
    return Settings(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# resolve_frontend_url (unit)
# ---------------------------------------------------------------------------


class TestResolveFrontendUrl:
    """The shared resolver used by SSO and MCP OAuth."""

    def test_prefers_modulo_frontend_url_when_set(self) -> None:
        settings = _base_settings(
            modulo_frontend_url="https://app.example.com",
            modulo_public_url="https://marketing.example.com",
            cors_origins="https://marketing.example.com,https://app.example.com",
        )
        assert resolve_frontend_url(settings) == "https://app.example.com"

    def test_falls_back_to_modulo_public_url(self) -> None:
        settings = _base_settings(
            modulo_frontend_url="",
            modulo_public_url="https://app.example.com",
            cors_origins="https://marketing.example.com,https://app.example.com",
        )
        assert resolve_frontend_url(settings) == "https://app.example.com"

    def test_independent_of_cors_ordering(self) -> None:
        """The first CORS origin must NOT influence the redirect target."""
        settings = _base_settings(
            modulo_frontend_url="",
            modulo_public_url="https://app.example.com",
            cors_origins="https://modulo.run,https://app.example.com,http://localhost:5173",
        )
        assert resolve_frontend_url(settings) == "https://app.example.com"

    def test_strips_trailing_slash(self) -> None:
        settings = _base_settings(
            modulo_frontend_url="https://app.example.com/",
        )
        assert resolve_frontend_url(settings) == "https://app.example.com"

    def test_localhost_default_when_both_empty(self) -> None:
        settings = _base_settings(
            modulo_frontend_url="",
            modulo_public_url="",
            cors_origins="http://localhost:5173",
        )
        assert resolve_frontend_url(settings) == "http://localhost:5173"


# ---------------------------------------------------------------------------
# SSO thin delegates (integration: sso._frontend_url and _redirect_to_frontend)
# ---------------------------------------------------------------------------


class TestSsoFrontendUrlDelegate:
    """Verify ``sso._frontend_url`` and ``_redirect_to_frontend`` delegate correctly."""

    def test_sso_uses_frontend_url_not_cors(self) -> None:
        """FAR-837 regression: redirect must use MODULO_PUBLIC_URL, not CORS[0]."""
        settings = _base_settings(
            modulo_frontend_url="",
            modulo_public_url="https://app.modulo.run",
            cors_origins="https://modulo.run,https://app.modulo.run,http://localhost:5173",
        )
        assert _frontend_url(settings) == "https://app.modulo.run"

    def test_sso_prefers_frontend_url_when_set(self) -> None:
        settings = _base_settings(
            modulo_frontend_url="https://custom.example.com",
            modulo_public_url="https://app.example.com",
        )
        assert _frontend_url(settings) == "https://custom.example.com"

    def test_redirect_to_frontend_includes_tokens(self) -> None:
        settings = _base_settings(
            modulo_frontend_url="",
            modulo_public_url="https://app.example.com",
        )
        tokens = {"access_token": "at-123", "refresh_token": "rt-456"}
        resp = _redirect_to_frontend(tokens, settings)
        assert resp.status_code == 307
        assert resp.headers["location"] == (
            "https://app.example.com/auth/callback#access_token=at-123&refresh_token=rt-456"
        )

    def test_redirect_to_frontend_no_cors_dependency(self) -> None:
        """Redirect target is stable regardless of CORS list ordering."""
        settings = _base_settings(
            modulo_frontend_url="",
            modulo_public_url="https://app.modulo.run",
            cors_origins="https://modulo.run,https://app.modulo.run",
        )
        tokens = {"access_token": "tok", "refresh_token": "ref"}
        resp = _redirect_to_frontend(tokens, settings)
        assert resp.headers["location"].startswith("https://app.modulo.run/")
