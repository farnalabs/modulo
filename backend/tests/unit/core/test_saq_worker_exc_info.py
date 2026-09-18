"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.saq_worker`` (PR #97).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import pipeline_execution as pe
from modulo.core import saq_worker as sw


async def test_execute_run_job_kwargs_stamp_failure() -> None:
    run_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    job = MagicMock()
    job.kwargs = {}
    job.update = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(sw, "_get_async_engine", return_value=MagicMock()),
        patch.object(pe, "claim_run_async", new=AsyncMock(return_value="tok")),
        patch.object(pe, "load_and_setup", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(pe, "fail_run_terminal", new=AsyncMock()),
        patch.object(sw, "_log") as log,
    ):
        await sw.execute_run({"job": job}, run_id=run_id, org_id=org_id)
    log.warning.assert_called_once_with(
        "SAQ execute_run: job kwargs stamp failed for run %s", uuid.UUID(run_id), exc_info=True
    )


async def test_persist_sweep_stats_redis_failure() -> None:
    client = MagicMock()
    client.set = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("redis.asyncio.Redis.from_url", return_value=client),
        patch.object(sw, "get_settings", return_value=MagicMock(redis_url="redis://localhost")),
        patch.object(sw, "_log") as log,
    ):
        await sw._persist_sweep_stats("key", {"x": 1}, 30)
    log.warning.assert_called_once_with("saq_worker.sweep_stats_persist_failed key=%s", "key", exc_info=True)
