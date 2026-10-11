"""FAR-438 / FAR-402 P5 runtime retry+compensation regression tests.

Covers the RUNTIME wrapper (``make_retrying_node_fn`` in ``runtime_retry``):
control-flow terminal faults raised by a watched node MUST reach the run-level
terminal path and must NOT be swallowed by a compensation edge that would
otherwise continue the run. These are DB-free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt

from modulo.core.pipeline_engine.decorator import RunCancelledError
from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError
from modulo.core.pipeline_engine.retry_compensation import NodeRetryPolicy
from modulo.core.pipeline_engine.runtime_retry import (
    _classify_attempt,
    _should_retry,
    make_retrying_node_fn,
)


def _wrap_with_compensation(fault_exc: BaseException) -> tuple[Callable, list[int]]:
    """Return (wrapped_fn, comp_calls) for a watched node that raises ``fault_exc``.

    The watched node carries an outgoing ``on_failure_target`` compensation edge;
    ``comp_calls`` records whether the compensation target was ever executed.
    """

    async def watched(state: dict) -> dict:
        raise fault_exc

    comp_calls: list[int] = []

    async def comp(state: dict) -> dict:
        comp_calls.append(1)
        return {"_compensated": True}

    def resolver(node_id: str):
        if node_id == "comp":
            return comp
        return None

    wrapped = make_retrying_node_fn(
        watched,
        node_id="watched",
        node_def=None,
        pipeline_retry_policy=None,
        outgoing_edges=[{"source": "watched", "target": "next", "on_failure_target": "comp"}],
        raw_fn_resolver=resolver,
    )
    return wrapped, comp_calls


def test_control_flow_fault_run_cancelled_does_not_run_compensation() -> None:
    async def run() -> None:
        wrapped, comp_calls = _wrap_with_compensation(RunCancelledError("operator cancel"))
        with pytest.raises(RunCancelledError):
            await wrapped({})
        # The compensation edge must NOT run — a cancelled watched node must
        # re-raise so the executor can transition the run to cancelled.
        assert comp_calls == []

    asyncio.run(run())


def test_control_flow_fault_graph_interrupt_does_not_run_compensation() -> None:
    async def run() -> None:
        wrapped, comp_calls = _wrap_with_compensation(GraphInterrupt("hitl interrupt"))
        with pytest.raises(GraphInterrupt):
            await wrapped({})
        # The compensation edge must NOT run — an interrupted watched node must
        # re-raise so the executor can park the run awaiting human input.
        assert comp_calls == []

    asyncio.run(run())


def test_control_flow_fault_tier_refused_does_not_run_compensation() -> None:
    """FAR-592 (D6): the Local-tier refusal is in the control-flow set — a
    compensation edge must NOT absorb a deterministic refusal (the run must
    terminal-fail with ``sandbox.tier_refused``, never continue)."""
    from modulo.core.pipeline_engine.node_runner import SandboxTierRefusedError

    async def run() -> None:
        wrapped, comp_calls = _wrap_with_compensation(SandboxTierRefusedError("tier refused"))
        with pytest.raises(SandboxTierRefusedError):
            await wrapped({})
        assert comp_calls == []

    asyncio.run(run())


def test_tier_refused_is_never_retryable_inline() -> None:
    """FAR-592 (D6): the never-retryable set shows the typed refusal (the
    inline node retry must NOT re-execute a node body that can only re-hit
    the same deterministic refusal)."""
    from modulo.core.pipeline_engine.node_runner import SandboxTierRefusedError
    from modulo.core.pipeline_engine.runtime_retry import (
        _CONTROL_FLOW_NO_COMPENSATION_NAMES,
        _NEVER_RETRYABLE_NAMES,
        _is_control_flow_fault,
        _is_never_retryable,
    )

    assert SandboxTierRefusedError.__name__ in _NEVER_RETRYABLE_NAMES
    assert SandboxTierRefusedError.__name__ in _CONTROL_FLOW_NO_COMPENSATION_NAMES
    assert _is_never_retryable(SandboxTierRefusedError("refusal"))
    assert _is_control_flow_fault(SandboxTierRefusedError("refusal"))


# --------------------------------------------------------------------------- #
# _classify_attempt / _should_retry (pure classification helpers)
# --------------------------------------------------------------------------- #

_STALLED = {"output": {"stall_reason": "no output for 120s"}}


@pytest.mark.parametrize(
    ("result", "exc", "policy_events", "expected_kind", "expected_event"),
    [
        # Plain success -> terminal_success (result is returned as-is).
        ({"ok": True}, None, frozenset({"error", "stall"}), "terminal_success", None),
        # Stall + policy retries stall -> retry on the "stall" event.
        (_STALLED, None, frozenset({"stall", "error"}), "retry", "stall"),
        # Stall + policy declines stall -> terminal_success (the stall result
        # is returned to the graph, never swallowed).
        (_STALLED, None, frozenset({"error"}), "terminal_success", None),
        # Retryable-class exception + policy retries error -> retry on "error".
        (None, SandboxNodeFailedError("transient"), frozenset({"error"}), "retry", "error"),
        # TimeoutError + policy retries timeout -> retry on "timeout".
        (None, TimeoutError("too slow"), frozenset({"timeout"}), "retry", "timeout"),
        # Retryable-class exception + policy declines the event -> terminal
        # failure carrying the classified event (edge-retry may still use it).
        (None, TimeoutError("too slow"), frozenset({"error"}), "terminal_failure", "timeout"),
        # Programming bug -> never retryable -> terminal_failure, event None.
        (None, ValueError("bug"), frozenset({"error", "timeout", "stall"}), "terminal_failure", None),
    ],
)
def test_classify_attempt_matrix(
    result: dict[str, Any] | None,
    exc: BaseException | None,
    policy_events: frozenset[str],
    expected_kind: str,
    expected_event: str | None,
) -> None:
    policy = NodeRetryPolicy(max_attempts=3, backoff_seconds=0.0, events=policy_events)
    outcome = _classify_attempt(result, policy, exc)
    assert outcome.kind == expected_kind
    assert outcome.event == expected_event


def test_should_retry_budget_predicate() -> None:
    retry_outcome = _classify_attempt(None, NodeRetryPolicy(3, 0.0, frozenset({"error"})), SandboxNodeFailedError("x"))
    success_outcome = _classify_attempt({"ok": True}, NodeRetryPolicy(3, 0.0, frozenset({"error"})), None)
    # A retryable outcome retries only while within the attempt budget.
    assert _should_retry(retry_outcome, attempt_n=1, policy=NodeRetryPolicy(3, 0.0, frozenset({"error"})))
    assert not _should_retry(retry_outcome, attempt_n=3, policy=NodeRetryPolicy(3, 0.0, frozenset({"error"})))
    # max_retries == 0 (max_attempts == 1) -> the first attempt can never retry.
    assert not _should_retry(retry_outcome, attempt_n=1, policy=NodeRetryPolicy(1, 0.0, frozenset({"error"})))
    # Terminal outcomes never retry regardless of budget.
    assert not _should_retry(success_outcome, attempt_n=1, policy=NodeRetryPolicy(3, 0.0, frozenset({"error"})))


# --------------------------------------------------------------------------- #
# make_retrying_node_fn invariants (attempts, backoff schedule, stall, marker)
# --------------------------------------------------------------------------- #


async def test_backoff_sleep_sequence_is_exact_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempts 1..4 with backoff=1.0 sleep exactly [1.0, 2.0, 4.0] (cap not hit)."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = 0

    async def always_fail(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise SandboxNodeFailedError("permanent sandbox failure")

    node = {"id": "n1", "retry": {"max_attempts": 4, "backoff": 1.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(always_fail, node_id="n1", node_def=node, pipeline_retry_policy={})

    with pytest.raises(SandboxNodeFailedError):
        await wrapped({})
    # Total attempts remain max_attempts (max_retries + 1 = 4).
    assert calls == 4
    # Exact schedule: min(backoff * 2**(attempt_n - 1), cap) for the three
    # retries after attempts 1, 2 and 3 (attempt 4 exhausts, no sleep).
    assert sleeps == [1.0, 2.0, 4.0]


async def test_max_retries_zero_is_single_attempt_without_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = 0

    async def always_fail(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise SandboxNodeFailedError("boom")

    node = {"id": "n1", "retry": {"max_attempts": 1, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(always_fail, node_id="n1", node_def=node, pipeline_retry_policy={})

    with pytest.raises(SandboxNodeFailedError):
        await wrapped({})
    assert calls == 1
    assert not sleeps


async def test_stall_result_retries_when_policy_allows_stall() -> None:
    calls = 0

    async def stalling(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            return {"output": {"stall_reason": "no output for 120s"}}
        return {"output": {"ok": True}}

    node = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["stall"]}}
    wrapped = make_retrying_node_fn(stalling, node_id="n1", node_def=node, pipeline_retry_policy={})

    result = await wrapped({})
    assert result == {"output": {"ok": True}}
    assert calls == 3


async def test_stall_result_returned_when_policy_declines_stall() -> None:
    calls = 0

    async def stalling(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"output": {"stall_reason": "no output for 120s"}}

    # Policy retries "error" only — a stall is not on the set, so the stall
    # output is returned to the graph on the FIRST attempt (no retry).
    node = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(stalling, node_id="n1", node_def=node, pipeline_retry_policy={})

    result = await wrapped({})
    assert result == {"output": {"stall_reason": "no output for 120s"}}
    assert calls == 1


async def test_no_compensated_marker_when_loop_exits_on_success() -> None:
    async def ok(state: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def comp(state: dict[str, Any]) -> dict[str, Any]:
        return {"_compensated": True}

    def resolver(node_id: str) -> Callable[[dict[str, Any]], Any] | None:
        if node_id == "comp":
            return comp
        return None

    # A compensation edge EXISTS but the node succeeds — the marker must be
    # absent: the compensated marker fires only on genuine terminal failure.
    wrapped = make_retrying_node_fn(
        ok,
        node_id="n1",
        node_def=None,
        pipeline_retry_policy=None,
        outgoing_edges=[{"source": "n1", "target": "next", "on_failure_target": "comp"}],
        raw_fn_resolver=resolver,
    )

    result = await wrapped({})
    assert result == {"ok": True}
    assert "_compensated" not in result
