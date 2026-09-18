"""Unit tests for the pipeline cost-control circuit breaker (FAR-105, spec §8.10).

All DB interaction is mocked; we verify threshold tripping, idempotent trips,
admin reset, and notifier dispatch wiring.
"""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller import (
    check_pipeline_circuit_breaker,
    reset_pipeline_circuit_breaker,
    sum_pipeline_monthly_spend,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
_RUN_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


@pytest.fixture
def mock_session() -> AsyncMock:
    s = AsyncMock()
    s.add = MagicMock()
    return s


def _make_pipeline(threshold: Decimal | None = None, tripped: bool = False) -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "data-pipeline"
    p.circuit_breaker_threshold = threshold
    p.circuit_breaker_tripped = tripped
    p.circuit_breaker_tripped_at = None
    return p


def _pipeline_result(pipeline: MagicMock) -> MagicMock:
    res = MagicMock()
    res.scalar_one_or_none.return_value = pipeline
    return res


# ---------------------------------------------------------------------------
# sum_pipeline_monthly_spend
# ---------------------------------------------------------------------------


class TestSumPipelineMonthlySpend:
    async def test_sums_runs_for_pipeline_current_month(self, mock_session: AsyncMock) -> None:
        sum_res = MagicMock()
        sum_res.scalar_one.return_value = Decimal("950.00")
        mock_session.execute = AsyncMock(return_value=sum_res)

        total = await sum_pipeline_monthly_spend(mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID)

        assert total == Decimal("950.00")
        stmt = mock_session.execute.call_args.args[0]
        compiled = stmt.compile()
        assert _PIPELINE_ID in compiled.params.values()
        assert _ORG_ID in compiled.params.values()

    async def test_excludes_inflight_run(self, mock_session: AsyncMock) -> None:
        sum_res = MagicMock()
        sum_res.scalar_one.return_value = Decimal("850.00")
        mock_session.execute = AsyncMock(return_value=sum_res)

        await sum_pipeline_monthly_spend(mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, exclude_run_id=_RUN_ID)

        stmt = mock_session.execute.call_args.args[0]
        compiled = stmt.compile()
        assert _RUN_ID in compiled.params.values()

    async def test_empty_sum_returns_zero(self, mock_session: AsyncMock) -> None:
        sum_res = MagicMock()
        sum_res.scalar_one.return_value = None
        mock_session.execute = AsyncMock(return_value=sum_res)

        total = await sum_pipeline_monthly_spend(mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID)

        assert total == Decimal(0)


# ---------------------------------------------------------------------------
# check_pipeline_circuit_breaker
# ---------------------------------------------------------------------------


class TestCheckPipelineCircuitBreaker:
    @pytest.mark.parametrize(
        ("cost_usd", "reason_keyword"),
        [
            (None, "none"),
            (Decimal("NaN"), "finite"),
            (Decimal("Infinity"), "finite"),
            (Decimal(-5), "non_negative"),
        ],
    )
    async def test_rejects_invalid_cost(
        self, mock_session: AsyncMock, cost_usd: Decimal | None, reason_keyword: str
    ) -> None:
        approved, reason = await check_pipeline_circuit_breaker(
            mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=cost_usd
        )

        assert approved is False
        assert reason_keyword in (reason or "").lower()
        mock_session.execute.assert_not_called()

    async def test_no_threshold_approves_without_trip(self, mock_session: AsyncMock) -> None:
        pipeline = _make_pipeline(threshold=None)
        mock_session.execute = AsyncMock(return_value=_pipeline_result(pipeline))

        approved, reason = await check_pipeline_circuit_breaker(
            mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=Decimal(100)
        )

        assert approved is True
        assert reason is None

    async def test_already_tripped_rejects_fail_closed(self, mock_session: AsyncMock) -> None:
        pipeline = _make_pipeline(threshold=Decimal(1000), tripped=True)
        mock_session.execute = AsyncMock(return_value=_pipeline_result(pipeline))

        approved, reason = await check_pipeline_circuit_breaker(
            mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=Decimal(1)
        )

        assert approved is False
        assert reason == "circuit_breaker_tripped"
        # No spend re-read, no trip re-apply — fail-closed immediate reject.
        mock_session.execute.assert_awaited_once()

    async def test_trips_when_threshold_exceeded(self, mock_session: AsyncMock) -> None:
        pipeline = _make_pipeline(threshold=Decimal(1000))
        pipeline_result = _pipeline_result(pipeline)
        monthly_result = MagicMock()
        monthly_result.scalar_one.return_value = Decimal("950.00")
        update_result = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[pipeline_result, monthly_result, update_result])
        dispatch_mock = AsyncMock()
        with patch("modulo.core.cost_controller._dispatch_circuit_breaker_tripped", new=dispatch_mock):
            approved, reason = await check_pipeline_circuit_breaker(
                mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=Decimal(100)
            )

        assert approved is False
        assert reason == "circuit_breaker_tripped"
        assert pipeline.circuit_breaker_tripped is True
        dispatch_mock.assert_awaited_once()
        # pipeline select + monthly SUM + trigger update
        assert mock_session.execute.await_count == 3

    async def test_stays_within_threshold_no_trip(self, mock_session: AsyncMock) -> None:
        pipeline = _make_pipeline(threshold=Decimal(1000))
        pipeline_result = _pipeline_result(pipeline)
        monthly_result = MagicMock()
        monthly_result.scalar_one.return_value = Decimal("900.00")
        mock_session.execute = AsyncMock(side_effect=[pipeline_result, monthly_result])

        approved, reason = await check_pipeline_circuit_breaker(
            mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=Decimal(50)
        )

        assert approved is True
        assert reason is None
        assert mock_session.execute.await_count == 2


# ---------------------------------------------------------------------------
# reset_pipeline_circuit_breaker
# ---------------------------------------------------------------------------


class TestResetPipelineCircuitBreaker:
    async def test_clears_trip_and_reactivates_triggers(self, mock_session: AsyncMock) -> None:
        pipeline = _make_pipeline(threshold=Decimal(1000), tripped=True)
        pipeline_result = _pipeline_result(pipeline)
        update_result = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[pipeline_result, update_result])

        reset = await reset_pipeline_circuit_breaker(mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID)

        assert reset is True
        assert pipeline.circuit_breaker_tripped is False
        assert pipeline.circuit_breaker_tripped_at is None
        assert mock_session.execute.await_count == 2

    async def test_missing_pipeline_returns_false(self, mock_session: AsyncMock) -> None:
        missing = MagicMock()
        missing.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=missing)

        reset = await reset_pipeline_circuit_breaker(mock_session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID)

        assert reset is False
        mock_session.execute.assert_awaited_once()
