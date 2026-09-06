"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.pipeline_execution`` (PR #97).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import pipeline_execution as pe


async def test_maybe_alert_retry_storm_connect_failure() -> None:
    aengine = MagicMock()
    aengine.connect = MagicMock(side_effect=RuntimeError("boom"))
    run_id = "run-1"
    with patch.object(pe, "_log") as log:
        await pe._maybe_alert_retry_storm(aengine, run_id, "org-1")
    log.warning.assert_called_once_with("pipeline_execution.retry_storm_alert_failed run=%s", run_id, exc_info=True)


async def test_resolve_result_status_read_failure() -> None:
    exec_result = MagicMock(status=None)
    rid = "rid-1"
    with (
        patch.object(pe, "_read_run_status", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(pe, "_log") as log,
    ):
        status = await pe._resolve_result_status(exec_result, MagicMock(), "run", "org", rid)
    assert status is None
    log.warning.assert_called_once_with(
        "run_executor_with_watchdog: could not read run status for %s", rid, exc_info=True
    )


async def test_resume_run_job_kwargs_stamp_failure() -> None:
    rid = uuid.uuid4()
    oid = uuid.uuid4()
    job = MagicMock()
    job.kwargs = {}
    job.update = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(pe, "claim_resume_run_async", new=AsyncMock(return_value="tok")),
        patch.object(pe, "load_and_setup", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(pe, "fail_run_terminal", new=AsyncMock()),
        patch.object(pe, "_log") as log,
    ):
        await pe.resume_run(async_engine=MagicMock(), run_id=str(rid), org_id=str(oid), job=job)
    log.warning.assert_called_once_with("resume_run: job kwargs stamp failed for run %s", rid, exc_info=True)
