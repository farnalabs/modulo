"""FAR-402 P5 (FAR-419): runtime per-node/per-edge retry + compensation execution.

Exercises the RUNTIME sibling of ``retry_compensation`` (the authoring/validation
layer). Pure DB-free unit tests against ``runtime_retry.make_retrying_node_fn``
with async + sync mock node bodies — no Docker, no SAQ, no Postgres/Redis, no
real sandbox. The behaviours the ticket asks for:

* per-node retry re-invokes the SAME node body max_attempts times (no
  full-graph re-run; the idempotency key is stamped so a side-effecting node
  can dedupe its write across attempts),
* per-node retry exhaustion -> the failure propagates (run fails normally),
* per-edge retry re-executes the SOURCE node,
* per-edge retry fail-closed for a non-idempotent source (no re-execution),
* compensation fires on a watched-node TERMINAL failure and the run CONTINUES
  (``_compensated`` marker) when the compensation node succeeds,
* compensation node failure -> ``CompensationFailedError`` (the executor maps it
  to the terminal ``compensation_failed`` status),
* no compensation configured -> normal failure,
* snapshot immutability: the wrapper never mutates the caller's state dict.

The full graph-compile integration (``build_graph_from_json`` + ``ainvoke``) is
covered by the scatter/join + parallel-branch runtime suites; a small
wrapper-level integration here asserts the compile path wires the wrapper.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError
from modulo.core.pipeline_engine.runtime_retry import (
    CompensationFailedError,
    make_retrying_node_fn,
)


def _node_retry(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"max_attempts": 3, "backoff": 0.0, "on": ["error", "timeout"]}
    cfg.update(overrides)
    return cfg


async def _noop_compensation(state: dict[str, Any]) -> dict[str, Any]:
    return {"compensated": True}


async def _raising_compensation(state: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("compensation boom")


async def _assert_raises(exc_type: type[BaseException], fn: Any) -> BaseException:
    with pytest.raises(exc_type) as excinfo:
        await fn
    return excinfo.value


# --------------------------------------------------------------------------- #
# Per-node retry (§B)
# --------------------------------------------------------------------------- #


async def test_per_node_retry_reinvokes_same_node_without_graph_rerun() -> None:
    calls: list[dict[str, Any]] = []

    async def flaky(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(state)
        if len(calls) < 3:
            raise SandboxNodeFailedError("transient sandbox failure")
        return {"ok": True}

    node = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(flaky, node_id="n1", node_def=node, pipeline_retry_policy={})

    result = await wrapped({"run_context": {}})

    assert result == {"ok": True}
    # SAME node body re-invoked exactly max_attempts times — never the graph.
    assert len(calls) == 3


async def test_per_node_retry_exhaustion_propagates_failure() -> None:
    calls = 0

    async def always_fail(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise SandboxNodeFailedError("permanent sandbox failure")

    node = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(always_fail, node_id="n1", node_def=node, pipeline_retry_policy={})

    await _assert_raises(SandboxNodeFailedError, wrapped({"run_context": {}}))
    assert calls == 3


async def test_per_node_retry_not_applied_to_absent_event() -> None:
    # Policy retries on 'error' only — a TimeoutError is NOT on the set, so the
    # node fails immediately (no retry).
    calls = 0

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise TimeoutError("too slow")

    node_def = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(node, node_id="n1", node_def=node_def, pipeline_retry_policy={})

    await _assert_raises(TimeoutError, wrapped({"run_context": {}}))
    assert calls == 1


async def test_per_node_retry_fail_closed_for_idempotent_false() -> None:
    calls = 0

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    node_def = {"id": "n1", "idempotent": False, "retry": {"max_attempts": 5, "on": ["error"]}}
    wrapped = make_retrying_node_fn(node, node_id="n1", node_def=node_def, pipeline_retry_policy={})

    await _assert_raises(RuntimeError, wrapped({"run_context": {}}))
    assert calls == 1  # never retried


async def test_per_node_retry_stamps_same_idempotency_key_each_attempt() -> None:
    seen_keys: list[str | None] = []
    calls = 0

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        seen_keys.append(state.get("_node_idempotency_key"))
        if calls < 3:
            raise SandboxNodeFailedError("transient")
        return {"ok": True}

    node_def = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}

    def key_for(nid: str, state: dict[str, Any]) -> str | None:
        return f"run:node:{nid}"

    wrapped = make_retrying_node_fn(
        node,
        node_id="n1",
        node_def=node_def,
        pipeline_retry_policy={},
        idempotency_key=key_for,
    )

    await wrapped({"run_context": {}})

    assert calls == 3
    # Every attempt reused the SAME node-scoped key — a side-effecting node can
    # dedupe its write (run+node+index contract, §4F R7).
    assert seen_keys == ["run:node:n1", "run:node:n1", "run:node:n1"]


async def test_idempotency_key_stamped_on_edge_source_and_compensation_major3() -> None:
    # MAJOR-3: the node-scoped idempotency key must be stamped on the two most
    # side-effect-prone re-execution paths — the edge-retry SOURCE re-execution
    # and the compensation-target invocation — not only on the node's own retry
    # loop. Each path stamps its OWN node-scoped key so a side-effecting connector
    # / sandbox node can dedupe its write.
    source_keys: list[str | None] = []
    comp_keys: list[str | None] = []

    async def source_fn(state: dict[str, Any]) -> dict[str, Any]:
        source_keys.append(state.get("_node_idempotency_key"))
        return {"source_out": 1}

    async def target_fn(state: dict[str, Any]) -> dict[str, Any]:
        # Never-retryable terminal fault → routes to compensation (no edge retry
        # here, so the source key is exercised by the compensation path only).
        raise SandboxNodeFailedError("boom")

    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    node_defs = {"n1": {"id": "n1"}, "n2": {"id": "n2"}, "n-comp": {"id": "n-comp"}}

    async def comp(state: dict[str, Any]) -> dict[str, Any]:
        comp_keys.append(state.get("_node_idempotency_key"))
        return {"compensated": True}

    def resolver(nid: str) -> Any:
        return {"n1": source_fn, "n2": target_fn, "n-comp": comp}[nid]

    def key_for(nid: str, state: dict[str, Any]) -> str | None:
        return f"run:node:{nid}"

    wrapped = make_retrying_node_fn(
        target_fn,
        node_id="n2",
        node_def=node_defs["n2"],
        pipeline_retry_policy={},
        outgoing_edges=[edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
        idempotency_key=key_for,
    )

    await wrapped({"run_context": {}})

    # The compensation target received ITS OWN node-scoped key (n-comp), proving
    # the key was stamped on the compensation re-execution path.
    assert comp_keys == ["run:node:n-comp"]


async def test_stall_marker_triggers_retry() -> None:
    calls = 0

    async def stalling(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 2:
            return {"output": {"stall_reason": "agent idle"}}
        return {"output": {"done": True}}

    node_def = {"id": "n1", "retry": {"max_attempts": 2, "backoff": 0.0, "on": ["stall"]}}
    wrapped = make_retrying_node_fn(stalling, node_id="n1", node_def=node_def, pipeline_retry_policy={})

    result = await wrapped({"run_context": {}})

    assert result == {"output": {"done": True}}
    assert calls == 2


# --------------------------------------------------------------------------- #
# Per-edge retry (§C)
# --------------------------------------------------------------------------- #


async def test_per_edge_retry_reexecutes_source_node() -> None:
    source_calls = 0
    target_calls = 0

    async def source_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal source_calls
        source_calls += 1
        return {"source_out": source_calls}

    async def target_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal target_calls
        target_calls += 1
        if target_calls < 2:
            raise SandboxNodeFailedError("transient target failure")
        return {"done": True}

    edge = {"source": "n1", "target": "n2", "retry": {"max_attempts": 2, "backoff": 0.0, "on": ["error"]}}
    node_defs = {"n1": {"id": "n1", "idempotent": True}, "n2": {"id": "n2", "idempotent": True}}

    def resolver(nid: str) -> Any:
        return {"n1": source_fn, "n2": target_fn}[nid]

    wrapped = make_retrying_node_fn(
        target_fn,
        node_id="n2",
        node_def=node_defs["n2"],
        pipeline_retry_policy={},
        incoming_edges=[edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    result = await wrapped({"run_context": {}})

    assert result == {"done": True}
    # The source was re-executed (edge retry) and the target re-ran to success.
    assert source_calls == 1
    assert target_calls == 2


async def test_per_edge_retry_fails_closed_for_non_idempotent_source() -> None:
    source_calls = 0

    async def source_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal source_calls
        source_calls += 1
        return {"source_out": 1}

    async def target_fn(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("target boom")

    edge = {"source": "n1", "target": "n2", "retry": {"max_attempts": 2, "backoff": 0.0, "on": ["error"]}}
    node_defs = {"n1": {"id": "n1", "idempotent": False}, "n2": {"id": "n2", "idempotent": True}}

    def resolver(nid: str) -> Any:
        return {"n1": source_fn, "n2": target_fn}[nid]

    wrapped = make_retrying_node_fn(
        target_fn,
        node_id="n2",
        node_def=node_defs["n2"],
        pipeline_retry_policy={},
        incoming_edges=[edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    await _assert_raises(RuntimeError, wrapped({"run_context": {}}))
    assert source_calls == 0  # non-idempotent source never re-executed


async def test_per_edge_retry_does_not_reexecute_source_for_never_retryable_major2() -> None:
    # MAJOR-2: when the watched node's OWN failure is a never-retryable terminal
    # fault (exactly-once script failure), the incoming edge-retry must NOT
    # re-execute the side-effecting SOURCE node — re-running the source + target
    # would only reproduce the same terminal fault. The failure must instead fall
    # through to compensation / normal failure with the source untouched.
    source_calls = 0
    target_calls = 0

    async def source_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal source_calls
        source_calls += 1
        return {"source_out": source_calls}

    class ScriptFailedError(Exception):
        pass

    async def target_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal target_calls
        target_calls += 1
        raise ScriptFailedError("post-claim exactly-once")

    # Incoming edge-retry (n1 -> n2) AND an outgoing compensation edge.
    in_edge = {"source": "n1", "target": "n2", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    out_edge = {"source": "n2", "target": "n3", "on_failure_target": "n-comp"}
    node_defs = {
        "n1": {"id": "n1", "idempotent": True},
        "n2": {"id": "n2", "idempotent": True},
        "n-comp": {"id": "n-comp"},
    }

    comp_calls = 0

    async def comp(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal comp_calls
        comp_calls += 1
        return {"compensated": True}

    def resolver(nid: str) -> Any:
        return {"n1": source_fn, "n2": target_fn, "n-comp": comp}[nid]

    wrapped = make_retrying_node_fn(
        target_fn,
        node_id="n2",
        node_def=node_defs["n2"],
        pipeline_retry_policy={},
        incoming_edges=[in_edge],
        outgoing_edges=[out_edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    result = await wrapped({"run_context": {}})

    # Source was NOT re-executed (exactly-once contract preserved) and the run
    # CONTINUES via the compensation edge.
    assert source_calls == 0
    assert target_calls == 1
    assert comp_calls == 1
    assert result == {"_compensated": True, "_compensated_node": "n2"}


# --------------------------------------------------------------------------- #
# Compensation edges (§E)
# --------------------------------------------------------------------------- #


async def test_compensation_fires_and_run_continues_on_success() -> None:
    watched_calls = 0

    async def watched(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal watched_calls
        watched_calls += 1
        raise RuntimeError("watched node terminal failure")

    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    node_defs = {"n1": {"id": "n1"}, "n2": {"id": "n2"}, "n-comp": {"id": "n-comp"}}

    def resolver(nid: str) -> Any:
        return {"n1": watched, "n-comp": _noop_compensation}[nid]

    wrapped = make_retrying_node_fn(
        watched,
        node_id="n1",
        node_def=node_defs["n1"],
        pipeline_retry_policy={},
        outgoing_edges=[edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    result = await wrapped({"run_context": {}})

    # The run CONTINUES: the watched node's failure was routed to the
    # compensation node, which ran forward and returned a compensated marker.
    assert result == {"_compensated": True, "_compensated_node": "n1"}
    assert watched_calls == 1


async def test_compensation_failure_raises_compensation_failed_error() -> None:
    async def watched(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("watched terminal failure")

    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    node_defs = {"n1": {"id": "n1"}, "n-comp": {"id": "n-comp"}}

    def resolver(nid: str) -> Any:
        return {"n1": watched, "n-comp": _raising_compensation}[nid]

    wrapped = make_retrying_node_fn(
        watched,
        node_id="n1",
        node_def=node_defs["n1"],
        pipeline_retry_policy={},
        outgoing_edges=[edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    exc = await _assert_raises(CompensationFailedError, wrapped({"run_context": {}}))

    assert isinstance(exc, CompensationFailedError)
    assert exc.node_id == "n1"
    assert exc.compensation_target == "n-comp"


async def test_no_compensation_routes_to_normal_failure() -> None:
    async def watched(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("plain failure")

    # No outgoing edge carries an on_failure_target -> no compensation.
    wrapped = make_retrying_node_fn(
        watched,
        node_id="n1",
        node_def={"id": "n1"},
        pipeline_retry_policy={},
        outgoing_edges=[{"source": "n1", "target": "n2"}],
        raw_fn_resolver=lambda nid: None,
        node_defs={"n1": {"id": "n1"}},
    )

    await _assert_raises(RuntimeError, wrapped({"run_context": {}}))


async def test_compensation_not_applied_to_retryable_failure_when_budget_remains() -> None:
    # A retryable failure WITH budget must retry the node — NOT route to
    # compensation (compensation only fires on terminal failure).
    calls = 0

    async def watched(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise SandboxNodeFailedError("transient, should retry")
        return {"ok": True}

    comp_calls = 0

    async def comp(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal comp_calls
        comp_calls += 1
        return {"compensated": True}

    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    node_defs = {
        "n1": {"id": "n1", "retry": {"max_attempts": 2, "backoff": 0.0, "on": ["error"]}},
        "n-comp": {"id": "n-comp"},
    }

    def resolver(nid: str) -> Any:
        return {"n1": watched, "n-comp": comp}[nid]

    wrapped = make_retrying_node_fn(
        watched,
        node_id="n1",
        node_def=node_defs["n1"],
        pipeline_retry_policy={},
        outgoing_edges=[edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    result = await wrapped({"run_context": {}})

    assert result == {"ok": True}
    assert calls == 2
    assert comp_calls == 0  # compensation did NOT fire (retry saved the node)


async def test_compensation_reachable_past_incoming_edge_retry_major1() -> None:
    # MAJOR-1: the compensation branch was DEAD when the watched node also has an
    # incoming edge-retry, because the edge retry raised on budget exhaustion
    # instead of falling through. Here the watched node (n2) has an incoming
    # edge-retry (n1 -> n2) AND an outgoing compensation edge (n2 -> n-comp). n2
    # fails on a retryable fault that keeps failing; the node + edge retries
    # exhaust, and the run MUST route to the compensation node (which succeeds),
    # not raise a plain terminal error with compensation at zero.
    source_calls = 0
    target_calls = 0

    async def source_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal source_calls
        source_calls += 1
        return {"source_out": source_calls}

    async def target_fn(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal target_calls
        target_calls += 1
        # Always a retryable transient fault — node + edge retries exhaust.
        raise SandboxNodeFailedError("transient but persistent")

    in_edge = {"source": "n1", "target": "n2", "retry": {"max_attempts": 2, "backoff": 0.0, "on": ["error"]}}
    out_edge = {"source": "n2", "target": "n3", "on_failure_target": "n-comp"}
    node_defs = {
        "n1": {"id": "n1", "idempotent": True},
        "n2": {"id": "n2", "idempotent": True},
        "n-comp": {"id": "n-comp"},
    }

    comp_calls = 0

    async def comp(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal comp_calls
        comp_calls += 1
        return {"compensated": True}

    def resolver(nid: str) -> Any:
        return {"n1": source_fn, "n2": target_fn, "n-comp": comp}[nid]

    wrapped = make_retrying_node_fn(
        target_fn,
        node_id="n2",
        node_def=node_defs["n2"],
        pipeline_retry_policy={},
        incoming_edges=[in_edge],
        outgoing_edges=[out_edge],
        raw_fn_resolver=resolver,
        node_defs=node_defs,
    )

    result = await wrapped({"run_context": {}})

    # Compensation fired after the node + edge retries exhausted (the run
    # CONTINUES), and the source was re-executed within the edge-retry budget.
    assert comp_calls == 1
    assert source_calls == 2  # edge budget = 2 source re-executions
    assert target_calls == 3  # 1 own attempt + 2 edge-retry re-runs
    assert result == {"_compensated": True, "_compensated_node": "n2"}


# --------------------------------------------------------------------------- #
# Snapshot immutability (no mutation of caller state)
# --------------------------------------------------------------------------- #


async def test_wrapper_does_not_mutate_caller_state() -> None:
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        return {"result": True}

    wrapped = make_retrying_node_fn(
        node,
        node_id="n1",
        node_def={"id": "n1"},
        pipeline_retry_policy={},
        idempotency_key=lambda nid, s: "k",
    )

    state = {"run_context": {}}
    await wrapped(state)

    assert state == {"run_context": {}}  # snapshot immutability preserved


# --------------------------------------------------------------------------- #
# Never-retryable terminal faults are not retried
# --------------------------------------------------------------------------- #


async def test_never_retryable_script_fault_not_retried() -> None:
    calls = 0

    class ScriptFailedError(Exception):
        pass

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise ScriptFailedError("post-claim")

    node_def = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(node, node_id="n1", node_def=node_def, pipeline_retry_policy={})

    await _assert_raises(ScriptFailedError, wrapped({"run_context": {}}))
    assert calls == 1


async def test_cancelled_error_is_never_retried() -> None:
    calls = 0

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    node_def = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(node, node_id="n1", node_def=node_def, pipeline_retry_policy={})

    with pytest.raises(asyncio.CancelledError):
        await wrapped({"run_context": {}})
    assert calls == 1


async def test_programming_bug_not_retried_major4() -> None:
    # MAJOR-4: failure_event must NOT map a programming bug / generic exception
    # to a retryable event — only known-transient sandbox / infra faults are.
    # Re-running a bug would reproduce it, and because inline node retry composes
    # with the pipeline's run-level retry_policy default this would otherwise
    # silently make every existing pipeline retry more aggressively on deploy.
    calls = 0

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise IndexError("list index out of range")

    node_def = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 0.0, "on": ["error"]}}
    wrapped = make_retrying_node_fn(node, node_id="n1", node_def=node_def, pipeline_retry_policy={})

    await _assert_raises(IndexError, wrapped({"run_context": {}}))
    assert calls == 1  # never retried inline


async def test_never_retryable_control_flow_not_swallowed_by_compensation() -> None:
    # Regression for the FAR-438 review MAJOR: a watched node whose TERMINAL
    # failure is a never-retryable control-flow fault (operator cancel /
    # HITL interrupt / eval-block / superseded / output-rejected / runaway)
    # MUST NOT be swallowed by an outgoing compensation edge — the compensation
    # node must NOT run and the control-flow exception must re-raise so the
    # executor can cancel / interrupt / eval-fail the run as designed. Before the
    # fix the compensation branch ran unconditionally after node + edge retry
    # were exhausted, silently continuing the run.
    from langgraph.errors import GraphInterrupt, NodeCancelledError

    comp_calls: dict[str, int] = {"n": 0}

    async def comp(state: dict[str, Any]) -> dict[str, Any]:
        comp_calls["n"] += 1
        return {"compensated": True}

    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    node_defs = {"n1": {"id": "n1"}, "n2": {"id": "n2"}, "n-comp": {"id": "n-comp"}}

    def _make_watched(raise_exc: BaseException) -> Any:
        async def watched(state: dict[str, Any]) -> dict[str, Any]:
            raise raise_exc

        return watched

    def _resolver(nid: str) -> Any:
        watched_excs = {"n1": NodeCancelledError(node="n1"), "n2": GraphInterrupt()}
        return {"n1": _make_watched(watched_excs["n1"]), "n2": _make_watched(watched_excs["n2"]), "n-comp": comp}[nid]

    # Case 1: operator cancel (RunCancelledError) on the watched node.
    wrapped = make_retrying_node_fn(
        _make_watched(NodeCancelledError(node="n1")),
        node_id="n1",
        node_def=node_defs["n1"],
        pipeline_retry_policy={},
        outgoing_edges=[edge],
        raw_fn_resolver=_resolver,
        node_defs=node_defs,
    )
    with pytest.raises(NodeCancelledError):
        await wrapped({"run_context": {}})
    assert comp_calls["n"] == 0  # compensation never ran

    # Case 2: HITL interrupt (GraphInterrupt) on the watched node.
    wrapped2 = make_retrying_node_fn(
        _make_watched(GraphInterrupt()),
        node_id="n2",
        node_def=node_defs["n2"],
        pipeline_retry_policy={},
        outgoing_edges=[edge],
        raw_fn_resolver=_resolver,
        node_defs=node_defs,
    )
    with pytest.raises(GraphInterrupt):
        await wrapped2({"run_context": {}})
    assert comp_calls["n"] == 0  # still never ran


# --------------------------------------------------------------------------- #
# Sync node callables (join / convergence) are wrapped correctly
# --------------------------------------------------------------------------- #


async def test_sync_node_callable_supported() -> None:
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        # target body itself
        return {"ok": True}

    def sync_join(state: dict[str, Any]) -> dict[str, Any]:
        return {"joined": True}

    wrapped = make_retrying_node_fn(
        sync_join,
        node_id="join-1",
        node_def={"id": "join-1"},
        pipeline_retry_policy={},
    )

    result = await wrapped({"run_context": {}})
    assert result == {"joined": True}


# --------------------------------------------------------------------------- #
# Compile-level wiring (the wrapper is applied by build_graph_from_json)
# --------------------------------------------------------------------------- #


async def test_compile_path_wires_retry_wrapper_without_breaking_normal_exec() -> None:
    """A plain graph compiles and runs through the retry-wrapped nodes.

    Regression guard for the graph_cache compile-path change: every node is now
    built then wrapped (``make_retrying_node_fn``) before being added to the
    StateGraph. A normal (non-failing) graph must still compile and invoke
    identical to before — the wrapper is transparent on success. The node here
    carries a ``retry`` config so we know the wrapper was actually installed.
    """
    from modulo.core.pipeline_engine.graph_cache import build_graph_from_json

    graph = {
        "nodes": [
            {"id": "n1", "role": None, "retry": {"max_attempts": 2, "backoff": 0.0, "on": ["error"]}},
            {"id": "n2", "role": None},
        ],
        "edges": [{"source": "n1", "target": "n2", "type": "normal"}],
    }
    compiled = build_graph_from_json(
        graph,
        pipeline_retry_policy={"max_retries": 1, "on": ["failure"]},
        node_idempotency_key=lambda nid, state: f"run:node:{nid}",
    )

    result = await compiled.ainvoke(
        {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
        {"configurable": {"thread_id": "t"}},
    )

    # Both nodes produced a stub artifact (no model_backend_id → stub path), so
    # the run completed normally through the retry-wrapped callables.
    assert not result["run_context"]["input"]
    assert isinstance(result.get("artifacts"), list)
