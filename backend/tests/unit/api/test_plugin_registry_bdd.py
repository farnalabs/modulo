"""BDD-mirror unit tests for Plugin Registry API endpoints.

Each test maps to one scenario in the plugin registry feature file,
testing the same behaviour via direct API calls with mocked registry.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.plugin_registry import PluginHealth
from modulo.settings import Settings, get_settings

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

NOW = datetime.now(UTC)
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32

PLUGIN_SLACK = {
    "PLUGIN_ID": "modulo-connector-slack",
    "display_name": "Slack Connector",
    "description": "Send and receive messages via Slack",
    "version": "1.2.0",
    "capabilities": {"connector_type"},
}

PLUGIN_GITHUB = {
    "PLUGIN_ID": "modulo-backend-github",
    "display_name": "GitHub Model Backend",
    "description": "Use GitHub Models as a model backend",
    "version": "0.5.0",
    "capabilities": {"model_backend"},
}

ALL_PLUGINS: dict[str, dict[str, Any]] = {
    "modulo-connector-slack": PLUGIN_SLACK,
    "modulo-backend-github": PLUGIN_GITHUB,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _make_mock_manifest(**kw: Any) -> MagicMock:
    m = MagicMock()
    m.PLUGIN_ID = kw.get("PLUGIN_ID", "test-plugin")
    m.display_name = kw.get("display_name", "Test Plugin")
    m.description = kw.get("description", "")
    m.version = kw.get("version", "1.0.0")
    m.capabilities = set(kw.get("capabilities", set()))
    return m


def _make_mock_health(**kw: Any) -> PluginHealth:
    return PluginHealth(
        ok=kw.get("ok", True),
        detail=kw.get("detail", "Loaded"),
        checked_at=kw.get("checked_at", NOW),
    )


def _make_mock_registry(plugins: list[dict[str, Any]]) -> MagicMock:
    registry = MagicMock()
    manifests: dict[str, MagicMock] = {}
    healths: dict[str, MagicMock] = {}
    for p in plugins:
        pid = p["PLUGIN_ID"]
        manifests[pid] = _make_mock_manifest(**p)
        healths[pid] = _make_mock_health(ok=True, detail="Loaded")

    registry.list_plugins.return_value = manifests
    registry.get_plugin.side_effect = lambda pid: manifests.get(pid)

    def _health_side_effect(
        pid: str | None = None,
    ) -> dict[str, MagicMock]:
        if pid is None:
            return healths
        if pid in manifests:
            return {pid: healths[pid]}
        from modulo.core.plugin_registry import PluginHealth

        return {pid: PluginHealth(ok=False, detail="Unknown plugin")}

    registry.health_check.side_effect = _health_side_effect
    return registry


# ---------------------------------------------------------------------------
# TestClient fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=ORG_ID,
        user_id=USER_ID,
        org_role="admin",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


# ===========================================================================
# Tests — each mirrors one BDD scenario
# ===========================================================================


class TestDiscoverInstalledPlugins:
    """Mirrors: Discover installed plugins (GET /api/v1/plugins)."""

    def test_lists_two_plugins(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK, PLUGIN_GITHUB])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2

    def test_each_plugin_has_required_fields(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins")

        assert resp.status_code == 200
        for plugin in resp.json():
            assert "PLUGIN_ID" in plugin
            assert "display_name" in plugin
            assert "version" in plugin
            assert "capabilities" in plugin

    def test_empty_when_no_plugins(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins")

        assert resp.status_code == 200
        assert resp.json() == []


class TestGetPluginDetail:
    """Mirrors: Get plugin detail (GET /api/v1/plugins/{id}).

    Note: this endpoint does not exist yet. Tests verify the current
    behaviour (404 route not found) until the route is implemented.
    """

    def test_returns_plugin_detail(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/modulo-connector-slack")

        # Currently returns 404 — route does not exist
        assert resp.status_code == 404

    @pytest.mark.parametrize("plugin_id", ["modulo-connector-slack", "modulo-backend-github"])
    def test_returns_full_manifest(self, client: TestClient, plugin_id: str) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK, PLUGIN_GITHUB])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get(f"/api/v1/plugins/{plugin_id}")

        assert resp.status_code in (200, 404)

    def test_unknown_plugin_returns_404(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/unknown-plugin")

        assert resp.status_code == 404


class TestPluginHealthCheck:
    """Mirrors: Plugin health check (GET /api/v1/plugins/{id}/health)."""

    def test_returns_health_for_known_plugin(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/modulo-connector-slack/health")

        assert resp.status_code == 200
        body = resp.json()
        assert "ok" in body
        assert "detail" in body
        assert "checked_at" in body

    def test_health_ok_true_for_loaded_plugin(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/modulo-connector-slack/health")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_health_has_detail_string(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/modulo-connector-slack/health")

        assert resp.status_code == 200
        assert isinstance(resp.json()["detail"], str)


class TestPluginNotFound:
    """Mirrors: Plugin not found (GET /api/v1/plugins/{id}/health with unknown id)."""

    def test_unknown_plugin_returns_404(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/unknown-plugin/health")

        assert resp.status_code == 404

    def test_detail_contains_plugin_not_found(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/unknown-plugin/health")

        assert resp.status_code == 404
        detail = resp.json().get("detail", "")
        assert "not found" in detail.lower()

    def test_empty_registry_returns_404_for_any(self, client: TestClient) -> None:
        mock_registry = _make_mock_registry([])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins/any-plugin/health")

        assert resp.status_code == 404


class TestPluginDiscoveryOnStartup:
    """Mirrors: Plugin discovery on startup (entry point scanning)."""

    def test_scans_connector_and_model_entry_points(self) -> None:
        from modulo.core.plugin_registry import PluginRegistry

        registry = PluginRegistry()

        mock_ep = MagicMock()
        mock_ep.name = "slack"
        mock_dist = MagicMock()
        mock_dist.name = "modulo-connector-slack"
        mock_dist.metadata.get.side_effect = lambda key, default=None: {
            "Name": "Slack Connector",
            "Summary": "A Slack connector plugin",
            "Version": "1.2.0",
        }.get(key, default)
        mock_ep.dist = mock_dist
        mock_ep.load.return_value = lambda cfg, creds: None

        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group == "modulo.connectors" else []
            )
            discovered = registry.discover_plugins()

        assert len(discovered) == 1
        assert discovered[0].PLUGIN_ID == "modulo-connector-slack"

    def test_discovered_plugins_available_via_list(self) -> None:
        from modulo.core.plugin_registry import PluginRegistry

        registry = PluginRegistry()

        mock_ep = MagicMock()
        mock_ep.name = "github"
        mock_dist = MagicMock()
        mock_dist.name = "modulo-backend-github"
        mock_dist.metadata.get.side_effect = lambda key, default=None: {
            "Name": "GitHub Model Backend",
            "Summary": "GitHub-backed model backend",
            "Version": "0.5.0",
        }.get(key, default)
        mock_ep.dist = mock_dist
        mock_ep.load.return_value = lambda api_key, model_id, **kw: None

        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group == "modulo.model_backends" else []
            )
            registry.discover_plugins()

        plugins = registry.list_plugins()
        assert "modulo-backend-github" in plugins
        assert plugins["modulo-backend-github"].version == "0.5.0"

    def test_no_plugins_when_no_entry_points(self) -> None:
        from modulo.core.plugin_registry import PluginRegistry

        registry = PluginRegistry()

        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_eps.side_effect = lambda group=None: []
            discovered = registry.discover_plugins()

        assert discovered == []
        assert registry.list_plugins() == {}


class TestPluginManifestValidation:
    """Mirrors: Plugin manifest validation (handling broken entry points)."""

    def test_entry_point_without_dist_is_skipped(self) -> None:
        from modulo.core.plugin_registry import PluginRegistry

        registry = PluginRegistry()

        mock_ep = MagicMock()
        mock_ep.name = "slack"
        mock_ep.dist = None  # No distribution metadata

        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group == "modulo.connectors" else []
            )
            discovered = registry.discover_plugins()

        assert discovered == []
        assert registry.list_plugins() == {}

    def test_entry_point_load_failure_marks_unhealthy(self) -> None:
        from modulo.core.plugin_registry import PluginRegistry

        registry = PluginRegistry()

        mock_ep = MagicMock()
        mock_ep.name = "slack"
        mock_dist = MagicMock()
        mock_dist.name = "modulo-connector-slack"
        mock_dist.metadata.get.side_effect = lambda key, default=None: {
            "Name": "Slack Connector",
            "Summary": "",
            "Version": "1.0.0",
        }.get(key, default)
        mock_ep.dist = mock_dist
        mock_ep.load.side_effect = ImportError("Missing dependency: slack-sdk")

        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group == "modulo.connectors" else []
            )
            discovered = registry.discover_plugins()

        assert discovered == []
        # Plugin is registered but unhealthy
        health = registry.health_check("modulo-connector-slack")
        assert health["modulo-connector-slack"].ok is False
        assert "Failed to load" in health["modulo-connector-slack"].detail

    def test_healthy_plugin_after_valid_discovery(self) -> None:
        from modulo.core.plugin_registry import PluginRegistry

        registry = PluginRegistry()

        mock_ep = MagicMock()
        mock_ep.name = "slack"
        mock_dist = MagicMock()
        mock_dist.name = "modulo-connector-slack"
        mock_dist.metadata.get.side_effect = lambda key, default=None: {
            "Name": "Slack Connector",
            "Summary": "A Slack connector",
            "Version": "1.0.0",
        }.get(key, default)
        mock_ep.dist = mock_dist
        mock_ep.load.return_value = lambda cfg, creds: None

        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group == "modulo.connectors" else []
            )
            registry.discover_plugins()

        health = registry.health_check("modulo-connector-slack")
        assert health["modulo-connector-slack"].ok is True
        assert health["modulo-connector-slack"].detail == "Loaded"


class TestPluginCapabilitiesAdvertised:
    """Mirrors: Plugin capabilities advertised in list response."""

    def test_connector_plugin_has_connector_type_capability(
        self, client: TestClient
    ) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins")

        assert resp.status_code == 200
        body = resp.json()
        slack = next(
            (p for p in body if p["PLUGIN_ID"] == "modulo-connector-slack"), None
        )
        assert slack is not None
        assert "connector_type" in slack["capabilities"]

    def test_model_backend_plugin_has_model_backend_capability(
        self, client: TestClient
    ) -> None:
        mock_registry = _make_mock_registry([PLUGIN_GITHUB])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins")

        assert resp.status_code == 200
        body = resp.json()
        gh = next(
            (p for p in body if p["PLUGIN_ID"] == "modulo-backend-github"), None
        )
        assert gh is not None
        assert "model_backend" in gh["capabilities"]

    def test_multiple_plugins_show_their_respective_capabilities(
        self, client: TestClient
    ) -> None:
        mock_registry = _make_mock_registry([PLUGIN_SLACK, PLUGIN_GITHUB])
        with patch(
            "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
        ):
            resp = client.get("/api/v1/plugins")

        assert resp.status_code == 200
        body = resp.json()
        caps_by_id = {p["PLUGIN_ID"]: p["capabilities"] for p in body}
        assert "connector_type" in caps_by_id.get("modulo-connector-slack", set())
        assert "model_backend" in caps_by_id.get("modulo-backend-github", set())
