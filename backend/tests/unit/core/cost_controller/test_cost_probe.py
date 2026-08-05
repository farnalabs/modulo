"""Unit tests for the cost probe (spec §4.7, PR A2).

The probe fixture family: a sample passes with zero mismatches; a deliberate
mismatch fires the probe counters; a clamped-marker run is skipped; a missing
org ledger row fires the WATCH counter (NOT rolled back); a null-team run and a
clamped day are handled; the canonical trigger rule (>=5 sampled, 2-distinct x
2-consecutive) with the temporal-adjacency reset; the duplicate-terminal flood
with the cooldown; the ≥1-org / heartbeat-advance rules.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.cost_controller.probe import (
    _duplicate_flood_trigger,
    _evaluate_run,
    _evaluate_trigger,
    _org_row_watch,
    run_probe,
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
    total: str = "10.000000",
    breakdown: list[dict[str, Any]] | None = None,
    started_at: datetime | None = None,
    ledger_written: bool = True,
    ledger_refused_at: Any = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.total_cost_usd = Decimal(total)
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
