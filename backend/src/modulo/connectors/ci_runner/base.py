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

    The four abstract methods below (trigger_run, get_run_status, get_run_logs,
    list_runs) form the capability contract that CI-runners implement. They ARE
    reachable from the pipeline engine: a ``dispatch`` node routes
    ``connector_binding.operation="dispatch"`` to the method named by its
    ``dispatch_action``, and ``await_completion=True`` polls ``get_run_status``
    with the id ``trigger_run`` returned until the substrate reports a terminal
    status.

    **Run-id contract (FAR-1141).** The ``CIRun.id`` a connector PRODUCES in
    ``trigger_run`` / ``list_runs`` / ``get_run_status`` is the exact string
    its own ``get_run_status`` / ``get_run_logs`` CONSUME — for every provider:

    * GitHub Actions — ``owner/repo/run_id``
    * GitLab CI — ``project_id/pipeline_id``
    * CircleCI — the pipeline UUID
    * Buildkite — ``org/pipeline_slug/build_number``
    * Jenkins — ``job_name/build_number`` (or ``job_name/queue/queue_id`` while
      the triggered build is still resolving through the queue)
    * TeamCity — the build id
    * Azure Pipelines — ``pipeline_id/run_id``

    A connector must never hand back an id its own readers reject (a bare or
    empty id that fails the next call is a contract breach): when no usable id
    can be resolved, ``trigger_run`` raises instead of returning it.
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
        """Trigger a CI pipeline run and return the created run descriptor.

        The returned ``CIRun.id`` must satisfy the run-id contract above —
        it is the key ``await_completion`` polls ``get_run_status`` with.
        """

    @abstractmethod
    async def get_run_status(self, run_id: str) -> CIRun:
        """Fetch the current status of a CI run (``run_id`` per the contract)."""

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
