"""RuntimeProviderHub — registry and resolution of RuntimeProvider implementations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

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
        self._lock = asyncio.Lock()

    async def register(self, name: str, provider: RuntimeProvider) -> None:
        """Register a RuntimeProvider under a symbolic name."""
        async with self._lock:
            if name in self._providers:
                raise ValueError(f"RuntimeProvider '{name}' is already registered")
            self._providers[name] = provider

    async def unregister(self, name: str) -> None:
        """Remove a registered provider by name."""
        async with self._lock:
            if name not in self._providers:
                _log.warning("RuntimeProvider '%s' is not registered", name)
                return
            self._providers.pop(name, None)

    async def get(self, name: str) -> RuntimeProvider | None:
        """Look up a registered provider by name."""
        async with self._lock:
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
        2. Match by ``provider_type`` against provider_id.
        3. Fall back to the first registered provider.
        4. Return None if nothing is registered.
        """
        providers = dict(self._providers)
        hint: str | None = getattr(profile, "provider_hint", None)
        if hint:
            hint_normalized = hint.lower()
            if hint_normalized in providers:
                return providers[hint_normalized]
            _log.warning("RuntimeProvider hint '%s' specified but no matching provider registered", hint)

        provider_type: str | None = getattr(profile, "provider_type", None)
        if provider_type:
            for provider in providers.values():
                if provider.provider_id == provider_type:
                    return provider

        for provider in providers.values():
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
                    await self.register(provider_name, provider)
                case "e2b":
                    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

                    api_key = provider_config.get("api_key")
                    if not api_key:
                        _log.warning("E2B provider '%s' has no api_key, skipping", provider_name)
                        continue
                    provider = E2BRuntimeProvider(api_key=api_key)
                    await self.register(provider_name, provider)
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
        Idempotent: returns the existing lease if one already exists for run_id.
        """
        from modulo.db.models.workspace_lease import WorkspaceLease

        existing = await session.execute(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id))
        if existing.scalar_one_or_none() is not None:
            return existing.scalar_one()

        provider = self.resolve(profile)
        if provider is None:
            raise ValueError(
                f"No RuntimeProvider registered for profile type '{getattr(profile, 'provider_type', None)}'"
            )

        workspace_ref = await provider.create_workspace(profile, session)

        lease = WorkspaceLease(
            organisation_id=profile.organisation_id,
            environment_profile_id=profile.id,
            run_id=run_id,
            provider_ref=workspace_ref.get("ref") or workspace_ref.get("container_id", ""),
            status="running",
            lease_started_at=datetime.now(UTC),
        )
        session.add(lease)
        return lease

    async def destroy_lease(self, lease: Any, session: Any | None = None) -> None:
        """Destroy a workspace lease and update its status."""
        profile = getattr(lease, "environment_profile", None)
        provider = self.resolve(profile) if profile is not None else None
        if provider is None:
            providers = dict(self._providers)
            for p in providers.values():
                provider = p
                break

        if provider is None:
            _log.warning("No RuntimeProvider registered, cannot destroy lease %s", lease)
            return

        try:
            await provider.destroy_workspace(lease.provider_ref if hasattr(lease, "provider_ref") else lease)
        except Exception:
            _log.exception("Failed to destroy workspace for lease %s", lease)

        if hasattr(lease, "status"):
            lease.status = "completed"
        if session is not None:
            session.add(lease)
