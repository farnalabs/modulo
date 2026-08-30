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
