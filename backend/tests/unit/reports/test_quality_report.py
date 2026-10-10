"""Unit tests for quality report — generation, formatting, and delivery."""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import operators

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
from modulo.db.models.base import Base
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from tests.unit.reports.helpers import (
    SLACK_URL,
    SLACK_URL_2,
    has_predicate,
    make_http_response,
    patched_quality_delivery,
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
        # Exact block order is a construction detail, not a contract: the
        # builder assembles a fixed literal list. What callers can rely on is
        # the composition of the report and that the header leads it.
        assert collections.Counter(types) == collections.Counter(
            {"header": 1, "context": 2, "divider": 4, "section": 4}
        )
        assert types[0] == "header"


# ---------------------------------------------------------------------------
# deliver_quality_report
# ---------------------------------------------------------------------------


class TestWebhookSigning:
    def test_serialize_json_body_is_byte_stable(self) -> None:
        from modulo.core.reports.scheduler import _serialize_json_body

        body = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]}
        assert _serialize_json_body(body) == b'{"blocks":[{"text":{"text":"hi","type":"mrkdwn"},"type":"section"}]}'

    def test_sign_payload_matches_known_vector(self) -> None:
        from modulo.core.reports.scheduler import _sign_payload

        secret = "secret-key"
        body = b'{"a":1}'
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        assert _sign_payload(secret, body) == f"sha256={expected}"

    def test_sign_payload_accepts_non_string_secret(self) -> None:
        from modulo.core.reports.scheduler import _sign_payload

        body = b'{"a":1}'
        expected = hmac.new(str(b"secret-key").encode("utf-8"), body, hashlib.sha256).hexdigest()
        assert _sign_payload(b"secret-key", body) == f"sha256={expected}"


class TestDeliverQualityReport:
    def _client(self, **post_kwargs: object) -> AsyncMock:
        client = AsyncMock()
        client.post = AsyncMock(**post_kwargs)
        return client

    async def test_returns_success_for_2xx(self) -> None:
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url]}

        with patched_quality_delivery(self._client(return_value=make_http_response())):
            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 1
        assert results[0]["status"] == "delivered"
        assert results[0]["status_code"] == 200
        assert results[0]["error"] is None

    async def test_accepts_preformatted_json_string(self) -> None:
        """The scheduler always hands ``deliver_quality_report`` the formatter's
        JSON *string*, so that production branch must be exercised, not just the
        raw-dict convenience path."""
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url]}
        preformatted = format_slack_message(_REPORT_DELIVERY)
        mock_client = self._client(return_value=make_http_response())

        with patched_quality_delivery(mock_client):
            results = await deliver_quality_report(preformatted, recipient_config)

        assert results[0]["status"] == "delivered"
        assert mock_client.post.await_args.kwargs["json"] == {"blocks": json.loads(preformatted)}

    async def test_rejects_malformed_json_string(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            await deliver_quality_report("not json", {"webhook_urls": [SLACK_URL]})

    async def test_returns_failure_for_non_2xx_after_exhaustion(self) -> None:
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url]}

        with patched_quality_delivery(
            self._client(return_value=make_http_response(is_success=False, status_code=500, text="error"))
        ):
            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] == 500

    async def test_error_text_truncated_to_200_chars(self) -> None:
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url]}

        with patched_quality_delivery(
            self._client(return_value=make_http_response(is_success=False, status_code=500, text="x" * 500))
        ):
            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 1
        assert len(results[0]["error"]) == 200

    async def test_single_url_failure_does_not_block_others(self) -> None:
        url1 = SLACK_URL
        url2 = SLACK_URL_2
        recipient_config = {"webhook_urls": [url1, url2]}

        with patched_quality_delivery(
            self._client(
                side_effect=[
                    make_http_response(is_success=False, status_code=500, text="fail"),
                    make_http_response(),
                ]
            )
        ):
            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert results[1]["status"] == "delivered"

    async def test_request_error_caught_per_url(self) -> None:
        url1 = SLACK_URL
        url2 = SLACK_URL_2
        recipient_config = {"webhook_urls": [url1, url2]}

        with patched_quality_delivery(
            self._client(
                side_effect=[
                    httpx.RequestError("Connection refused"),
                    make_http_response(),
                ]
            )
        ):
            results = await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert results[0]["error"] is not None
        assert results[1]["status"] == "delivered"

    # --- HMAC-SHA256 webhook signing (PRD 8.11) ---

    async def test_signed_delivery_sends_signature_header_and_bytes(self) -> None:
        from modulo.core.reports.scheduler import _serialize_json_body, _sign_payload

        url = SLACK_URL
        secret = "super-secret"
        recipient_config = {"webhook_urls": [url], "signing_secret": secret}
        mock_client = self._client(return_value=make_http_response())

        with patched_quality_delivery(mock_client):
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
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url]}
        mock_client = self._client(return_value=make_http_response())

        with patched_quality_delivery(mock_client):
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        kwargs = call.kwargs
        assert "json" in kwargs
        assert "content" not in kwargs
        assert "X-Modulo-Signature" not in kwargs["headers"]

    async def test_signature_is_verifiable_from_raw_body(self) -> None:
        url = SLACK_URL
        secret = "verify-me"
        recipient_config = {"webhook_urls": [url], "signing_secret": secret}
        mock_client = self._client(return_value=make_http_response())

        with patched_quality_delivery(mock_client):
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        kwargs = call.kwargs
        received_signature = kwargs["headers"]["X-Modulo-Signature"]
        # Recipient recomputes the signature over the exact bytes they received.
        recomputed = hmac.new(secret.encode("utf-8"), kwargs["content"], hashlib.sha256).hexdigest()
        assert received_signature == f"sha256={recomputed}"

    async def test_empty_signing_secret_treated_as_unsigned(self) -> None:
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url], "signing_secret": ""}
        mock_client = self._client(return_value=make_http_response())

        with patched_quality_delivery(mock_client):
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        call = mock_client.post.await_args
        assert call is not None
        assert "X-Modulo-Signature" not in call.kwargs["headers"]
        assert "json" in call.kwargs

    # --- Configurable delivery timeout ---

    async def test_custom_timeout_used(self) -> None:
        url = SLACK_URL
        recipient_config = {"webhook_urls": [url], "timeout": 5.0}

        with patched_quality_delivery(self._client(return_value=make_http_response())) as client_cls:
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert client_cls.call_args.kwargs["timeout"] == 5.0

    async def test_default_timeout_used_when_absent(self) -> None:
        from modulo.core.reports.scheduler import _REPORT_HTTP_TIMEOUT

        url = SLACK_URL
        recipient_config = {"webhook_urls": [url]}

        with patched_quality_delivery(self._client(return_value=make_http_response())) as client_cls:
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT

    async def test_invalid_timeout_falls_back_to_default(self) -> None:
        from modulo.core.reports.scheduler import _REPORT_HTTP_TIMEOUT

        url = SLACK_URL
        recipient_config = {"webhook_urls": [url], "timeout": "abc"}

        with patched_quality_delivery(self._client(return_value=make_http_response())) as client_cls:
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT

    async def test_zero_timeout_falls_back_to_default(self) -> None:
        from modulo.core.reports.scheduler import _REPORT_HTTP_TIMEOUT

        url = SLACK_URL
        recipient_config = {"webhook_urls": [url], "timeout": 0}

        with patched_quality_delivery(self._client(return_value=make_http_response())) as client_cls:
            await deliver_quality_report(_REPORT_DELIVERY, recipient_config)

        assert client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT


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
    ) -> SimpleNamespace:
        """Build a session whose six ``execute`` calls mirror the production
        read order in ``generate_quality_report``:

        1/2. ``_query_weekly_agg`` (current, then previous) — reads
             ``row.run_count`` / ``row.total_spend`` via ``.one()``
        3/4. ``_query_eval_summary`` (current, then previous) — reads
             ``row.total_evals`` / ``row.passed_evals`` via ``.one()``
        5.   daily run counts — reads ``row.run_date`` / ``run_count`` /
             ``total_spend`` via ``.all()``
        6.   daily eval rates — reads ``row.eval_date`` / ``total`` / ``passed``
             via ``.all()``

        Rows are ``SimpleNamespace`` rather than a bare ``MagicMock`` so that a
        renamed production attribute raises ``AttributeError`` loudly instead
        of silently reading ``MagicMock.auto-spec`` style defaults.
        """
        weekly = SimpleNamespace(**weekly_row)
        evals = SimpleNamespace(**eval_row)
        daily = [SimpleNamespace(**row) for row in daily_rows]
        daily_eval = [SimpleNamespace(**row) for row in daily_eval_rows]
        # Production read order: weekly .one() x2, eval .one() x2, then the
        # two .all() queries (daily runs, daily eval rates).
        one_results: list[MagicMock] = [
            MagicMock(one=lambda: weekly),
            MagicMock(one=lambda: weekly),
            MagicMock(one=lambda: evals),
            MagicMock(one=lambda: evals),
        ]
        calls: list[object] = []

        async def execute(stmt: object) -> MagicMock:
            calls.append(stmt)
            if one_results:
                return one_results.pop(0)
            if len(calls) == 5:
                return MagicMock(all=lambda: daily)
            if len(calls) == 6:
                return MagicMock(all=lambda: daily_eval)
            raise AssertionError(f"unexpected extra execute() call #{len(calls)}")

        return SimpleNamespace(execute=execute)

    async def test_returns_correct_structure(self) -> None:
        org_id = uuid.uuid4()
        today = datetime.now(UTC).date()
        current_start = today - timedelta(days=6)

        session = self._make_session(
            daily_rows=[
                {"run_date": current_start, "run_count": 10, "total_spend": 5.0},
            ],
            daily_eval_rows=[
                {"eval_date": current_start, "total": 10, "passed": 8},
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


# ---------------------------------------------------------------------------
# generate_quality_report — SQL predicate structure
# ---------------------------------------------------------------------------


class TestQualityReportSqlPredicates:
    async def test_each_statement_carries_its_scoping_predicates(self) -> None:
        """Pin each statement's load-bearing predicates individually.

        Both weekly roll-ups must exclude team-scoped rows (``team_id IS
        NULL``); both eval summaries and the daily eval rates must exclude
        guardrail results (``eval_id NOT IN (...)``); and every statement must
        be tenant-scoped to the report's org. Asserting per recorded statement
        (not pooled across all six) means a filter dropped from one query
        cannot be masked by another query that still carries it. Dropping any
        of these would silently change every reported figure."""
        org_id = uuid.uuid4()
        statements: list[object] = []

        def _one_result(**cols: object) -> MagicMock:
            result = MagicMock()
            one_row = SimpleNamespace(**cols)
            result.one = lambda: one_row
            return result

        def _all_result(rows: list) -> MagicMock:
            result = MagicMock()
            result.all.return_value = rows
            return result

        queue = [
            _one_result(run_count=0, total_spend=0.0),
            _one_result(run_count=0, total_spend=0.0),
            _one_result(total_evals=0, passed_evals=0),
            _one_result(total_evals=0, passed_evals=0),
            _all_result([]),
            _all_result([]),
        ]

        session = AsyncMock()

        async def _record(stmt: object) -> MagicMock:
            statements.append(stmt)
            return queue.pop(0)

        session.execute = AsyncMock(side_effect=_record)

        await generate_quality_report(session, org_id)

        # Verified execution order in generate_quality_report:
        #   0/1 current/previous weekly roll-up (_query_weekly_agg)
        #   2/3 current/previous eval summary (_query_eval_summary)
        #   4   daily run counts
        #   5   daily eval rates (_query_daily_eval_rates)
        assert len(statements) == 6

        # Both weekly roll-ups exclude team-scoped rows.
        assert has_predicate(statements[0].whereclause, operators.is_, "team_id")
        assert has_predicate(statements[1].whereclause, operators.is_, "team_id")

        # The daily run-count query is org-level scoped too — without the
        # team_id filter it sums the org row PLUS every team row, and trend[]
        # double-counts against the org-level-filtered summary.
        assert has_predicate(statements[4].whereclause, operators.is_, "team_id")

        # Both eval summaries and the daily eval rates exclude guardrail results.
        for statement in (statements[2], statements[3], statements[5]):
            assert has_predicate(statement.whereclause, operators.not_in_op, "eval_id")

        # Every statement is tenant-scoped to the requested organisation.
        for statement in statements:
            assert has_predicate(statement.whereclause, operators.eq, "organisation_id", org_id)


# ---------------------------------------------------------------------------
# generate_quality_report — daily trend org-level scoping (real DB)
# ---------------------------------------------------------------------------


class TestDailyTrendOrgLevelScope:
    """trend[] must sum org-level ledger rows only (``team_id IS NULL``).

    ``check_and_record_spend`` writes the org row AND a team row for a
    team-owned run, so the org row already includes team runs. A daily query
    without the ``team_id IS NULL`` filter sums both and double-counts,
    making trend[] disagree with the report's own summary (which is filtered
    via ``_query_weekly_agg``). These tests run against a real in-memory
    SQLite DB — the double-count is in the SQL SUM, so a mocked session
    cannot demonstrate it.
    """

    async def test_trend_excludes_team_rows_and_matches_summary(self) -> None:
        eng = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with eng.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(
                    sync_conn,
                    tables=[
                        OrgDailyRunCount.__table__,
                        EvalResult.__table__,
                        EvalDefinition.__table__,
                    ],
                )
            )
        try:
            maker = async_sessionmaker(eng, expire_on_commit=False)
            org_id = uuid.uuid4()
            today = datetime.now(UTC).date()
            async with maker() as session, session.begin():
                # Org-level row (team_id IS NULL): 10 runs, $5.00.
                session.add(
                    OrgDailyRunCount(
                        organisation_id=org_id,
                        team_id=None,
                        run_date=today,
                        run_count=10,
                        total_spend_usd=Decimal("5.00"),
                    )
                )
                # Team-scoped row for the SAME date: 4 runs, $2.00.
                session.add(
                    OrgDailyRunCount(
                        organisation_id=org_id,
                        team_id=uuid.uuid4(),
                        run_date=today,
                        run_count=4,
                        total_spend_usd=Decimal("2.00"),
                    )
                )
            async with maker() as session:
                report = await generate_quality_report(session, org_id)
        finally:
            await eng.dispose()

        entry = next(e for e in report["trend"] if e["date"] == today.isoformat())
        # Org-level values only — NOT the org+team sum (14 runs / $7.00).
        assert entry["run_count"] == 10
        assert entry["token_spend_usd"] == 5.0
        # Trend agrees with the summary (both org-level scoped).
        assert report["summary"]["total_runs"] == 10
        assert report["summary"]["total_cost_usd"] == 5.0
        assert entry["run_count"] == report["summary"]["total_runs"]
        assert entry["token_spend_usd"] == report["summary"]["total_cost_usd"]
