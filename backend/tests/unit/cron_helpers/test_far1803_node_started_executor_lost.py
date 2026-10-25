"""FAR-1803 — a node-started run whose executor heartbeat died is invisible.

The observed class (prod, 2026-10-14/15 — runs 35905, e0d9d90a, 7494c4ef and
the 2026-10-15 population 36255/36269/36274/36278/36283/36325-36327):

* ``status='running'``, ``dispatcher='saq'``
* ``dispatch_phase='first_node_dispatched'`` (a node WAS dispatched into an
  E2B sandbox — marker ``{"state": "dispatching", "provider": "e2b", ...}``)
* heartbeat STALE by hours (the executor process is gone)
* ZERO node outputs / checkpoints / token usage — ``nodes: []`` in the API
* still ``running`` 2h+ later while ``dispatcher_reconcile`` reported
  ``scanned=..., nodeless_failed=0, claimed_but_never_dispatched=0,
  node_started_terminalized=0`` — every counter 0 while the runs existed.

Why the reconciler could not see the class (code-traced at HEAD):

1. the generic stale-heartbeat leg, the nodeless predicate and the early-detect
   predicate all EXCLUDE ``first_node_dispatched`` (FAR-1649), so the row is
   never selected — it is not ``scanned`` at all;
2. the nodeless router selects only the ZERO-progress shape (phase leg), so
   ``nodeless_failed`` stays 0;
3. the mid-graph age gate (B4) — the terminaliser the code says owns
   node-started rows once their watchdogs are dead — carries the FAR-1740 (3)
   live-marker arm, which shields a ``first_node_dispatched`` row carrying a
   LIVE ``sandbox_dispatch_state`` marker **regardless of whether the executor
   that wrote the marker is still alive**; so ``age_terminalized`` stays 0;
4. the claim-cap terminalizer needs ``claim_count >= 20`` (observed: 1-3);
   the only collector is the 25h runner-marker sweep.

This module pins (a) the REPRO — the current, wrong behaviour, asserted
against the real predicates — and (b) the REGRESSION tests for the fix:

* the scan admits the class under its own leg (and counts it
  ``node_started_executor_lost``, never re-dispatching it), and
* the age gate's live-marker shield lifts once the heartbeat is stale, so the
  row is terminalised ``no_progress`` at the age bound instead of living
  forever.
"""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.db.models.run import Run
from tests.unit.cron_helpers.test_node_started_redispatch_guard import _run_reconcile, _run_row

STALE_WINDOW_SECONDS = 600  # the harness's 2 * saq_job_heartbeat (300s)


def _compile_scan_predicate() -> str:
    predicate = ch._build_re_dispatch_predicate(
        reenqueue_window=600,
        stale_window=STALE_WINDOW_SECONDS,
        capacity_redispatch_seconds=120,
    )
    return str(sa.select(Run.id).where(predicate).compile(compile_kwargs={"literal_binds": True}))


def _class_row(run_id: uuid.UUID | None = None) -> Any:
    """The observed FAR-1803 row shape: node-started + heartbeat stale."""
    return _run_row(
        run_id or uuid.uuid4(),
        status="running",
        stale=True,
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
    )


class _CaptureSession:
    """Minimal async session capturing executed statements (SQL-shape checks)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.calls.append((str(stmt), dict(params or {})))
        result = MagicMock()
        result.all.return_value = []
        result.rowcount = 0
        return result

    async def flush(self) -> None:
        return


class TestFar1803ReproResolved:
    """The REPRO that pinned the defect — each test below was run against the
    PRE-FIX tree on 2026-10-15 and PASSED, asserting the then-current wrong
    behaviour (the observations recorded in their docstrings). The fix commit
    flips each assertion to the corrected behaviour, so all three now FAIL
    without the fix — they are the unit-level regression proof of the same
    defect the integration repro observed on real Postgres."""

    def test_repro_scan_predicate_now_admits_the_observed_class(self) -> None:
        """PRE-FIX OBSERVED: the scan had NO leg for a ``running``
        ``first_node_dispatched`` row with a stale heartbeat — the only
        positive ``dispatch_phase = ...`` match in the whole predicate was the
        guardrail-override resume leg, so the observed class was never
        SELECTED (not ``scanned``, not counted, invisible to every reconcile
        counter). POST-FIX: a dedicated FAR-1803 leg brings the positive-match
        count to 2; FAR-2122's non-SAQ node-started resume leg is a third."""
        # Pre-fix: 1 (the FAR-1728 override leg only). Post-fix: 3 — the
        # override leg, the FAR-1803 saq/executor-lost leg and the FAR-2122
        # non-saq checkpoint-resume leg.
        sql = _compile_scan_predicate()
        positive_phase_matches = sql.count("dispatch_phase = 'first_node_dispatched'")
        assert positive_phase_matches == 3
        # The FAR-1649 phase exclusion on the generic stale leg is unchanged —
        # re-dispatch of a node-started run stays impossible.
        assert "dispatch_phase IS NULL" in sql

    def test_repro_age_gate_shield_now_carries_the_liveness_condition(self) -> None:
        """PRE-FIX OBSERVED: the B4 age gate's node-started shield applied
        whatever the heartbeat said — a row whose executor died hours ago kept
        the shield and the age gate never collected it (observed: runs sitting
        ``running`` for 7h+ with the live ``{"state": "dispatching"}`` marker
        intact, only the 25h marker sweep could ever collect them). POST-FIX:
        the shield lifts once the heartbeat is stale."""
        assert "heartbeat_at >= now() - (:heartbeat_fresh * interval '1 second')" in ch._MID_GRAPH_WEDGE_SQL

    async def test_repro_full_tick_now_reports_the_class_under_its_own_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRE-FIX OBSERVED: feeding the observed row through a real reconcile
        tick left it ``running``, never re-dispatched it (correct) but reported
        it ONLY under ``node_started_deferred`` — a counter documented as
        "production should see 0; non-zero is a regression alarm" — and no
        counter named for the class existed at all. POST-FIX: it is counted
        ``node_started_executor_lost`` and still never re-dispatched."""
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [_class_row()])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["nodeless_failed"] == 0
        assert summary["node_started_executor_lost"] == 1
        assert summary["node_started_deferred"] == 0


class TestFar1803Regression:
    """The FIX — these FAIL against the pre-fix tree (KeyError / count
    mismatches / missing binds) and pass with the FAR-1803 change. The two
    scope-preservation guards (fresh heartbeat, guardrail-override class)
    pass on both sides by design — they pin what the fix must NOT touch."""

    def test_scan_predicate_admits_the_node_started_stale_heartbeat_class(self) -> None:
        """The scan carries a dedicated leg: ``running`` + ``saq`` +
        ``first_node_dispatched`` + STALE (or NULL) heartbeat, never the
        guardrail-override class (its own leg owns resume_run)."""
        sql = _compile_scan_predicate()
        # Three positive phase matches now: the FAR-1728 override leg, the
        # FAR-1803 saq leg and FAR-2122's non-saq checkpoint-resume leg.
        assert sql.count("dispatch_phase = 'first_node_dispatched'") == 3
        # The new leg pairs the phase with the staleness gate...
        assert (
            "runs.dispatcher = 'saq' AND runs.dispatch_phase = 'first_node_dispatched' "
            "AND (runs.heartbeat_at IS NULL OR runs.heartbeat_at < now() - 600 * interval '1 second')"
        ) in sql
        # ...and refuses the guardrail-override class (its leg re-dispatches
        # resume_run; this leg must never shadow it). ``not_(...is_(True))``
        # compiles to ``IS NOT true`` — NULL (the common case) passes, so a
        # row without the marker column value is still admitted.
        assert "runs.guardrail_override_resume_pending IS NOT true" in sql

    async def test_reconcile_counts_the_class_under_its_own_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real tick classifies the row as ``node_started_executor_lost`` —
        visible in the readiness detail — and STILL never re-dispatches it
        (FAR-1649: re-executing a node-started run is unsafe)."""
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [_class_row()])

        reenqueue.assert_not_awaited()
        assert summary["repaired"] == 0
        assert summary["node_started_executor_lost"] == 1
        # The class leaves the belt's counter — the belt keeps its own
        # race/fail-closed scope (fresh heartbeat / missing phase attribute).
        assert summary["node_started_deferred"] == 0

    async def test_reconcile_never_counts_a_fresh_heartbeat_row_as_executor_lost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A node-started row whose heartbeat is FRESH is a LIVE attempt: it
        is not this class (the belt still holds it back from re-dispatch)."""
        row = _run_row(
            uuid.uuid4(),
            status="running",
            stale=False,
            dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
        )
        summary, reenqueue, _session = await _run_reconcile(monkeypatch, [row])

        reenqueue.assert_not_awaited()
        assert summary.get("node_started_executor_lost", 0) == 0
        assert summary["node_started_deferred"] == 1

    def test_guardrail_override_class_is_not_the_executor_lost_class(self) -> None:
        """FAR-1728's override class (the dedicated marker column set) is
        owned by its own resume leg — the classification must never claim it
        (so the row loop's intercept cannot shadow the resume path)."""
        row = _class_row()
        row.guardrail_override_resume_pending = True
        assert ch._is_node_started_executor_lost_row(row, STALE_WINDOW_SECONDS) is False
        # Control: without the marker it IS the FAR-1803 class.
        row.guardrail_override_resume_pending = False
        assert ch._is_node_started_executor_lost_row(row, STALE_WINDOW_SECONDS) is True

    async def test_age_gate_shield_lifts_once_the_heartbeat_is_stale(self) -> None:
        """The B4 shield now carries the liveness condition: it applies only
        while ``heartbeat_at`` is FRESH (the owning executor is alive). With a
        stale/NULL heartbeat ``NOT (shield)`` is true and the age gate
        terminalises the row ``no_progress`` at the bound."""
        session = _CaptureSession()
        await ch._terminalize_mid_graph_wedges(
            session, uuid.uuid4(), max_age_minutes=135, max_rows=None, stale_seconds=STALE_WINDOW_SECONDS
        )
        sql, params = session.calls[0]
        assert "heartbeat_at IS NOT NULL" in sql
        assert "heartbeat_at >= now() - (:heartbeat_fresh * interval '1 second')" in sql
        # The staleness bound travels as a BIND (the same value the scan's
        # stale-heartbeat legs use — one definition of "stale").
        assert params["heartbeat_fresh"] == STALE_WINDOW_SECONDS
        # The original shield pieces are unchanged (FAR-1740 intent).
        assert "dispatch_phase = :first_node_phase" in sql
        assert "sandbox_dispatch_state IS NOT NULL" in sql
        assert "NOT LIKE :live_marker_tombstone" in sql

    async def test_age_gate_stale_bound_defaults_to_the_reconcile_stale_window(self) -> None:
        """Direct callers (and the integration suite) that omit
        ``stale_seconds`` get the SAME bound the reconcile scan's
        stale-heartbeat legs use: ``2 * SAQ_JOB_HEARTBEAT``."""
        from modulo.core.cron_helpers import RECONCILE_STALE_HEARTBEAT_FACTOR, get_settings

        session = _CaptureSession()
        await ch._terminalize_mid_graph_wedges(session, uuid.uuid4(), max_age_minutes=135, max_rows=None)
        _sql, params = session.calls[0]
        expected = RECONCILE_STALE_HEARTBEAT_FACTOR * int(get_settings().saq_job_heartbeat)
        assert params["heartbeat_fresh"] == expected

    def test_non_saq_row_is_not_the_executor_lost_class(self) -> None:
        """The saq scope guard: a running node-started row owned by a
        non-'saq' dispatcher is NOT this class. The stale_run_recovery
        sweep's ``worker_lost`` arm owns those rows, so the reconcile
        classification must never claim (and hold) them."""
        row = _class_row()
        row.dispatcher = "arq"
        assert ch._is_node_started_executor_lost_row(row, STALE_WINDOW_SECONDS) is False

    def test_null_heartbeat_row_is_the_executor_lost_class(self) -> None:
        """A node-started row with NO heartbeat evidence at all is this
        class: nothing alive is writing a heartbeat, so the SQL leg's
        NULL-heartbeat staleness verdict applies — the row is counted
        (held back, never re-dispatched), matching ``heartbeat_at IS NULL``
        in the scan leg."""
        row = _class_row()
        row.heartbeat_at = None
        assert ch._is_node_started_executor_lost_row(row, STALE_WINDOW_SECONDS) is True

    def test_unverifiable_heartbeat_fails_closed_into_the_belt(self) -> None:
        """A heartbeat value that is not a ``datetime`` cannot have its
        staleness verified, so the classification fails CLOSED: the row is
        NOT claimed as executor-lost and keeps the belt's fail-closed hold
        (``node_started_deferred``) rather than being re-dispatched."""
        row = _class_row()
        row.heartbeat_at = "not-a-timestamp"
        assert ch._is_node_started_executor_lost_row(row, STALE_WINDOW_SECONDS) is False
