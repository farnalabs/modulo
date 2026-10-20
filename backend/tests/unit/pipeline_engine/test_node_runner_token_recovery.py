"""FAR-1994 — recover agent-reported token usage from retained raw output.

A ``sandbox_agent`` node that produces no parseable ``output.json`` raises
``SandboxNodeFailedError`` and records ZERO token usage, even though the agent
demonstrably burned tokens. The retained raw output (the FULL retained blob up
to the node's retention cap — never the 4,000-char display slice) carries the
opencode JSONL ``step_finish`` events with the real counters. These tests pin the
recovery, its fail-open behaviour, its bounded work, and its provenance class
(agent-reported, NOT server-measured).
"""

from __future__ import annotations

import json
import time
from typing import Any

from modulo.core.pipeline_engine.node_runner import (
    RECOVERED_TOKEN_USAGE_ATTR,
    RECOVERED_TOKEN_USAGE_PROVENANCE,
    SandboxNodeFailedError,
    _attach_recovered_token_usage,
    _recover_token_usage_from_raw_output,
    merge_recovered_token_usage,
    recovered_reported_token_usage,
)


def _step_finish(
    *,
    total: int,
    input_tokens: int,
    output_tokens: int,
    cache_read: int | None = None,
    cache_write: int | None = None,
    cost: float = 0.01,
) -> dict[str, Any]:
    """One opencode ``step_finish`` JSONL event, shaped as the CLI emits it."""
    tokens: dict[str, Any] = {"total": total, "input": input_tokens, "output": output_tokens, "reasoning": 0}
    if cache_read is not None or cache_write is not None:
        tokens["cache"] = {"read": cache_read or 0, "write": cache_write or 0}
    return {"type": "step_finish", "part": {"cost": cost}, "tokens": tokens, "cost": cost}


def _blob(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_sums_tokens_across_every_step_finish_event() -> None:
    """Two events are SUMMED — proving the recovery is not last-event-only."""
    raw = _blob(
        [
            {"type": "step_start", "part": {}},
            _step_finish(total=100, input_tokens=60, output_tokens=40, cache_read=15, cache_write=5),
            {"type": "text", "part": {"text": "working"}},
            _step_finish(total=50, input_tokens=30, output_tokens=20, cache_read=9, cache_write=1),
        ]
    )

    usage = _recover_token_usage_from_raw_output(raw)

    assert usage == {
        "input": 90,
        "output": 60,
        "total": 150,
        "cache_read": 24,
        "cache_write": 6,
    }


def test_no_step_finish_event_returns_empty_usage_without_raising() -> None:
    raw = "\n".join(json.dumps({"type": event_type, "part": {}}) for event_type in ("step_start", "text", "done"))

    usage = _recover_token_usage_from_raw_output(raw)

    assert not usage


def test_malformed_and_partial_json_lines_are_skipped() -> None:
    """A truncated line and invalid counters never abort the scan."""
    good = _step_finish(total=20, input_tokens=12, output_tokens=8)
    raw = "\n".join(
        [
            '{"type":"step_finish","tokens":{"total":999',  # truncated JSON
            "not json at all",
            json.dumps({"type": "step_finish", "tokens": {"total": -5, "input": "many", "output": None}}),
            json.dumps({"type": "step_finish"}),  # no tokens object
            json.dumps(good),
        ]
    )

    usage = _recover_token_usage_from_raw_output(raw)

    assert usage == {"input": 12, "output": 8, "total": 20}


def test_non_event_payloads_mentioning_step_finish_are_skipped() -> None:
    """A JSON line that merely CONTAINS the event type is not an event.

    The cheap substring pre-filter lets two non-events reach the JSON parse: a
    payload that is not an object, and an object whose ``type`` is a different
    (longer) event name. Neither may contribute counters.
    """
    raw = "\n".join(
        [
            json.dumps(["step_finish"]),  # valid JSON, not an object
            json.dumps({"type": "step_finish_extra", "tokens": {"total": 999}}),  # wrong type
            json.dumps(_step_finish(total=5, input_tokens=2, output_tokens=3)),
        ]
    )

    usage = _recover_token_usage_from_raw_output(raw)

    assert usage == {"input": 2, "output": 3, "total": 5}


def test_invalid_cache_counter_is_skipped_without_dropping_its_sibling() -> None:
    """An implausible cache counter is ignored; the valid sibling still sums."""
    raw = json.dumps(
        {
            "type": "step_finish",
            "tokens": {"total": 10, "input": 6, "output": 4, "cache": {"read": 3, "write": None}},
        }
    )

    usage = _recover_token_usage_from_raw_output(raw)

    assert usage == {"input": 6, "output": 4, "total": 10, "cache_read": 3}
    assert "cache_write" not in usage


def test_none_and_empty_input_are_fail_open() -> None:
    assert not _recover_token_usage_from_raw_output(None)
    assert not _recover_token_usage_from_raw_output("")


def test_attach_sets_the_recovered_usage_on_the_exception() -> None:
    """The raise-site hook carries the recovered shape on the exception."""
    exc = SandboxNodeFailedError("no output", node_id="node-1")
    raw = _blob([_step_finish(total=12, input_tokens=7, output_tokens=5)])

    _attach_recovered_token_usage(exc, raw)

    assert getattr(exc, RECOVERED_TOKEN_USAGE_ATTR) == {"input": 7, "output": 5, "total": 12}


def test_attach_is_a_noop_without_a_step_finish_event() -> None:
    """No recoverable event attaches NOTHING — the exception is untouched."""
    exc = SandboxNodeFailedError("no output", node_id="node-1")

    _attach_recovered_token_usage(exc, "\n".join([json.dumps({"type": "text", "part": {"text": "hi"}})]))

    assert not hasattr(exc, RECOVERED_TOKEN_USAGE_ATTR)


def test_large_blob_completes_quickly_and_sums_correctly() -> None:
    """~10,000 lines is one linear pass — bounded work, correct total."""
    lines: list[str] = []
    expected_total = 0
    for index in range(10_000):
        if index % 100 == 0:
            event = _step_finish(total=7, input_tokens=4, output_tokens=3)
            expected_total += 7
            lines.append(json.dumps(event))
        else:
            lines.append(json.dumps({"type": "text", "part": {"text": f"line {index}"}}))
    raw = "\n".join(lines)

    started = time.monotonic()
    usage = _recover_token_usage_from_raw_output(raw)
    elapsed = time.monotonic() - started

    assert usage["total"] == expected_total
    assert usage["input"] == 400
    assert usage["output"] == 300
    assert elapsed < 5.0


def test_recovered_usage_is_agent_reported_not_server_measured() -> None:
    """The recovered figures ride the ``reported_*`` family, never canonical keys."""
    exc = SandboxNodeFailedError("no output", node_id="node-1")
    setattr(exc, RECOVERED_TOKEN_USAGE_ATTR, {"input": 10, "output": 5, "total": 15})

    reported = recovered_reported_token_usage(exc)

    assert RECOVERED_TOKEN_USAGE_PROVENANCE == "reported"
    assert reported == {
        "reported_input_tokens": 10,
        "reported_output_tokens": 5,
        "reported_total_tokens": 15,
    }
    assert "input_tokens" not in reported
    assert "output_tokens" not in reported
    assert "total_tokens" not in reported


def test_merge_folds_reported_keys_into_the_failed_nodes_entry() -> None:
    exc = SandboxNodeFailedError("no output", node_id="node-1")
    setattr(exc, RECOVERED_TOKEN_USAGE_ATTR, {"input": 10, "output": 5, "total": 15})

    merged = merge_recovered_token_usage({"other": {"total_tokens": 3}}, exc)

    assert merged is not None
    assert merged["other"] == {"total_tokens": 3}
    assert merged["node-1"] == {
        "reported_input_tokens": 10,
        "reported_output_tokens": 5,
        "reported_total_tokens": 15,
    }


def test_merge_is_a_noop_without_recovered_usage() -> None:
    """A plain failure (nothing recovered) leaves the accumulator identity intact."""
    exc = SandboxNodeFailedError("no output", node_id="node-1")
    existing = {"node-1": {"input_tokens": 3}}

    assert merge_recovered_token_usage(existing, exc) is existing
    assert merge_recovered_token_usage(None, exc) is None
