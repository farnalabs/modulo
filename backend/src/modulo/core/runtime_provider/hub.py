"""RuntimeProviderHub — registry and resolution of RuntimeProvider implementations."""

from __future__ import annotations

import logging
from typing import Any

from modulo.core.runtime_provider import RuntimeProvider

_log = logging.getLogger(__name__)


class RuntimeProviderHub:
    """Central registry for RuntimeProvider implementations.

    Providers are registered by name and can be resolved against an
    EnvironmentProfile's capabilities, image_ref, or other attributes.
    """

    def __init__(self) -> None:
        self._providers: dict[str, RuntimeProvider] = {}

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
        """Return a copy of the provider registry."""
        return dict(self._providers)

    def resolve(
        self,
        profile: Any,
    ) -> RuntimeProvider | None:
        """Resolve the most suitable RuntimeProvider for the given profile.

        Resolution strategy:
        1. If the profile declares a ``provider_hint``, look it up by name.
        2. Otherwise iterate registered providers and return the first
           whose ``supports()`` returns True.
        3. Fall back to the first registered provider.
        4. Return None if nothing is registered.
        """
        hint: str | None = getattr(profile, "provider_hint", None)
        if hint:
            hint_normalized = hint.lower()
            if hint_normalized in self._providers:
                return self._providers[hint_normalized]
            _log.warning("RuntimeProvider hint '%s' specified but no matching provider registered", hint)

        for provider in self._providers.values():
            try:
                if provider.supports(profile):
                    return provider
            except Exception:
                _log.warning("supports() raised for provider %s", provider, exc_info=True)
                continue

        # Fallback: return first registered provider
        for provider in self._providers.values():
            return provider

        return None
