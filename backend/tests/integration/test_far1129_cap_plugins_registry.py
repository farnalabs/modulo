"""FAR-1129 cap-plugins: verify the plugin registry against REAL installed plugins.

The existing unit coverage mock ``importlib.metadata.entry_points`` end-to-end,
so the glue between ``PluginRegistry`` and the real entry-point metadata format
is never exercised. These tests install a small plugin into a temp directory
(a real importable module + real ``*.dist-info`` metadata on ``sys.path``) and
drive ``PluginRegistry.discover_plugins`` / ``build_connector`` against it, so
the metadata parsing, entry-point ``load()``, builder invocation, health check,
and error paths all run through the real implementation.

Nothing here needs a container: the plugin package is self-contained and the
registry reads it via stdlib ``importlib.metadata``.

Installing a package is not required to prove the boundary — a ``*.dist-info``
directory on ``sys.path`` is the exact artifact ``importlib.metadata`` discovers,
so authoring it directly (instead of shelling out to pip) keeps the test
deterministic, network-free, and fast while still exercising the real registry.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import pathlib
import sys

import pytest

from modulo.connectors.base import ConnectorBase, ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.core.plugin_registry import PluginNotFoundError, PluginRegistry

pytestmark = pytest.mark.integration

_PLUGIN_NAME = "modulo-demo-plugin"
_CONNECTOR_TYPE = "demo_connector"

_CONNECTOR_PLUGIN_SRC = """\
from modulo.connectors.base import ConnectorBase, ConnectorPayload, ConnectorQuery, ConnectorResult, ConnectorType
from modulo.model_backends.base import HealthResult


class DemoConnector(ConnectorBase):
    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CUSTOM

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def query(self, query: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[{"source": "real-plugin", "resource": query.resource}])

    async def write(self, payload: ConnectorPayload) -> dict:
        return {"written": payload.resource}


def build_connector(config, creds):
    return DemoConnector()
"""

_BROKEN_PLUGIN_SRC = """\
raise ImportError("deliberately broken plugin module")
"""


def _write_plugin(site: pathlib.Path, name: str, entry_points: str, module_src: str) -> None:
    """Write a self-contained plugin package + metadata into ``site``."""
    plugin_dir = site / name.replace("-", "_")
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(module_src)

    # importlib.metadata indexes dist-info directories by the NORMALIZED
    # (underscore) package name, so the directory must use underscores even
    # though the METADATA Name uses the display (dash) form.
    dist_info = site / f"{name.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        "Version: 1.0.0\n"
        "Summary: A demo plugin used by FAR-1129 integration tests\n"
    )
    (dist_info / "entry_points.txt").write_text(entry_points)
    (dist_info / "top_level.txt").write_text(name.replace("-", "_") + "\n")


@pytest.fixture(scope="module")
def plugin_site(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Create a real installed plugin package on ``sys.path`` for this module."""
    site = tmp_path_factory.mktemp("far1129_site")
    _write_plugin(
        site,
        _PLUGIN_NAME,
        f"[modulo.connectors]\n{_CONNECTOR_TYPE} = {_PLUGIN_NAME.replace('-', '_')}:build_connector\n",
        _CONNECTOR_PLUGIN_SRC,
    )
    _write_plugin(
        site,
        "modulo-broken-plugin",
        "[modulo.connectors]\nbroken_connector = modulo_broken_plugin:build_connector\n",
        _BROKEN_PLUGIN_SRC,
    )
    _write_plugin(
        site,
        "modulo-raising-plugin",
        "[modulo.connectors]\nraising_connector = modulo_raising_plugin:build_connector\n",
        "def build_connector(config, creds):\n    raise RuntimeError('builder misbehaving')\n",
    )

    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    yield site
    with contextlib.suppress(ValueError):
        sys.path.remove(str(site))
    importlib.invalidate_caches()


def test_real_plugin_discovered_and_built(plugin_site: pathlib.Path) -> None:
    registry = PluginRegistry()
    discovered = registry.discover_plugins()

    manifest = registry.get_plugin(_PLUGIN_NAME)
    assert manifest is not None
    assert manifest.PLUGIN_ID == _PLUGIN_NAME
    assert manifest.version == "1.0.0"
    assert "connector_type" in manifest.capabilities

    ids = [m.PLUGIN_ID for m in discovered]
    assert _PLUGIN_NAME in ids
    assert registry.has_connector_type(_CONNECTOR_TYPE)
    assert _CONNECTOR_TYPE in registry.connector_types

    connector = registry.build_connector(_CONNECTOR_TYPE, {"url": "http://loopback/"}, {"token": "secret"})
    assert isinstance(connector, ConnectorBase)
    assert connector.connector_type == ConnectorType.CUSTOM


async def test_real_plugin_connector_roundtrip(plugin_site: pathlib.Path) -> None:
    registry = PluginRegistry()
    registry.discover_plugins()

    connector = registry.build_connector(_CONNECTOR_TYPE, {"url": "http://loopback/"}, {"token": "secret"})

    health = await connector.health_check()
    assert health.ok is True

    result = await connector.query(ConnectorQuery(resource="issues", filters={"state": "open"}))
    # Records carry the resource that actually reached the plugin over the real builder.
    assert result.records
    assert result.records[0]["source"] == "real-plugin"
    assert result.records[0]["resource"] == "issues"

    write_result = await connector.write(ConnectorPayload(resource="notes", data={"body": "hello"}))
    assert write_result["written"] == "notes"


def test_plugin_conflicting_metadata_reported_in_health(plugin_site: pathlib.Path) -> None:
    registry = PluginRegistry()
    registry.discover_plugins()

    health = registry.health_check(_PLUGIN_NAME)
    assert health[_PLUGIN_NAME].ok is True
    assert "metadata found" in health[_PLUGIN_NAME].detail

    broken = registry.health_check("modulo-broken-plugin")
    assert broken["modulo-broken-plugin"].ok is False
    assert "Failed to load entry point" in broken["modulo-broken-plugin"].detail


def test_broken_entry_point_registered_as_unhealthy(plugin_site: pathlib.Path) -> None:
    registry = PluginRegistry()
    registry.discover_plugins()

    errors = registry.entry_point_errors
    assert "modulo-broken-plugin" in errors
    assert "Failed to load entry point" in errors["modulo-broken-plugin"]

    broken = registry.get_plugin("modulo-broken-plugin")
    assert broken is not None
    assert broken.PLUGIN_ID == "modulo-broken-plugin"


def test_connector_builder_raises_is_wrapped(plugin_site: pathlib.Path) -> None:
    registry = PluginRegistry()
    registry.discover_plugins()

    with pytest.raises(RuntimeError, match="Connector builder for type 'raising_connector' failed"):
        registry.build_connector("raising_connector", {}, {})


def test_unknown_connector_type_raises(plugin_site: pathlib.Path) -> None:
    registry = PluginRegistry()
    registry.discover_plugins()

    with pytest.raises(PluginNotFoundError):
        registry.build_connector("no_such_connector", {}, {})


def test_plugin_site_is_genuinely_metadata_backed(plugin_site: pathlib.Path) -> None:
    # Guards against the fixture silently failing to install: the demo plugin
    # must be visible to real importlib.metadata over sys.path.
    found = [d.metadata["Name"] for d in importlib.metadata.distributions()]
    assert _PLUGIN_NAME in found
    ep = importlib.metadata.entry_points(group="modulo.connectors", name=_CONNECTOR_TYPE)
    assert ep
    builder = next(iter(ep)).load()
    assert callable(builder)
