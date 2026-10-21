"""Unit tests for the shared evidence readers in ``agent_event_evidence``.

FAR-1914 M2: both the node runner and cost finalize read the opencode CLI's
``step_finish`` events through ONE leaf module, so the two call sites cannot
drift. These tests pin that contract: the ``part``-nested shape is the ONLY
one parsed, junk degrades to nothing (no fabricated zero), and a liveness
window is never credited without a proof.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modulo.core.agent_event_evidence import heartbeat_liveness_seconds, parse_step_finish_evidence


def _event(
    *,
    cost: float | None = 0.000478,
    tokens: dict[str, int] | None = None,
    cache: dict[str, int] | None = None,
) -> str:
    """A ``step_finish`` line exactly as the shipped CLI emits it (nested)."""
    part: dict[str, object] = {}
    if cost is not None:
        part["cost"] = cost
    if tokens is not None and cache is not None:
        part["tokens"] = {**tokens, "cache": cache}
    elif tokens is not None:
        part["tokens"] = tokens
    return '{"type": "step_finish", "part": ' + _dumps(part) + "}"


def _dumps(obj: object) -> str:
    return json.dumps(obj)


class TestParseStepFinishEvidence:
    def test_nested_happy_path_sums_cost(self) -> None:
        raw = _event(cost=0.0005)

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {}
        assert cost == Decimal("0.0005")

    def test_tokens_carry_the_producer_shape(self) -> None:
        raw = _event(tokens={"input": 68_000, "output": 10_000, "total": 78_000})

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {"input": 68_000, "output": 10_000, "total": 78_000}
        assert cost is not None

    def test_cache_pairs_map_to_producer_keys(self) -> None:
        raw = _event(tokens={"input": 1, "output": 1, "total": 2}, cache={"read": 5, "write": 1})

        tokens, _ = parse_step_finish_evidence(raw)

        assert tokens == {"input": 1, "output": 1, "total": 2, "cache_read": 5, "cache_write": 1}

    def test_top_level_only_shape_is_not_parsed(self) -> None:
        """The historical wrong shape (tokens/cost at the event's top level).

        The shipped CLI never emitted it; accepting it would keep a wrong
        parser alive and let a hand-written shape attribute cost.
        """

        raw = json.dumps(
            {
                "type": "step_finish",
                "tokens": {"total": 78_000, "input": 68_000, "output": 10_000},
                "cost": 0.000478,
            }
        )

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {}
        assert cost is None

    def test_multiple_events_sum(self) -> None:
        raw = "\n".join(
            [
                _event(cost=0.0003, tokens={"input": 38, "output": 10, "total": 48}),
                '{"type": "text", "part": {"text": "working"}}',
                _event(cost=0.0002, tokens={"input": 20, "output": 5, "total": 25}),
            ]
        )

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {"input": 58, "output": 15, "total": 73}
        assert cost == Decimal("0.0005")

    def test_malformed_lines_and_other_events_yield_nothing(self) -> None:
        raw = 'not json\n{"type": "step_start"}\n[]\nnull\n'

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {}
        assert cost is None

    def test_empty_and_none_yield_nothing(self) -> None:
        for raw in (None, "", "   "):
            tokens, cost = parse_step_finish_evidence(raw)
            assert tokens == {}
            assert cost is None


class TestCostCoercionErrors:
    """A bad report stays distinguishable from a genuine zero."""

    def test_nonsense_costs_are_rejected_but_valid_sibling_sums(self) -> None:
        raw = "\n".join(
            [
                _event(cost="not-a-number"),
                _event(cost=-1),
                _event(cost=0.001),
            ]
        )

        _, cost = parse_step_finish_evidence(raw)

        assert cost == Decimal("0.001")

    def test_boolean_cost_is_rejected(self) -> None:

        raw = json.dumps({"type": "step_finish", "part": {"cost": True, "tokens": {"total": 5}}})

        _, cost = parse_step_finish_evidence(raw)

        assert cost is None

    def test_tokens_only_carries_no_cost(self) -> None:
        raw = _event(cost=None, tokens={"total": 78_000})

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {"total": 78_000}
        assert cost is None

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-number",
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    )
    def test_invalid_cost_is_skipped(self, bad: object) -> None:

        raw = json.dumps({"type": "step_finish", "part": {"cost": bad, "tokens": {"total": 5}}})

        _, cost = parse_step_finish_evidence(raw)

        assert cost is None

    def test_boolean_token_counter_is_rejected(self) -> None:

        raw = json.dumps({"type": "step_finish", "part": {"tokens": {"total": True}}})

        tokens, _ = parse_step_finish_evidence(raw)

        assert tokens == {}

    def test_huge_exponent_cost_degrades_instead_of_raising_decimal_overflow(self) -> None:
        """FAR-1914 round-3 M2: the shared parser must never raise.

        ``Decimal("1E+999999999")`` is FINITE, so it passes an ``is_finite``
        check and then raises ``decimal.Overflow`` on the running sum — a
        perfectly valid string an agent-controlled stream can carry. It must be
        rejected per event (like any other implausible cost), not propagated:
        the caller is on the terminal path and a raise would replace its typed
        ``SandboxNodeFailedError`` and drop the salvage.
        """
        raw = json.dumps(
            {
                "type": "step_finish",
                "part": {"cost": "1E+999999999", "tokens": {"total": 7}},
            }
        )

        tokens, cost = parse_step_finish_evidence(raw)

        # The valid token counter still survives; only the cost is rejected.
        assert tokens == {"total": 7}
        assert cost is None

    def test_implausibly_large_cost_is_rejected_but_a_plausible_sibling_sums(self) -> None:
        raw = "\n".join(
            [
                _event(cost=999_999_999.0),
                _event(cost=0.002),
            ]
        )

        _, cost = parse_step_finish_evidence(raw)

        assert cost == Decimal("0.002")


class TestHeartbeatLivenessSeconds:
    def test_missing_stamps_return_none(self) -> None:
        assert heartbeat_liveness_seconds(None, datetime.now(UTC), None) is None
        assert heartbeat_liveness_seconds(datetime.now(UTC), None, None) is None

    def test_heartbeat_at_or_before_start_returns_none(self) -> None:
        start = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

        assert heartbeat_liveness_seconds(start, start, None) is None
        assert heartbeat_liveness_seconds(start, start - timedelta(seconds=1), None) is None

    def test_positive_window(self) -> None:
        start = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
        heartbeat = start + timedelta(hours=1)

        assert heartbeat_liveness_seconds(start, heartbeat, None) == 3600.0

    def test_completed_at_caps_a_restamped_heartbeat(self) -> None:
        start = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
        heartbeat = start + timedelta(hours=2)
        completed = start + timedelta(hours=1)

        assert heartbeat_liveness_seconds(start, heartbeat, completed) == 3600.0

    def test_none_completed_uses_the_heartbeat(self) -> None:
        start = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
        heartbeat = start + timedelta(minutes=10)

        assert heartbeat_liveness_seconds(start, heartbeat, None) == 600.0
