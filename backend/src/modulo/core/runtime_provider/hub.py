"""RuntimeProviderHub — registry and resolution of RuntimeProvider implementations."""

from __future__ import annotations

from typing import Any

from modulo.core.runtime_provider import RuntimeProvider


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
           whose ``supports()`` returns True (if the provider implements
           the supports protocol).
        3. Fall back to the first registered provider.
        4. Return None if nothing is registered.
        """
        hint: str | None = getattr(profile, "provider_hint", None)
        if hint and hint in self._providers:
            return self._providers[hint]

        for provider in self._providers.values():
            supports = getattr(provider, "supports", None)
            if supports is not None and callable(supports):
                try:
                    if supports(profile):
                        return provider
                except Exception:  # noqa: S112  # nosec
                    continue

        # Fallback: return first registered provider
        for provider in self._providers.values():
            return provider

        return None
