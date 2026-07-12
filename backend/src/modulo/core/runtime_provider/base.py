"""RuntimeProvider ABC — abstract interface for agent execution environments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RuntimeProvider(ABC):
    """Abstract base for a runtime backend (Docker, E2B, local, etc.)."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Provider type identifier, e.g. 'local_docker' | 'e2b'."""
        ...

    @abstractmethod
    async def create_workspace(self, profile: Any, session: Any = None) -> Any:
        """Provision a sandbox/container from the profile. Returns a WorkspaceLease."""
        ...

    @abstractmethod
    async def destroy_workspace(self, workspace: Any) -> None:
        """Tear down the sandbox. Idempotent."""
        ...

    @abstractmethod
    async def workspace_health(self, workspace: Any) -> bool:
        """Check if the workspace is still alive."""
        ...

    @abstractmethod
    async def execute_command(
        self,
        workspace: Any,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Run a command inside the workspace. Returns {stdout, stderr, exit_code}."""
        ...

    @abstractmethod
    async def write_file(self, workspace: Any, path: str, content: str) -> None:
        """Write a file inside the workspace."""
        ...

    @abstractmethod
    async def read_file(self, workspace: Any, path: str) -> str:
        """Read a file from inside the workspace."""
        ...

    @abstractmethod
    async def list_files(self, workspace: Any, path: str) -> list[str]:
        """List files in a directory inside the workspace."""
        ...

    async def close(self) -> None:
        """Release provider-level resources. Override in subclasses that hold connections."""
        return
