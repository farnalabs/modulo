"""Coverage for the runner-marker block inside ``_run_reconcile_sweeps``.

``modulo.core.cron_helpers._run_reconcile_sweeps`` runs seven best-effort
compensating sweeps. The runner-marker block (the D8 FAR-594 reconciliation)
has three arms — a partial-failure arm (``except RunnerMarkerSweepError``), a
success arm (``else``), and a generic fail-open arm — and none of the
``runner_markers_*`` / ``runner_capacity_violations`` counters was asserted
anywhere in the repo before this module.

Mock/fake based (no real Postgres/Redis), following the patch pattern in
``tests/unit/core/test_cron_helpers_residual.py``: the sweeps imported inside
the function are patched at their SOURCE module and the factory helpers are
stubbed so argument evaluation never reaches a real engine.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core.runner_capacity import RunnerMarkerSweepError

_PARTIAL_FAILURE_TOKEN = "runner_marker_sweep_partial_failure"
_RUNNER_MARKER_FAILED_TOKEN = "runner_marker_sweep_failed"
_DRIFT_FAILED_TOKEN = "drift_sweep_failed"
_ENFORCEMENT_FAILED_TOKEN = "enforcement_sweep_failed"


def _enter_benign_sweeps(stack: ExitStack) -> None:
    """Patch the six non-runner-marker sweeps benign so a test exercises only
    the runner-marker block. Sweeps imported inside the function are patched at
    their source module; the module-level ones on ``cron_helpers`` itself."""
    stack.enter_context(patch.object(ch, "_open_factory", lambda: MagicMock()))
    stack.enter_context(patch.object(ch, "_open_system_factory", lambda: MagicMock()))
    stack.enter_context(patch.object(ch, "run_classification_reconcile", new_callable=AsyncMock, return_value={}))
    stack.enter_context(patch.object(ch, "_sweep_workspace_input_drift_flags", new_callable=AsyncMock, return_value={}))
    stack.enter_context(patch.object(ch, "enforce_no_delivery_streaks", new_callable=AsyncMock, return_value={}))
    stack.enter_context(patch("modulo.auth.api_key.revoke_run_api_key_sweep", new_callable=AsyncMock, return_value={}))
    stack.enter_context(
        patch(
            "modulo.core.rollback_thresholds.evaluate_rollback_thresholds",
            new_callable=AsyncMock,
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "modulo.core.analytics.enforcement_sweep.sweep_schema_enforcement_facts",
            new_callable=AsyncMock,
            return_value={},
        )
    )


@pytest.mark.asyncio
async def test_runner_marker_partial_failure_lands_counts_and_zeroes_violations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The partial-failure arm (qa F5): ``RunnerMarkerSweepError`` carries the
    tick's partial counts into the summary and zeroes the capacity-violation
    signal, and the failure is logged loudly."""
    redis_client = AsyncMock()
    summary: dict[str, Any] = {}
    error = RunnerMarkerSweepError(scanned=5, cleared=2, transitioned=1, org_failures=1)

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                side_effect=error,
            )
        )
        stack.enter_context(caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"))
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert summary["runner_markers_scanned"] == 5
    assert summary["runner_markers_cleared"] == 2
    assert summary["runner_markers_transitioned"] == 1
    assert summary["runner_markers_orgs_failed"] == 1
    assert summary["runner_capacity_violations"] == 0
    assert any(_PARTIAL_FAILURE_TOKEN in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_runner_marker_success_records_counters(caplog: pytest.LogCaptureFixture) -> None:
    """The success arm: every key the sweep returns lands in the summary."""
    redis_client = AsyncMock()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                return_value={"scanned": 9, "cleared": 4, "transitioned": 2, "orgs_failed": 0, "violations": 1},
            )
        )
        stack.enter_context(caplog.at_level(logging.INFO, logger="modulo.core.cron_helpers"))
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert summary["runner_markers_scanned"] == 9
    assert summary["runner_markers_cleared"] == 4
    assert summary["runner_markers_transitioned"] == 2
    assert summary["runner_markers_orgs_failed"] == 0
    assert summary["runner_capacity_violations"] == 1


@pytest.mark.asyncio
async def test_best_effort_sweeps_swallow_drift_runner_enforcement_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The generic fail-open arms: a raising drift / runner-marker /
    enforcement sweep must never abort the caller — each is logged and the
    counter it owns is zeroed."""
    redis_client = AsyncMock()
    summary: dict[str, Any] = {}
    boom = RuntimeError("boom")

    with ExitStack() as stack:
        stack.enter_context(patch.object(ch, "_open_factory", lambda: MagicMock()))
        stack.enter_context(patch.object(ch, "_open_system_factory", lambda: MagicMock()))
        stack.enter_context(patch.object(ch, "run_classification_reconcile", new_callable=AsyncMock, return_value={}))
        stack.enter_context(
            patch.object(ch, "_sweep_workspace_input_drift_flags", new_callable=AsyncMock, side_effect=boom)
        )
        stack.enter_context(patch.object(ch, "enforce_no_delivery_streaks", new_callable=AsyncMock, return_value={}))
        stack.enter_context(
            patch("modulo.auth.api_key.revoke_run_api_key_sweep", new_callable=AsyncMock, return_value={})
        )
        stack.enter_context(
            patch(
                "modulo.core.rollback_thresholds.evaluate_rollback_thresholds",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                side_effect=boom,
            )
        )
        stack.enter_context(
            patch(
                "modulo.core.analytics.enforcement_sweep.sweep_schema_enforcement_facts",
                new_callable=AsyncMock,
                side_effect=boom,
            )
        )
        stack.enter_context(caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"))
        # Reaching the assertions at all proves the call returned normally.
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert summary["runner_markers_scanned"] == 0
    assert summary["enforcement_sweep_scanned"] == 0
    assert any(_DRIFT_FAILED_TOKEN in message for message in caplog.messages)
    assert any(_RUNNER_MARKER_FAILED_TOKEN in message for message in caplog.messages)
    assert any(_ENFORCEMENT_FAILED_TOKEN in message for message in caplog.messages)
