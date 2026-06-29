"""Plugin Registry — discovery, registration, and health-checking for third-party plugins.

Plugins extend Modulo with additional connector types and model backend providers
via standard Python ``importlib.metadata.entry_points`` (setuptools entry points).

Entry point groups:
    - ``modulo.connectors`` — provides ``(config, creds) -> ConnectorBase`` builders
    - ``modulo.model_backends`` — provides ``(api_key, model_id, **default_params)``
      ``-> ModelBackendBase`` builders

Usage:
    registry = PluginRegistry()
    registry.discover_plugins()
    connector = registry.build_connector("my_connector", config, creds)
    backend = registry.build_model_backend("my_provider", api_key, model_id)
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from modulo.connectors.base import ConnectorBase
from modulo.model_backends.base import ModelBackendBase

logger = logging.getLogger(__name__)

_DISCOVERED: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    global _DISCOVERED
    if _DISCOVERED is None:
        _DISCOVERED = PluginRegistry()
    return _DISCOVERED


@dataclass
class PluginManifest:
    """Metadata for an installed Modulo plugin."""

    PLUGIN_ID: str
    display_name: str
    description: str
    version: str
    capabilities: set[str] = field(default_factory=set)


@dataclass
class PluginHealth:
    ok: bool
    detail: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PluginRegistry:
    """Discovers installed plugins via entry_points and manages connector/backend builders.

    Thread-safe when only read operations are used after initial discovery.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, PluginManifest] = {}
        self._health: dict[str, PluginHealth] = {}
        self._connector_builders: dict[str, Callable[..., ConnectorBase]] = {}
        self._backend_builders: dict[str, Callable[..., ModelBackendBase]] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_plugins(self) -> list[PluginManifest]:
        """Scan installed packages for ``modulo.connectors`` and ``modulo.model_backends``
        entry points.

        Returns the list of newly discovered manifests.
        """
        discovered: list[PluginManifest] = []
        for group in ("modulo.connectors", "modulo.model_backends"):
            for ep in importlib.metadata.entry_points(group=group):
                manifest = self._load_entry_point(ep, group)
                if manifest is not None:
                    self._plugins[manifest.PLUGIN_ID] = manifest
                    discovered.append(manifest)
        if discovered:
            ids = [p.PLUGIN_ID for p in discovered]
            logger.info("Discovered %d plugin(s): %s", len(discovered), ids)
        return discovered

    def _load_entry_point(self, ep: importlib.metadata.EntryPoint, group: str) -> PluginManifest | None:
        """Load an entry point and register its builder.

        Returns a ``PluginManifest`` or ``None`` if the module could not be imported.
        """
        dist = ep.dist
        if dist is None:
            return None
        plugin_id = dist.name
        display_name = dist.metadata.get("Name", plugin_id)
        description = dist.metadata.get("Summary", "")
        version = dist.metadata.get("Version", "0.0.0")

        manifest = PluginManifest(
            PLUGIN_ID=plugin_id,
            display_name=display_name,
            description=description,
            version=version,
        )

        try:
            builder = ep.load()
        except Exception:
            logger.exception("Failed to load entry point %s from package %s", ep.name, plugin_id)
            detail = f"Failed to load entry point {ep.name}"
            self._plugins[plugin_id] = manifest
            self._health[plugin_id] = PluginHealth(ok=False, detail=detail)
            return None

        if group == "modulo.connectors":
            self._connector_builders[ep.name] = builder
            manifest.capabilities.add("connector_type")
            logger.debug("Registered connector type '%s' from plugin %s", ep.name, plugin_id)
        elif group == "modulo.model_backends":
            self._backend_builders[ep.name] = builder
            manifest.capabilities.add("model_backend")
            logger.debug("Registered model backend '%s' from plugin %s", ep.name, plugin_id)

        self._plugins[plugin_id] = manifest
        self._health[plugin_id] = PluginHealth(ok=True, detail="Loaded")
        return manifest

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def build_connector(self, type_id: str, config: dict[str, Any], creds: dict[str, Any]) -> ConnectorBase:
        """Build a connector from a plugin-registered builder.

        Raises ``KeyError`` if no plugin provides this connector type.
        """
        builder = self._connector_builders.get(type_id)
        if builder is None:
            raise KeyError(f"No plugin registered connector type {type_id!r}")
        return builder(config, creds)

    def build_model_backend(
        self, provider: str, model_id: str, api_key: str, **default_params: Any
    ) -> ModelBackendBase:
        """Build a model backend from a plugin-registered builder.

        Raises ``KeyError`` if no plugin provides this provider.
        """
        builder = self._backend_builders.get(provider)
        if builder is None:
            raise KeyError(f"No plugin registered model backend provider {provider!r}")
        return builder(api_key=api_key, model_id=model_id, **default_params)

    def register_connector_type(
        self, type_id: str, builder: Callable[..., ConnectorBase], manifest: PluginManifest
    ) -> None:
        """Explicitly register a connector type builder (e.g. from an in-tree module)."""
        self._connector_builders[type_id] = builder
        manifest.capabilities.add("connector_type")
        self._plugins[manifest.PLUGIN_ID] = manifest
        self._health[manifest.PLUGIN_ID] = PluginHealth(ok=True, detail="Registered in-tree")
        logger.info("Manually registered connector type '%s' from plugin %s", type_id, manifest.PLUGIN_ID)

    def register_model_backend(
        self, provider: str, builder: Callable[..., ModelBackendBase], manifest: PluginManifest
    ) -> None:
        """Explicitly register a model backend builder (e.g. from an in-tree module)."""
        self._backend_builders[provider] = builder
        manifest.capabilities.add("model_backend")
        self._plugins[manifest.PLUGIN_ID] = manifest
        self._health[manifest.PLUGIN_ID] = PluginHealth(ok=True, detail="Registered in-tree")
        logger.info("Manually registered model backend '%s' from plugin %s", provider, manifest.PLUGIN_ID)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_plugins(self) -> dict[str, PluginManifest]:
        """Return all discovered plugin manifests keyed by PLUGIN_ID."""
        return dict(self._plugins)

    def get_plugin(self, plugin_id: str) -> PluginManifest | None:
        return self._plugins.get(plugin_id)

    def has_connector_type(self, type_id: str) -> bool:
        return type_id in self._connector_builders

    def has_model_backend(self, provider: str) -> bool:
        return provider in self._backend_builders

    @property
    def connector_types(self) -> frozenset[str]:
        return frozenset(self._connector_builders)

    @property
    def backend_providers(self) -> frozenset[str]:
        return frozenset(self._backend_builders)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self, plugin_id: str | None = None) -> dict[str, PluginHealth]:
        """Verify all (or a single) plugin is still importable.

        Returns a dict of ``{plugin_id: PluginHealth}``.
        """
        if plugin_id is not None:
            if plugin_id in self._health:
                return {plugin_id: self._health[plugin_id]}
            manifest = self._plugins.get(plugin_id)
            if manifest is None:
                return {plugin_id: PluginHealth(ok=False, detail="Unknown plugin")}
            return {plugin_id: self._check_single(manifest)}

        results: dict[str, PluginHealth] = {}
        for pid, manifest in self._plugins.items():
            results[pid] = self._check_single(manifest)
        self._health.update(results)
        return results

    def _check_single(self, manifest: PluginManifest) -> PluginHealth:
        """Verify that the manifest's source package is still importable."""
        try:
            importlib.metadata.metadata(manifest.PLUGIN_ID)
            return PluginHealth(ok=True, detail="Package metadata found")
        except importlib.metadata.PackageNotFoundError:
            return PluginHealth(ok=False, detail="Package not found in installed packages")
