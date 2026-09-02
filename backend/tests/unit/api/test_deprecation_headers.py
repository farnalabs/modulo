"""Unit tests for DeprecationHeaderMiddleware."""

from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.deprecation_headers import DeprecationHeaderMiddleware

FUTURE_SUNSET = "2099-12-31"
PAST_SUNSET = "2020-01-01"


def _future_sunset() -> str:
    """Return an ISO date comfortably in the future so deprecation headers are exercised (200 path)."""
    return (date.today() + timedelta(days=30)).isoformat()


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/old-endpoint")
    async def old_endpoint():
        return {"data": "legacy"}

    @app.get("/api/v2/new-endpoint")
    async def new_endpoint():
        return {"data": "modern"}

    app.add_middleware(DeprecationHeaderMiddleware)
    return app


class TestDeprecationHeaderMiddleware:
    def setup_method(self) -> None:
        DeprecationHeaderMiddleware.clear()

    def test_non_deprecated_route_has_no_deprecation_header(self):
        """Routes not registered as deprecated should NOT get Deprecation header."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v2/new-endpoint")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers

    def test_deprecated_route_gets_deprecation_true(self):
        """Routes matching a deprecated prefix should get Deprecation: true."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"

    def test_deprecated_route_gets_sunset_header_when_set(self):
        """Sunset header should be added when sunset date is provided."""
        sunset = _future_sunset()
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", sunset=sunset)
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert resp.headers.get("Sunset") == sunset

    def test_deprecated_route_gets_link_header_when_migration_url_set(self):
        """Link header should be added when migration_url is provided."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", migration_url="/docs/migrations/v2")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert resp.headers.get("Link") == '/docs/migrations/v2; rel="deprecation"'

    def test_deprecated_route_gets_all_headers_when_fully_configured(self):
        """All three headers should appear when sunset and migration_url are set."""
        sunset = _future_sunset()
        DeprecationHeaderMiddleware.deprecate(
            "/api/v1/old-endpoint",
            sunset=sunset,
            migration_url="/docs/migrations/v2",
        )
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Sunset") == sunset
        assert resp.headers.get("Link") == '/docs/migrations/v2; rel="deprecation"'

    def test_path_prefix_matches_subpaths(self):
        """Path prefix matching should work for sub-routes under the prefix."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"

    def test_non_matching_subpath_no_header(self):
        """Path prefix should not match unrelated paths."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v2/new-endpoint")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers

    def test_clear_resets_registry(self):
        """Calling clear() should remove all registered deprecation rules."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", sunset=FUTURE_SUNSET)
        DeprecationHeaderMiddleware.clear()
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers

    def test_no_deprecations_registered_no_headers(self):
        """When no routes are registered as deprecated, no headers should appear."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers
        assert "Sunset" not in resp.headers

    def test_410_gone_when_sunset_past(self):
        """A deprecated route whose sunset date has passed returns 410 Gone."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", sunset=PAST_SUNSET)
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 410
        body = resp.json()
        assert "detail" in body
        assert "no longer available" in body["detail"]

    def test_200_with_headers_when_sunset_future(self):
        """A deprecated route with a future sunset date returns 200 with headers."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", sunset=FUTURE_SUNSET)
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Sunset") == FUTURE_SUNSET

    def test_410_includes_deprecation_header(self):
        """410 Gone response still includes Deprecation: true header."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", sunset="2020-06-15")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/old-endpoint")
        assert resp.status_code == 410
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Sunset") == "2020-06-15"

    def test_non_matching_path_no_410(self):
        """Unrelated paths should not trigger 410 even if a deprecation exists."""
        DeprecationHeaderMiddleware.deprecate("/api/v1/old-endpoint", sunset=PAST_SUNSET)
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v2/new-endpoint")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers
