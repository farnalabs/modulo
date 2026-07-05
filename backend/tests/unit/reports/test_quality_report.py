"""Unit tests for quality report — generation, formatting, and delivery."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.reports.quality_report import (
    _fmt_delta,
    _format_eval_breakdown,
    _format_summary_block,
    _format_trend_block,
    _format_trend_section,
    _pct_delta,
    _trend_symbol,
    deliver_quality_report,
    format_slack_message,
    generate_quality_report,
)

# ---------------------------------------------------------------------------
# _pct_delta
# ---------------------------------------------------------------------------


class TestPctDelta:
    def test_returns_none_when_previous_zero(self) -> None:
        assert _pct_delta(10.0, 0.0) is None

    def test_returns_correct_positive(self) -> None:
        assert _pct_delta(150.0, 100.0) == 50.0

    def test_returns_negative_when_current_less(self) -> None:
        assert _pct_delta(50.0, 100.0) == -50.0

    def test_returns_zero_when_equal(self) -> None:
        assert _pct_delta(100.0, 100.0) == 0.0

    def test_handles_floats_correctly(self) -> None:
        assert _pct_delta(110.0, 200.0) == -45.0


# ---------------------------------------------------------------------------
# _trend_symbol
# ---------------------------------------------------------------------------


class TestTrendSymbol:
    def test_up_arrow_when_delta_above_5(self) -> None:
        assert _trend_symbol(10.0) == "\u2191"

    def test_down_arrow_when_delta_below_neg5(self) -> None:
        assert _trend_symbol(-10.0) == "\u2193"

    def test_flat_when_delta_zero(self) -> None:
        assert _trend_symbol(0.0) == "\u2192"

    def test_flat_when_delta_none(self) -> None:
        assert _trend_symbol(None) == "\u2192"

    def test_within_threshold_returns_flat(self) -> None:
        assert _trend_symbol(3.0) == "\u2192"
        assert _trend_symbol(-3.0) == "\u2192"
        assert _trend_symbol(0.1) == "\u2192"
        assert _trend_symbol(-0.1) == "\u2192"
        assert _trend_symbol(4.9) == "\u2192"
        assert _trend_symbol(-4.9) == "\u2192"

    def test_exact_threshold_strict_returns_flat(self) -> None:
        assert _trend_symbol(5.0) == "\u2192"
        assert _trend_symbol(5.1) == "\u2191"
        assert _trend_symbol(-5.0) == "\u2192"
        assert _trend_symbol(-5.1) == "\u2193"


# ---------------------------------------------------------------------------
# _fmt_delta
# ---------------------------------------------------------------------------


class TestFmtDelta:
    def test_returns_na_when_none(self) -> None:
        assert _fmt_delta(None) == "N/A"

    def test_formatted_with_plus(self) -> None:
        assert _fmt_delta(10.0) == "+10.0%"

    def test_formatted_with_minus(self) -> None:
        assert _fmt_delta(-10.0) == "-10.0%"

    def test_formatted_zero(self) -> None:
        assert _fmt_delta(0.0) == "+0.0%"


# ---------------------------------------------------------------------------
# _format_summary_block
# ---------------------------------------------------------------------------


class TestFormatSummaryBlock:
    def test_returns_section_block_structure(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": 85.5, "total_cost_usd": 42.50}
        block = _format_summary_block(summary)
        assert block["type"] == "section"
        assert len(block["fields"]) == 3

    def test_shows_em_dash_when_pass_rate_none(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": None, "total_cost_usd": 42.50}
        block = _format_summary_block(summary)
        fields_text = [f["text"] for f in block["fields"]]
        assert any("\u2014" in t for t in fields_text)

    def test_shows_percentage_when_pass_rate_present(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": 85.5, "total_cost_usd": 42.50}
        block = _format_summary_block(summary)
        fields_text = [f["text"] for f in block["fields"]]
        assert any("85.5%" in t for t in fields_text)


# ---------------------------------------------------------------------------
# _format_eval_breakdown
# ---------------------------------------------------------------------------


class TestFormatEvalBreakdown:
    def test_returns_correct_structure(self) -> None:
        eval_bd = {
            "current_week": {"total_evals": 50, "passed_evals": 40, "pass_rate": 80.0},
            "previous_week": {"total_evals": 40, "passed_evals": 30, "pass_rate": 75.0},
        }
        block = _format_eval_breakdown(eval_bd)
        assert block["type"] == "section"
        assert "This week: 40/50" in block["text"]["text"]
        assert "Last week: 30/40" in block["text"]["text"]

    def test_handles_none_pass_rate(self) -> None:
        eval_bd = {
            "current_week": {"total_evals": 50, "passed_evals": 40, "pass_rate": None},
            "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
        }
        block = _format_eval_breakdown(eval_bd)
        assert "\u2014" in block["text"]["text"]


# ---------------------------------------------------------------------------
# _format_trend_block
# ---------------------------------------------------------------------------


class TestFormatTrendBlock:
    def test_includes_all_7_days(self) -> None:
        today = datetime.now(UTC).date()
        trend = []
        for i in range(7):
            d = today - timedelta(days=6 - i)
            trend.append({
                "date": d.isoformat(),
                "run_count": i * 10,
                "eval_pass_rate": 80.0 + i,
                "token_spend_usd": float(i * 5),
            })
        block = _format_trend_block(trend)
        assert block["type"] == "section"
        for entry in trend:
            assert entry["date"] in block["text"]["text"]

    def test_handles_none_eval_pass_rate(self) -> None:
        trend = [{"date": "2026-07-01", "run_count": 10, "eval_pass_rate": None, "token_spend_usd": 5.0}]
        block = _format_trend_block(trend)
        assert "\u2014" in block["text"]["text"]


# ---------------------------------------------------------------------------
# _format_trend_section
# ---------------------------------------------------------------------------


class TestFormatTrendSection:
    def test_shows_all_three_metrics(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0}
        wow = {
            "runs_delta_pct": 10.0,
            "eval_pass_rate_delta_pct": 5.0,
            "cost_delta_pct": -3.0,
            "previous_week_runs": 90,
            "previous_week_avg_pass_rate": 80.0,
            "previous_week_cost_usd": 51.5,
        }
        block = _format_trend_section(wow, summary)
        assert block["type"] == "section"
        text = block["text"]["text"]
        assert "*Runs*" in text
        assert "*Eval Pass Rate*" in text
        assert "*Cost*" in text

    def test_handles_none_prev_pass_rate(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0}
        wow = {
            "runs_delta_pct": 10.0,
            "eval_pass_rate_delta_pct": 5.0,
            "cost_delta_pct": -3.0,
            "previous_week_runs": 90,
            "previous_week_avg_pass_rate": None,
            "previous_week_cost_usd": 51.5,
        }
        block = _format_trend_section(wow, summary)
        assert "\u2014" in block["text"]["text"]


# ---------------------------------------------------------------------------
# format_slack_message
# ---------------------------------------------------------------------------


class TestFormatSlackMessage:
    def test_returns_valid_json(self) -> None:
        report = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0},
            "week_over_week": {
                "runs_delta_pct": 10.0,
                "eval_pass_rate_delta_pct": 5.0,
                "cost_delta_pct": -3.0,
                "previous_week_runs": 90,
                "previous_week_avg_pass_rate": 80.0,
                "previous_week_cost_usd": 51.5,
            },
            "trend": [{"date": "2026-07-01", "run_count": 10, "eval_pass_rate": 85.0, "token_spend_usd": 5.0}],
            "eval_breakdown": {
                "current_week": {"total_evals": 50, "passed_evals": 40, "pass_rate": 80.0},
                "previous_week": {"total_evals": 40, "passed_evals": 30, "pass_rate": 75.0},
            },
        }
        result = format_slack_message(report)
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    def test_contains_all_expected_block_types(self) -> None:
        report = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0},
            "week_over_week": {
                "runs_delta_pct": 10.0,
                "eval_pass_rate_delta_pct": 5.0,
                "cost_delta_pct": -3.0,
                "previous_week_runs": 90,
                "previous_week_avg_pass_rate": 80.0,
                "previous_week_cost_usd": 51.5,
            },
            "trend": [{"date": "2026-07-01", "run_count": 10, "eval_pass_rate": 85.0, "token_spend_usd": 5.0}],
            "eval_breakdown": {
                "current_week": {"total_evals": 50, "passed_evals": 40, "pass_rate": 80.0},
                "previous_week": {"total_evals": 40, "passed_evals": 30, "pass_rate": 75.0},
            },
        }
        result = format_slack_message(report)
        parsed = json.loads(result)
        types = [b["type"] for b in parsed]
        assert "header" in types
        assert "context" in types
        assert "divider" in types
        assert "section" in types

    def test_contains_weekly_quality_report_header(self) -> None:
        report = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 0, "avg_eval_pass_rate": None, "total_cost_usd": 0.0},
            "week_over_week": {
                "runs_delta_pct": None,
                "eval_pass_rate_delta_pct": None,
                "cost_delta_pct": None,
                "previous_week_runs": 0,
                "previous_week_avg_pass_rate": None,
                "previous_week_cost_usd": 0.0,
            },
            "trend": [],
            "eval_breakdown": {
                "current_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
                "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
            },
        }
        result = format_slack_message(report)
        assert "Weekly Quality Report" in result


# ---------------------------------------------------------------------------
# deliver_quality_report
# ---------------------------------------------------------------------------


class TestDeliverQualityReport:
    async def test_returns_success_for_2xx(self) -> None:
        import httpx

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        report_data = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 10, "avg_eval_pass_rate": 90.0, "total_cost_usd": 5.0},
            "week_over_week": {
                "runs_delta_pct": None,
                "eval_pass_rate_delta_pct": None,
                "cost_delta_pct": None,
                "previous_week_runs": 0,
                "previous_week_avg_pass_rate": None,
                "previous_week_cost_usd": 0.0,
            },
            "trend": [],
            "eval_breakdown": {
                "current_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
                "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
            },
        }
        recipient_config = {"webhook_urls": [url]}

        with patch.object(httpx, "AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.is_success = True
            mock_response.status_code = 200
            mock_response.text = "ok"
            mock_client.post = AsyncMock(return_value=mock_response)

            results = await deliver_quality_report(report_data, recipient_config)

        assert len(results) == 1
        assert results[0]["status"] == "delivered"
        assert results[0]["status_code"] == 200
        assert results[0]["error"] is None

    async def test_returns_failure_for_non_2xx(self) -> None:
        import httpx

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        report_data = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 10, "avg_eval_pass_rate": 90.0, "total_cost_usd": 5.0},
            "week_over_week": {
                "runs_delta_pct": None,
                "eval_pass_rate_delta_pct": None,
                "cost_delta_pct": None,
                "previous_week_runs": 0,
                "previous_week_avg_pass_rate": None,
                "previous_week_cost_usd": 0.0,
            },
            "trend": [],
            "eval_breakdown": {
                "current_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
                "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
            },
        }
        recipient_config = {"webhook_urls": [url]}

        with patch.object(httpx, "AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.is_success = False
            mock_response.status_code = 500
            mock_response.text = "Internal Server Error " + "x" * 300
            mock_client.post = AsyncMock(return_value=mock_response)

            results = await deliver_quality_report(report_data, recipient_config)

        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] == 500

    async def test_error_text_truncated_to_200_chars(self) -> None:
        import httpx

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        report_data = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 10, "avg_eval_pass_rate": 90.0, "total_cost_usd": 5.0},
            "week_over_week": {
                "runs_delta_pct": None,
                "eval_pass_rate_delta_pct": None,
                "cost_delta_pct": None,
                "previous_week_runs": 0,
                "previous_week_avg_pass_rate": None,
                "previous_week_cost_usd": 0.0,
            },
            "trend": [],
            "eval_breakdown": {
                "current_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
                "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
            },
        }
        recipient_config = {"webhook_urls": [url]}

        with patch.object(httpx, "AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.is_success = False
            mock_response.status_code = 500
            mock_response.text = "x" * 500
            mock_client.post = AsyncMock(return_value=mock_response)

            results = await deliver_quality_report(report_data, recipient_config)

        assert len(results) == 1
        assert len(results[0]["error"]) == 200

    async def test_single_url_failure_does_not_block_others(self) -> None:
        import httpx

        url1 = "https://hooks.slack.com/services/T1/B1/xxx"
        url2 = "https://hooks.slack.com/services/T1/B2/yyy"
        report_data = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 10, "avg_eval_pass_rate": 90.0, "total_cost_usd": 5.0},
            "week_over_week": {
                "runs_delta_pct": None,
                "eval_pass_rate_delta_pct": None,
                "cost_delta_pct": None,
                "previous_week_runs": 0,
                "previous_week_avg_pass_rate": None,
                "previous_week_cost_usd": 0.0,
            },
            "trend": [],
            "eval_breakdown": {
                "current_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
                "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
            },
        }
        recipient_config = {"webhook_urls": [url1, url2]}

        with patch.object(httpx, "AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            resp1 = MagicMock()
            resp1.is_success = False
            resp1.status_code = 500
            resp1.text = "fail"
            resp2 = MagicMock()
            resp2.is_success = True
            resp2.status_code = 200
            resp2.text = "ok"

            mock_client.post = AsyncMock(side_effect=[resp1, resp2])

            results = await deliver_quality_report(report_data, recipient_config)

        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert results[1]["status"] == "delivered"

    async def test_request_error_caught_per_url(self) -> None:
        import httpx

        url1 = "https://hooks.slack.com/services/T1/B1/xxx"
        url2 = "https://hooks.slack.com/services/T1/B2/yyy"
        report_data = {
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 10, "avg_eval_pass_rate": 90.0, "total_cost_usd": 5.0},
            "week_over_week": {
                "runs_delta_pct": None,
                "eval_pass_rate_delta_pct": None,
                "cost_delta_pct": None,
                "previous_week_runs": 0,
                "previous_week_avg_pass_rate": None,
                "previous_week_cost_usd": 0.0,
            },
            "trend": [],
            "eval_breakdown": {
                "current_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
                "previous_week": {"total_evals": 0, "passed_evals": 0, "pass_rate": None},
            },
        }
        recipient_config = {"webhook_urls": [url1, url2]}

        with patch.object(httpx, "AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            mock_client.post = AsyncMock(
                side_effect=[
                    httpx.RequestError("Connection refused"),
                    MagicMock(is_success=True, status_code=200, text="ok"),
                ]
            )

            results = await deliver_quality_report(report_data, recipient_config)

        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert results[0]["error"] is not None
        assert results[1]["status"] == "delivered"


# ---------------------------------------------------------------------------
# generate_quality_report
# ---------------------------------------------------------------------------


class TestGenerateQualityReport:
    def _make_session(
        self,
        daily_rows: list,
        daily_eval_rows: list,
        weekly_row: dict,
        eval_row: dict,
    ) -> AsyncMock:
        session = AsyncMock()

        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)

        def _mock_one(**cols: object) -> MagicMock:
            row = MagicMock()
            for k, v in cols.items():
                setattr(row, k, v)
            return row

        def _daily_result() -> MagicMock:
            r = MagicMock()
            r.all.return_value = daily_rows
            return r

        def _daily_eval_result() -> MagicMock:
            r = MagicMock()
            r.all.return_value = daily_eval_rows
            return r

        def _weekly_result() -> MagicMock:
            r = MagicMock()
            r.one.return_value = _mock_one(
                run_count=weekly_row.get("run_count"),
                total_spend=weekly_row.get("total_spend"),
            )
            return r

        def _eval_result() -> MagicMock:
            r = MagicMock()
            r.one.return_value = _mock_one(
                total_evals=eval_row.get("total_evals"),
                passed_evals=eval_row.get("passed_evals"),
            )
            return r

        # Execution order in generate_quality_report:
        # 1. _query_weekly_agg (current) → uses .one()
        # 2. _query_weekly_agg (previous) → uses .one()
        # 3. _query_eval_summary (current) → uses .one()
        # 4. _query_eval_summary (previous) → uses .one()
        # 5. Daily run count query → uses .all()
        # 6. Daily eval rates query → uses .all()
        session.execute = AsyncMock(
            side_effect=[
                _weekly_result(),  # current weekly
                _weekly_result(),  # previous weekly
                _eval_result(),    # current eval
                _eval_result(),    # previous eval
                _daily_result(),   # daily rows
                _daily_eval_result(),  # daily eval rows
            ]
        )
        return session

    async def test_returns_correct_structure(self) -> None:
        org_id = uuid.uuid4()
        today = datetime.now(UTC).date()
        current_start = today - timedelta(days=6)

        session = self._make_session(
            daily_rows=[
                MagicMock(run_date=current_start, run_count=10, total_spend=5.0),
            ],
            daily_eval_rows=[
                MagicMock(eval_date=current_start, total=10, passed=8),
            ],
            weekly_row={"run_count": 10, "total_spend": 5.0},
            eval_row={"total_evals": 10, "passed_evals": 8},
        )

        report = await generate_quality_report(session, org_id)

        assert "period" in report
        assert "summary" in report
        assert "week_over_week" in report
        assert "trend" in report
        assert "eval_breakdown" in report
        assert report["summary"]["total_runs"] == 10

    async def test_zero_runs_produces_runs_delta_pct_none(self) -> None:
        org_id = uuid.uuid4()
        session = self._make_session(
            daily_rows=[],
            daily_eval_rows=[],
            weekly_row={"run_count": 0, "total_spend": 0.0},
            eval_row={"total_evals": 0, "passed_evals": 0},
        )

        report = await generate_quality_report(session, org_id)

        assert report["summary"]["total_runs"] == 0
        assert report["week_over_week"]["runs_delta_pct"] is None

    async def test_zero_evals_produces_pass_rate_none(self) -> None:
        org_id = uuid.uuid4()
        session = self._make_session(
            daily_rows=[],
            daily_eval_rows=[],
            weekly_row={"run_count": 10, "total_spend": 5.0},
            eval_row={"total_evals": 0, "passed_evals": 0},
        )

        report = await generate_quality_report(session, org_id)

        assert report["summary"]["avg_eval_pass_rate"] is None
        assert report["eval_breakdown"]["current_week"]["pass_rate"] is None

    async def test_missing_dates_in_trend_produce_zero_runs_and_none_pass_rate(self) -> None:
        org_id = uuid.uuid4()
        session = self._make_session(
            daily_rows=[],  # No daily rows at all
            daily_eval_rows=[],
            weekly_row={"run_count": 0, "total_spend": 0.0},
            eval_row={"total_evals": 0, "passed_evals": 0},
        )

        report = await generate_quality_report(session, org_id)

        assert len(report["trend"]) == 7
        for entry in report["trend"]:
            assert entry["run_count"] == 0
            assert entry["eval_pass_rate"] is None

    async def test_cost_defaults_to_zero(self) -> None:
        org_id = uuid.uuid4()

        session = self._make_session(
            daily_rows=[],
            daily_eval_rows=[],
            weekly_row={"run_count": 0, "total_spend": 0.0},
            eval_row={"total_evals": 0, "passed_evals": 0},
        )

        report = await generate_quality_report(session, org_id)

        assert report["summary"]["total_cost_usd"] == 0.0
