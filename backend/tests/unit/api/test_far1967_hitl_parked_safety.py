"""FAR-1967 — the parked-run safety property that justifies its exemption.

The architecture gate ``tests/architecture/test_run_terminalisation_coverage.py``
classifies ``hitl_parked`` as an ``exempt`` in-flight status
(``operator_decision_recovery``): the park sweep moves a gate-expired run out of
``awaiting_human`` so it stops occupying review state, and the run is recovered
only by a human decision (or the F6a gated recovery when a decision committed
during the park).

That exemption is honest only while the parked state cannot accumulate
unboundedly and cannot wedge without a recovery path. Two of the three
properties it rests on are pinned elsewhere:

* org-bounded — ``tests/unit/test_run_admission.py``
  ``TestPipelineCapacityExcludesAwaitingHuman`` asserts the org count statement
  includes ``hitl_parked`` (a parked run holds no PIPELINE slot but IS counted
  by the org-level ``run_concurrency_limit`` gate);
* recovered — ``tests/unit/cron_helpers/test_dispatcher_reconcile.py``
  ``TestHitlResumeOrSkipPredicateMatrix::test_parked_with_committed_decision_resumes``
  and ``TestReDispatchPredicateMatchesParked`` assert the F6a recovery selects a
  parked run whose gate decision committed.

This module pins the third: a parked run is CANCELLABLE through the normal
cancel path (so an operator can always release it). It is a sibling of
``test_far1610_cancel_run_lock_bound.py`` — the same direct ``_cancel_run``
seam, without re-creating the HTTP dependency-override fixture that
``test_runs_endpoint.py`` already owns.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.runs import _cancel_run


def _principal() -> MagicMock:
    principal = MagicMock()
    principal.organisation_id = uuid.uuid4()
    principal.account_id = uuid.uuid4()
    return principal


async def test_parked_run_cancels_through_the_normal_path() -> None:
    """A ``hitl_parked`` run reaches ``request_cancellation`` — it is never
    rejected as terminal and is persisted cancelled like any other in-flight
    status (the "normal path" ``POST /runs/{id}/cancel`` delegates to)."""
    run = MagicMock()
    run.status = "hitl_parked"
    session = MagicMock()
    principal = _principal()
    request = AsyncMock(return_value=run)
    finalize = AsyncMock()

    with (
        patch("modulo.api.routes.runs.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.api.routes.runs.request_cancellation", new=request),
        patch("modulo.core.cost_controller.finalize.finalize_cancelled_run", new=finalize),
    ):
        await _cancel_run(session, principal, uuid.uuid4())

    request.assert_awaited_once()
    kwargs: dict[str, Any] = request.await_args.kwargs
    assert kwargs["reason"] == "user_requested"
    assert kwargs["actor"] == str(principal.account_id)


async def test_parked_run_skips_cost_finalization() -> None:
    """``hitl_parked`` is in the paused class: cancelling a parked run must NOT
    run ``finalize_cancelled_run`` (the parked run holds no accrued-spend
    accounting the cancel path owns — it is the ``awaiting_human`` state after
    expiry). This also proves the parked branch is a real membership test, not
    a blanket skip: a ``running`` run on the same path DOES finalize."""
    parked = MagicMock()
    parked.status = "hitl_parked"
    parked_finalize = AsyncMock()

    with (
        patch("modulo.api.routes.runs.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=parked)),
        patch("modulo.api.routes.runs.request_cancellation", new=AsyncMock(return_value=parked)),
        patch("modulo.core.cost_controller.finalize.finalize_cancelled_run", new=parked_finalize),
    ):
        await _cancel_run(MagicMock(), _principal(), uuid.uuid4())

    parked_finalize.assert_not_awaited()

    running = MagicMock()
    running.status = "running"
    running_finalize = AsyncMock()

    with (
        patch("modulo.api.routes.runs.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=running)),
        patch("modulo.api.routes.runs.request_cancellation", new=AsyncMock(return_value=running)),
        patch("modulo.core.cost_controller.finalize.finalize_cancelled_run", new=running_finalize),
    ):
        await _cancel_run(MagicMock(), _principal(), uuid.uuid4())

    running_finalize.assert_awaited_once()


@pytest.mark.parametrize("status", ["hitl_parked", "awaiting_human", "claimed"])
async def test_every_paused_status_is_cancellable(status: str) -> None:
    """The paused class as a set: each member reaches ``request_cancellation``."""
    run = MagicMock()
    run.status = status
    request = AsyncMock(return_value=run)

    with (
        patch("modulo.api.routes.runs.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.api.routes.runs.request_cancellation", new=request),
        patch("modulo.core.cost_controller.finalize.finalize_cancelled_run", new=AsyncMock()),
    ):
        await _cancel_run(MagicMock(), _principal(), uuid.uuid4())

    request.assert_awaited_once()
