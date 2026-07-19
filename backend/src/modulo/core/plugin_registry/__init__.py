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

import asyncio
import copy
import importlib.metadata
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from modulo.connectors.base import ConnectorBase
from modulo.model_backends.base import ModelBackendBase

logger = logging.getLogger(__name__)

__all__ = [
    "PluginHealth",
    "PluginManifest",
    "PluginNotFoundError",
    "PluginRegistry",
    "get_plugin_registry",
    "reset_plugin_registry",
]

_ENTRY_POINT_CONNECTORS = "modulo.connectors"
_ENTRY_POINT_MODEL_BACKENDS = "modulo.model_backends"
_ENTRY_POINT_GROUPS: tuple[str, ...] = (
    _ENTRY_POINT_CONNECTORS,
    _ENTRY_POINT_MODEL_BACKENDS,
)

_HLTH_LOADED = "Loaded"
_HLTH_REGISTERED_IN_TREE = "Registered in-tree"
_HLTH_METADATA_FOUND = "Package metadata found"
_HLTH_PACKAGE_NOT_FOUND = "Package not found in installed packages"
_HLTH_UNKNOWN_PLUGIN = "Unknown plugin"
_HLTH_FAILED_LOAD = "Failed to load entry point"
_HLTH_BUILD_FAILED = "Plugin builder raised an error"

_CAP_CONNECTOR = "connector_type"
_CAP_MODEL_BACKEND = "model_backend"


class PluginNotFoundError(KeyError):
    """Raised when a plugin or builder is not found in the registry."""


_DISCOVERED: PluginRegistry | None = None
_DISCOVERED_LOCK: threading.Lock = threading.Lock()


def get_plugin_registry() -> PluginRegistry:
    global _DISCOVERED
    if _DISCOVERED is None:
        with _DISCOVERED_LOCK:
            if _DISCOVERED is None:
                _DISCOVERED = PluginRegistry()
    return _DISCOVERED


def reset_plugin_registry() -> None:
    """Reset the cached singleton — useful for test teardown."""
    global _DISCOVERED
    with _DISCOVERED_LOCK:
        _DISCOVERED = None


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

    Thread-safe for concurrent reads. Mutation operations acquire ``_lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plugins: dict[str, PluginManifest] = {}
        self._health: dict[str, PluginHealth] = {}
        self._entry_point_errors: dict[str, str] = {}
        self._connector_builders: dict[str, Callable[..., ConnectorBase]] = {}
        self._backend_builders: dict[str, Callable[..., ModelBackendBase]] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_plugins(self) -> list[PluginManifest]:
        """Scan installed packages for entry points.

        Returns the list of newly discovered manifests.
        """
        discovered: list[PluginManifest] = []
        try:
            entries: list[tuple[str, importlib.metadata.EntryPoint]] = [
                (group, ep) for group in _ENTRY_POINT_GROUPS for ep in importlib.metadata.entry_points(group=group)
            ]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to query entry points during plugin discovery")
            return discovered

        for group, ep in entries:
            manifest = self._load_entry_point(ep, group)
            if manifest is not None:
                discovered.append(manifest)

        if discovered:
            ids = [p.PLUGIN_ID for p in discovered]
            logger.info("Discovered %d plugin(s): %s", len(discovered), ids)
        return [copy.deepcopy(m) for m in discovered]

    def _load_entry_point(self, ep: importlib.metadata.EntryPoint, group: str) -> PluginManifest | None:
        """Load an entry point and register its builder.

        ``ep.load()`` runs outside the registry lock so that a slow or
        hanging plugin import does not block concurrent readers.

        Returns a ``PluginManifest`` or ``None`` if the module could not be imported.
        """
        dist = ep.dist
        if dist is None:
            return None
        plugin_id = dist.name
        display_name = dist.metadata.get("Name", plugin_id)
        description = dist.metadata.get("Summary", "")
        version = dist.metadata.get("Version", "0.0.0")

        try:
            builder = ep.load()
        except (ImportError, TypeError, AttributeError, SyntaxError):
            logger.exception("Failed to load entry point %s from package %s", ep.name, plugin_id)
            detail = f"Failed to load entry point {ep.name}"
            with self._lock:
                self._entry_point_errors[plugin_id] = detail
                existing = self._plugins.get(plugin_id)
                if existing is None:
                    existing = PluginManifest(
                        PLUGIN_ID=plugin_id,
                        display_name=display_name,
                        description=description,
                        version=version,
                    )
                self._plugins[plugin_id] = existing
                self._health[plugin_id] = PluginHealth(ok=False, detail=detail)
            return None

        with self._lock:
            self._entry_point_errors.pop(plugin_id, None)
            existing = self._plugins.get(plugin_id)
            if existing is not None:
                manifest = existing
            else:
                manifest = PluginManifest(
                    PLUGIN_ID=plugin_id,
                    display_name=display_name,
                    description=description,
                    version=version,
                )

            if group == _ENTRY_POINT_CONNECTORS:
                self._connector_builders[ep.name] = builder
                manifest.capabilities.add(_CAP_CONNECTOR)
                logger.debug("Registered connector type '%s' from plugin %s", ep.name, plugin_id)
            elif group == _ENTRY_POINT_MODEL_BACKENDS:
                self._backend_builders[ep.name] = builder
                manifest.capabilities.add(_CAP_MODEL_BACKEND)
                logger.debug("Registered model backend '%s' from plugin %s", ep.name, plugin_id)

            self._plugins[plugin_id] = manifest
            self._health[plugin_id] = PluginHealth(ok=True, detail=_HLTH_LOADED)
        return manifest

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def build_connector(self, type_id: str, config: dict[str, Any], creds: dict[str, Any]) -> ConnectorBase:
        """Build a connector from a plugin-registered builder.

        Raises ``PluginNotFoundError`` if no plugin provides this connector type.
        """
        if not isinstance(type_id, str):
            raise TypeError("type_id must be a string")
        if not isinstance(config, dict):
            raise TypeError("config must be a dict")
        if not isinstance(creds, dict):
            raise TypeError("creds must be a dict")
        with self._lock:
            builder = self._connector_builders.get(type_id)
        if builder is None:
            raise PluginNotFoundError(f"No plugin registered connector type {type_id!r}")
        try:
            return builder(config, creds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Connector builder %s for type %s raised an error", builder.__name__, type_id)
            raise RuntimeError(f"Connector builder for type {type_id!r} failed") from exc

    def build_model_backend(
        self, provider: str, model_id: str, api_key: str, **default_params: Any
    ) -> ModelBackendBase:
        """Build a model backend from a plugin-registered builder.

        Raises ``PluginNotFoundError`` if no plugin provides this provider.
        """
        if not isinstance(provider, str):
            raise TypeError("provider must be a string")
        if not isinstance(model_id, str):
            raise TypeError("model_id must be a string")
        if not isinstance(api_key, str):
            raise TypeError("api_key must be a string")
        with self._lock:
            builder = self._backend_builders.get(provider)
        if builder is None:
            raise PluginNotFoundError(f"No plugin registered model backend provider {provider!r}")
        try:
            return builder(api_key=api_key, model_id=model_id, **default_params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Model backend builder %s for provider %s raised an error", builder.__name__, provider)
            raise RuntimeError(f"Model backend builder for provider {provider!r} failed") from exc

    def register_connector_type(
        self, type_id: str, builder: Callable[..., ConnectorBase], manifest: PluginManifest
    ) -> None:
        """Explicitly register a connector type builder (e.g. from an in-tree module)."""
        if not isinstance(type_id, str):
            raise TypeError("type_id must be a string")
        if not callable(builder):
            raise TypeError("builder must be callable")
        if not isinstance(manifest, PluginManifest):
            raise TypeError("manifest must be a PluginManifest")
        with self._lock:
            if type_id in self._connector_builders:
                logger.warning("Overwriting existing connector type '%s' from plugin %s", type_id, manifest.PLUGIN_ID)
            self._connector_builders[type_id] = builder
            self._finalize_registration(manifest, _CAP_CONNECTOR)

    def register_model_backend(
        self, provider: str, builder: Callable[..., ModelBackendBase], manifest: PluginManifest
    ) -> None:
        """Explicitly register a model backend builder (e.g. from an in-tree module)."""
        if not isinstance(provider, str):
            raise TypeError("provider must be a string")
        if not callable(builder):
            raise TypeError("builder must be callable")
        if not isinstance(manifest, PluginManifest):
            raise TypeError("manifest must be a PluginManifest")
        with self._lock:
            if provider in self._backend_builders:
                logger.warning("Overwriting existing model backend '%s' from plugin %s", provider, manifest.PLUGIN_ID)
            self._backend_builders[provider] = builder
            self._finalize_registration(manifest, _CAP_MODEL_BACKEND)

    def _finalize_registration(self, manifest: PluginManifest, capability: str) -> None:
        """Shared bookkeeping for registering a plugin's manifest and health."""
        manifest.capabilities.add(capability)
        self._plugins[manifest.PLUGIN_ID] = manifest
        self._entry_point_errors.pop(manifest.PLUGIN_ID, None)
        self._health[manifest.PLUGIN_ID] = PluginHealth(ok=True, detail=_HLTH_REGISTERED_IN_TREE)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_plugins(self) -> dict[str, PluginManifest]:
        """Return all discovered plugin manifests keyed by PLUGIN_ID."""
        with self._lock:
            return {pid: copy.deepcopy(m) for pid, m in self._plugins.items()}

    def get_plugin(self, plugin_id: str) -> PluginManifest | None:
        with self._lock:
            m = self._plugins.get(plugin_id)
            return copy.deepcopy(m) if m is not None else None

    def has_connector_type(self, type_id: str) -> bool:
        with self._lock:
            return type_id in self._connector_builders

    def has_model_backend(self, provider: str) -> bool:
        with self._lock:
            return provider in self._backend_builders

    @property
    def connector_types(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._connector_builders)

    @property
    def backend_providers(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._backend_builders)

    @property
    def entry_point_errors(self) -> dict[str, str]:
        """Return a copy of entry-point load errors keyed by plugin_id."""
        with self._lock:
            return dict(self._entry_point_errors)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self, plugin_id: str | None = None) -> dict[str, PluginHealth]:
        """Verify all (or a single) plugin is still importable.

        Returns a dict of ``{plugin_id: PluginHealth}``.
        """
        with self._lock:
            if plugin_id is not None:
                manifest = self._plugins.get(plugin_id)
                if manifest is None:
                    return {plugin_id: PluginHealth(ok=False, detail=_HLTH_UNKNOWN_PLUGIN)}
                manifests = [manifest]
                cached_errors = {manifest.PLUGIN_ID: self._entry_point_errors.get(manifest.PLUGIN_ID)}
            else:
                manifests = list(self._plugins.values())
                cached_errors = {m.PLUGIN_ID: self._entry_point_errors.get(m.PLUGIN_ID) for m in manifests}

        results: dict[str, PluginHealth] = {}
        for manifest in manifests:
            results[manifest.PLUGIN_ID] = self._check_single(manifest, cached_errors.get(manifest.PLUGIN_ID))

        with self._lock:
            self._health.update(results)
        return results

    def _check_single(self, manifest: PluginManifest, cached_error: str | None = None) -> PluginHealth:
        """Verify that the manifest's source package is still importable.

        If the entry point failed to load at discovery time, ``cached_error``
        is used as the definitive health status.  Otherwise the package's
        metadata is re-checked to detect uninstallation.
        """
        if cached_error:
            return PluginHealth(ok=False, detail=cached_error)
        try:
            importlib.metadata.metadata(manifest.PLUGIN_ID)
            return PluginHealth(ok=True, detail=_HLTH_METADATA_FOUND)
        except importlib.metadata.PackageNotFoundError:
            return PluginHealth(ok=False, detail=_HLTH_PACKAGE_NOT_FOUND)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error checking metadata for plugin %s", manifest.PLUGIN_ID)
            return PluginHealth(ok=False, detail="Error checking plugin metadata")
