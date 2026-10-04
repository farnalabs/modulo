"""FAR-1417 — a real SAQ job must bind its organisation before the job runs.

Companion to ``tests/unit/api/test_org_context_wiring.py``: the worker path
had the same defect (``org_id_var`` never set), so a worker-side ERROR was
dropped from ``error_events`` too.

These tests build a REAL ``saq.worker.Worker`` from the REAL
``_base_worker_settings`` output and drive ``Worker.process()`` against a
fake queue — no Redis, and no hand-setting of the contextvar. The assertion
is made from inside the job function, which is where it matters.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from saq.job import Job, Status
from saq.worker import Worker

import modulo.core.saq_worker as sw
from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

_ORG_ID = "8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f71"
_JOB_LOGGER = "test.saq_org_context.job"
_FUNCTION_NAME = "modulo.core.saq_worker.org_context_probe"


class _FakeQueue:
    """Minimal queue stand-in: hands out the prepared jobs, records finishes."""

    name = "runs"

    def __init__(self) -> None:
        self._jobs: list[Job] = []
        self.finished: list[tuple[str, Status]] = []

    def add_job(self, function: str, kwargs: dict[str, Any] | None = None) -> Job:
        # The Job must carry its queue: saq refuses update()/finish() without one.
        job = Job(function=function, kwargs=kwargs, queue=self)
        self._jobs.append(job)
        return job

    async def dequeue(self, **_kwargs: Any) -> Job | None:
        # Worker.process() passes timeout=/poll_interval=; this fake never blocks.
        return self._jobs.pop(0) if self._jobs else None

    async def update(self, job: Job, **kwargs: Any) -> None:
        return None

    async def finish(self, job: Job, status: Status, *, result: Any = None, error: str | None = None) -> None:
        self.finished.append((job.key, status))

    async def retry(self, job: Job, error: str | None = None) -> None:
        return None

    def job_id(self, key: str) -> str:
        return f"{self.name}:{key}"


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit`` (see the API test)."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self.orgs: list[str | None] = []

    async def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.orgs.append(org_id_var.get())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


_seen: dict[str, Any] = {}


async def _org_context_probe(_ctx: dict[str, Any], **_job_kwargs: Any) -> str:
    """The job under test: record what the job's own context sees, and log an ERROR."""
    _seen["org"] = org_id_var.get()
    logging.getLogger(_JOB_LOGGER).error("saqorg.job_error")
    return "ok"


@pytest.fixture(autouse=True)
def _clean_rate_limit_state() -> Iterator[None]:
    ErrorTrackingLogHandler._last_write_time.clear()
    _seen.clear()
    yield
    ErrorTrackingLogHandler._last_write_time.clear()
    _seen.clear()


@pytest.fixture
def sink() -> _RecordingSink:
    return _RecordingSink()


@pytest.fixture
def capture(sink: _RecordingSink) -> Iterator[None]:
    """Attach a REAL ``ErrorTrackingLogHandler`` (with a recording sink) to the root logger."""
    handler = ErrorTrackingLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield
    finally:
        root.removeHandler(handler)


def _build_worker(queue: _FakeQueue) -> Worker:
    """Build a Worker from the production settings dict, queue swapped for the fake."""
    settings_mock = MagicMock(saq_worker_concurrency=1)
    with (
        patch.object(sw, "get_settings", return_value=settings_mock),
        patch.object(sw, "_build_queue", return_value=queue),
    ):
        settings = sw._base_worker_settings("runs", [(_FUNCTION_NAME, _org_context_probe)])
    assert settings["before_process"] is sw._before_process_hook
    return Worker(**settings)


async def _drain() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_job_error_is_forwarded_with_the_jobs_org(
    capture: None,
    sink: _RecordingSink,
) -> None:
    queue = _FakeQueue()
    queue.add_job(_FUNCTION_NAME, {"org_id": _ORG_ID})
    worker = _build_worker(queue)

    # process() runs in its own task, exactly as saq's ``_process`` schedules
    # it — so anything the hook binds stays inside the job's context.
    assert await asyncio.create_task(worker.process()) is True
    await _drain()

    assert _seen["org"] == _ORG_ID
    assert "saqorg.job_error" in sink.messages
    assert sink.orgs == [_ORG_ID]
    assert org_id_var.get() is None
    assert queue.finished and queue.finished[0][1].name == "COMPLETE"  # type: ignore[union-attr]


async def test_system_job_without_org_is_not_attributed_to_a_previous_org(
    capture: None,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A no-org job must bind None explicitly — never inherit the previous job's org."""
    queue = _FakeQueue()
    queue.add_job(_FUNCTION_NAME, {"org_id": _ORG_ID})
    queue.add_job(_FUNCTION_NAME)
    worker = _build_worker(queue)

    assert await asyncio.create_task(worker.process()) is True
    assert await asyncio.create_task(worker.process()) is True
    await _drain()

    assert _seen["org"] is None
    # Only the org-scoped job's ERROR was forwarded; the system job's was
    # dropped with an announcement (and never attributed to the previous org).
    assert sink.orgs == [_ORG_ID]
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert org_id_var.get() is None
