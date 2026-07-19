"""Abstract base for CI-runner connectors."""

from abc import abstractmethod
from typing import Any

from modulo.connectors.base import (
    CIRun,
    CIRunLog,
    CIRunStatus,
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
)


class ConnectorTypeError(TypeError):
    """Raised when an operation is not supported by the connector type."""


class CIRunnerBase(ConnectorBase):
    """Abstract base for CI system connectors (GitHub Actions, GitLab CI, etc.).

    All CI runners expose the same capability contract (trigger_run, get_run_status,
    get_run_logs, list_runs) so pipeline nodes bind to the ``ci-runner`` connector
    type rather than a specific provider.
    """

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CI_RUNNER

    @abstractmethod
    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        """Trigger a CI pipeline run and return the created run descriptor."""

    @abstractmethod
    async def get_run_status(self, run_id: str) -> CIRun:
        """Fetch the current status of a CI run."""

    @abstractmethod
    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        """Fetch logs for a CI run, with optional cursor-based pagination."""

    @abstractmethod
    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        """List recent CI runs, optionally filtered by pipeline or status."""

    async def query(self, _q: ConnectorQuery) -> ConnectorResult:
        raise ConnectorTypeError(
            "CI runners do not support query(). Use CI-specific methods "
            "(trigger_run, get_run_status, get_run_logs, list_runs) instead.",
        )

    async def write(self, _payload: ConnectorPayload) -> dict[str, Any]:
        raise ConnectorTypeError(
            "CI runners do not support write(). Use CI-specific methods "
            "(trigger_run, get_run_status, get_run_logs, list_runs) instead.",
        )
