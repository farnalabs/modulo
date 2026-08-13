"""Unit tests for the cost probe (spec §4.7, PR A2).

The probe fixture family: a sample passes with zero mismatches; a deliberate
mismatch fires the probe counters; a clamped-marker run is skipped; a missing
org ledger row fires the WATCH counter (NOT rolled back); a null-team run and a
clamped day are handled; the canonical trigger rule (>=5 sampled, 2-distinct x
2-consecutive) with the temporal-adjacency reset; the duplicate-terminal flood
with the cooldown; the ≥1-org / heartbeat-advance rules.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller.probe import (
    PROBE_TERMINAL_STATUSES,
    _assert_sample_query_index,
    _duplicate_flood_trigger,
    _evaluate_run,
    _evaluate_trigger,
    _org_row_watch,
    _probe_org,
    _sample_runs,
    run_probe,
    set_duplicate_terminal_cooldown,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime.now(UTC)


class _FixedClock:
    """``probe.datetime`` replaced with a FIXED clock in the trigger/flood tests.

    ``_NOW`` is frozen at module import (pytest collects/imports every test
    module upfront). Without a frozen clock, a full-suite run — which can take
    >15 minutes between collection and execution — silently breaks the
    trigger's 10-minute adjacency window (``PROBE_ADJACENCY_GAP_SECONDS``) and
    the flood's 10-minute window, turning ``test_trigger_two_consecutive_
    cadences_fires`` into ``assert False is True``. ``fromisoformat`` stays
    real so blob/event timestamps parse exactly as in production.
    """

    @staticmethod
    def now(tz: Any = None) -> datetime:
        return _NOW if tz is None else _NOW.astimezone(tz)

    fromisoformat = staticmethod(datetime.fromisoformat)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    # AsyncMock supports the async context-manager protocol natively — assigning
    # __aenter__/__aexit__ instance attrs would create NEVER-AWAITED coroutines
    # (async-with uses the class methods), tripping pytest's RuntimeWarning->error.
    begin_cm = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_sampled_run(
    *,
    run_id: uuid.UUID | None = None,
    total: str | None = "10.000000",
    breakdown: list[Any] | None = None,
    started_at: datetime | None = None,
    ledger_written: bool = True,
    ledger_refused_at: Any = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.total_cost_usd = Decimal(total) if total is not None else None
    run.cost_breakdown = breakdown if breakdown is not None else [{"component": "x", "amount_usd": total}]
    run.started_at = started_at or _NOW
    run.ledger_written = ledger_written
    run.ledger_refused_at = ledger_refused_at
    return run


# ---------------------------------------------------------------------------
# _evaluate_run — total == sum, clamped-marker skip, malformed drop
# ---------------------------------------------------------------------------


def test_evaluate_run_ok_when_total_matches_sum() -> None:
    run = _make_sampled_run(total="0.133200", breakdown=[{"component": "x", "amount_usd": "0.133200"}])
    assert _evaluate_run(run) == "ok"


def test_evaluate_run_mismatch_when_total_differs() -> None:
    run = _make_sampled_run(total="0.999999", breakdown=[{"component": "x", "amount_usd": "0.133200"}])
    assert _evaluate_run(run) == "mismatch"


def test_evaluate_run_clamped_marker_skipped() -> None:
    run = _make_sampled_run(
        total="99999999.999999",
        breakdown=[{"total_clamped": True, "amount_usd": "0.000000"}, {"component": "x", "amount_usd": "5.000000"}],
    )
    assert _evaluate_run(run) == "clamped"


def test_evaluate_run_malformed_amount_drops_run() -> None:
    run = _make_sampled_run(total="1.000000", breakdown=[{"component": "x", "amount_usd": "not-a-number"}])
    assert _evaluate_run(run) == "malformed"


def test_evaluate_run_none_total_drops_run() -> None:
    run = _make_sampled_run(total=None)
    assert _evaluate_run(run) == "malformed"


def test_evaluate_run_none_breakdown_drops_run() -> None:
    run = MagicMock()
    run.total_cost_usd = Decimal("1.000000")
    run.cost_breakdown = None
    assert _evaluate_run(run) == "malformed"


def test_evaluate_run_malformed_total_drops_run() -> None:
    run = MagicMock()
    run.total_cost_usd = "not-a-number"
    run.cost_breakdown = [{"component": "x", "amount_usd": "1.000000"}]
    assert _evaluate_run(run) == "malformed"


def test_evaluate_run_skips_non_dict_entries() -> None:
    """Non-dict entries are ignored in the sum."""
    run = _make_sampled_run(
        total="2.000000",
        breakdown=[
            {"component": "x", "amount_usd": "2.000000"},
            "garbage",
            {"amount_usd": None},
        ],
    )
    assert _evaluate_run(run) == "ok"


def test_evaluate_run_any_clamped_marker_short_circuits_to_clamped() -> None:
    """A single total_clamped entry marks the whole run as clamped."""
    run = _make_sampled_run(
        total="2.000000",
        breakdown=[
            {"component": "x", "amount_usd": "2.000000"},
            {"amount_usd": "5.000000", "total_clamped": True},
        ],
    )
    assert _evaluate_run(run) == "clamped"


# ---------------------------------------------------------------------------
# _sample_runs — the N=50 most recent terminal runs
# ---------------------------------------------------------------------------


async def test_sample_runs_returns_all_rows() -> None:
    session = AsyncMock()
    rows = [object(), object()]
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    assert await _sample_runs(session, _ORG_ID) == rows
    session.execute.assert_awaited_once()


def test_probe_terminal_statuses_include_budget_exceeded() -> None:
    """A budget-breached run with a breakdown is part of the cost-accuracy sample."""
    assert "budget_exceeded" in PROBE_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# _assert_sample_query_index — the one-per-process EXPLAIN gate
# ---------------------------------------------------------------------------


async def test_explain_gate_uses_reset_on_success() -> None:
    session = AsyncMock()
    plan_rows = MagicMock()
    plan_rows.all.return_value = [("Index Scan using ix_runs_probe",)]
    session.execute = AsyncMock(return_value=plan_rows)
    session.execute.side_effect = [AsyncMock(), plan_rows, AsyncMock()] * 2
    with patch("modulo.core.cost_controller.probe._explain_checked", False):
        await _assert_sample_query_index(session, _ORG_ID)
    executed = [call.args[0] for call in session.execute.await_args_list]
    assert any("SET enable_seqscan = off" in str(cmd) for cmd in executed)
    assert any("RESET enable_seqscan" in str(cmd) for cmd in executed)
    assert any("EXPLAIN" in str(cmd) for cmd in executed)
    with patch("modulo.core.cost_controller.probe._explain_checked", False):
        await _assert_sample_query_index(session, _ORG_ID)
    assert len(session.execute.await_args_list) == 6  # run twice, not once


async def test_explain_gate_resets_on_missing_index() -> None:
    """A plan without the index still RESETs seqscan and warns (never crashes)."""
    session = AsyncMock()
    plan_rows = MagicMock()
    plan_rows.all.return_value = [("Seq Scan on runs",)]
    session.execute = AsyncMock(return_value=plan_rows)
    session.execute.side_effect = [AsyncMock(), plan_rows, AsyncMock()]
    with (
        patch("modulo.core.cost_controller.probe._explain_checked", False),
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await _assert_sample_query_index(session, _ORG_ID)
    executed = [call.args[0] for call in session.execute.await_args_list]
    assert any("RESET enable_seqscan" in str(cmd) for cmd in executed)
    mock_log.warning.assert_called_once()


async def test_explain_gate_reset_on_failure() -> None:
    """A failed EXPLAIN still runs RESET via finally, then logs."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.execute.side_effect = [AsyncMock(), RuntimeError("boom"), AsyncMock()]
    with (
        patch("modulo.core.cost_controller.probe._explain_checked", False),
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await _assert_sample_query_index(session, _ORG_ID)
    executed = [call.args[0] for call in session.execute.await_args_list]
    assert any("RESET enable_seqscan" in str(cmd) for cmd in executed)
    mock_log.exception.assert_called_once()


async def test_explain_gate_short_circuits_after_first_success() -> None:
    """Once checked, the EXPLAIN gate does not re-run in the same process."""
    session = AsyncMock()
    with patch("modulo.core.cost_controller.probe._explain_checked", True):
        await _assert_sample_query_index(session, _ORG_ID)
    session.execute.assert_not_awaited()


async def test_explain_gate_cancelled_error_re_raised() -> None:
    """A CancelledError inside the EXPLAIN path is never swallowed — it propagates."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        patch("modulo.core.cost_controller.probe._explain_checked", False),
        pytest.raises(asyncio.CancelledError),
    ):
        await _assert_sample_query_index(session, _ORG_ID)
    session.execute.assert_awaited()


# ---------------------------------------------------------------------------
# _org_row_watch — the org-row existence WATCH
# ---------------------------------------------------------------------------
async def test_org_row_watch_missing_row_counts() -> None:
    runs = [_make_sampled_run(ledger_written=True, ledger_refused_at=None)]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    assert await _org_row_watch(session, _ORG_ID, runs) == 1


async def test_org_row_watch_sufficient_row_no_fire() -> None:
    runs = [_make_sampled_run(total="10.000000", ledger_written=True, ledger_refused_at=None)]
    row = MagicMock()
    row.run_date = _NOW.date()
    row.total_spend_usd = Decimal("20.000000")
    row.clamped = False
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))
    assert await _org_row_watch(session, _ORG_ID, runs) == 0


async def test_org_row_watch_insufficient_row_counts() -> None:
    runs = [_make_sampled_run(total="10.000000", ledger_written=True, ledger_refused_at=None)]
    row = MagicMock()
    row.run_date = _NOW.date()
    row.total_spend_usd = Decimal("5.000000")
    row.clamped = False
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))
    assert await _org_row_watch(session, _ORG_ID, runs) == 1


async def test_org_row_watch_clamped_day_skipped() -> None:
    """A clamped day is a known anomaly, not a missing-row signal."""
    runs = [_make_sampled_run(total="10.000000", ledger_written=True, ledger_refused_at=None)]
    row = MagicMock()
    row.run_date = _NOW.date()
    row.total_spend_usd = Decimal("99999999.999999")
    row.clamped = True
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))
    assert await _org_row_watch(session, _ORG_ID, runs) == 0


async def test_org_row_watch_null_team_and_refused_runs_excluded() -> None:
    """team-carrying AND null-team runs are watched alike; refused runs are not."""
    runs = [
        _make_sampled_run(ledger_written=True, ledger_refused_at=None),  # watched
        _make_sampled_run(ledger_written=False, ledger_refused_at=None),  # not watched
        _make_sampled_run(ledger_written=True, ledger_refused_at=_NOW),  # refused — not watched
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    assert await _org_row_watch(session, _ORG_ID, runs) == 1


async def test_org_row_watch_no_watched_runs_returns_zero() -> None:
    """No watched runs (all refused/unwritten) short-circuits without a query."""
    runs = [
        _make_sampled_run(ledger_written=False, ledger_refused_at=None),
        _make_sampled_run(ledger_written=True, ledger_refused_at=_NOW),
    ]
    session = AsyncMock()
    assert await _org_row_watch(session, _ORG_ID, runs) == 0
    session.execute.assert_not_awaited()


async def test_org_row_watch_none_started_at_skipped() -> None:
    """A watched run with no started_at contributes nothing to any date bucket."""
    run = _make_sampled_run(ledger_written=True, ledger_refused_at=None)
    run.started_at = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    assert await _org_row_watch(session, _ORG_ID, [run]) == 0


async def test_org_row_watch_malformed_total_skipped() -> None:
    """A watched run whose total cannot be decimal-parsed is skipped, not fatal."""
    run = MagicMock()
    run.id = uuid.uuid4()
    run.total_cost_usd = "not-a-number"
    run.cost_breakdown = [{"component": "x", "amount_usd": "1.000000"}]
    run.started_at = _NOW
    run.ledger_written = True
    run.ledger_refused_at = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    assert await _org_row_watch(session, _ORG_ID, [run]) == 0


async def test_org_row_watch_single_date_grouped_across_runs() -> None:
    """Two watched runs on the same date sum into ONE date bucket (1 row expected)."""
    runs = [
        _make_sampled_run(total="3.000000", ledger_written=True, ledger_refused_at=None),
        _make_sampled_run(total="4.000000", ledger_written=True, ledger_refused_at=None),
    ]
    row = MagicMock()
    row.run_date = _NOW.date()
    row.total_spend_usd = Decimal("7.000000")
    row.clamped = False
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))
    assert await _org_row_watch(session, _ORG_ID, runs) == 0


# ---------------------------------------------------------------------------
# _evaluate_trigger — the canonical probe rule
# ---------------------------------------------------------------------------


def _blob(mismatch_runs: list[str], age_minutes: int) -> dict[str, Any]:
    return {
        "last_cadence_mismatch_runs": mismatch_runs,
        "last_cadence_at": (_NOW - timedelta(minutes=age_minutes)).isoformat(),
    }


async def _trigger(mismatches: list[str], sampled: int, blob: Any, gap_minutes: int = 4) -> bool:
    session = _make_session()
    with (
        patch("modulo.core.cost_controller.probe.datetime", _FixedClock),
        patch("modulo.core.cost_controller.probe.read_system_config", new=AsyncMock(return_value=blob)),
        patch("modulo.core.cost_controller.probe.acquire_kv_lock", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe.write_system_config", new=AsyncMock()),
    ):
        return await _evaluate_trigger(session, _ORG_ID, runs=[object()] * sampled, mismatches=mismatches)


async def test_trigger_single_cadence_does_not_fire() -> None:
    """A single cadence with >=2 distinct mismatches does NOT fire (needs 2 consecutive)."""
    assert await _trigger(["r1", "r2"], sampled=5, blob=None) is False


async def test_trigger_two_consecutive_cadences_fires() -> None:
    assert await _trigger(["r1", "r2"], sampled=5, blob=_blob(["r9", "r8"], age_minutes=4)) is True


async def test_trigger_quiet_org_never_fires() -> None:
    """An org with <5 sampled runs NEVER triggers — counter/log only."""
    assert await _trigger(["r1", "r2"], sampled=4, blob=_blob(["r9", "r8"], age_minutes=4)) is False


async def test_trigger_single_deterministic_flaky_excluded() -> None:
    """One distinct mismatch never reaches the 2-distinct threshold."""
    assert await _trigger(["r1", "r1"], sampled=5, blob=_blob(["r9", "r9"], age_minutes=4)) is False


async def test_trigger_temporal_adjacency_resets_after_gap() -> None:
    """A persisted chain older than 2x the cadence does NOT count as consecutive."""
    assert await _trigger(["r1", "r2"], sampled=5, blob=_blob(["r9", "r8"], age_minutes=15)) is False


async def test_trigger_malformed_blob_timestamp_treated_as_no_chain() -> None:
    """A corrupt persisted timestamp resets the chain (fail-safe, never crashes)."""
    blob = {"last_cadence_mismatch_runs": ["r9", "r8"], "last_cadence_at": "not-a-date"}
    assert await _trigger(["r1", "r2"], sampled=5, blob=blob) is False


async def test_trigger_non_list_prior_runs_treated_as_zero_chain() -> None:
    """A persisted blob whose mismatch list is not a list counts as no prior chain."""
    blob = {"last_cadence_mismatch_runs": "r9,r8", "last_cadence_at": (_NOW - timedelta(minutes=4)).isoformat()}
    assert await _trigger(["r1", "r2"], sampled=5, blob=blob) is False


# ---------------------------------------------------------------------------
# _duplicate_flood_trigger — the flood hard-gate input + cooldown
# ---------------------------------------------------------------------------


async def _flood(events: Any, suppressed_until: Any = None) -> bool:
    session = _make_session()
    side_effect = [suppressed_until] if suppressed_until is not None else [None]
    side_effect.append(events)
    with (
        patch("modulo.core.cost_controller.probe.datetime", _FixedClock),
        patch("modulo.core.cost_controller.probe.read_system_config", new=AsyncMock(side_effect=side_effect)),
    ):
        return await _duplicate_flood_trigger(session)


async def test_flood_fires_over_five_distinct_runs() -> None:
    events = [{"run_id": f"r{i}", "ts": _NOW.isoformat()} for i in range(6)]
    assert await _flood(events) is True


async def test_flood_single_run_does_not_fire() -> None:
    events = [{"run_id": "r1", "ts": _NOW.isoformat()}] * 6
    assert await _flood(events) is False


async def test_flood_old_events_outside_window_ignored() -> None:
    events = [{"run_id": f"r{i}", "ts": (_NOW - timedelta(minutes=20)).isoformat()} for i in range(6)]
    assert await _flood(events) is False


async def test_flood_suppressed_by_cooldown() -> None:
    events = [{"run_id": f"r{i}", "ts": _NOW.isoformat()} for i in range(6)]
    until = (_NOW + timedelta(minutes=10)).isoformat()
    assert await _flood(events, suppressed_until=until) is False


async def test_flood_expired_cooldown_does_not_suppress() -> None:
    events = [{"run_id": f"r{i}", "ts": _NOW.isoformat()} for i in range(6)]
    expired = (_NOW - timedelta(minutes=20)).isoformat()
    assert await _flood(events, suppressed_until=expired) is True


async def test_flood_malformed_cooldown_ignored() -> None:
    """A corrupt suppressed-until value expires by fallthrough — never raises."""
    events = [{"run_id": f"r{i}", "ts": _NOW.isoformat()} for i in range(6)]
    assert await _flood(events, suppressed_until="garbage") is True


async def test_flood_non_list_events_no_fire() -> None:
    """A corrupt (non-list) event store must never crash the probe."""
    assert await _flood(events="not-a-list") is False


async def test_flood_ignores_malformed_events_and_missing_run_ids() -> None:
    """Events with corrupt/missing timestamps or run_ids are skipped, not counted."""
    events = [
        {"run_id": "r1", "ts": _NOW.isoformat()},
        {"run_id": "r2", "ts": "not-a-date"},
        {"ts": _NOW.isoformat()},  # no run_id
        {"run_id": "r3"},  # no ts
        "garbage",  # not a dict
    ]
    assert await _flood(events) is False


# ---------------------------------------------------------------------------
# _probe_org — per-org sample, five signals, trigger evaluation
# ---------------------------------------------------------------------------


async def _probe_org_session_factory(session: AsyncMock) -> Any:
    """A factory yielding a session whose ``begin()`` is a valid async CM.

    ``_probe_org`` opens ``async with session_factory() as session,
    session.begin():`` — the factory must yield a REAL session mock whose
    ``begin()`` returns an async context manager (AsyncMock does this natively).
    """

    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return _ctx


async def test_probe_org_happy_path_records_signals() -> None:
    """A clean sample: mismatch runs counted, no clamped/missing signals, no rollback log."""
    session = _make_session()
    ok_run = _make_sampled_run(total="1.000000")
    bad_run = _make_sampled_run(
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        total="2.000000",
        breakdown=[{"component": "x", "amount_usd": "9.000000"}],
    )
    with (
        patch("modulo.core.cost_controller.probe.set_rls_org", new=AsyncMock()) as mock_rls,
        patch("modulo.core.cost_controller.probe._assert_sample_query_index", new=AsyncMock()),
        patch(
            "modulo.core.cost_controller.probe._sample_runs", new=AsyncMock(return_value=[ok_run, bad_run])
        ) as mock_sample,
        patch("modulo.core.cost_controller.probe._org_row_watch", new=AsyncMock(return_value=0)),
        patch("modulo.core.cost_controller.probe._evaluate_trigger", new=AsyncMock(return_value=False)),
        patch("modulo.core.cost_controller.probe._duplicate_flood_trigger", new=AsyncMock(return_value=False)),
        patch("modulo.core.cost_controller.probe.record_probe_mismatch_runs") as mock_mismatch,
        patch("modulo.core.cost_controller.probe.record_probe_total_eq_mismatch") as mock_eq,
        patch("modulo.core.cost_controller.probe.record_probe_clamped_skip") as mock_clamped,
        patch("modulo.core.cost_controller.probe.record_probe_missing_ledger_row") as mock_missing,
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await _probe_org(await _probe_org_session_factory(session), _ORG_ID)
    mock_rls.assert_awaited_once()
    mock_sample.assert_awaited_once_with(session, _ORG_ID)
    mock_mismatch.assert_called_once_with(1)
    mock_eq.assert_called_once()
    mock_clamped.assert_not_called()
    mock_missing.assert_not_called()
    mock_log.error.assert_not_called()
    mock_log.info.assert_called_once()


async def test_probe_org_records_clamped_skip_and_missing_rows() -> None:
    """Marker runs bump the clamped counter; missing ledger rows bump the WATCH counter."""
    session = _make_session()
    clamped_run = _make_sampled_run(
        total="99999999.999999", breakdown=[{"total_clamped": True, "amount_usd": "5.000000"}]
    )
    with (
        patch("modulo.core.cost_controller.probe.set_rls_org", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe._assert_sample_query_index", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe._sample_runs", new=AsyncMock(return_value=[clamped_run, clamped_run])),
        patch("modulo.core.cost_controller.probe._org_row_watch", new=AsyncMock(return_value=2)),
        patch("modulo.core.cost_controller.probe._evaluate_trigger", new=AsyncMock(return_value=False)),
        patch("modulo.core.cost_controller.probe._duplicate_flood_trigger", new=AsyncMock(return_value=False)),
        patch("modulo.core.cost_controller.probe.record_probe_mismatch_runs") as mock_mismatch,
        patch("modulo.core.cost_controller.probe.record_probe_total_eq_mismatch") as mock_eq,
        patch("modulo.core.cost_controller.probe.record_probe_clamped_skip") as mock_clamped,
        patch("modulo.core.cost_controller.probe.record_probe_missing_ledger_row") as mock_missing,
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await _probe_org(await _probe_org_session_factory(session), _ORG_ID)
    mock_mismatch.assert_called_once_with(0)
    mock_eq.assert_not_called()
    mock_clamped.assert_called_once_with(2)
    mock_missing.assert_called_once_with(2)
    mock_log.error.assert_not_called()


async def test_probe_org_logs_rollback_trigger_on_rule_or_flood() -> None:
    """Either trigger firing emits the rollback error log with both flags."""
    session = _make_session()
    bad_run = _make_sampled_run(
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        total="9.000000",
        breakdown=[{"component": "x", "amount_usd": "1.000000"}],
    )
    with (
        patch("modulo.core.cost_controller.probe.set_rls_org", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe._assert_sample_query_index", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe._sample_runs", new=AsyncMock(return_value=[bad_run])),
        patch("modulo.core.cost_controller.probe._org_row_watch", new=AsyncMock(return_value=0)),
        patch("modulo.core.cost_controller.probe._evaluate_trigger", new=AsyncMock(return_value=True)),
        patch("modulo.core.cost_controller.probe._duplicate_flood_trigger", new=AsyncMock(return_value=True)),
        patch("modulo.core.cost_controller.probe.record_probe_mismatch_runs"),
        patch("modulo.core.cost_controller.probe.record_probe_total_eq_mismatch"),
        patch("modulo.core.cost_controller.probe.record_probe_clamped_skip"),
        patch("modulo.core.cost_controller.probe.record_probe_missing_ledger_row"),
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await _probe_org(await _probe_org_session_factory(session), _ORG_ID)
    mock_log.error.assert_called_once()
    extra = mock_log.error.call_args.kwargs["extra"]
    assert extra["probe_rule"] is True
    assert extra["duplicate_flood"] is True
    assert extra["org_id"] == str(_ORG_ID)
    assert extra["mismatch_run_ids"] == [str(bad_run.id)]


async def test_probe_org_eval_failure_logged_and_skipped() -> None:
    """A run whose evaluation raises is logged and skipped, never fatal."""
    session = _make_session()
    with (
        patch("modulo.core.cost_controller.probe.set_rls_org", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe._assert_sample_query_index", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe._sample_runs", new=AsyncMock(return_value=[_make_sampled_run()])),
        patch("modulo.core.cost_controller.probe._evaluate_run", side_effect=RuntimeError("boom")),
        patch("modulo.core.cost_controller.probe._org_row_watch", new=AsyncMock(return_value=0)),
        patch("modulo.core.cost_controller.probe._evaluate_trigger", new=AsyncMock(return_value=False)),
        patch("modulo.core.cost_controller.probe._duplicate_flood_trigger", new=AsyncMock(return_value=False)),
        patch("modulo.core.cost_controller.probe.record_probe_mismatch_runs") as mock_mismatch,
        patch("modulo.core.cost_controller.probe.record_probe_total_eq_mismatch") as mock_eq,
        patch("modulo.core.cost_controller.probe.record_probe_clamped_skip") as mock_clamped,
        patch("modulo.core.cost_controller.probe.record_probe_missing_ledger_row") as mock_missing,
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await _probe_org(await _probe_org_session_factory(session), _ORG_ID)
    mock_log.warning.assert_called_once()
    mock_mismatch.assert_called_once_with(0)
    mock_eq.assert_not_called()
    mock_clamped.assert_not_called()
    mock_missing.assert_not_called()


# ---------------------------------------------------------------------------
# run_probe — org enumeration, ≥1-org gate, heartbeat advance
# ---------------------------------------------------------------------------


def _make_factory(session: AsyncMock) -> Any:
    """An async contextmanager factory — ``async with factory() as s`` yields
    the SAME session mock (``as s`` binds the yielded value, not the
    ``__aenter__`` result)."""

    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return _ctx


async def test_run_probe_zero_org_does_not_advance_heartbeat() -> None:
    session = _make_session()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = []
    with patch("modulo.core.cost_controller.probe.set_probe_last_success_ts") as mock_ts:
        summary = await run_probe(_make_factory(session))
    assert summary["advanced_heartbeat"] is False
    mock_ts.assert_not_called()


async def test_run_probe_advances_heartbeat_on_any_success() -> None:
    session = _make_session()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = [_ORG_ID]
    with (
        patch("modulo.core.cost_controller.probe._probe_org", new=AsyncMock()),
        patch("modulo.core.cost_controller.probe.set_probe_last_success_ts") as mock_ts,
    ):
        summary = await run_probe(_make_factory(session))
    assert summary["orgs_succeeded"] == 1
    assert summary["advanced_heartbeat"] is True
    mock_ts.assert_called_once()


async def test_run_probe_per_org_exception_isolation() -> None:
    """ONE org's failure cannot abort the whole sample."""
    session = _make_session()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = [_ORG_ID, uuid.uuid4()]
    with (
        patch("modulo.core.cost_controller.probe._probe_org", new=AsyncMock(side_effect=[None, RuntimeError("boom")])),
        patch("modulo.core.cost_controller.probe.set_probe_last_success_ts") as mock_ts,
    ):
        summary = await run_probe(_make_factory(session))
    assert summary["orgs_succeeded"] == 1
    assert summary["orgs_failed"] == 1
    assert summary["advanced_heartbeat"] is True
    mock_ts.assert_called_once()


async def test_run_probe_all_orgs_failed_does_not_advance() -> None:
    session = _make_session()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = [_ORG_ID]
    with (
        patch("modulo.core.cost_controller.probe._probe_org", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("modulo.core.cost_controller.probe.set_probe_last_success_ts") as mock_ts,
    ):
        summary = await run_probe(_make_factory(session))
    assert summary["orgs_succeeded"] == 0
    assert summary["orgs_failed"] == 1
    assert summary["advanced_heartbeat"] is False
    mock_ts.assert_not_called()


async def test_run_probe_enumeration_failure_reports_error() -> None:
    """Org enumeration failure aborts before sampling; summary carries the error."""
    session = _make_session()
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("modulo.core.cost_controller.probe.set_probe_last_success_ts") as mock_ts:
        summary = await run_probe(_make_factory(session))
    assert summary["error"] == "enumeration_failed"
    assert summary["orgs_enumerated"] == 0
    assert summary["advanced_heartbeat"] is False
    mock_ts.assert_not_called()


async def test_run_probe_enumeration_cancelled_error_propagates() -> None:
    """A CancelledError during org enumeration is re-raised, never caught as a failure."""
    session = _make_session()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_probe(_make_factory(session))


async def test_run_probe_org_cancelled_error_propagates() -> None:
    """A CancelledError inside one org's probe is re-raised (per-org isolation catches only Exception)."""
    session = _make_session()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = [_ORG_ID]
    with (
        patch("modulo.core.cost_controller.probe._probe_org", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_probe(_make_factory(session))


async def test_run_probe_zero_org_enumerated_is_quiet_success() -> None:
    """A zero-org install returns before sampling with no heartbeat advance."""
    session = _make_session()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = []
    with patch("modulo.core.cost_controller.probe._probe_org", new=AsyncMock()) as mock_probe:
        summary = await run_probe(_make_factory(session))
    assert summary == {
        "orgs_enumerated": 0,
        "orgs_succeeded": 0,
        "orgs_failed": 0,
        "advanced_heartbeat": False,
    }
    mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# set_duplicate_terminal_cooldown — the flood cooldown writer
# ---------------------------------------------------------------------------


async def test_cooldown_writes_suppression_until() -> None:
    """Writes a future isoformat timestamp under the system_config key."""
    session = _make_session()
    written: dict[str, str] = {}

    async def _fake_write(s: Any, key: str, value: Any) -> None:
        written[key] = value

    with patch("modulo.core.cost_controller.probe.write_system_config", side_effect=_fake_write):
        await set_duplicate_terminal_cooldown(_make_factory(session))
    raw = written.get("duplicate_terminal_suppressed_until")
    assert raw is not None
    parsed = datetime.fromisoformat(raw)
    assert parsed > _NOW


async def test_cooldown_survives_underlying_failure() -> None:
    """A DB failure is logged and swallowed — the worker must not crash on write."""
    session = _make_session()
    with (
        patch("modulo.core.cost_controller.probe.write_system_config", side_effect=RuntimeError("boom")),
        patch("modulo.core.cost_controller.probe._log") as mock_log,
    ):
        await set_duplicate_terminal_cooldown(_make_factory(session))
    mock_log.exception.assert_called_once()


async def test_cooldown_cancelled_error_propagates() -> None:
    """A CancelledError during the cooldown write is re-raised, never swallowed."""
    session = _make_session()
    with (
        patch("modulo.core.cost_controller.probe.write_system_config", side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await set_duplicate_terminal_cooldown(_make_factory(session))
