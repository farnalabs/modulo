"""Unit tests for quality report — generation, formatting, and delivery."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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
# Shared fixtures
# ---------------------------------------------------------------------------

_REPORT_WITH_DATA = {
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

_REPORT_EMPTY = {
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

_REPORT_DELIVERY = {
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


def _mock_resp(is_success: bool = True, status_code: int = 200, text: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.is_success = is_success
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# _pct_delta
# ---------------------------------------------------------------------------


class TestPctDelta:
    @pytest.mark.parametrize(
        ("current", "previous", "expected"),
        [
            (10.0, 0.0, None),
            (150.0, 100.0, 50.0),
            (50.0, 100.0, -50.0),
            (100.0, 100.0, 0.0),
            (110.0, 200.0, -45.0),
        ],
    )
    def test_pct_delta(self, current: float, previous: float, expected: float | None) -> None:
        assert _pct_delta(current, previous) == expected


# ---------------------------------------------------------------------------
# _trend_symbol
# ---------------------------------------------------------------------------


class TestTrendSymbol:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (10.0, "\u2191"),
            (-10.0, "\u2193"),
            (0.0, "\u2192"),
            (None, "\u2192"),
        ],
    )
    def test_trend_symbol(self, delta: float | None, expected: str) -> None:
        assert _trend_symbol(delta) == expected

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

    def test_invert_flips_arrows_for_lower_is_better_metrics(self) -> None:
        # With invert=True a negative delta (improvement) renders an up arrow.
        assert _trend_symbol(-10.0, invert=True) == "\u2191"
        assert _trend_symbol(10.0, invert=True) == "\u2193"

    def test_invert_preserves_threshold_and_none(self) -> None:
        assert _trend_symbol(3.0, invert=True) == "\u2192"
        assert _trend_symbol(-3.0, invert=True) == "\u2192"
        assert _trend_symbol(5.0, invert=True) == "\u2192"
        assert _trend_symbol(-5.0, invert=True) == "\u2192"
        assert _trend_symbol(None, invert=True) == "\u2192"

    def test_invert_default_off(self) -> None:
        assert _trend_symbol(10.0) == "\u2191"
        assert _trend_symbol(-10.0) == "\u2193"


# ---------------------------------------------------------------------------
# _fmt_delta
# ---------------------------------------------------------------------------


class TestFmtDelta:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (None, "N/A"),
            (10.0, "+10.0%"),
            (-10.0, "-10.0%"),
            (0.0, "+0.0%"),
        ],
    )
    def test_fmt_delta(self, delta: float | None, expected: str) -> None:
        assert _fmt_delta(delta) == expected


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
            trend.append(
                {
                    "date": d.isoformat(),
                    "run_count": i * 10,
                    "eval_pass_rate": 80.0 + i,
                    "token_spend_usd": float(i * 5),
                }
            )
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

    def test_cost_line_uses_inverted_trend_semantics(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0}
        wow = {
            "runs_delta_pct": -10.0,
            "eval_pass_rate_delta_pct": 5.0,
            "cost_delta_pct": 12.0,
            "previous_week_runs": 90,
            "previous_week_avg_pass_rate": 80.0,
            "previous_week_cost_usd": 44.6,
        }
        block = _format_trend_section(wow, summary)
        text = block["text"]["text"]
        cost_line = next(line for line in text.split("\n") if "*Cost*" in line)
        assert cost_line.startswith("\u2193 *Cost*"), f"cost increase should render DOWN arrow: {cost_line}"

    def test_cost_decrease_renders_up_arrow(self) -> None:
        summary = {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0}
        wow = {
            "runs_delta_pct": 10.0,
            "eval_pass_rate_delta_pct": 5.0,
            "cost_delta_pct": -12.0,
            "previous_week_runs": 90,
            "previous_week_avg_pass_rate": 80.0,
            "previous_week_cost_usd": 56.8,
        }
        block = _format_trend_section(wow, summary)
        text = block["text"]["text"]
        cost_line = next(line for line in text.split("\n") if "*Cost*" in line)
        assert cost_line.startswith("\u2191 *Cost*"), f"cost decrease should render UP arrow: {cost_line}"


# ---------------------------------------------------------------------------
# format_slack_message
# ---------------------------------------------------------------------------


class TestFormatSlackMessage:
    def test_returns_valid_json(self) -> None:
        result = format_slack_message(_REPORT_WITH_DATA)
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    def test_contains_all_expected_block_types(self) -> None:
        result = format_slack_message(_REPORT_WITH_DATA)
        parsed = json.loads(result)
        types = [b["type"] for b in parsed]
        assert "header" in types
        assert "context" in types
        assert "divider" in types
        assert "section" in types

    def test_contains_weekly_quality_report_header(self) -> None:
        result = format_slack_message(_REPORT_EMPTY)
        assert "Weekly Quality Report" in result


# ---------------------------------------------------------------------------
# format_slack_message — Slack Block Kit schema compliance
# ---------------------------------------------------------------------------


class TestSlackBlockKitSchema:
    _MAX_BLOCKS = 50
    _MAX_HEADER_TEXT = 150
    _MAX_SECTION_TEXT = 3000
    _MAX_SECTION_FIELDS = 10
    _MAX_FIELD_TEXT = 2000
    _MAX_CONTEXT_ELEMENTS = 10
    _MAX_ELEMENT_TEXT = 3000

    def _blocks(self, report: dict) -> list[dict]:
        return json.loads(format_slack_message(report))

    def test_block_count_within_limit(self) -> None:
        for report in (_REPORT_WITH_DATA, _REPORT_EMPTY, _REPORT_DELIVERY):
            blocks = self._blocks(report)
            assert len(blocks) <= self._MAX_BLOCKS, f"block count {len(blocks)} exceeds {self._MAX_BLOCKS}"

    def test_blocks_are_valid_types(self) -> None:
        allowed = {"section", "divider", "context", "header"}
        for report in (_REPORT_WITH_DATA, _REPORT_EMPTY):
            for block in self._blocks(report):
                assert block["type"] in allowed, f"unexpected block type {block['type']}"

    def test_header_text_within_limit(self) -> None:
        for report in (_REPORT_WITH_DATA, _REPORT_EMPTY):
            for block in self._blocks(report):
                if block["type"] == "header":
                    assert block["text"]["type"] == "plain_text"
                    text = block["text"]["text"]
                    assert len(text) <= self._MAX_HEADER_TEXT, f"header too long ({len(text)} chars)"

    def test_section_text_and_fields_within_limits(self) -> None:
        for report in (_REPORT_WITH_DATA, _REPORT_EMPTY):
            for block in self._blocks(report):
                if block["type"] != "section":
                    continue
                text = block.get("text", {}).get("text", "")
                assert len(text) <= self._MAX_SECTION_TEXT, f"section text too long ({len(text)} chars)"
                fields = block.get("fields", [])
                assert len(fields) <= self._MAX_SECTION_FIELDS, f"too many fields: {len(fields)}"
                for field in fields:
                    assert len(field.get("text", "")) <= self._MAX_FIELD_TEXT, "section field too long"

    def _assert_context_block_within_limits(self, block: dict) -> None:
        elements = block.get("elements", [])
        assert len(elements) <= self._MAX_CONTEXT_ELEMENTS, "too many context elements"
        for element in elements:
            assert element["type"] in {"mrkdwn", "plain_text"}
            assert len(element.get("text", "")) <= self._MAX_ELEMENT_TEXT, "context element too long"

    def test_context_elements_within_limits(self) -> None:
        for report in (_REPORT_WITH_DATA, _REPORT_EMPTY):
            context_blocks = [b for b in self._blocks(report) if b["type"] == "context"]
            assert context_blocks, "report must contain at least one context block"
            for block in context_blocks:
                self._assert_context_block_within_limits(block)

    def test_context_block_with_too_many_elements_fails(self) -> None:
        block = {"type": "context", "elements": [{"type": "mrkdwn", "text": "x"}] * (self._MAX_CONTEXT_ELEMENTS + 1)}
        with pytest.raises(AssertionError, match="too many context elements"):
            self._assert_context_block_within_limits(block)

    def test_context_block_with_overlength_element_fails(self) -> None:
        block = {"type": "context", "elements": [{"type": "mrkdwn", "text": "x" * (self._MAX_ELEMENT_TEXT + 1)}]}
        with pytest.raises(AssertionError, match="context element too long"):
            self._assert_context_block_within_limits(block)

    def test_expected_structure_for_populated_report(self) -> None:
        blocks = self._blocks(_REPORT_WITH_DATA)
        types = [b["type"] for b in blocks]
        assert types == [
            "header",
            "context",
            "divider",
            "section",
            "divider",
            "section",
            "divider",
            "section",
            "divider",
            "section",
            "context",
        ]

    def test_slack_payload_wrapper_round_trip(self) -> None:
        blocks = self._blocks(_REPORT_WITH_DATA)
        payload = {"blocks": blocks}
        assert json.dumps(payload) == json.dumps(json.loads(json.dumps(payload)))


# ---------------------------------------------------------------------------
# deliver_quality_report
# ---------------------------------------------------------------------------

# Patch _REPORT_MAX_RETRIES down to 1 to avoid retry delays in tests
_SCHEDULER_PATH = "modulo.core.reports.scheduler"


class TestWebhookSigning:
    def test_serialize_json_body_is_byte_stable(self) -> None:
        from modulo.core.reports.scheduler import _serialize_json_body

        body = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]}
        assert _serialize_json_body(body) == b'{"blocks":[{"text":{"text":"hi","type":"mrkdwn"},"type":"section"}]}'

    def test_sign_payload_matches_known_vector(self) -> None:
        import hashlib
        import hmac

        from modulo.core.reports.scheduler import _sign_payload

        secret = "secret-key"
        body = b'{"a":1}'
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        assert _sign_payload(secret, body) == f"sha256={expected}"

    def test_sign_payload_accepts_non_string_secret(self) -> None:
        import hashlib
        import hmac

        from modulo.core.reports.scheduler import _sign_payload

        body = b'{"a":1}'
        expected = hmac.new(str(b"secret-key").encode("utf-8"), body, hashlib.sha256).hexdigest()
        assert _sign_payload(b"secret-key", body) == f"sha256={expected}"


class TestDeliverQualityReport:
    async def test_returns_success_for_2xx(self) -> None:
        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 1
        assert results[0]["status"] == "delivered"
        assert results[0]["status_code"] == 200
        assert results[0]["error"] is None

    async def test_returns_failure_for_non_2xx_after_exhaustion(self) -> None:
        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp(is_success=False, status_code=500, text="error"))

            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] == 500

    async def test_error_text_truncated_to_200_chars(self) -> None:
        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp(is_success=False, status_code=500, text="x" * 500))

            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 1
        assert len(results[0]["error"]) == 200

    async def test_single_url_failure_does_not_block_others(self) -> None:
        url1 = "https://hooks.slack.com/services/T1/B1/xxx"
        url2 = "https://hooks.slack.com/services/T1/B2/yyy"
        recipient_config = {"webhook_urls": [url1, url2]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(
                side_effect=[
                    _mock_resp(is_success=False, status_code=500, text="fail"),
                    _mock_resp(),
                ]
            )

            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert results[1]["status"] == "delivered"

    async def test_request_error_caught_per_url(self) -> None:
        url1 = "https://hooks.slack.com/services/T1/B1/xxx"
        url2 = "https://hooks.slack.com/services/T1/B2/yyy"
        recipient_config = {"webhook_urls": [url1, url2]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(
                side_effect=[
                    httpx.RequestError("Connection refused"),
                    _mock_resp(),
                ]
            )

            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert results[0]["error"] is not None
        assert results[1]["status"] == "delivered"

    # --- HMAC-SHA256 webhook signing (PRD 8.11) ---

    async def test_signed_delivery_sends_signature_header_and_bytes(self) -> None:
        from modulo.core.reports.scheduler import _serialize_json_body, _sign_payload

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        secret = "super-secret"
        recipient_config = {"webhook_urls": [url], "signing_secret": secret}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        kwargs = call.kwargs
        assert "content" in kwargs, "signed payload must be sent as raw bytes"
        assert "json" not in kwargs, "signed payload must not be sent via json= (bytes would differ)"
        assert kwargs["headers"].get("Content-Type") == "application/json"

        expected_blocks = {"blocks": json.loads(format_slack_message(_REPORT_DELIVERY))}
        body_bytes = _serialize_json_body(expected_blocks)
        assert kwargs["content"] == body_bytes, "sent bytes must match signed bytes exactly"

        expected_sig = _sign_payload(secret, body_bytes)
        assert kwargs["headers"]["X-Modulo-Signature"] == expected_sig

    async def test_unsigned_delivery_sends_json_without_signature(self) -> None:
        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        kwargs = call.kwargs
        assert "json" in kwargs
        assert "content" not in kwargs
        assert "X-Modulo-Signature" not in kwargs["headers"]

    async def test_signature_is_verifiable_from_raw_body(self) -> None:
        import hashlib
        import hmac

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        secret = "verify-me"
        recipient_config = {"webhook_urls": [url], "signing_secret": secret}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        kwargs = call.kwargs
        received_signature = kwargs["headers"]["X-Modulo-Signature"]
        # Recipient recomputes the signature over the exact bytes they received.
        recomputed = hmac.new(secret.encode("utf-8"), kwargs["content"], hashlib.sha256).hexdigest()
        assert received_signature == f"sha256={recomputed}"

    async def test_empty_signing_secret_treated_as_unsigned(self) -> None:
        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url], "signing_secret": ""}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        assert "X-Modulo-Signature" not in call.kwargs["headers"]
        assert "json" in call.kwargs

    # --- Configurable delivery timeout ---

    async def test_custom_timeout_used(self) -> None:
        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url], "timeout": 5.0}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert mock_client_cls.call_args.kwargs["timeout"] == 5.0

    async def test_default_timeout_used_when_absent(self) -> None:
        from modulo.core.reports.scheduler import _REPORT_HTTP_TIMEOUT

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url]}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert mock_client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT

    async def test_invalid_timeout_falls_back_to_default(self) -> None:
        from modulo.core.reports.scheduler import _REPORT_HTTP_TIMEOUT

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url], "timeout": "abc"}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert mock_client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT

    async def test_zero_timeout_falls_back_to_default(self) -> None:
        from modulo.core.reports.scheduler import _REPORT_HTTP_TIMEOUT

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        recipient_config = {"webhook_urls": [url], "timeout": 0}

        with (
            patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
            patch.object(httpx, "AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_mock_resp())

            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert mock_client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT


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
            return MagicMock(**dict(cols.items()))

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
        # 1. _query_weekly_agg (current) -> uses .one()
        # 2. _query_weekly_agg (previous) -> uses .one()
        # 3. _query_eval_summary (current) -> uses .one()
        # 4. _query_eval_summary (previous) -> uses .one()
        # 5. Daily run count query -> uses .all()
        # 6. Daily eval rates query -> uses .all()
        session.execute = AsyncMock(
            side_effect=[
                _weekly_result(),  # current weekly
                _weekly_result(),  # previous weekly
                _eval_result(),  # current eval
                _eval_result(),  # previous eval
                _daily_result(),  # daily rows
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
            daily_rows=[],
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

    async def test_propagates_sqlalchemy_errors(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        org_id = uuid.uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))

        with pytest.raises(SQLAlchemyError, match="db down"):
            await generate_quality_report(session, org_id)

    async def test_propagates_unexpected_errors(self) -> None:
        org_id = uuid.uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await generate_quality_report(session, org_id)

    async def test_reraises_cancelled_error(self) -> None:
        org_id = uuid.uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await generate_quality_report(session, org_id)
