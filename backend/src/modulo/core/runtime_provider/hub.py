"""RuntimeProviderHub — registry and resolution of RuntimeProvider implementations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from modulo.core.runtime_provider import RuntimeProvider, WorkspaceSpec

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from modulo.db.models.environment_profile import EnvironmentProfile

_log = logging.getLogger(__name__)

_TYPE_UNSET = object()


class RuntimeProviderHub:
    """Central registry for RuntimeProvider implementations.

    All providers implement the canonical WorkspaceSpec-based interface.
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
    ) -> RuntimeProvider | None:
        """Resolve the most suitable RuntimeProvider for the given profile.

        Resolution strategy:
        1. If the profile declares a ``provider_hint``, look it up by name.
        2. Treat an explicit ``provider_type`` as authoritative and match it
           against registered names or provider identity metadata.
        3. Try each provider's ``supports()`` and return the first match.
        4. Fall back to the first registered provider.
        5. Return None if nothing is registered.
        """
        providers = dict(self._providers)
        provider = self._resolve_by_hint(providers, profile)
        if provider is not None:
            return provider

        type_result = self._resolve_by_type(providers, profile)
        if type_result is not _TYPE_UNSET:
            return type_result

        return self._resolve_by_supports(providers, profile)

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
    ) -> RuntimeProvider | None:
        raw_provider_type: Any = getattr(profile, "provider_type", None)
        if not isinstance(raw_provider_type, str) or not raw_provider_type.strip():
            return cast("RuntimeProvider | None", _TYPE_UNSET)
        provider_type = raw_provider_type.strip().lower()

        direct_match = providers.get(provider_type)
        if direct_match is not None:
            return direct_match

        matches = [
            provider for _, provider in sorted(providers.items()) if provider.matches_provider_type(provider_type)
        ]
        if matches:
            return matches[0]

        _log.warning(
            "RuntimeProvider type '%s' requested but no matching provider is available",
            raw_provider_type,
        )
        return None

    @staticmethod
    def _resolve_by_supports(
        providers: dict[str, RuntimeProvider],
        profile: Any,
    ) -> RuntimeProvider | None:
        for provider in providers.values():
            supports = getattr(provider, "supports", None)
            if supports is None:
                continue
            try:
                if provider.supports(profile):
                    return provider
            except Exception:
                _log.debug("supports() check failed for a provider", exc_info=True)

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

    async def create_lease(
        self,
        profile: EnvironmentProfile,
        run_id: uuid.UUID,
        session: AsyncSession,
    ) -> Any:
        """Create a workspace lease from an EnvironmentProfile.

        Returns a WorkspaceLease ORM instance (not yet committed).
        Idempotent: returns the existing lease if one already exists for run_id.
        Uses SELECT ... FOR UPDATE to prevent TOCTOU races.
        """
        from modulo.db.models.workspace_lease import WorkspaceLease

        existing = await session.execute(
            select(WorkspaceLease).where(WorkspaceLease.run_id == run_id).with_for_update()
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row

        provider = self.resolve(profile)
        if provider is None:
            raise ValueError(
                f"No RuntimeProvider registered for profile type '{getattr(profile, 'provider_type', None)}'"
            )

        profile_config = dict(getattr(profile, "config_json", {}) or {})
        repo_url = profile_config.pop("repo_url", "")
        repo_ref = profile_config.pop("repo_ref", "")
        resource_limits = profile_config
        labels: dict[str, str] = {}
        if repo_url:
            labels["repo_url"] = repo_url
        if repo_ref:
            labels["repo_ref"] = repo_ref

        spec = WorkspaceSpec(
            environment_profile_id=profile.id,
            organisation_id=profile.organisation_id,
            run_id=run_id,
            image_ref=profile.image_ref or "",
            capabilities=list(getattr(profile, "capabilities_json", [])),
            resource_limits=resource_limits,
            labels=labels,
        )
        workspace_ref = await provider.create_workspace(spec)

        if isinstance(workspace_ref, dict):
            provider_ref = workspace_ref.get("ref") or workspace_ref.get("container_id", "")
        else:
            provider_ref = str(workspace_ref)

        lease = WorkspaceLease(
            organisation_id=profile.organisation_id,
            environment_profile_id=profile.id,
            run_id=run_id,
            provider_ref=provider_ref,
            status="running",
            lease_started_at=datetime.now(UTC),
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        session.add(lease)
        return lease

    async def destroy_lease(self, lease: Any, session: Any | None = None) -> None:
        """Destroy a workspace lease and update its status."""
        profile = getattr(lease, "environment_profile", None)
        provider = self.resolve(profile) if profile is not None else None
        if provider is None:
            providers = self.list_providers()
            for p in providers.values():
                provider = p
                break

        if provider is None:
            _log.warning("No RuntimeProvider registered, cannot destroy lease %s", lease)
            return

        provider_ref = getattr(lease, "provider_ref", None)
        try:
            await provider.destroy_workspace(provider_ref or lease)
        except Exception:
            _log.exception("Failed to destroy workspace for lease %s", lease)

        if hasattr(lease, "status"):
            lease.status = "completed"
        if session is not None:
            session.add(lease)
