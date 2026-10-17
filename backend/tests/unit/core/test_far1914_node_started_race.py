"""FAR-1914: the nodeless repair must re-verify the zero-node premise on FRESH state.

Production evidence (run ``e6131612``, 2026-10-15): ``dispatcher_reconcile``
terminal-failed a Prompt-to-PR run through the nodeless chokepoint while its own
``error_detail`` admitted the row "should have been excluded by the node-started
selection legs" — the run's ``dispatch_phase`` was ``first_node_dispatched`` with
``dispatch_phase_elapsed=0.3s``, i.e. a node STARTED between the reconcile scan
(whose row is a snapshot) and the per-row action. The scan row looked nodeless;
the fresh row had started a node.

``_reconcile_nodeless_repair`` now re-reads the run through
``_fresh_row_is_node_started_or_gone`` and DEFERS when the fresh row has started
a node (or left ``running``) instead of terminalising / re-dispatching it. These
tests fail without that guard.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED


def _settings() -> MagicMock:
    """A settings double carrying only the knobs the repair reads as ints."""
    return MagicMock(saq_nodeless_redispatch_budget=4)


def _nodeless_snapshot(*, phase: Any, claim_count: int) -> SimpleNamespace:
    """The reconcile SELECT row: a running SAQ row that LOOKS claimed-but-nodeless."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        dispatcher="saq",
        node_token_usage=None,
        outputs_absent=True,
        checkpoints_absent=True,
        started_at=datetime.now(UTC) - timedelta(minutes=60),
        dispatched_at=datetime.now(UTC) - timedelta(minutes=60),
        sandbox_dispatch_state=None,
        dispatch_phase=phase,
        claim_count=claim_count,
        retry_policy=None,
    )


class _FreshSession:
    """Minimal async session: ``session.get(Run, pk)`` returns the FRESH row."""

    def __init__(self, fresh: Any) -> None:
        self._fresh = fresh

    async def get(self, model: Any, pk: Any) -> Any:
        return self._fresh


class _UnreadableSession:
    """A session whose fresh read fails — the guard must fail CLOSED."""

    async def get(self, model: Any, pk: Any) -> Any:
        raise RuntimeError("fresh read failed")


def _summary() -> dict[str, Any]:
    return {
        "nodeless_failed": 0,
        "nodeless_redispatched": 0,
        "nodeless_capped": 0,
        "skipped": 0,
        "node_started_deferred": 0,
    }


@pytest.mark.asyncio
async def test_fresh_node_started_row_is_not_terminalized() -> None:
    """The race: the snapshot is pre-node but the FRESH row started a node.

    The repair must NOT terminal-fail (nor re-dispatch) — the row is deferred to
    the node-started belt / age gate. This is the e6131612 defect."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        fresh = SimpleNamespace(status="running", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED, node_token_usage=None)
        summary = _summary()
        fail = AsyncMock()
        redispatch = AsyncMock()
        with patch.object(ch, "_fail_nodeless_run", fail), patch.object(ch, "_redispatch_nodeless", redispatch):
            handled = await ch._reconcile_nodeless_repair(
                _FreshSession(fresh), MagicMock(), uuid.uuid4(), snapshot, 35, 0, summary, []
            )
    finally:
        monkeypatch.undo()

    assert handled is None
    fail.assert_not_awaited()
    redispatch.assert_not_awaited()
    assert summary["node_started_deferred"] == 1
    assert summary["nodeless_failed"] == 0


@pytest.mark.asyncio
async def test_control_fresh_still_nodeless_row_is_terminalized() -> None:
    """Control: the fresh row is still pre-node, so the terminal-fail proceeds.

    Proves the guard discriminates on the FRESH phase, not on the call shape."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        fresh = SimpleNamespace(status="running", dispatch_phase="claimed", node_token_usage=None)
        summary = _summary()
        fail = AsyncMock()
        with patch.object(ch, "_fail_nodeless_run", fail):
            handled = await ch._reconcile_nodeless_repair(
                _FreshSession(fresh), MagicMock(), uuid.uuid4(), snapshot, 35, 0, summary, []
            )
    finally:
        monkeypatch.undo()

    assert handled is not None
    fail.assert_awaited_once()
    assert summary["node_started_deferred"] == 0


@pytest.mark.asyncio
async def test_control_fresh_left_running_row_is_deferred() -> None:
    """A fresh row that has LEFT ``running`` is not a nodeless zombie any more."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        fresh = SimpleNamespace(status="complete", dispatch_phase="claimed", node_token_usage=None)
        summary = _summary()
        fail = AsyncMock()
        with patch.object(ch, "_fail_nodeless_run", fail):
            handled = await ch._reconcile_nodeless_repair(
                _FreshSession(fresh), MagicMock(), uuid.uuid4(), snapshot, 35, 0, summary, []
            )
    finally:
        monkeypatch.undo()

    assert handled is None
    fail.assert_not_awaited()
    assert summary["node_started_deferred"] == 1


@pytest.mark.asyncio
async def test_unreadable_fresh_state_fails_closed() -> None:
    """An unreadable fresh state never acts: the guard fails CLOSED (defer)."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        summary = _summary()
        fail = AsyncMock()
        with patch.object(ch, "_fail_nodeless_run", fail):
            handled = await ch._reconcile_nodeless_repair(
                _UnreadableSession(), MagicMock(), uuid.uuid4(), snapshot, 35, 0, summary, []
            )
    finally:
        monkeypatch.undo()

    assert handled is None
    fail.assert_not_awaited()
    assert summary["node_started_deferred"] == 1


def test_zero_progress_shape_excludes_a_started_node() -> None:
    """FAR-1914 parity: the row-level mirror carries the phase leg.

    ``_is_zero_progress_shape_row``'s docstring claimed to mirror
    ``_ZERO_PROGRESS_SHAPE_SQL`` but omitted its ``first_node_dispatched`` leg, so
    a started node read as zero-progress to the FAR-1740 tail guard.
    """
    started = SimpleNamespace(
        node_token_usage=None,
        outputs_absent=True,
        checkpoints_absent=True,
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
    )
    pre_node = SimpleNamespace(
        node_token_usage=None,
        outputs_absent=True,
        checkpoints_absent=True,
        dispatch_phase="claimed",
    )
    no_phase_column = SimpleNamespace(node_token_usage=None, outputs_absent=True, checkpoints_absent=True)

    assert ch._is_zero_progress_shape_row(started) is False
    assert ch._is_zero_progress_shape_row(pre_node) is True
    # A row source without the column keeps the pre-FAR-1914 verdict (additive leg).
    assert ch._is_zero_progress_shape_row(no_phase_column) is True
