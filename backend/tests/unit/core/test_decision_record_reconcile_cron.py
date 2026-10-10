"""Unit tests for the ``decision_record_reconcile`` SAQ system cron (FAR-1108 8b).

Proves the wrapper is wired to a real periodic path: it opens the system
session, delegates to the read-only scanner, persists a liveness stats blob
every tick, logs anomalies loudly, and on failure persists the error then
re-raises so SAQ's retries engage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.eval_engine.decision_reconcile import DecisionReconcileReport, DecisionRecordAnomaly


def _sessionmaker_like() -> MagicMock:
    """A sessionmaker mock usable as ``async with factory() as s, s.begin():``."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = session_cm
    return factory


@pytest.mark.asyncio
async def test_clean_tick_persists_stats_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    from modulo.core import saq_worker as sw

    report = DecisionReconcileReport(scanned=12, anomalies=[])
    with (
        patch.object(sw, "_make_system_session_factory", return_value=_sessionmaker_like()),
        patch(
            "modulo.core.eval_engine.decision_reconcile.reconcile_decision_records",
            new=AsyncMock(return_value=report),
        ),
        patch.object(sw, "_persist_sweep_stats", new=AsyncMock()) as persist,
        caplog.at_level("WARNING"),
    ):
        stats = await sw.decision_record_reconcile({})

    assert stats["scanned"] == 12
    assert stats["total_anomalies"] == 0
    persist.assert_awaited_once()
    key, blob, _ttl = persist.await_args.args
    assert key == sw.DECISION_RECORD_RECONCILE_STATS_KEY
    assert blob["total_anomalies"] == 0
    assert "decision_record.reconcile.anomalies" not in caplog.text


@pytest.mark.asyncio
async def test_anomalies_are_logged_and_counted(caplog: pytest.LogCaptureFixture) -> None:
    from modulo.core import saq_worker as sw

    report = DecisionReconcileReport(
        scanned=5,
        anomalies=[
            DecisionRecordAnomaly(kind="missing_eval_result_id", detail={"decision_id": "d1"}),
            DecisionRecordAnomaly(kind="missing_eval_result_id", detail={"decision_id": "d2"}),
            DecisionRecordAnomaly(kind="orphaned_run", detail={"decision_id": "d3", "run_id": "r1"}),
        ],
    )
    with (
        patch.object(sw, "_make_system_session_factory", return_value=_sessionmaker_like()),
        patch(
            "modulo.core.eval_engine.decision_reconcile.reconcile_decision_records",
            new=AsyncMock(return_value=report),
        ),
        patch.object(sw, "_persist_sweep_stats", new=AsyncMock()) as persist,
        caplog.at_level("WARNING"),
    ):
        stats = await sw.decision_record_reconcile({})

    assert stats["total_anomalies"] == 3
    assert stats["anomaly_missing_eval_result_id"] == 2
    assert stats["anomaly_orphaned_run"] == 1
    assert "decision_record.reconcile.anomalies" in caplog.text
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_persists_error_then_reraises() -> None:
    from modulo.core import saq_worker as sw

    with (
        patch.object(sw, "_make_system_session_factory", return_value=_sessionmaker_like()),
        patch(
            "modulo.core.eval_engine.decision_reconcile.reconcile_decision_records",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(sw, "_persist_sweep_stats", new=AsyncMock()) as persist,
        pytest.raises(RuntimeError),
    ):
        await sw.decision_record_reconcile({})

    persist.assert_awaited_once()
    _, blob, _ttl = persist.await_args.args
    assert "sweep_failed" in blob["error"]
    assert "RuntimeError" in blob["error"]
