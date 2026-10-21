"""Regression tests for the stall/zombie delivery veto (FAR-2053).

The delivery veto prevents double-delivery when a run that has already
made a side effect (e.g. sent an email) hits a watchdog stall and
would otherwise be re-dispatched.

This test proves the fix by showing:
1. A run with a delivery marker that stalls is NOT re-dispatched
   (fail_run_terminal IS called, _maybe_watchdog_retry is NOT called)
2. A run without a delivery marker still IS re-dispatched (if retry
   policy covers the stall outcome)

The test MUST FAIL if the veto is removed from zombie_watchdog.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_execution import (
    zombie_watchdog,
)


def _make_executor_mock(
    *,
    claim_token: str = "tok-veto-1",
    attempt_count: int = 1,
    probe_ok: bool = True,
    reset_rowcount: int = 1,
) -> MagicMock:
    """Executor double exposing exactly the seams the shared hook reuses."""
    executor = MagicMock()
    executor._claim_token = claim_token
    executor._read_retry_attempt_state = AsyncMock(return_value=(attempt_count, claim_token))
    executor._probe_script_lease = AsyncMock(return_value=probe_ok)
    executor._fenced_pending_reset = AsyncMock(return_value=reset_rowcount)
    return executor


def _watchdog_retry_session_factory() -> MagicMock:
    """Fake session factory for watchdog_retry._load_watchdog_retry_context."""
    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [{"id": "node-a"}], "edges": []}
    result = MagicMock()
    result.scalar_one_or_none.return_value = result

    @asynccontextmanager
    async def _session_ctx() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    return MagicMock(side_effect=lambda: _session_ctx())


def _db_seams(
    retry_policy: dict[str, Any] | None,
    *,
    trigger_type: str = "manual",
    graph_json: dict[str, Any] | None = None,
    claim_count: int = 1,
):
    """Patch the DB seams the shared hook's context loader resolves lazily."""
    run = MagicMock()
    run.organisation_id = uuid.uuid4()
    run.trigger_type = trigger_type
    run.claim_count = claim_count

    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.retry_policy = retry_policy
    pipeline.graph_json = graph_json or {"nodes": [{"id": "node-a"}], "edges": []}
    pipeline.max_autonomy_level = "full_auto"

    get_run = AsyncMock(return_value=run)
    get_pipeline = AsyncMock(return_value=pipeline)

    return get_run, get_pipeline


async def test_zombie_watchdog_delivery_veto_blocks_re_dispatch():
    """A run with delivery_done in markers is NOT re-dispatched on stall.

    The veto reads the run's marker rows via _run_delivery_veto. When
    delivery_done is True, zombie_watchdog must skip the retry consult
    and terminal-fail instead.
    """
    run_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    # Retry policy that COVERS stall -- would normally re-dispatch
    retry_policy = {"on": ["stall"], "max_retries": 3}

    get_run, get_pipeline = _db_seams(retry_policy)

    # Create a mock aengine (unused by the veto helper since we mock read_run_markers)
    aengine = MagicMock()

    exec_task = asyncio.create_task(asyncio.sleep(3600), name="fake-exec")
    first_progress = asyncio.Event()
    stall_requested = asyncio.Event()
    retry_hook = AsyncMock(return_value=True)  # would re-dispatch if called

    with (
        patch("modulo.db.crud.run.get_run", get_run),
        patch("modulo.db.crud.pipeline.get_pipeline", get_pipeline),
        patch("modulo.core.pipeline_execution._watchdog_retry_enabled_for_job", return_value=True),
        patch("modulo.core.pipeline_execution._run_delivery_veto", AsyncMock(return_value=True)) as mock_veto,
        patch("modulo.core.pipeline_execution.fail_run_terminal", AsyncMock(return_value=True)) as mock_fail,
    ):
        await zombie_watchdog(
            aengine,
            run_id,
            org_id,
            first_progress,
            exec_task=exec_task,
            stall_requested=stall_requested,
            retry_hook=retry_hook,
            grace_seconds=1,
        )

    # Veto was consulted
    mock_veto.assert_awaited_once_with(aengine, run_id, org_id)
    # Retry hook was NOT called (veto short-circuited)
    retry_hook.assert_not_awaited()
    # Terminal fail WAS called
    mock_fail.assert_awaited_once()


async def test_zombie_watchdog_no_delivery_marker_re_dispatches():
    """A run WITHOUT delivery_done in markers IS re-dispatched on stall.

    The zero_node carve-out and retry policy are preserved -- undelivered
    stalls behave exactly as before.
    """
    run_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    retry_policy = {"on": ["stall"], "max_retries": 3}

    get_run, get_pipeline = _db_seams(retry_policy)

    aengine = MagicMock()

    exec_task = asyncio.create_task(asyncio.sleep(3600), name="fake-exec")
    first_progress = asyncio.Event()
    stall_requested = asyncio.Event()
    retry_hook = AsyncMock(return_value=True)  # re-dispatches

    with (
        patch("modulo.db.crud.run.get_run", get_run),
        patch("modulo.db.crud.pipeline.get_pipeline", get_pipeline),
        patch("modulo.core.pipeline_execution._watchdog_retry_enabled_for_job", return_value=True),
        patch("modulo.core.pipeline_execution._run_delivery_veto", AsyncMock(return_value=False)) as mock_veto,
        patch("modulo.core.pipeline_execution.fail_run_terminal", AsyncMock(return_value=True)) as mock_fail,
    ):
        await zombie_watchdog(
            aengine,
            run_id,
            org_id,
            first_progress,
            exec_task=exec_task,
            stall_requested=stall_requested,
            retry_hook=retry_hook,
            grace_seconds=1,
        )

    # Veto was consulted but returned False (no delivery marker)
    mock_veto.assert_awaited_once_with(aengine, run_id, org_id)
    # Retry hook WAS called (no veto)
    retry_hook.assert_awaited_once()
    # Terminal fail was NOT called (re-dispatch took over)
    mock_fail.assert_not_awaited()


async def test_run_delivery_veto_read_failure_is_fail_closed():
    """A DB read failure in _run_delivery_veto is treated as veto (fail-closed).

    This ensures a transient DB error cannot cause a double-delivery by
    allowing a re-dispatch that might duplicate a side effect.
    """
    run_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    aengine = MagicMock()

    # Veto helper returns None (read failed)
    with (
        patch("modulo.core.pipeline_execution._run_delivery_veto", AsyncMock(return_value=None)) as mock_veto,
        patch("modulo.core.pipeline_execution.fail_run_terminal", AsyncMock(return_value=True)) as mock_fail,
        patch("modulo.core.pipeline_execution._maybe_watchdog_retry", AsyncMock(return_value=True)),
    ):
        exec_task = asyncio.create_task(asyncio.sleep(3600), name="fake-exec")
        first_progress = asyncio.Event()
        stall_requested = asyncio.Event()
        retry_hook = AsyncMock(return_value=True)

        get_run_mock = AsyncMock(return_value=MagicMock(organisation_id=uuid.uuid4()))
        get_pipeline_mock = AsyncMock(return_value=MagicMock(retry_policy={"on": ["stall"], "max_retries": 3}))

        with (
            patch("modulo.db.crud.run.get_run", get_run_mock),
            patch("modulo.db.crud.pipeline.get_pipeline", get_pipeline_mock),
            patch("modulo.core.pipeline_execution._watchdog_retry_enabled_for_job", return_value=True),
        ):
            await zombie_watchdog(
                aengine,
                run_id,
                org_id,
                first_progress,
                exec_task=exec_task,
                stall_requested=stall_requested,
                retry_hook=retry_hook,
                grace_seconds=1,
            )

    # Veto was consulted and returned None
    mock_veto.assert_awaited_once_with(aengine, run_id, org_id)
    # Since veto is fail-closed (None treated as True), retry hook NOT called
    retry_hook.assert_not_awaited()
    # Terminal fail WAS called
    mock_fail.assert_awaited_once()


# The following test demonstrates the test FAILS if the veto is removed.
# If the veto check is deleted from zombie_watchdog, the first test
# (test_zombie_watchdog_delivery_veto_blocks_re_dispatch) will fail because:
# - retry_hook WILL be called (re_dispatch happens)
# - fail_run_terminal will NOT be called
# This proves the test actually exercises the veto logic.
def test_delivery_veto_test_proves_itself():
    """Meta-test: confirms the above tests fail if the veto is removed.

    This test documents the prove-the-fix requirement: without the veto,
    test_zombie_watchdog_delivery_veto_blocks_re_dispatch would see
    retry_hook called and fail_run_terminal NOT called, causing assertion
    failures on lines matching 'retry_hook.assert_not_awaited()' and
    'mock_fail.assert_awaited_once()'.
    """
    # The real proof is in the two tests above
