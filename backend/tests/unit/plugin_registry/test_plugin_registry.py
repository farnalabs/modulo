"""Unit tests for the PluginRegistry."""

from typing import Any
from unittest.mock import patch

import pytest

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
)
from modulo.core.plugin_registry import (
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    get_plugin_registry,
)
from modulo.model_backends.base import HealthResult, ModelBackendBase

# ---------------------------------------------------------------------------
# Stub plugin implementations for testing
# ---------------------------------------------------------------------------


class _StubPluginConnector(ConnectorBase):
    """A minimal connector for testing plugin integration."""

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CUSTOM

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[{"plugin": "stub"}])

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        return {"written": True}


def _build_stub_connector(config: dict[str, Any], creds: dict[str, Any]) -> ConnectorBase:
    return _StubPluginConnector()


class _StubPluginBackend(ModelBackendBase):
    """A minimal model backend for testing plugin integration."""

    def __init__(self, api_key: str = "", model_id: str = "", **kwargs: Any) -> None:
        self._api_key = api_key
        self._model_id = model_id

    @property
    def backend_id(self) -> str:
        return f"stub/{self._model_id}"

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def invoke(self, messages: list, **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage

        return AIMessage(content=f"stub reply to {len(messages)} messages")

    async def stream(self, messages: list, **kwargs: Any) -> Any:  # type: ignore[override]
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content="stub ")


def _build_stub_backend(api_key: str, model_id: str, **kwargs: Any) -> ModelBackendBase:
    return _StubPluginBackend(api_key=api_key, model_id=model_id, **kwargs)


# ---------------------------------------------------------------------------
# PluginRegistry — manual registration and builder lookup
# ---------------------------------------------------------------------------


def test_register_and_build_connector():
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="modulo-plugin-stub",
        display_name="Stub Plugin",
        description="A stub for testing",
        version="1.0.0",
    )
    registry.register_connector_type("stub_connector", _build_stub_connector, manifest)

    assert registry.has_connector_type("stub_connector")
    assert "stub_connector" in registry.connector_types

    connector = registry.build_connector("stub_connector", {}, {})
    assert isinstance(connector, _StubPluginConnector)


def test_register_and_build_model_backend():
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="modulo-plugin-stub-mb",
        display_name="Stub MB Plugin",
        description="A stub model backend for testing",
        version="0.1.0",
    )
    registry.register_model_backend("stub_provider", _build_stub_backend, manifest)

    assert registry.has_model_backend("stub_provider")
    assert "stub_provider" in registry.backend_providers

    backend = registry.build_model_backend("stub_provider", "stub-model", "test-key")
    assert isinstance(backend, _StubPluginBackend)
    assert backend.backend_id == "stub/stub-model"


def test_build_unknown_connector_raises():
    registry = PluginRegistry()
    with pytest.raises(KeyError, match="stub_connector"):
        registry.build_connector("stub_connector", {}, {})


def test_build_unknown_backend_raises():
    registry = PluginRegistry()
    with pytest.raises(KeyError, match="stub_provider"):
        registry.build_model_backend("stub_provider", "x", "y")


def test_list_plugins_after_registration():
    registry = PluginRegistry()
    m1 = PluginManifest(PLUGIN_ID="p1", display_name="P1", description="", version="0.1.0")
    m2 = PluginManifest(PLUGIN_ID="p2", display_name="P2", description="", version="0.2.0")

    def _builder(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    registry.register_connector_type("c1", _builder, m1)
    registry.register_model_backend("b1", _builder, m2)

    plugins = registry.list_plugins()
    assert "p1" in plugins
    assert "p2" in plugins
    assert plugins["p1"].display_name == "P1"
    assert plugins["p1"].capabilities == {"connector_type"}
    assert plugins["p2"].capabilities == {"model_backend"}


def test_get_plugin():
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="test-plugin",
        display_name="Test",
        description="",
        version="1.0.0",
    )
    registry.register_connector_type("t1", lambda c, cr: _StubPluginConnector(), manifest)
    assert registry.get_plugin("test-plugin") is manifest
    assert registry.get_plugin("nonexistent") is None


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


def test_health_check_unknown_plugin():
    registry = PluginRegistry()
    result = registry.health_check("nonexistent")
    assert result["nonexistent"].ok is False
    assert "Unknown" in result["nonexistent"].detail


def test_health_check_registered_plugin():
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="modulo-plugin-stub",
        display_name="Stub",
        description="",
        version="1.0.0",
    )
    registry.register_connector_type("stub", _build_stub_connector, manifest)
    result = registry.health_check("modulo-plugin-stub")
    # The _check_single will try importlib.metadata.metadata('modulo-plugin-stub')
    # which won't be found (not actually installed), but the test at least
    # exercises the path without exceptions.
    assert "modulo-plugin-stub" in result


def test_health_check_all_plugins():
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="test-pkg",
        display_name="Test",
        description="",
        version="0.0.0",
    )
    registry.register_connector_type("t1", _build_stub_connector, manifest)
    results = registry.health_check()
    assert isinstance(results, dict)
    assert "test-pkg" in results


# ---------------------------------------------------------------------------
# Integration with ConnectorHub's _build_connector
# ---------------------------------------------------------------------------


def test_has_connector_type_and_build_via_registry():
    """The plugin registry responds to has_connector_type and build_connector
    — the same interface used by connector_hub._build_connector fallback."""
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="test-plugin-connector",
        display_name="Test Connector Plugin",
        description="",
        version="1.0.0",
    )
    registry.register_connector_type("my_custom_connector", _build_stub_connector, manifest)

    assert registry.has_connector_type("my_custom_connector")
    assert not registry.has_connector_type("unknown_type")

    connector = registry.build_connector("my_custom_connector", {}, {})
    assert isinstance(connector, _StubPluginConnector)


def test_has_model_backend_and_build_via_registry():
    """The plugin registry responds to has_model_backend and build_model_backend
    — the same interface used by model_backend_hub._build_backend fallback."""
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="test-plugin-backend",
        display_name="Test Backend Plugin",
        description="",
        version="1.0.0",
    )
    registry.register_model_backend("my_custom_provider", _build_stub_backend, manifest)

    assert registry.has_model_backend("my_custom_provider")
    assert not registry.has_model_backend("unknown_provider")

    backend = registry.build_model_backend("my_custom_provider", "test-model", "key")
    assert isinstance(backend, _StubPluginBackend)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_get_plugin_registry_singleton():
    r1 = get_plugin_registry()
    r2 = get_plugin_registry()
    assert r1 is r2


# ---------------------------------------------------------------------------
# PluginHealth dataclass
# ---------------------------------------------------------------------------


def test_plugin_health_defaults():
    h = PluginHealth(ok=True)
    assert h.detail == ""
    assert h.checked_at is not None


# ---------------------------------------------------------------------------
# Discovery — mocked entry points
# ---------------------------------------------------------------------------


def _make_mock_entry_point(
    group: str,
    ep_name: str,
    dist_name: str = "pkg-demo",
    dist_version: str = "1.0.0",
    load_result: object = None,
    load_side_effect: type[Exception] | None = None,
) -> object:
    """Build a minimal mock of importlib.metadata.EntryPoint."""
    import types

    dist = types.SimpleNamespace(
        name=dist_name,
        metadata={
            "Name": dist_name,
            "Summary": "A demo plugin",
            "Version": dist_version,
        },
    )
    loader = types.SimpleNamespace()
    if load_side_effect is not None:

        def _fail(*a: object, **kw: object) -> object:
            raise load_side_effect("boom")

        loader.load = _fail
    else:
        loader.load = lambda: load_result

    ep = types.SimpleNamespace(name=ep_name, dist=dist, load=loader.load)
    return ep


class _DiscoveryStubConnector(ConnectorBase):
    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CUSTOM

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[{"d": "c"}])

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        return {"ok": True}


def _discovery_stub_builder(config: dict, creds: dict) -> ConnectorBase:
    return _DiscoveryStubConnector()


def test_discover_plugins_no_plugins():
    registry = PluginRegistry()
    with patch("modulo.core.plugin_registry.importlib.metadata.entry_points", return_value=[]):
        discovered = registry.discover_plugins()
    assert discovered == []
    assert registry.list_plugins() == {}


def test_discover_plugins_connector_entry_point():
    registry = PluginRegistry()
    mock_ep = _make_mock_entry_point(
        "modulo.connectors",
        "my_demo_connector",
        load_result=_discovery_stub_builder,
    )
    with patch(
        "modulo.core.plugin_registry.importlib.metadata.entry_points",
        side_effect=[[mock_ep], []],
    ):
        discovered = registry.discover_plugins()

    assert len(discovered) == 1
    assert discovered[0].PLUGIN_ID == "pkg-demo"
    assert "connector_type" in discovered[0].capabilities
    assert registry.has_connector_type("my_demo_connector")
    connector = registry.build_connector("my_demo_connector", {}, {})
    assert isinstance(connector, _DiscoveryStubConnector)


def test_discover_plugins_backend_entry_point():
    registry = PluginRegistry()
    mock_ep = _make_mock_entry_point(
        "modulo.model_backends",
        "my_demo_backend",
        load_result=_build_stub_backend,
    )
    with patch(
        "modulo.core.plugin_registry.importlib.metadata.entry_points",
        side_effect=[[], [mock_ep]],
    ):
        discovered = registry.discover_plugins()

    assert len(discovered) == 1
    assert "model_backend" in discovered[0].capabilities
    assert registry.has_model_backend("my_demo_backend")


def test_discover_plugins_both_groups():
    """Discovering from both entry-point groups populates connectors and backends."""
    registry = PluginRegistry()
    ep1 = _make_mock_entry_point("modulo.connectors", "c1", dist_name="pkg-a", load_result=_discovery_stub_builder)
    ep2 = _make_mock_entry_point("modulo.model_backends", "b1", dist_name="pkg-b", load_result=_build_stub_backend)
    with patch(
        "modulo.core.plugin_registry.importlib.metadata.entry_points",
        side_effect=[[ep1], [ep2]],
    ):
        discovered = registry.discover_plugins()

    assert len(discovered) == 2
    assert registry.has_connector_type("c1")
    assert registry.has_model_backend("b1")


def test_discover_plugins_duplicate_plugin_id():
    """When two entry points share the same dist name, capabilities are merged."""
    registry = PluginRegistry()
    ep1 = _make_mock_entry_point("modulo.connectors", "c1", dist_name="pkg-x", load_result=_discovery_stub_builder)
    ep2 = _make_mock_entry_point("modulo.model_backends", "b1", dist_name="pkg-x", load_result=_build_stub_backend)
    with patch(
        "modulo.core.plugin_registry.importlib.metadata.entry_points",
        side_effect=[[ep1], [ep2]],
    ):
        registry.discover_plugins()

    # Capabilities from both entry points are merged on the same PLUGIN_ID
    plugins = registry.list_plugins()
    assert "pkg-x" in plugins
    assert plugins["pkg-x"].capabilities == {"connector_type", "model_backend"}


def test_discover_plugins_entry_point_no_dist():
    """An entry point without a distribution is silently skipped."""
    registry = PluginRegistry()
    no_dist = _make_mock_entry_point("modulo.connectors", "c1", dist_name="pkg-x")
    no_dist.dist = None  # type: ignore[attr-defined]
    with patch(
        "modulo.core.plugin_registry.importlib.metadata.entry_points",
        return_value=[no_dist],
    ):
        discovered = registry.discover_plugins()
    assert discovered == []


def test_discover_plugins_entry_point_load_failure():
    """If an entry point's load() raises, the plugin is marked unhealthy and skipped."""
    registry = PluginRegistry()
    fail_ep = _make_mock_entry_point(
        "modulo.connectors",
        "failing_con",
        dist_name="pkg-broken",
        load_side_effect=ImportError,
    )
    with patch(
        "modulo.core.plugin_registry.importlib.metadata.entry_points",
        side_effect=[[fail_ep], []],
    ):
        discovered = registry.discover_plugins()
    assert discovered == []

    # The entry point failed to load → no manifest stored → known as "Unknown"
    health = registry.health_check("pkg-broken")
    assert health["pkg-broken"].ok is False


# ---------------------------------------------------------------------------
# Property accessors
# ---------------------------------------------------------------------------


def test_connector_types_property():
    registry = PluginRegistry()
    manifest = PluginManifest(PLUGIN_ID="p1", display_name="P1", description="", version="1")
    registry.register_connector_type("type_a", _build_stub_connector, manifest)
    registry.register_connector_type("type_b", _build_stub_connector, manifest)
    assert registry.connector_types == frozenset({"type_a", "type_b"})


def test_backend_providers_property():
    registry = PluginRegistry()
    m1 = PluginManifest(PLUGIN_ID="pa", display_name="Pa", description="", version="1")
    m2 = PluginManifest(PLUGIN_ID="pb", display_name="Pb", description="", version="1")
    registry.register_model_backend("prov_a", _build_stub_backend, m1)
    registry.register_model_backend("prov_b", _build_stub_backend, m2)
    assert registry.backend_providers == frozenset({"prov_a", "prov_b"})


# ---------------------------------------------------------------------------
# Empty / degenerate edge cases
# ---------------------------------------------------------------------------


def test_list_plugins_empty():
    assert PluginRegistry().list_plugins() == {}


def test_get_plugin_manifest_for_unknown_id():
    assert PluginRegistry().get_plugin("nope") is None


def test_has_connector_type_on_empty_registry():
    assert not PluginRegistry().has_connector_type("anything")


def test_has_model_backend_on_empty_registry():
    assert not PluginRegistry().has_model_backend("anything")


def test_health_check_all_empty():
    results = PluginRegistry().health_check()
    assert results == {}
