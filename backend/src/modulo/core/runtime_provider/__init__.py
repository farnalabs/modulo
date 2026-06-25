"""Runtime provider abstraction for agent execution environments.

Supports creating ephemeral or persistent workspaces (containers, VMs,
sandboxed processes) and executing commands within them.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol


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
        timeout: int | None = None,  # noqa: ASYNC109
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


class RuntimeProviderFactory(Protocol):
    """Callable that produces a RuntimeProvider given a profile."""

    def __call__(self, profile: Any) -> RuntimeProvider: ...
