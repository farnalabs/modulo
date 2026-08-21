"""BDD step definitions: Quality Report Webhook Delivery (signing + timeout + trend)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.reports.quality_report import _format_trend_section, deliver_quality_report

scenarios("../../features/reports/quality_report_delivery.feature")

_SCHEDULER_PATH = "modulo.core.reports.scheduler"
_DOWN_ARROW = "\u2193"
_UP_ARROW = "\u2191"


def _mock_resp() -> MagicMock:
    resp = MagicMock()
    resp.is_success = True
    resp.status_code = 200
    resp.text = "ok"
    return resp


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a quality report delivery config with a signing secret "{secret}"'))
def config_with_signing_secret(secret: str, request) -> None:
    request.node._recipient_config = {
        "webhook_urls": ["https://hooks.slack.com/services/T1/B1/xxx"],
        "signing_secret": secret,
    }
    request.node._signing_secret = secret


@given("a quality report delivery config without a signing secret")
def config_without_signing_secret(request) -> None:
    request.node._recipient_config = {"webhook_urls": ["https://hooks.slack.com/services/T1/B1/xxx"]}
    request.node._signing_secret = None


@given(parsers.parse("a quality report delivery config with a timeout of {timeout:d} seconds"))
def config_with_timeout(timeout: int, request) -> None:
    request.node._recipient_config = {
        "webhook_urls": ["https://hooks.slack.com/services/T1/B1/xxx"],
        "timeout": float(timeout),
    }


@given(parsers.parse("a weekly report where cost increased by {pct:d} percent"))
def report_cost_increased(pct: int, request) -> None:
    _store_report(request, cost_delta_pct=float(pct))


@given(parsers.parse("a weekly report where cost decreased by {pct:d} percent"))
def report_cost_decreased(pct: int, request) -> None:
    _store_report(request, cost_delta_pct=-float(pct))


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("I deliver the quality report")
def deliver_report(request) -> None:
    report = {
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
    recipient_config: dict[str, Any] = request.node._recipient_config

    with (
        patch(f"{_SCHEDULER_PATH}._REPORT_MAX_RETRIES", 1),
        patch.object(httpx, "AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock(return_value=_mock_resp())

        asyncio.run(deliver_quality_report(report, recipient_config))

        request.node._delivery_client = mock_client
        request.node._delivery_client_cls = mock_client_cls


@when("I format the weekly trend section")
def format_trend_section(request) -> None:
    summary = {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0}
    wow = {
        "runs_delta_pct": 10.0,
        "eval_pass_rate_delta_pct": 5.0,
        "cost_delta_pct": request.node._cost_delta_pct,
        "previous_week_runs": 90,
        "previous_week_avg_pass_rate": 80.0,
        "previous_week_cost_usd": 51.5,
    }
    request.node._trend_block = _format_trend_section(wow, summary)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the delivery request is sent as raw JSON bytes")
def delivery_sent_as_bytes(request) -> None:
    call = request.node._delivery_client.post.await_args
    assert call is not None
    assert "content" in call.kwargs, "expected raw-bytes payload (content=), got json= instead"
    assert "json" not in call.kwargs


@then("the delivery request is sent via the json argument")
def delivery_sent_via_json(request) -> None:
    call = request.node._delivery_client.post.await_args
    assert call is not None
    assert "json" in call.kwargs, "expected json= payload for unsigned delivery"


@then("the delivery request includes an X-Modulo-Signature header")
def delivery_has_signature_header(request) -> None:
    call = request.node._delivery_client.post.await_args
    assert call is not None
    assert "X-Modulo-Signature" in call.kwargs["headers"]


@then("the delivery request does not include an X-Modulo-Signature header")
def delivery_has_no_signature_header(request) -> None:
    call = request.node._delivery_client.post.await_args
    assert call is not None
    assert "X-Modulo-Signature" not in call.kwargs["headers"]


@then(parsers.parse('the signature matches an HMAC-SHA256 of the sent bytes computed with "{secret}"'))
def delivery_signature_matches(secret: str, request) -> None:
    from modulo.core.reports.scheduler import _serialize_json_body, _sign_payload

    call = request.node._delivery_client.post.await_args
    assert call is not None
    body_bytes = call.kwargs["content"]
    assert body_bytes == _serialize_json_body(json.loads(body_bytes))
    expected = _sign_payload(secret, body_bytes)
    assert call.kwargs["headers"]["X-Modulo-Signature"] == expected


@then(parsers.parse("the delivery client used a timeout of {timeout:d} seconds"))
def delivery_timeout_used(timeout: int, request) -> None:
    client_cls = request.node._delivery_client_cls
    assert client_cls.call_args.kwargs["timeout"] == float(timeout)


@then("the cost line starts with the down arrow")
def cost_line_down_arrow(request) -> None:
    cost_line = _cost_line(request.node._trend_block)
    assert cost_line.startswith(_DOWN_ARROW), f"expected DOWN arrow, got: {cost_line}"


@then("the cost line starts with the up arrow")
def cost_line_up_arrow(request) -> None:
    cost_line = _cost_line(request.node._trend_block)
    assert cost_line.startswith(_UP_ARROW), f"expected UP arrow, got: {cost_line}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_report(request, cost_delta_pct: float) -> None:
    request.node._cost_delta_pct = cost_delta_pct


def _cost_line(block: dict[str, Any]) -> str:
    text = block["text"]["text"]
    return next(line for line in text.split("\n") if "*Cost*" in line)
