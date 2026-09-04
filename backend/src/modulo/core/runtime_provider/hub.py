"""RuntimeProviderHub — registry and resolution of RuntimeProvider implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from modulo.core.runtime_provider import (
    ProviderNotConfiguredError,
    RuntimeProvider,
    env_var_for_provider_type,
)

_log = logging.getLogger(__name__)


class RuntimeProviderHub:
    """Central registry for RuntimeProvider implementations.

    All providers implement the canonical WorkspaceSpec-based interface.
    Resolution is deterministic: an explicit ``provider_hint`` or
    ``provider_type`` match wins; anything unresolvable raises
    :class:`ProviderNotConfiguredError` — there is no ``supports()`` guessing
    and no first-registered fallback (ADR 029).
    """

    def __init__(self) -> None:
        self._providers: dict[str, RuntimeProvider] = {}
        self._lock = asyncio.Lock()

    def register(self, name: str, provider: RuntimeProvider) -> None:
        """Register a RuntimeProvider under a symbolic name."""
        if name in self._providers:
            raise ValueError(f"RuntimeProvider '{name}' is already registered")
        self._providers[name] = provider

    def unregister(self, name: str) -> None:
        """Remove a registered provider by name."""
        if name not in self._providers:
            _log.warning("RuntimeProvider '%s' is not registered", name)
            return
        self._providers.pop(name, None)

    def get(self, name: str) -> RuntimeProvider | None:
        """Look up a registered provider by name."""
        return self._providers.get(name)

    def list_providers(self) -> dict[str, RuntimeProvider]:
        """Return a thread-safe copy of the provider registry."""
        return dict(self._providers)

    def resolve(
        self,
        profile: Any,
    ) -> RuntimeProvider:
        """Resolve the RuntimeProvider for the given profile deterministically.

        Resolution strategy:
        1. If the profile declares a ``provider_hint`` matching a registered
           name, use it.
        2. Treat an explicit ``provider_type`` as authoritative and match it
           against registered names or provider identity (id + aliases).
        3. Anything else raises :class:`ProviderNotConfiguredError` naming
           the env var that would register the provider (when documented).
        """
        providers = dict(self._providers)
        provider = self._resolve_by_hint(providers, profile)
        if provider is not None:
            return provider
        return self._resolve_by_type(providers, profile)

    @staticmethod
    def _resolve_by_hint(
        providers: dict[str, RuntimeProvider],
        profile: Any,
    ) -> RuntimeProvider | None:
        raw_hint: Any = getattr(profile, "provider_hint", None)
        if not isinstance(raw_hint, str) or not raw_hint.strip():
            return None
        hint_normalized = raw_hint.strip().lower()
        if hint_normalized not in providers:
            _log.warning("RuntimeProvider hint '%s' specified but no matching provider registered", raw_hint)
            return None
        return providers[hint_normalized]

    @staticmethod
    def _resolve_by_type(
        providers: dict[str, RuntimeProvider],
        profile: Any,
    ) -> RuntimeProvider:
        raw_provider_type: Any = getattr(profile, "provider_type", None)
        if isinstance(raw_provider_type, str) and raw_provider_type.strip():
            provider_type = raw_provider_type.strip().lower()

            direct_match = providers.get(provider_type)
            if direct_match is not None:
                return direct_match

            matches = [
                provider for _, provider in sorted(providers.items()) if provider.matches_provider_type(provider_type)
            ]
            if matches:
                return matches[0]

            raise ProviderNotConfiguredError(
                provider_type,
                env_var_for_provider_type(provider_type),
            )

        provider_type = str(raw_provider_type) if raw_provider_type is not None else "unspecified"
        raise ProviderNotConfiguredError(provider_type, None)

    async def initialise(self, config: dict[str, Any]) -> None:
        """Factory-load providers from a configuration dict.

        The config dict maps provider names to provider-specific configs.
        Supported provider types: runner_docker (legacy alias local_docker), e2b.
        """
        for provider_name, provider_config in config.items():
            if provider_name in self._providers:
                _log.warning("Provider '%s' already registered, skipping factory init", provider_name)
                continue
            provider_type = provider_config.get("type", provider_name)
            match provider_type:
                case "local_docker" | "runner_docker":
                    from modulo.core.runtime_provider.docker import DockerRuntimeProvider

                    docker_host = provider_config.get("docker_host")
                    default_image = provider_config.get("default_image", "python:3.12-slim")
                    docker_provider = DockerRuntimeProvider(
                        docker_host=docker_host,
                        default_image=default_image,
                    )
                    try:
                        self.register(provider_name, docker_provider)
                    except ValueError:
                        _log.warning("Provider '%s' already registered, skipping", provider_name)
                case "e2b":
                    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

                    api_key = provider_config.get("api_key")
                    if not api_key:
                        _log.warning("E2B provider '%s' has no api_key, skipping", provider_name)
                        continue
                    e2b_provider = E2BRuntimeProvider(api_key=api_key)
                    try:
                        self.register(provider_name, e2b_provider)
                    except ValueError:
                        _log.warning("Provider '%s' already registered, skipping", provider_name)
                case _:
                    _log.warning("Unknown provider type '%s' in config, skipping", provider_type)

    async def aclose(self) -> None:
        """Close every registered provider's owned resources.

        Per-provision disposal (ADR 029): provider-owned clients are closed
        explicitly, never left to GC. Failures are logged per provider and
        never mask the remaining teardown.
        """
        for name, provider in list(self._providers.items()):
            try:
                await provider.close()
            except Exception:
                _log.exception("Failed to close runtime provider '%s'", name)
