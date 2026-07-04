"""Unit tests for the API changelog endpoint."""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.routes.changelog import router as changelog_router, _SEED_ENTRIES, ChangelogEntry


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(changelog_router)
    return app


class TestChangelog:
    def test_list_changelog_returns_sorted(self):
        """GET /api/v1/changelog returns 200 with list sorted by date descending."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/changelog")
        assert resp.status_code == 200
        entries = resp.json()
        assert isinstance(entries, list)
        assert len(entries) >= 1
        dates = [e["date"] for e in entries]
        assert dates == sorted(dates, reverse=True)

    def test_latest_changelog_returns_most_recent(self):
        """GET /api/v1/changelog/latest returns the most recent entry."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/changelog/latest")
        assert resp.status_code == 200
        entry = resp.json()
        assert entry["version"] == _SEED_ENTRIES[-1].version

    def test_latest_changelog_404_when_empty(self):
        """When _SEED_ENTRIES is empty, /latest returns 404."""
        with patch("modulo.api.routes.changelog._SEED_ENTRIES", []):
            app = _make_app()
            with TestClient(app) as client:
                resp = client.get("/api/v1/changelog/latest")
        assert resp.status_code == 404

    def test_changelog_entry_model_fields(self):
        """Response model has all required fields."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/changelog")
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        entry = entries[0]
        assert "version" in entry
        assert "date" in entry
        assert "summary" in entry
        assert "changes" in entry
        assert "deprecations" in entry
        assert "migration_url" in entry

    def test_changelog_migration_url_link(self):
        """migration_url appears in the response when set."""
        entry_with_url = ChangelogEntry(
            version="99.0",
            date="2099-01-01",
            summary="Test migration",
            changes=["Test change"],
            migration_url="https://example.com/migration",
        )
        with patch("modulo.api.routes.changelog._SEED_ENTRIES", [entry_with_url]):
            app = _make_app()
            with TestClient(app) as client:
                resp = client.get("/api/v1/changelog")
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) == 1
        assert entries[0]["migration_url"] == "https://example.com/migration"
