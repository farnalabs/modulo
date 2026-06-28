"""Unit tests for CsrfMiddleware — cookie vs bearer, token validation, exempt paths."""

import secrets
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.csrf import CsrfMiddleware
from modulo.settings import Settings

CSRF_COOKIE = "XSRF-TOKEN"
CSRF_HEADER = "X-CSRF-Token"


def _make_settings(**kwargs: Any) -> Settings:
    overrides = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "testpass",
        "modulo_csrf_enabled": True,
    }
    overrides.update(kwargs)
    return Settings(**overrides)


def _make_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/safe")
    async def safe_get():
        return {"ok": True}

    @app.head("/safe")
    async def safe_head():
        return {"ok": True}

    @app.options("/safe")
    async def safe_options():
        return {"ok": True}

    @app.post("/unsafe")
    async def unsafe_post():
        return {"ok": True}

    @app.put("/unsafe")
    async def unsafe_put():
        return {"ok": True}

    @app.patch("/unsafe")
    async def unsafe_patch():
        return {"ok": True}

    @app.delete("/unsafe")
    async def unsafe_delete():
        return {"ok": True}

    @app.post("/api/v1/health")
    async def health():
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def auth_login():
        return {"ok": True}

    @app.post("/api/v1/triggers/test/webhook")
    async def webhook():
        return {"ok": True}

    app.add_middleware(
        CsrfMiddleware,
        settings=settings or _make_settings(),
    )
    return app


class TestSafeMethods:
    def test_get_skips_csrf(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/safe")
        assert resp.status_code == 200

    def test_head_skips_csrf(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.head("/safe")
        assert resp.status_code == 200

    def test_options_skips_csrf(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.options("/safe")
        assert resp.status_code == 200


class TestBearerAuth:
    def test_bearer_token_skips_csrf(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/unsafe",
                headers={"Authorization": "Bearer test-token"},
            )
        assert resp.status_code == 200

    def test_bearer_token_without_csrf_header(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/unsafe",
                headers={"Authorization": "Bearer test-token"},
            )
        assert resp.status_code == 200

    def test_bearer_token_with_invalid_csrf(self):
        app = _make_app()
        with TestClient(app) as client:
            client.cookies.set(CSRF_COOKIE, "cookie-token")
            resp = client.post(
                "/unsafe",
                headers={
                    "Authorization": "Bearer test-token",
                    CSRF_HEADER: "wrong-token",
                },
            )
        assert resp.status_code == 200


class TestCookieAuth:
    def test_post_without_csrf_cookie_fails(self):
        """Cookie-authenticated POST without CSRF cookie gets 403."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post("/unsafe")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "csrf_token_mismatch"

    def test_post_without_csrf_header_fails(self):
        """Cookie-authenticated POST without CSRF header gets 403."""
        app = _make_app()
        with TestClient(app) as client:
            client.cookies.set(CSRF_COOKIE, "valid-token")
            resp = client.post("/unsafe")
        assert resp.status_code == 403

    def test_post_with_mismatched_token_fails(self):
        """Cookie-authenticated POST with mismatched CSRF gets 403."""
        app = _make_app()
        with TestClient(app) as client:
            client.cookies.set(CSRF_COOKIE, "cookie-token")
            resp = client.post(
                "/unsafe",
                headers={CSRF_HEADER: "different-token"},
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "csrf_token_mismatch"

    def test_post_with_valid_csrf_succeeds(self):
        """Cookie-authenticated POST with matching CSRF succeeds."""
        app = _make_app()
        with TestClient(app) as client:
            token = secrets.token_hex(32)
            client.cookies.set(CSRF_COOKIE, token)
            resp = client.post(
                "/unsafe",
                headers={CSRF_HEADER: token},
            )
        assert resp.status_code == 200

    def test_put_with_valid_csrf_succeeds(self):
        app = _make_app()
        with TestClient(app) as client:
            token = secrets.token_hex(32)
            client.cookies.set(CSRF_COOKIE, token)
            resp = client.put(
                "/unsafe",
                headers={CSRF_HEADER: token},
            )
        assert resp.status_code == 200

    def test_delete_with_valid_csrf_succeeds(self):
        app = _make_app()
        with TestClient(app) as client:
            token = secrets.token_hex(32)
            client.cookies.set(CSRF_COOKIE, token)
            resp = client.delete(
                "/unsafe",
                headers={CSRF_HEADER: token},
            )
        assert resp.status_code == 200

    def test_patch_with_valid_csrf_succeeds(self):
        app = _make_app()
        with TestClient(app) as client:
            token = secrets.token_hex(32)
            client.cookies.set(CSRF_COOKIE, token)
            resp = client.patch(
                "/unsafe",
                headers={CSRF_HEADER: token},
            )
        assert resp.status_code == 200


class TestExemptPaths:
    def test_health_exempt(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post("/api/v1/health")
        assert resp.status_code == 200

    def test_auth_login_exempt(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")
        assert resp.status_code == 200

    def test_webhook_exempt(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post("/api/v1/triggers/test/webhook")
        assert resp.status_code == 200


class TestDisabled:
    def test_disabled_skips_all_checks(self):
        settings = _make_settings(modulo_csrf_enabled=False)
        app = _make_app(settings=settings)
        with TestClient(app) as client:
            resp = client.post("/unsafe")
        assert resp.status_code == 200

    def test_disabled_without_auth(self):
        settings = _make_settings(modulo_csrf_enabled=False)
        app = _make_app(settings=settings)
        with TestClient(app) as client:
            resp = client.post("/unsafe")
        assert resp.status_code == 200


class TestCustomExemptPaths:
    def test_custom_exempt_path_added(self):
        settings = _make_settings(modulo_csrf_exempt_paths="/custom-exempt")
        app = _make_app(settings=settings)

        @app.post("/custom-exempt")
        async def custom_exempt():
            return {"ok": True}

        with TestClient(app) as client:
            resp = client.post("/custom-exempt")
        assert resp.status_code == 200
