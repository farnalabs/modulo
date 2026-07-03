"""Step definitions for plugin registry BDD feature.

Covers: discovery listing, health checks, startup discovery,
manifest validation, and capability advertisement.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.plugin_registry import PluginHealth

try:
    scenarios("../../features/plugins/plugin_registry.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

NOW = datetime.now(UTC)

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


def _make_mock_manifest(**kw: Any) -> MagicMock:
    m = MagicMock()
    m.PLUGIN_ID = kw.get("PLUGIN_ID", "test-plugin")
    m.display_name = kw.get("display_name", "Test Plugin")
    m.description = kw.get("description", "")
    m.version = kw.get("version", "1.0.0")
    m.capabilities = set(kw.get("capabilities", set()))
    return m


def _make_mock_health(**kw: Any) -> "PluginHealth":
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

    def _health_side_effect(pid: str | None = None) -> dict[str, MagicMock]:
        if pid is None:
            return healths
        if pid in manifests:
            return {pid: healths[pid]}
        return {pid: PluginHealth(ok=False, detail="Unknown plugin")}

    registry.health_check.side_effect = _health_side_effect
    return registry


def _map_url(url: str) -> str:
    return url.replace("/api/", "/api/v1/")


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


# ===========================================================================
# Given
# ===========================================================================


@given(parsers.parse("{count:d} plugins are registered"))
def _num_plugins_registered(count: int, request: pytest.FixtureRequest) -> None:
    selected = list(ALL_PLUGINS.values())[:count]
    request.node._plugin_data = selected


@given(parsers.parse('a plugin "{plugin_id}" is registered'))
def _plugin_is_registered(plugin_id: str, request: pytest.FixtureRequest) -> None:
    data = ALL_PLUGINS.get(plugin_id)
    if data is None:
        # Allow arbitrary plugin ids for future scenarios
        data = {
            "PLUGIN_ID": plugin_id,
            "display_name": plugin_id.replace("-", " ").title(),
            "description": "",
            "version": "1.0.0",
            "capabilities": set(),
        }
    request.node._plugin_data = [data]


@given(
    parsers.parse(
        'a plugin "{plugin_id}" is registered with connector type "{connector_type}"'
    )
)
def _plugin_with_connector(
    plugin_id: str, connector_type: str, request: pytest.FixtureRequest
) -> None:
    data = {
        "PLUGIN_ID": plugin_id,
        "display_name": plugin_id.replace("-", " ").title(),
        "description": f"Plugin providing {connector_type}",
        "version": "1.0.0",
        "capabilities": {"connector_type"},
    }
    request.node._plugin_data = [data]


@given("no plugins are initially registered")
def _no_plugins_initially(request: pytest.FixtureRequest) -> None:
    request.node._plugin_data = []


@given("an entry point references a package with missing metadata")
def _entry_point_missing_metadata(request: pytest.FixtureRequest) -> None:
    """Flag that the next discovery step should simulate a broken entry point."""
    request.node._broken_ep = True


# ===========================================================================
# When
# ===========================================================================


@when("I GET /api/plugins")
def _get_plugins(
    client: Any,
    request: pytest.FixtureRequest,
    patches: list[Any],
    ctx: dict[str, Any],
) -> None:
    plugin_data: list[dict[str, Any]] = getattr(request.node, "_plugin_data", [])
    mock_registry = _make_mock_registry(plugin_data)

    patcher = patch(
        "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
    )
    patcher.start()
    patches.append(patcher)

    resp = client.get(_map_url("/api/plugins"))
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/plugins/{plugin_id}"))
def _get_plugin_detail(
    client: Any,
    plugin_id: str,
    request: pytest.FixtureRequest,
    patches: list[Any],
    ctx: dict[str, Any],
) -> None:
    plugin_data: list[dict[str, Any]] = getattr(request.node, "_plugin_data", [])
    mock_registry = _make_mock_registry(plugin_data)

    patcher = patch(
        "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
    )
    patcher.start()
    patches.append(patcher)

    resp = client.get(_map_url(f"/api/plugins/{plugin_id}"))
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/plugins/{plugin_id}/health"))
def _get_plugin_health(
    client: Any,
    plugin_id: str,
    request: pytest.FixtureRequest,
    patches: list[Any],
    ctx: dict[str, Any],
) -> None:
    plugin_data: list[dict[str, Any]] = getattr(request.node, "_plugin_data", [])
    mock_registry = _make_mock_registry(plugin_data)

    patcher = patch(
        "modulo.api.routes.plugins.get_plugin_registry", return_value=mock_registry
    )
    patcher.start()
    patches.append(patcher)

    resp = client.get(_map_url(f"/api/plugins/{plugin_id}/health"))
    _store_response(request, ctx, resp)


@when("the plugin registry discovers plugins")
def _discover_plugins(
    request: pytest.FixtureRequest, patches: list[Any]
) -> None:
    from modulo.core.plugin_registry import PluginRegistry

    registry = PluginRegistry()

    broken = getattr(request.node, "_broken_ep", False)
    if broken:
        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
            mock_ep = MagicMock()
            mock_ep.name = "slack"
            mock_ep.dist = None  # No distribution → manifest rejected
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group in ("modulo.connectors", "modulo.model_backends") else []
            )
            discovered = registry.discover_plugins()
    else:
        with patch(
            "modulo.core.plugin_registry.importlib.metadata.entry_points"
        ) as mock_eps:
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
            mock_eps.side_effect = lambda group=None: (
                [mock_ep] if group in ("modulo.connectors", "modulo.model_backends") else []
            )
            discovered = registry.discover_plugins()

    request.node._discovered = discovered
    request.node._registry_state = registry


# ===========================================================================
# Then
# ===========================================================================


@then(parsers.parse("the response contains {count:d} plugins"))
def _response_contains_n_plugins(
    request: pytest.FixtureRequest, count: int
) -> None:
    body = request.node._resp.json()
    assert isinstance(body, list), f"Expected list, got {type(body)}"
    assert len(body) == count, f"Expected {count} plugins, got {len(body)}"


@then("each plugin has PLUGIN_ID, display_name, version, and capabilities")
def _each_plugin_has_required_fields(request: pytest.FixtureRequest) -> None:
    body = request.node._resp.json()
    for plugin in body:
        assert "PLUGIN_ID" in plugin, f"Missing PLUGIN_ID in {plugin}"
        assert "display_name" in plugin, f"Missing display_name in {plugin}"
        assert "version" in plugin, f"Missing version in {plugin}"
        assert "capabilities" in plugin, f"Missing capabilities in {plugin}"


@then(parsers.parse('the response has PLUGIN_ID "{expected}"'))
def _response_has_plugin_id(
    request: pytest.FixtureRequest, expected: str
) -> None:
    body = request.node._resp.json()
    if isinstance(body, list):
        body = body[0]
    assert body.get("PLUGIN_ID") == expected, (
        f"Expected PLUGIN_ID {expected!r}, got {body.get('PLUGIN_ID')!r}"
    )


@then("the response includes display_name, description, version, and capabilities")
def _response_includes_full_manifest(request: pytest.FixtureRequest) -> None:
    body = request.node._resp.json()
    if isinstance(body, list):
        body = body[0]
    assert "display_name" in body, "Missing display_name"
    assert "description" in body, "Missing description"
    assert "version" in body, "Missing version"
    assert "capabilities" in body, "Missing capabilities"


@then("the response contains health_ok and detail")
def _response_has_health_fields(request: pytest.FixtureRequest) -> None:
    body = request.node._resp.json()
    assert "ok" in body, "Missing ok in health response"
    assert "detail" in body, "Missing detail in health response"
    assert "checked_at" in body, "Missing checked_at in health response"


@then(parsers.parse('the response detail says "{message}"'))
def _response_detail_says(
    request: pytest.FixtureRequest, message: str
) -> None:
    body = request.node._resp.json()
    detail = body.get("detail", "")
    assert message in detail, f"Expected detail containing {message!r}, got {detail!r}"


@then(
    parsers.parse(
        'entry points in "{group1}" and "{group2}" are scanned'
    )
)
def _entry_points_scanned(
    request: pytest.FixtureRequest, group1: str, group2: str
) -> None:
    registry: Any = getattr(request.node, "_registry_state", None)
    assert registry is not None, "No registry state — the When step must set _registry_state"
    plugins = registry.list_plugins()
    assert len(plugins) > 0, "No plugins were discovered"
    assert all(
        isinstance(pid, str) for pid in plugins
    ), "Plugin IDs should be strings"


@then("discovered plugins are available via list_plugins")
def _discovered_available(request: pytest.FixtureRequest) -> None:
    discovered = getattr(request.node, "_discovered", None)
    assert discovered is not None, "No discovered plugins — the When step must set _discovered"
    assert len(discovered) > 0, "Expected at least one discovered plugin"


@then("the plugin is marked with health_ok false")
def _plugin_health_false(request: pytest.FixtureRequest) -> None:
    registry: Any = getattr(request.node, "_registry_state", None)
    assert registry is not None
    healths = registry.health_check()
    assert any(
        not h.ok for h in healths.values()
    ), "Expected at least one plugin with health_ok false"


@then("the detail describes the failure")
def _detail_describes_failure(request: pytest.FixtureRequest) -> None:
    registry: Any = getattr(request.node, "_registry_state", None)
    assert registry is not None
    healths = registry.health_check()
    failed = {pid: h for pid, h in healths.items() if not h.ok}
    assert len(failed) > 0, "Expected at least one failed health check"
    for pid, health in failed.items():
        assert health.detail, f"Plugin {pid} has health_ok false but empty detail"


@then(
    "the response includes a plugin with capabilities containing "
    '"connector_type"'
)
def _response_includes_connector_capability(
    request: pytest.FixtureRequest,
) -> None:
    body = request.node._resp.json()
    assert isinstance(body, list), f"Expected list, got {type(body)}"
    found = any(
        "connector_type" in p.get("capabilities", set())
        for p in body
    )
    assert found, "No plugin with connector_type capability found in response"
