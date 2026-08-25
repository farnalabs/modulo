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
    persistence_policy: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


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

    def supports(self, _profile: Any) -> bool:
        """Return True if this provider can handle the given profile.

        Base implementation returns ``False`` so providers that don't
        implement this method are skipped during auto-resolution.
        """
        return False

    def matches_provider_type(self, provider_type: str) -> bool:
        """Return whether this provider implements an explicit profile type."""
        normalized = provider_type.strip().lower()
        return bool(normalized) and normalized in {self.provider_id, *self.provider_aliases}

    async def close(self) -> None:
        """Release provider-level resources (connections, clients, etc.)."""
        return


def create_default_hub(max_local_concurrency: int = 2) -> RuntimeProviderHub:
    """Build a RuntimeProviderHub with the local provider always registered.

    If ``MODULO_E2B_API_KEY`` is set, the E2B provider is also registered.
    The local provider is registered first, so it becomes the fallback when
    no profile hint or ``supports()`` match is found.
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

    if os.environ.get("MODULO_E2B_API_KEY"):
        try:
            from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

            e2b = E2BRuntimeProvider()
            hub.register("e2b", e2b)
        except ImportError:
            _log.warning("E2B dependency not installed; skipping E2B provider")

    if os.environ.get("MODULO_DOCKER_HOST") or os.environ.get("DOCKER_HOST"):
        try:
            from modulo.core.runtime_provider.docker import DockerRuntimeProvider

            docker = DockerRuntimeProvider()
            hub.register("docker", docker)
        except ImportError:
            _log.warning("Docker dependency not installed; skipping Docker provider")

    return hub
