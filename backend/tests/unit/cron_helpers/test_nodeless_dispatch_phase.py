"""FAR-1088 (W3): the claimed-but-nodeless terminal record names the phase.

``_fail_nodeless_run`` reads ``runs.dispatch_phase`` and
``runs.dispatch_phase_entered_at`` off the already-loaded run row and renders
them into the terminal ``error_detail``, the alert-grade ERROR log, and the
ingested error context — so a reader can see WHERE the claim stalled without
forensics. A NULL phase must surface as ``dispatch_phase=unknown``, never be
silently omitted.

FAR-1649: the durable ``first_node_dispatched`` phase (FAR-1422) proves a
node STARTED even though no super-step ever completed, so such rows are NOT
zero-progress: the nodeless predicate / row recheck must never re-dispatch
them as "safe zero-node" zombies, and the chokepoint must never label them
"dispatched no node".
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core.pipeline_execution import PHASE_FIRST_NODE_DISPATCHED

_PHASE_ELAPSED_RE = re.compile(r"dispatch_phase_elapsed=([0-9]+\.[0-9])s")


def _make_run(**overrides: Any) -> SimpleNamespace:
    """A running, claimed-but-nodeless run row (the terminaliser's input)."""
    fields: dict[str, Any] = {
        "status": "running",
        "pipeline_id": uuid.uuid4(),
        "trigger_id": uuid.uuid4(),
        "claim_count": 3,
        "started_at": datetime.now(UTC) - timedelta(minutes=40),
        "dispatched_at": datetime.now(UTC) - timedelta(minutes=45),
        "dispatch_phase": None,
        "dispatch_phase_entered_at": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _SessionWithRun:
    """Minimal async session: ``session.get(Run, pk)`` returns the fixture row."""

    def __init__(self, run: SimpleNamespace) -> None:
        self._run = run

    async def get(self, model: Any, pk: Any) -> Any:
        return self._run


async def _terminal_fail(run: SimpleNamespace) -> tuple[dict[str, Any], AsyncMock]:
    """Run the chokepoint and return (tick summary, patched error ingest)."""
    summary = ch._dispatcher_summary()
    ingest = AsyncMock()
    with patch.object(ch, "_ingest_saq_error", ingest):
        await ch._fail_nodeless_run(_SessionWithRun(run), uuid.uuid4(), uuid.uuid4(), summary)
    return summary, ingest


def _elapsed_from(detail: str) -> float:
    match = _PHASE_ELAPSED_RE.search(detail)
    assert match is not None, f"no dispatch_phase_elapsed in detail: {detail!r}"
    return float(match.group(1))


async def test_phased_run_appends_phase_and_elapsed_to_terminal_detail() -> None:
    """A run that recorded a phase names it, with the seconds spent in it."""
    entered_at = datetime.now(UTC) - timedelta(seconds=30)
    run = _make_run(dispatch_phase="loading_setup", dispatch_phase_entered_at=entered_at)
    summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=loading_setup dispatch_phase_elapsed=" in detail
    assert summary["claimed_but_never_dispatched"] == 1
    assert run.status == "failed"


async def test_null_phase_renders_as_unknown_not_omitted() -> None:
    """No recorded phase is explicit: ``dispatch_phase=unknown``, and no
    elapsed token is invented for a NULL ``dispatch_phase_entered_at``."""
    run = _make_run()
    summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=unknown" in detail
    assert "dispatch_phase_elapsed" not in detail
    assert summary["claimed_but_never_dispatched"] == 1


async def test_elapsed_is_seconds_since_phase_entered() -> None:
    """The elapsed value is measured against the terminal ``completed_at``
    (here: 120s after phase entry), not a constant."""
    entered_at = datetime.now(UTC) - timedelta(seconds=120)
    run = _make_run(dispatch_phase="streaming", dispatch_phase_entered_at=entered_at)
    _summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=streaming dispatch_phase_elapsed=" in detail
    elapsed = _elapsed_from(detail)
    assert 120.0 <= elapsed <= 123.0
    # The rendered value is derived from the row's own completed_at.
    recomputed = (run.completed_at - entered_at).total_seconds()
    assert abs(recomputed - elapsed) < 0.2


async def test_naive_phase_timestamp_is_treated_as_utc() -> None:
    """A naive ``dispatch_phase_entered_at`` must not raise; it is assumed UTC."""
    naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    run = _make_run(dispatch_phase="loading_setup", dispatch_phase_entered_at=naive)
    summary, _ingest = await _terminal_fail(run)

    detail = run.error_detail
    assert isinstance(detail, str)
    assert "dispatch_phase=loading_setup dispatch_phase_elapsed=" in detail
    assert summary["claimed_but_never_dispatched"] == 1


async def test_phase_carried_in_error_log_and_ingested_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The phase rides alongside pipeline/trigger in the ERROR log line and
    the Error Dashboard context, not just the error_detail string."""
    entered_at = datetime.now(UTC) - timedelta(seconds=12)
    run = _make_run(dispatch_phase="streaming", dispatch_phase_entered_at=entered_at)
    with caplog.at_level(logging.ERROR):
        _summary, ingest = await _terminal_fail(run)

    context = ingest.await_args.kwargs["context"]
    assert context["dispatch_phase"] == "streaming"
    assert 12.0 <= context["dispatch_phase_elapsed"] <= 15.0

    messages = [record.getMessage() for record in caplog.records]
    assert any("dispatch_phase=streaming" in message for message in messages)


def _nodeless_row(*, dispatch_phase: Any) -> SimpleNamespace:
    """A zero-node-shaped reconcile row with the given durable phase.

    Mirrors the legs the reconcile SELECT / ``_is_nodeless_zombie_row`` read:
    every zero-node leg true (no token usage, ``outputs_absent``,
    ``checkpoints_absent``, old ``started_at``, no live dispatch marker) — the
    ONLY discriminator under test is ``dispatch_phase``.
    """
    return SimpleNamespace(
        status="running",
        node_token_usage=None,
        outputs_absent=True,
        checkpoints_absent=True,
        started_at=datetime.now(UTC) - timedelta(minutes=60),
        dispatched_at=datetime.now(UTC) - timedelta(minutes=60),
        sandbox_dispatch_state=None,
        dispatch_phase=dispatch_phase,
    )


class TestNodeStartedIsNotNodelessZombie:
    """FAR-1649: a durably-recorded first-node start defeats the zero-node
    premise everywhere the nodeless repair enforces it.

    Production shape (Backlog Groomer / Improve Tests / Improve Architecture,
    2026-10-10): every early-detect tick re-dispatched a node-STARTED run
    (claim_count 1->5, killing each in-flight attempt before its own
    provisioning / node-deadline watchdog could surface the real failure),
    then terminal-failed it with the FALSE "dispatched no node" label while
    ``dispatch_phase=first_node_dispatched`` sat on the same row. These tests
    fail without the FAR-1649 selection legs."""

    def test_row_recheck_rejects_first_node_dispatched(self) -> None:
        """The row-level recheck must refuse a node-started row even when
        every other zero-node leg matches (the re-dispatch safety premise
        "zero nodes executed" is false for it)."""
        row = _nodeless_row(dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
        assert ch._is_nodeless_zombie_row(row, 45) is False

    @pytest.mark.parametrize(
        "phase",
        [None, "claimed", "loading_setup", "setup_complete", "streaming"],
    )
    def test_row_recheck_still_accepts_pre_node_phases(self, phase: Any) -> None:
        """Genuine zero-node zombies (NULL phase or any pre-node phase) keep
        the existing nodeless repair — the fix never widens what counts as
        nodeless, only closes the node-started hole."""
        row = _nodeless_row(dispatch_phase=phase)
        assert ch._is_nodeless_zombie_row(row, 45) is True

    def test_row_recheck_treats_missing_phase_attribute_as_pre_node(self) -> None:
        """A legacy row source without the attribute counts as no durable
        node-start evidence (mirrors the SQL ``dispatch_phase IS NULL`` arm)."""
        row = _nodeless_row(dispatch_phase=None)
        delattr(row, "dispatch_phase")
        assert ch._is_nodeless_zombie_row(row, 45) is True

    def test_zero_progress_sql_shape_carries_the_phase_leg(self) -> None:
        """The shared zero-progress shape (router selection + age-gate
        NOT-arm) must exclude node-started rows, and the inlined SQL literal
        must stay pinned to the canonical phase constant (single source)."""
        assert "dispatch_phase" in ch._ZERO_PROGRESS_SHAPE_SQL
        assert PHASE_FIRST_NODE_DISPATCHED in ch._ZERO_PROGRESS_SHAPE_SQL
        assert f"dispatch_phase <> '{PHASE_FIRST_NODE_DISPATCHED}'" in ch._ZERO_PROGRESS_SHAPE_SQL

    def test_mid_graph_wedge_not_arm_now_covers_node_started_rows(self) -> None:
        """Routing consequence: because the age gate excludes exactly the
        zero-progress shape, closing the shape's node-started hole makes
        node-started rows collectable there with the truthful
        ``run.no_progress`` code instead of the nodeless "dispatched no
        node" label."""
        assembled = ch._MID_GRAPH_WEDGE_SQL.replace(ch._ZERO_PROGRESS_SHAPE_TOKEN, ch._ZERO_PROGRESS_SHAPE_SQL)
        assert "AND NOT (" in assembled
        assert PHASE_FIRST_NODE_DISPATCHED in assembled

    def test_nodeless_router_sql_excludes_node_started_rows(self) -> None:
        """The aged nodeless router composes the SAME shape — node-started
        rows are never routed to the ``agent.stall`` chokepoint."""
        assembled = ch._NODELESS_ROUTER_SQL.replace(ch._ZERO_PROGRESS_SHAPE_TOKEN, ch._ZERO_PROGRESS_SHAPE_SQL)
        assert f"dispatch_phase <> '{PHASE_FIRST_NODE_DISPATCHED}'" in assembled

    def test_orm_predicate_compiles_the_phase_leg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The reconciler's ORM nodeless predicate (also the FAR-873
        early-detect base) compiles the phase exclusion into its SQL."""
        from unittest.mock import MagicMock

        import sqlalchemy as sa

        from modulo.db.models.run import Run

        # A bare MagicMock settings double: the floor resolver falls back to
        # its default-config derivation for a missing field.
        monkeypatch.setattr(ch, "get_settings", lambda: MagicMock())
        predicate = ch._nodeless_zombie_predicate(35)
        compiled = sa.select(Run.id).where(predicate).compile()
        sql = str(compiled)
        assert "dispatch_phase" in sql
        assert PHASE_FIRST_NODE_DISPATCHED in dict(compiled.params).values()

    async def test_chokepoint_detail_is_truthful_for_node_started_row(self) -> None:
        """Defense in depth: if a node-started row ever reaches the
        chokepoint (phase transitioned between SELECT and re-read), the
        terminal detail must never claim "dispatched no node" — that
        self-contradicting label is what produced the FAR-1649
        misdiagnosis."""
        entered_at = datetime.now(UTC) - timedelta(seconds=180)
        run = _make_run(
            dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
            dispatch_phase_entered_at=entered_at,
        )
        summary, _ingest = await _terminal_fail(run)

        detail = run.error_detail
        assert isinstance(detail, str)
        assert "first node STARTED" in detail
        assert "dispatched no node" not in detail
        assert f"dispatch_phase={PHASE_FIRST_NODE_DISPATCHED}" in detail
        assert summary["claimed_but_never_dispatched"] == 1
        assert run.status == "failed"
