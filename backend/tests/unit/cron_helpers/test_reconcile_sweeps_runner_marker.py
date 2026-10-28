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

import asyncio
import logging
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core.runner_capacity import RunnerMarkerSweepError
from tests.unit.api.mock_session import make_redis_client, make_session_factory

_PARTIAL_FAILURE_TOKEN = "runner_marker_sweep_partial_failure"
_RUNNER_MARKER_FAILED_TOKEN = "runner_marker_sweep_failed"
_DRIFT_FAILED_TOKEN = "drift_sweep_failed"
_ENFORCEMENT_FAILED_TOKEN = "enforcement_sweep_failed"


def _enter_benign_sweeps(stack: ExitStack) -> None:
    """Patch the six non-runner-marker sweeps benign so a test exercises only
    the runner-marker block. Sweeps imported inside the function are patched at
    their source module; the module-level ones on ``cron_helpers`` itself."""
    stack.enter_context(patch.object(ch, "_open_factory", lambda: make_session_factory()))
    stack.enter_context(patch.object(ch, "_open_system_factory", lambda: make_session_factory()))
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
    # FAR-2116 — the pipeline-decay sweep reads the runs table; a test that is
    # not about it must not reach the (fake) database with a real aggregate.
    stack.enter_context(
        patch(
            "modulo.core.pipeline_decay_alerts.evaluate_pipeline_decay_alerts",
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
    redis_client = make_redis_client()
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
    redis_client = make_redis_client()
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
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}
    boom = RuntimeError("boom")

    with ExitStack() as stack:
        stack.enter_context(patch.object(ch, "_open_factory", lambda: make_session_factory()))
        stack.enter_context(patch.object(ch, "_open_system_factory", lambda: make_session_factory()))
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
    assert summary["runner_markers_cleared"] == 0
    assert summary["runner_markers_transitioned"] == 0
    assert summary["enforcement_sweep_scanned"] == 0
    assert any(_DRIFT_FAILED_TOKEN in message for message in caplog.messages)
    assert any(_RUNNER_MARKER_FAILED_TOKEN in message for message in caplog.messages)
    assert any(_ENFORCEMENT_FAILED_TOKEN in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_capacity_alerts_sweep_lands_counts_and_logs_when_it_dispatches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1344: a dispatch (or an org error) is logged and counted."""
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.capacity_alerts.evaluate_capacity_alerts",
                new_callable=AsyncMock,
                return_value={"orgs_evaluated": 2, "dispatched": 1, "notify_failed": 0, "org_errors": 0},
            )
        )
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(caplog.at_level(logging.INFO, logger="modulo.core.cron_helpers"))
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert summary["capacity_alerts_orgs"] == 2
    assert summary["capacity_alerts_dispatched"] == 1
    assert summary["capacity_alerts_failed"] == 0
    assert any("dispatcher_reconcile.capacity_alerts" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_capacity_alerts_sweep_reraises_cancelled() -> None:
    """Cancellation must abort the reconcile tick, not be swallowed as a warning."""
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.capacity_alerts.evaluate_capacity_alerts",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await ch._run_reconcile_sweeps(redis_client, summary)


@pytest.mark.asyncio
async def test_capacity_alerts_sweep_swallows_a_generic_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure in the capacity sweep is a warning, never an aborted tick."""
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.capacity_alerts.evaluate_capacity_alerts",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            )
        )
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"))
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert "capacity_alerts_dispatched" not in summary
    assert any("dispatcher_reconcile.capacity_alerts_failed" in message for message in caplog.messages)


# --- FAR-2116 — pipeline-decay sweep wiring -------------------------------


@pytest.mark.asyncio
async def test_pipeline_decay_sweep_lands_counts_and_logs_when_it_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raised/recovered alert is counted in the reconcile summary and logged."""
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.pipeline_decay_alerts.evaluate_pipeline_decay_alerts",
                new_callable=AsyncMock,
                return_value={
                    "pipelines_measured": 3,
                    "alerts": 1,
                    "recoveries": 1,
                    "notify_failed": 0,
                },
            )
        )
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(caplog.at_level(logging.INFO, logger="modulo.core.cron_helpers"))
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert summary["pipeline_decay_alerts_measured"] == 3
    assert summary["pipeline_decay_alerts_raised"] == 1
    assert summary["pipeline_decay_alerts_recovered"] == 1
    assert summary["pipeline_decay_alerts_failed"] == 0
    assert any("dispatcher_reconcile.pipeline_decay_alerts" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_pipeline_decay_sweep_reraises_cancelled() -> None:
    """Cancellation aborts the reconcile tick, never swallowed as a warning."""
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.pipeline_decay_alerts.evaluate_pipeline_decay_alerts",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await ch._run_reconcile_sweeps(redis_client, summary)


@pytest.mark.asyncio
async def test_pipeline_decay_sweep_swallows_a_generic_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure in the decay sweep is a warning, never an aborted tick."""
    redis_client = make_redis_client()
    summary: dict[str, Any] = {}

    with ExitStack() as stack:
        _enter_benign_sweeps(stack)
        stack.enter_context(
            patch(
                "modulo.core.pipeline_decay_alerts.evaluate_pipeline_decay_alerts",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            )
        )
        stack.enter_context(
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"))
        await ch._run_reconcile_sweeps(redis_client, summary)

    assert "pipeline_decay_alerts_raised" not in summary
    assert any("dispatcher_reconcile.pipeline_decay_alerts_failed" in message for message in caplog.messages)
