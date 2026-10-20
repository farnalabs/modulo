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
a node (or left ``running``) instead of terminalising / re-dispatching it. The
age-bound router ``_terminalize_aged_nodeless_zombies`` applies the same
recheck before its terminal write, so an aged row whose node started in the
SELECT→write window is deferred rather than failed. These tests fail without
those guards.
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


class _RouterSession:
    """Session double for the aged-nodeless router.

    ``execute`` is the router's SELECT snapshot (the aged zero-progress ids it
    collected); ``get`` is the per-row FRESH re-read the guard performs.
    """

    def __init__(self, selected: list[uuid.UUID], fresh: Any) -> None:
        self._selected = selected
        self._fresh = fresh
        self.flushed = False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        result = MagicMock()
        result.all.return_value = [(run_id,) for run_id in self._selected]
        return result

    async def get(self, model: Any, pk: Any) -> Any:
        return self._fresh

    async def flush(self) -> None:
        self.flushed = True


def _summary() -> dict[str, Any]:
    return {
        "nodeless_failed": 0,
        "nodeless_redispatched": 0,
        "nodeless_capped": 0,
        "skipped": 0,
        "nodeless_fresh_premise_deferred": 0,
        # The row-level belt's regression alarm — FAR-1914 deferrals must NOT
        # inflate it (it is documented as belt hits only).
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

    # The guard must state the row handled (matching the early-detect guard):
    # ``None`` told the caller to proceed with "normal repairs", and the stale
    # pre-node snapshot then fell through to an ``execute_run`` re-dispatch of
    # a run whose node had started (FAR-1649).
    assert handled == 0
    fail.assert_not_awaited()
    redispatch.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1
    # The belt's regression alarm must stay untouched: a routine fresh-premise
    # deferral is NOT a belt hit (the blocking review finding).
    assert summary["node_started_deferred"] == 0
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
    assert summary["nodeless_fresh_premise_deferred"] == 0


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

    assert handled == 0
    fail.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1


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

    assert handled == 0
    fail.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1


@pytest.mark.asyncio
async def test_fresh_row_vanished_is_deferred() -> None:
    """A fresh read that returns ``None`` is deferred (nothing safe to repair).

    The row can vanish between the scan and the per-row action (deleted, or not
    visible under the org's RLS). The guard fails CLOSED by returning True, so
    the repair neither re-dispatches nor terminal-fails it.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        summary = _summary()
        fail = AsyncMock()
        with patch.object(ch, "_fail_nodeless_run", fail):
            handled = await ch._reconcile_nodeless_repair(
                _FreshSession(None), MagicMock(), uuid.uuid4(), snapshot, 35, 0, summary, []
            )
    finally:
        monkeypatch.undo()

    assert handled == 0
    fail.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1


@pytest.mark.asyncio
async def test_early_detect_path_defers_a_started_node() -> None:
    """The FAR-873 early-detect branch re-verifies the FRESH row too.

    A row the early-detect window selects as a nodeless zombie must NOT be
    re-dispatched / terminal-failed when the fresh row has since started a
    node: the early path defers and returns the unchanged enqueue-failed
    counter.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        fresh = SimpleNamespace(status="running", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED, node_token_usage=None)
        summary = _summary()
        fail = AsyncMock()
        with patch.object(ch, "_fail_nodeless_run", fail):
            got = await ch._reconcile_one_row(
                _FreshSession(fresh),
                MagicMock(),
                MagicMock(),
                uuid.uuid4(),
                snapshot,
                35,
                0,
                summary,
                [],
                early_detect_minutes=20,
                stale_window=600,
            )
    finally:
        monkeypatch.undo()

    assert got == 0
    fail.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1
    assert summary["node_started_deferred"] == 0


@pytest.mark.asyncio
async def test_full_window_repair_returns_handled_with_no_early_detect() -> None:
    """Regression (the widened-window window, review 2026-10-18): with
    ``early_detect_minutes=None`` (early-detect DISABLED) the full-window
    repair is the only line of defence — its fresh-premise guard must return
    the row HANDLED so the caller can never fall through to the generic
    recovery path, which re-dispatches a job-gone RUNNING row as
    ``execute_run`` (FAR-1649: a node had started after the scan — the stale
    pre-node snapshot sails past the node-started belt because the belt keys
    on the snapshot's phase).

    Pinned: the deferred row carries its OWN counter, the widened-window tail
    guard (whose ``skipped`` count is not a fresh-premise signal) is never
    reached, and no re-dispatch / terminal-fail fires.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ch, "get_settings", _settings)
    try:
        snapshot = _nodeless_snapshot(phase="claimed", claim_count=5)
        fresh = SimpleNamespace(status="running", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED, node_token_usage=None)
        q = AsyncMock()
        q.job = AsyncMock(return_value=None)  # SAQ job gone (the zombie shape)
        summary = _summary()
        fail = AsyncMock()
        redispatch = AsyncMock()
        generic_redispatch = AsyncMock()
        ingest_error = AsyncMock()
        with (
            patch.object(ch, "_fail_nodeless_run", fail),
            patch.object(ch, "_redispatch_nodeless", redispatch),
            patch.object(ch, "_re_dispatch_reconciled_run", generic_redispatch),
            patch.object(ch, "_ingest_saq_error", ingest_error),
        ):
            got = await ch._reconcile_one_row(
                _FreshSession(fresh),
                q,
                MagicMock(),
                uuid.uuid4(),
                snapshot,
                40,
                0,
                summary,
                [],
                floor_nodeless_window=20,
                stale_window=600,
            )
    finally:
        monkeypatch.undo()

    assert got == 0
    # The row was deferred, NOT pursued by the generic recovery path.
    generic_redispatch.assert_not_awaited()
    redispatch.assert_not_awaited()
    fail.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1
    # The row-level belt's regression alarm must stay untouched (counter
    # docstrings document belt hits only).
    assert summary["node_started_deferred"] == 0
    # Not a tail-guard ``skipped`` either — the widened-window guard was never
    # reached (the fresh-premise guard returned first).
    assert summary["skipped"] == 0


@pytest.mark.asyncio
async def test_aged_nodeless_router_defers_a_freshly_started_node() -> None:
    """The age-bound router must re-verify the FRESH row before it writes.

    The router's SELECT is a snapshot: an aged zero-progress row it collected
    can have dispatched its first node by the time the per-row terminal write
    runs. The fresh read proves the premise false, so the row must be DEFERRED
    — never handed to the nodeless chokepoint (and therefore never
    terminal-failed on the stale "dispatched no node" premise). Without the
    guard the same setup calls ``_fail_nodeless_run`` and returns the id as
    routed.
    """
    run_id = uuid.uuid4()
    fresh = SimpleNamespace(status="running", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED, node_token_usage=None)
    summary = _summary()
    fail = AsyncMock()
    session = _RouterSession([run_id], fresh)
    with patch.object(ch, "_fail_nodeless_run", fail):
        routed = await ch._terminalize_aged_nodeless_zombies(
            session, uuid.uuid4(), max_age_minutes=135, summary=summary, max_rows=None
        )

    assert routed == []
    fail.assert_not_awaited()
    assert summary["nodeless_fresh_premise_deferred"] == 1
    # A routine fresh-premise race is NOT the row-level belt's alarm.
    assert summary["node_started_deferred"] == 0


@pytest.mark.asyncio
async def test_aged_nodeless_router_still_fails_a_fresh_pre_node_row() -> None:
    """Control: a fresh row that is STILL pre-node is routed as before.

    Proves the router discriminates on the FRESH phase (the stale snapshot is
    pre-node in both tests), not on the call shape — so the guard cannot be
    passing by simply deferring everything.
    """
    run_id = uuid.uuid4()
    fresh = SimpleNamespace(status="running", dispatch_phase="claimed", node_token_usage=None)
    summary = _summary()
    fail = AsyncMock()
    session = _RouterSession([run_id], fresh)
    with patch.object(ch, "_fail_nodeless_run", fail):
        routed = await ch._terminalize_aged_nodeless_zombies(
            session, uuid.uuid4(), max_age_minutes=135, summary=summary, max_rows=None
        )

    assert routed == [run_id]
    fail.assert_awaited_once()
    assert summary["nodeless_fresh_premise_deferred"] == 0


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
