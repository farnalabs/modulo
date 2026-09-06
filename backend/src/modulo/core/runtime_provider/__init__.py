"""Runtime provider abstraction for agent execution environments.

Supports creating ephemeral or persistent workspaces (containers, VMs,
sandboxed processes) and executing commands within them.
"""

from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub


class ProviderNotConfiguredError(RuntimeError):
    """A profile's ``provider_type`` has no registered runtime provider.

    Raised by :meth:`RuntimeProviderHub.resolve` when the profile's explicit
    provider type cannot be satisfied — either the type is unknown or the
    matching provider is not registered because its enabling environment
    variable is unset. Carries the provider type and (when known) the env
    var that would register the provider, so callers can surface remediation
    copy instead of a silent fallback.
    """

    def __init__(self, provider_type: str, env_var: str | None = None) -> None:
        self.provider_type = provider_type
        self.env_var = env_var
        if env_var and env_var in _DOCKER_ENV_VARS:
            message = (
                f"No runtime provider registered for provider_type '{provider_type}'. "
                "Set MODULO_DOCKER_HOST (or DOCKER_HOST, or any MODULO_RUNNER_* variable) "
                "and restart to enable it, or choose a different provider type for the profile."
            )
        elif env_var:
            message = (
                f"No runtime provider registered for provider_type '{provider_type}'. "
                f"Set the {env_var} environment variable (and restart) to enable it, "
                f"or choose a different provider type for the profile."
            )
        else:
            message = f"No runtime provider registered for provider_type '{provider_type}'."
        super().__init__(message)


# ---------------------------------------------------------------------------
# Provider-registration environment signals (single source of truth, FAR-587)
# ---------------------------------------------------------------------------
#
# Every documented signal lives here once; ``build_hub``'s registration gates
# and :func:`env_var_for_provider_type` (the remediation-copy mapping) both
# derive from these constants so the documented behaviour and the implemented
# behaviour can never drift apart.

_RUNNER_ENV_PREFIX = "MODULO_RUNNER_"
_DOCKER_ENV_VARS: tuple[str, ...] = ("MODULO_DOCKER_HOST", "DOCKER_HOST")
_E2B_ENV_VAR = "MODULO_E2B_API_KEY"

# Documented unconfigured behaviour (ADR 029 / FAR-587): every
# ``ck_env_profiles_provider_type`` CHECK value maps either to a provider that
# is always registered ("local") or to the env var whose presence registers
# the provider (docker-family types: _DOCKER_ENV_VARS; e2b: _E2B_ENV_VAR).
# Assertion tests pin this mapping against the model's CHECK.
_PROVIDER_ENV_VARS: dict[str, str] = {
    "e2b": _E2B_ENV_VAR,
    "runner_docker": _DOCKER_ENV_VARS[0],
    "docker": _DOCKER_ENV_VARS[0],
    "local_docker": _DOCKER_ENV_VARS[0],
}


def env_var_for_provider_type(provider_type: str) -> str | None:
    """Return the env var that registers ``provider_type``, if one is documented."""
    return _PROVIDER_ENV_VARS.get(provider_type.strip().lower())


@dataclass
class WorkspaceSpec:
    """Parameters for creating a new workspace from an EnvironmentProfile."""

    environment_profile_id: uuid.UUID
    organisation_id: uuid.UUID
    run_id: uuid.UUID | None = None
    image_ref: str = ""
    capabilities: list[str] = field(default_factory=list)
    timeout_seconds: int = 3600
    resource_limits: dict[str, Any] = field(default_factory=dict)
    egress_policy: str | None = None
    persistence_policy: str = "ephemeral"
    labels: dict[str, str] = field(default_factory=dict)
    # Provider-neutral metadata attached to the workspace itself (Docker maps
    # it to container Labels, E2B to sandbox metadata, Local ignores it).
    # Deliberately separate from ``labels``, which stays Env-var injection.
    workspace_metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecResult:
    """Result of executing a command in a workspace."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int | None = None


class RuntimeProvider(ABC):
    """Abstract base for a runtime backend (Docker, K8s, sandbox, etc.)."""

    provider_id = ""
    provider_aliases: frozenset[str] = frozenset()

    @abstractmethod
    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Provision a new workspace and return its provider-specific reference."""
        ...

    @abstractmethod
    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Run a command inside an existing workspace."""
        ...

    @abstractmethod
    async def destroy_workspace(self, provider_ref: str) -> None:
        """Tear down a workspace and release all associated resources."""
        ...

    @abstractmethod
    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the current status string for the workspace."""
        ...

    def matches_provider_type(self, provider_type: str) -> bool:
        """Return whether this provider implements an explicit profile type."""
        normalized = provider_type.strip().lower()
        return bool(normalized) and normalized in {self.provider_id, *self.provider_aliases}

    async def close(self) -> None:
        """Destroy provider-tracked live workspaces best-effort, then release owned clients.

        Invoked by :meth:`RuntimeProviderHub.aclose` (hub-aclose disposal,
        ADR 029). Implementations must not raise for individual workspace
        failures; the hub is itself best-effort per provider and owns no
        live state after this returns.
        """
        return


def build_hub(max_local_concurrency: int = 2) -> RuntimeProviderHub:
    """Build a fresh RuntimeProviderHub from the process environment.

    A new factory instance is returned on every call — the hub holds no
    process-global state and provider-owned clients are released explicitly
    via :meth:`RuntimeProviderHub.aclose` (per-provision disposal, ADR 029).

    Registration matrix (env-gated, operator opt-in = consent):

    - ``local`` — always registered (host-process fallback tier).
    - ``e2b`` — registered when ``MODULO_E2B_API_KEY`` is set.
    - ``runner_docker`` (aliases ``docker`` / ``local_docker``) — registered
      when any ``MODULO_RUNNER_*`` variable is set **or** a Docker endpoint
      (``MODULO_DOCKER_HOST`` / ``DOCKER_HOST``) is configured. Legacy
      ``local_docker`` profiles keep resolving identically to the pre-rename
      behaviour.
    """
    if max_local_concurrency < 1:
        _log.warning(
            "max_local_concurrency=%d is invalid, falling back to 2",
            max_local_concurrency,
        )
        max_local_concurrency = 2

    from modulo.core.runtime_provider.hub import RuntimeProviderHub
    from modulo.core.runtime_provider.local import LocalRuntimeProvider

    hub = RuntimeProviderHub()

    local = LocalRuntimeProvider(max_concurrency=max_local_concurrency)
    hub.register("local", local)

    if os.environ.get(_E2B_ENV_VAR):
        try:
            from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

            e2b = E2BRuntimeProvider()
            hub.register("e2b", e2b)
        except ImportError:
            _log.warning("E2B dependency not installed; skipping E2B provider")

    runner_signal = any(key.startswith(_RUNNER_ENV_PREFIX) for key in os.environ)
    if runner_signal or any(os.environ.get(var) for var in _DOCKER_ENV_VARS):
        try:
            from modulo.core.runtime_provider.docker import DockerRuntimeProvider

            docker = DockerRuntimeProvider()
            hub.register("runner_docker", docker)
        except ImportError:
            _log.warning("Docker dependency not installed; skipping Docker provider")

    return hub


# Backwards-compatible alias — the factory concept is unchanged; new callers
# should prefer :func:`build_hub`.
create_default_hub = build_hub
