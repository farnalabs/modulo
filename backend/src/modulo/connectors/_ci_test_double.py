"""Shared scaffolding for the in-module CI-runner test doubles.

The ``_*TestDouble`` classes inside the CI-runner connector modules each
re-implemented the same ``trigger_run`` / ``list_runs`` scaffolding verbatim.
That copy-paste is test scaffolding, not product logic, but because it lives in
production modules it tripped the new-code copy-paste (duplication) gate with no
signal of genuine logic duplication (observed on FAR-1568).

This mixin owns the shared behaviour exactly once. A double supplies only its
connector-specific run-id shape (the FAR-1141 run-id contract), the name of the
list that records triggered runs, and the default pipeline id for ``list_runs``;
everything else is inherited. Keeping one implementation also keeps the doubles'
emitted ids in lock-step with the shapes their own readers parse.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from modulo.connectors.base import CIRun, CIRunStatus


class CITestDoubleMixin(ABC):
    """Behaviour shared by every in-module CI-runner test double."""

    #: Pipeline id used by ``list_runs`` when the caller passes ``None``.
    _double_default_pipeline_id: str = ""
    #: Fixed run-id suffix appended to the resolved pipeline id by the default
    #: listed-run id shape (``<resolved>/<suffix>``).
    _double_listed_suffix: str = ""

    #: Injected ``uuid`` module handle and last observed status; both are set by
    #: the concrete double's ``__init__`` (or ``trigger_run``).
    _uuid: Any
    _status: CIRunStatus

    @abstractmethod
    def _record_triggered_run(self, run: CIRun, variables: dict[str, str] | None) -> None:
        """Append *run* to the double's own triggered-run record list."""

    def _double_trigger_id(self, pipeline_id: str) -> str:
        """Return the contract-shaped id for a freshly triggered run.

        The default shape is ``pipeline_id/run_id`` (Azure Pipelines, GitLab
        CI). Connectors with a different run-id contract override this.
        """
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        return f"{pipeline_id}/{self._uuid.uuid4()}"

    def _double_listed_id(self, resolved: str) -> str:
        """Return the contract-shaped id for a listed run.

        The default shape is ``<resolved>/<suffix>`` — the fixed run id a
        connector's ``list_runs`` reports (Azure Pipelines ``101``, GitLab CI
        ``pipeline-1``). Connectors with a different shape override this.
        """
        return f"{resolved}/{self._double_listed_suffix}"

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        run = CIRun(
            id=self._double_trigger_id(pipeline_id),
            pipeline_id=pipeline_id,
            status=CIRunStatus.QUEUED,
            branch=branch,
        )
        self._record_triggered_run(run, variables)
        self._status = CIRunStatus.QUEUED
        return run

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        _limit: int = 20,
    ) -> list[CIRun]:
        resolved = pipeline_id or self._double_default_pipeline_id
        return [
            CIRun(
                id=self._double_listed_id(resolved),
                pipeline_id=resolved,
                status=status or CIRunStatus.SUCCESS,
            ),
        ]
