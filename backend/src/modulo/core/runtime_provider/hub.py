from __future__ import annotations

"""RuntimeProviderHub — registry and resolution of RuntimeProvider implementations."""


import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from modulo.core.runtime_provider import RuntimeProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from modulo.db.models.environment_profile import EnvironmentProfile

_log = logging.getLogger(__name__)


class RuntimeProviderHub:
    """Central registry for RuntimeProvider implementations.

    Supports both the legacy RuntimeProvider interface (create_workspace(WorkspaceSpec))
    and the new RuntimeProvider ABC (from modulo.core.runtime_provider.base).
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

        for provider in self._providers.values():
            return provider

        return None

    async def initialise(self, config: dict[str, Any]) -> None:
        """Factory-load providers from a configuration dict.

        The config dict maps provider names to provider-specific configs.
        Supported provider types: local_docker, e2b.
        """
        for provider_name, provider_config in config.items():
            if provider_name in self._providers:
                _log.warning("Provider '%s' already registered, skipping factory init", provider_name)
                continue
            provider_type = provider_config.get("type", provider_name)
            match provider_type:
                case "local_docker":
                    from modulo.core.runtime_provider.local_docker import LocalDockerRuntimeProvider

                    docker_host = provider_config.get("docker_host")
                    default_image = provider_config.get("default_image", "python:3.12-slim")
                    provider = LocalDockerRuntimeProvider(
                        docker_host=docker_host,
                        default_image=default_image,
                    )
                    self.register(provider_name, provider)
                case "e2b":
                    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

                    api_key = provider_config.get("api_key")
                    if not api_key:
                        _log.warning("E2B provider '%s' has no api_key, skipping", provider_name)
                        continue
                    provider = E2BRuntimeProvider(api_key=api_key)
                    self.register(provider_name, provider)
                case _:
                    _log.warning("Unknown provider type '%s' in config, skipping", provider_type)

    async def create_lease(
        self,
        profile: EnvironmentProfile,
        run_id: uuid.UUID,
        session: AsyncSession,
    ) -> Any:
        """Create a workspace lease from an EnvironmentProfile.

        Returns a WorkspaceLease ORM instance (not yet committed).
        """
        provider_name = getattr(profile, "provider_type", None) or "local"
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ValueError(f"No RuntimeProvider registered for '{provider_name}'")

        workspace_ref = await provider.create_workspace(profile, session)

        from modulo.db.models.workspace_lease import WorkspaceLease

        lease = WorkspaceLease(
            organisation_id=profile.organisation_id,
            environment_profile_id=profile.id,
            run_id=run_id,
            provider_ref=str(workspace_ref),
            status="running",
            lease_started_at=datetime.now(UTC),
        )
        session.add(lease)
        return lease

    async def destroy_lease(self, lease: Any) -> None:
        """Destroy a workspace lease and update its status."""
        profile = getattr(lease, "environment_profile", None)
        provider_name = "local" if profile is None else getattr(profile, "provider_type", None) or "local"

        provider = self._providers.get(provider_name)
        if provider is None:
            _log.warning("No RuntimeProvider registered for '%s', cannot destroy lease", provider_name)
            return

        try:
            await provider.destroy_workspace(lease.provider_ref if hasattr(lease, "provider_ref") else lease)
        except Exception:
            _log.exception("Failed to destroy workspace for lease %s", lease)

        if hasattr(lease, "status"):
            lease.status = "completed"
