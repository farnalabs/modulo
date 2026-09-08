"""FAR-438 / FAR-402 P5 runtime retry+compensation regression tests.

Covers the RUNTIME wrapper (``make_retrying_node_fn`` in ``runtime_retry``):
control-flow terminal faults raised by a watched node MUST reach the run-level
terminal path and must NOT be swallowed by a compensation edge that would
otherwise continue the run. These are DB-free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from langgraph.errors import GraphInterrupt

from modulo.core.pipeline_engine.decorator import RunCancelledError
from modulo.core.pipeline_engine.runtime_retry import make_retrying_node_fn


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
