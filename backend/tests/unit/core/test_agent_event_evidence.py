"""Unit tests for the shared evidence readers in ``agent_event_evidence``.

FAR-1914 M2: both the node runner and cost finalize read the opencode CLI's
``step_finish`` events through ONE leaf module, so the two call sites cannot
drift. These tests pin that contract: the ``part``-nested shape is the ONLY
one parsed, junk degrades to nothing (no fabricated zero), and a liveness
window is never credited without a proof.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException

import pytest

from modulo.core import agent_event_evidence
from modulo.core.agent_event_evidence import (
    _add_step_cost,
    heartbeat_liveness_seconds,
    parse_step_finish_evidence,
)
from modulo.core.pipeline_engine.node_runner import (
    RECOVERED_TOKEN_USAGE_ATTR,
    SandboxNodeFailedError,
    _attach_recovered_token_usage,
)

#: A ~40 KB line whose nesting (~10k levels) is far past ``json.loads``'s
#: recursion budget. It mentions ``step_finish``, so it reaches the decoder;
#: the decoder raises ``RecursionError`` — a ``RuntimeError``, NOT a
#: ``ValueError``. Inside the retention window, so it is agent-controlled input
#: the parser must survive.
_DEEP_STEP_FINISH_LINE = "[" * 20_000 + '"step_finish"' + "]" * 20_000


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

    def test_deeply_nested_line_degrades_instead_of_raising_recursion_error(self) -> None:
        """A ~10k-level line inside the retention window must not escape.

        ``json.loads`` raises ``RecursionError`` (a ``RuntimeError``, not the
        ``ValueError`` the guard used to catch) for nesting past its budget.
        The documented contract is that the parser NEVER raises and degrades to
        ``({}, None)`` — otherwise the terminal path loses its salvage read.
        """

        tokens, cost = parse_step_finish_evidence(_DEEP_STEP_FINISH_LINE)

        assert tokens == {}
        assert cost is None

    def test_a_deeply_nested_line_does_not_discard_a_valid_sibling_event(self) -> None:
        """The failure is PER EVENT: the rest of the stream is still parsed."""
        raw = "\n".join([_DEEP_STEP_FINISH_LINE, _event(cost=0.0004, tokens={"total": 9})])

        tokens, cost = parse_step_finish_evidence(raw)

        assert tokens == {"total": 9}
        assert cost == Decimal("0.0004")


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


class TestTypedFailureSurvivesHostileInput:
    """The raise-site hook must never replace a typed failure with its own."""

    def test_deeply_nested_line_leaves_the_typed_failure_intact(self) -> None:
        """FAR-1914: a ``RecursionError`` here would replace the real failure.

        ``_attach_recovered_token_usage`` runs on the terminal path beside a
        ``SandboxNodeFailedError``. Before the guard covered ``RecursionError``
        it propagated out of the parse and destroyed the typed failure — and
        the caller lost the derived sandbox line/agent cost. The exception must
        come out exactly as it went in, with nothing attached.
        """
        exc = SandboxNodeFailedError("no output", node_id="node-1")

        _attach_recovered_token_usage(exc, _DEEP_STEP_FINISH_LINE)

        assert isinstance(exc, SandboxNodeFailedError)
        assert str(exc) == "no output"
        assert not hasattr(exc, RECOVERED_TOKEN_USAGE_ATTR)


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


class TestAddStepCost:
    """The belt-and-braces sum guard (FAR-1914 round-3 M2)."""

    @staticmethod
    def test_sums_plausible_costs() -> None:
        assert _add_step_cost(Decimal("0.0003"), Decimal("0.000178")) == Decimal("0.000478")

    @staticmethod
    def test_decimal_exception_degrades_to_none() -> None:
        """A decimal arithmetic failure must never escape into the caller.

        Per-step costs are bounded, so this cannot happen with real values; the
        test pins the degradation contract with a ``Decimal`` subclass whose
        reflected add raises, which is the only way to reach the guard.
        """

        class _OverflowingCost(Decimal):
            def __radd__(self, other: object) -> Decimal:
                raise DecimalException("overflow")

        assert _add_step_cost(Decimal(0), _OverflowingCost(1)) is None


def test_parse_step_finish_evidence_skips_an_overflowing_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable addition is skipped per event; the rest of the stream counts.

    Per-step costs are bounded so this cannot arise from real values; the test
    forces the ``Decimal`` overflow guard through the public parser to pin that
    a single bad event degrades to "no usable cost" rather than aborting the
    whole salvage read.
    """

    class _OverflowingCost(Decimal):
        def __radd__(self, other: object) -> Decimal:
            raise DecimalException("overflow")

    real_coerce = agent_event_evidence._coerce_step_cost

    def _coerce(value: object) -> Decimal | None:
        if value == "overflow":
            return _OverflowingCost(1)
        return real_coerce(value)

    monkeypatch.setattr(agent_event_evidence, "_coerce_step_cost", _coerce)
    stream = '{"type": "step_finish", "part": {"cost": "overflow", "tokens": {"total": 5}}}\n' + _event(
        cost=0.5, tokens={"total": 7}
    )

    tokens, cost = parse_step_finish_evidence(stream)

    assert cost == Decimal("0.5")
    assert tokens["total"] == 12
