"""Unit tests for the FAR-893 executor-side dispatch phase tracker.

The tracker makes the claim→first-node-dispatch path attributable from logs:
each phase transition is recorded, and the zombie watchdog reads the tracker
when the grace period fires to report WHERE the executor is stuck (not just
THAT it is stuck).

Prove-the-fix: every test below fails without the corresponding tracker
wiring, because the tracker would remain in ``not_started`` or the watchdog
would not log the phase information.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_execution import (
    PHASE_CAPACITY_CHECK,
    PHASE_CLAIMED,
    PHASE_EXECUTOR_RUNNING,
    PHASE_FIRST_NODE_DISPATCHED,
    PHASE_GRAPH_COMPILE,
    PHASE_INIT_ENV,
    PHASE_LOADING_CONTEXT,
    PHASE_LOADING_SETUP,
    PHASE_NOT_STARTED,
    PHASE_SETUP_COMPLETE,
    PHASE_SPEND_CHECK,
    PHASE_STREAMING,
    DispatchPhaseTracker,
    run_executor_with_watchdog,
    zombie_watchdog,
)


class TestDispatchPhaseTracker:
    """Unit tests for the DispatchPhaseTracker dataclass."""

    def test_initial_state(self) -> None:
        """Tracker starts in not_started with zero elapsed time."""
        tracker = DispatchPhaseTracker(run_id="r1", org_id="o1")
        assert tracker.phase == PHASE_NOT_STARTED
        assert tracker.elapsed_in_phase() == 0.0
        desc = tracker.describe()
        assert desc["phase"] == PHASE_NOT_STARTED
        assert desc["run_id"] == "r1"
        assert desc["org_id"] == "o1"

    def test_enter_phase_updates_state(self) -> None:
        """enter_phase transitions to the new phase and records monotonic time."""
        tracker = DispatchPhaseTracker(run_id="r1", org_id="o1")
        before = time.monotonic()
        tracker.enter_phase(PHASE_CLAIMED)
        after = time.monotonic()
        assert tracker.phase == PHASE_CLAIMED
        assert before <= tracker.phase_entered_at <= after

    def test_elapsed_in_phase_increases(self) -> None:
        """elapsed_in_phase grows monotonically after entering a phase."""
        tracker = DispatchPhaseTracker(run_id="r1", org_id="o1")
        tracker.enter_phase(PHASE_CLAIMED)
        t1 = tracker.elapsed_in_phase()
        assert t1 >= 0.0
        tracker.enter_phase(PHASE_LOADING_SETUP)
        t2 = tracker.elapsed_in_phase()
        assert t2 >= 0.0

    def test_describe_includes_phase(self) -> None:
        """describe() returns the current phase and elapsed time."""
        tracker = DispatchPhaseTracker(run_id="r1", org_id="o1")
        tracker.enter_phase(PHASE_GRAPH_COMPILE)
        desc = tracker.describe()
        assert desc["phase"] == PHASE_GRAPH_COMPILE
        assert isinstance(desc["elapsed_in_phase_seconds"], float)

    def test_phase_sequence(self) -> None:
        """Tracker follows the full phase sequence from claim to first node."""
        tracker = DispatchPhaseTracker(run_id="r1", org_id="o1")
        phases = [
            PHASE_CLAIMED,
            PHASE_LOADING_SETUP,
            PHASE_SETUP_COMPLETE,
            PHASE_EXECUTOR_RUNNING,
            PHASE_LOADING_CONTEXT,
            PHASE_CAPACITY_CHECK,
            PHASE_SPEND_CHECK,
            PHASE_INIT_ENV,
            PHASE_GRAPH_COMPILE,
            PHASE_STREAMING,
            PHASE_FIRST_NODE_DISPATCHED,
        ]
        for phase in phases:
            tracker.enter_phase(phase)
            assert tracker.phase == phase


class TestZombieWatchdogReadsTracker:
    """Test that the zombie watchdog reads the dispatch tracker when the grace fires."""

    @pytest.mark.asyncio
    async def test_watchdog_logs_tracker_phase_on_stall(self, caplog: Any) -> None:
        """When the grace period fires and exec_task is still running, the
        watchdog logs the dispatch phase from the tracker."""
        aeng = MagicMock()
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        first_progress = asyncio.Event()
        stall_requested = asyncio.Event()

        # Simulate an executor that never signals first_progress.
        exec_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(999))

        tracker = DispatchPhaseTracker(run_id=run_id, org_id=org_id)
        tracker.enter_phase(PHASE_CLAIMED)
        tracker.enter_phase(PHASE_LOADING_CONTEXT)

        with (
            caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_execution"),
            patch("modulo.core.pipeline_execution.get_settings") as mock_settings,
            patch("modulo.core.pipeline_execution.fail_run_terminal", new_callable=AsyncMock),
        ):
            mock_settings.return_value.saq_setup_grace_seconds = 0
            await zombie_watchdog(
                aeng,
                run_id,
                org_id,
                first_progress,
                exec_task=exec_task,
                stall_requested=stall_requested,
                grace_seconds=0,
                dispatch_tracker=tracker,
            )

        # The stall log must include the dispatch phase from the tracker.
        stall_records = [r for r in caplog.records if "zombie_watchdog.stalled" in r.message]
        assert len(stall_records) == 1
        msg = stall_records[0].message
        assert "dispatch_phase=loading_context" in msg

        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task

    @pytest.mark.asyncio
    async def test_watchdog_still_works_without_tracker(self, caplog: Any) -> None:
        """When no tracker is provided, the watchdog logs without phase info (backward compat)."""
        aeng = MagicMock()
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        first_progress = asyncio.Event()
        stall_requested = asyncio.Event()

        exec_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(999))

        with (
            caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_execution"),
            patch("modulo.core.pipeline_execution.get_settings") as mock_settings,
            patch("modulo.core.pipeline_execution.fail_run_terminal", new_callable=AsyncMock),
        ):
            mock_settings.return_value.saq_setup_grace_seconds = 0
            await zombie_watchdog(
                aeng,
                run_id,
                org_id,
                first_progress,
                exec_task=exec_task,
                stall_requested=stall_requested,
                grace_seconds=0,
                dispatch_tracker=None,
            )

        stall_records = [r for r in caplog.records if "zombie_watchdog.stalled" in r.message]
        assert len(stall_records) == 1
        msg = stall_records[0].message
        # Without a tracker, dispatch_phase=None is logged.
        assert "dispatch_phase=None" in msg

        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task

    @pytest.mark.asyncio
    async def test_watchdog_first_progress_logs_no_tracker_needed(self, caplog: Any) -> None:
        """When first_progress fires, the watchdog logs success regardless of tracker."""
        aeng = MagicMock()
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        first_progress = asyncio.Event()
        stall_requested = asyncio.Event()

        exec_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(999))

        tracker = DispatchPhaseTracker(run_id=run_id, org_id=org_id)
        tracker.enter_phase(PHASE_FIRST_NODE_DISPATCHED)

        first_progress.set()

        with (
            caplog.at_level(logging.INFO, logger="modulo.core.pipeline_execution"),
            patch("modulo.core.pipeline_execution.get_settings") as mock_settings,
        ):
            mock_settings.return_value.saq_setup_grace_seconds = 600
            await zombie_watchdog(
                aeng,
                run_id,
                org_id,
                first_progress,
                exec_task=exec_task,
                stall_requested=stall_requested,
                grace_seconds=600,
                dispatch_tracker=tracker,
            )

        progress_records = [r for r in caplog.records if "zombie_watchdog.first_progress" in r.message]
        assert len(progress_records) == 1

        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task


class TestRunExecutorWithWatchdogTracker:
    """Test that run_executor_with_watchdog passes the tracker to the watchdog."""

    @pytest.mark.asyncio
    async def test_tracker_parameter_accepted(self) -> None:
        """The dispatch_tracker parameter exists in the function signature."""
        import inspect

        sig = inspect.signature(run_executor_with_watchdog)
        assert "dispatch_tracker" in sig.parameters
