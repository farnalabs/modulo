"""Runtime per-node / per-edge retry + compensation execution (FAR-402 P5, §4F).

This module is the RUNTIME sibling of ``retry_compensation`` (the authoring
+ validation layer). It wires the config that P5 validated into ACTUAL
execution by wrapping a node's raw callable so that:

* **per-node retry** (§B): a node body that fails on a retryable event
  (``timeout`` / ``error`` / ``stall``) is re-invoked up to its effective
  ``max_attempts`` with a capped backoff, WITHOUT re-running the rest of the
  graph (LangGraph sees one successful node superstep). The node-scoped
  idempotency key (``run + node + index``, §4F R7) is threaded so a retry can
  dedupe side-effecting re-executions.
* **per-edge retry** (§C): when a target node's OWN retry budget is exhausted
  (or not applicable), an incoming edge that declares a transition ``retry``
  re-executes the SOURCE node (not the target) — the edge-level retry wraps
  node-retry. Fail-closed when the source is ``idempotent=false``.
* **compensation edges** (§E): when a watched node reaches TERMINAL failure and
  an outgoing edge declares an ``on_failure_target``, the wrapper routes to the
  compensation node as a FORWARD execution (no history rewrite). If the
  compensation node succeeds the run CONTINUES (a ``compensated`` marker is
  recorded for observability); if it fails the wrapper raises
  :class:`CompensationFailedError`, which the executor maps to the terminal
  ``COMPENSATION_FAILED`` status.

Configuration source
--------------------
The wrapper never re-implements the P5 resolution rules — it consumes
:mod:`retry_compensation` (``resolve_node_retry`` / ``parse_edge_retry`` /
``edge_retry_reattempts_source`` / ``edge_has_compensation``) so the
authoring-time and run-time vocabularies cannot drift.

Why the retry loop lives HERE (not in the executor stream loop)
---------------------------------------------------------------
LangGraph schedules node callables inside its superstep machinery; the
executor's ``astream_events`` loop only OBSERVES outcomes and has no mechanism
to re-invoke a single node in isolation without replaying the graph from a
checkpoint. Compiling a retry wrapper around each node's raw callable therefore
is the minimal, additive way to deliver within-node re-execution — the
state-graph structure (nodes / edges / entry point) is untouched. This wrapper
is applied at compile time in ``graph_cache`` (see ``build_graph_from_json``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from langgraph.errors import NodeCancelledError

from modulo.core.pipeline_engine import retry_compensation as rc
from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError
from modulo.core.pipeline_engine.retry_compensation import NodeRetryPolicy

_log = logging.getLogger(__name__)

# Dotted terminal code written when a watched node's compensation target fails.
# The executor maps CompensationFailedError to the ``compensation_failed``
# terminal status; this constant is the canonical error-code alias (S1192).
COMPENSATION_FAILED_CODE = "compensation_failed"

# State key stamped with the node-scoped idempotency key so a side-effecting
# connector / sandbox node can dedupe its write across retry attempts.
_NODE_IDEMPOTENCY_KEY = "_node_idempotency_key"

# Backoff cap for a node / edge-retry sleep. Mirrors the authoring bound
# (retry_compensation.RETRY_BACKOFF_CAP_SECONDS) so a malformed config cannot
# pin a worker in a sleep loop.
_BACKOFF_CAP_SECONDS = 300.0

# Never-retryable terminal fault class names (matched by name so this module
# stays dependency-light and closed over the codebase's own naming). A retry
# (per-node or per-edge) MUST NOT re-execute these:
#   * asyncio.CancelledError — framework control, always re-raise.
#   * FAR-296 Phase 2 Script*Error — script PROCESS started → exactly-once.
#   * Superseded / rejected / evaled / runaway / interrupt — already finalised
#     or must flow to the run-level terminal path, not a node retry.
_NEVER_RETRYABLE_NAMES: frozenset[str] = frozenset(
    {
        "ScriptFailedError",
        "ScriptInvalidOutputError",
        "ScriptSideEffectUnknownError",
        "ScriptBudgetKilledError",
        "SupersededNodeError",
        "OutputRejectedError",
        "EvalBlockedError",
        "RunCancelledError",
        "RunawayRunError",
        "GraphInterrupt",
    }
)


def _is_never_retryable(exc: BaseException) -> bool:
    """True for a terminal fault that must never be re-executed by a retry."""
    return isinstance(exc, asyncio.CancelledError) or type(exc).__name__ in _NEVER_RETRYABLE_NAMES


# Control-flow terminal faults that MUST reach the run-level terminal path and
# must NEVER be absorbed by a compensation edge (which would continue the run).
# These are run-lifecycle signals — operator cancel, HITL interrupt, eval-block,
# superseded, output-rejected, runaway — not node-body execution faults. The
# script-execution terminal faults (``Script*Error``) are deliberately excluded:
# a script that failed exactly-once is a genuine TERMINAL node failure a
# compensation edge may continue from, so they stay compensable (FAR-438 review
# MAJOR — a control-flow fault must re-raise instead of running compensation).
_CONTROL_FLOW_NO_COMPENSATION_NAMES: frozenset[str] = frozenset(
    {
        "EvalBlockedError",
        "SupersededNodeError",
        "OutputRejectedError",
        "RunawayRunError",
        "RunCancelledError",
        "NodeCancelledError",
        "GraphInterrupt",
    }
)


def _is_control_flow_fault(exc: BaseException) -> bool:
    """True for a run-lifecycle fault that must bypass compensation and re-raise.

    Mirrors the edge-retry guard (``failure_event`` returns None for these) so a
    watched node that was cancelled / interrupted / eval-blocked / superseded /
    output-rejected / runaway is NOT silently continued by a compensation edge.
    """
    return isinstance(exc, asyncio.CancelledError) or type(exc).__name__ in _CONTROL_FLOW_NO_COMPENSATION_NAMES


# Transient error classes whose node body is SAFE to re-execute inline (a retry
# can dedupe / re-attempt without causing a double side effect). A programming
# bug (``IndexError`` / ``ValueError`` / generic ``RuntimeError``) is NOT in this
# set, so ``failure_event`` returns ``None`` for it — a bug must NOT be silently
# retried on deploy. This is a *conscious* decision (FAR-402 MAJOR-4): the inline
# node retry composes with the pipeline's run-level ``retry_policy`` default
# (``resolve_node_retry``), so an over-broad mapping would make EVERY existing
# pipeline that sets ``retry_policy`` retry far more aggressively the moment this
# ships. ``TimeoutError`` maps to the dedicated ``"timeout"`` event; the
# retryable ``SandboxNodeFailedError`` family (and the transient
# ``NodeCancelledError``) map to ``"error"``.
_TRANSIENT_ERROR_CLASSES: tuple[type[BaseException], ...] = (SandboxNodeFailedError, NodeCancelledError)


def failure_event(exc: BaseException) -> str | None:
    """Classify a node-body exception to a retryable ``retry`` event, or None.

    Returns ``"timeout"`` for a ``TimeoutError`` and ``"error"`` for a known
    transient sandbox / infrastructure failure (``SandboxNodeFailedError`` and
    its retryable subclasses, plus the transient ``NodeCancelledError``). Any
    other exception — including programming bugs like ``IndexError`` and generic
    ``RuntimeError`` — maps to ``None`` (never retry inline): re-running a bug
    would only reproduce it, and because the inline node retry composes with the
    pipeline's run-level ``retry_policy`` default an over-broad mapping would
    silently make every existing pipeline retry more aggressively on deploy.
    Internal cancellation (``asyncio.CancelledError``) is never retried.
    """
    if _is_never_retryable(exc):
        return None
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, _TRANSIENT_ERROR_CLASSES):
        return "error"
    # Anything else (programming bugs, generic exceptions) is non-retryable.
    return None


def _stall_event(output: Any) -> bool:
    """True when a node output carries a ``stall_reason`` marker (FAR-98).

    A stalled sandbox-agent node RETURNS a failed output dict (with
    ``output.stall_reason``) rather than raising, so a ``stall`` retry must be
    detected from the result, not the exception.
    """
    if not isinstance(output, dict):
        return False
    inner = output.get("output")
    if not isinstance(inner, dict):
        return False
    return isinstance(inner.get("stall_reason"), str) and bool(inner.get("stall_reason"))


async def _await_result(raw_fn: Callable[[dict[str, Any]], Any], state: dict[str, Any]) -> dict[str, Any]:
    """Invoke a node callable and await it only if it returns an awaitable.

    Node callables are a mix of sync (join / pure convergence) and async
    (agent / sandbox / connector / manual) factories, so the wrapper must not
    assume an awaitable result. Any non-dict result is coerced to a state dict
    (LangGraph node contract) via a safe update.
    """
    result = raw_fn(state)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        return result
    return {"output": result}


def _backoff_seconds(policy: NodeRetryPolicy, attempt_n: int) -> float:
    """Capped exponential backoff for a retry attempt.

    ``attempt_n`` is the 1-based attempt that just failed. Schedule:
    ``min(policy.backoff_seconds * 2 ** (attempt_n - 1), cap)`` — fixed when
    ``backoff_seconds`` is 0 (no sleep). Never exceeds the module cap so a
    malformed config cannot wedge a worker.
    """
    if policy.backoff_seconds <= 0:
        return 0.0
    return min(float(policy.backoff_seconds) * float(2 ** (attempt_n - 1)), _BACKOFF_CAP_SECONDS)


def _compensated_marker(node_id: str) -> dict[str, Any]:
    """State update marking a watched node completed via a compensation edge.

    Returned in place of the failed node's own output so the run CONTINUES —
    the graph sees the watched node as completed rather than failed, and
    ``_compensated`` is threaded for observability / the audit trail.
    """
    return {"_compensated": True, "_compensated_node": node_id}


def resolve_compensation_target(node_id: str, outgoing_edges: list[dict[str, Any]]) -> str | None:
    """Return the ``on_failure_target`` for *node_id*, or None.

    A compensation edge is an outgoing edge from *node_id* that declares an
    ``on_failure_target`` (rc.edge_has_compensation). The first matching edge
    wins; edges with no compensation config are ignored.
    """
    for edge in outgoing_edges or []:
        if rc.edge_has_compensation(edge):
            return rc._string_or_default(edge.get("on_failure_target"))
    return None


def make_retrying_node_fn(
    raw_fn: Callable[[dict[str, Any]], Any],
    *,
    node_id: str,
    node_def: dict[str, Any] | None,
    pipeline_retry_policy: Any,
    outgoing_edges: list[dict[str, Any]] | None = None,
    incoming_edges: list[dict[str, Any]] | None = None,
    raw_fn_resolver: Callable[[str], Callable[[dict[str, Any]], Any] | None] | None = None,
    idempotency_key: Callable[[str, dict[str, Any]], str | None] | None = None,
    node_defs: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Wrap a node's raw callable with per-node / per-edge retry + compensation.

    Arguments:
      raw_fn: the underlying node callable (``async (state) -> dict``).
      node_id: the node's id (for compensation marker / idempotency key).
      node_def: the node's graph definition (carries ``retry`` / ``idempotent``).
      pipeline_retry_policy: the pipeline-level ``retry_policy`` default.
      outgoing_edges: edges where this node is the source (for compensation).
      incoming_edges: edges where this node is the target (for per-edge retry,
        which re-executes the SOURCE).
      raw_fn_resolver: node-id -> raw callable, used to re-execute the source
        node for an edge retry / to invoke a compensation target. ``None``
        disables per-edge retry and compensation (no re-graph).
      idempotency_key: ``(node_id, state) -> scoped key | None`` — stamped onto the
        state before each retry re-invocation so a side-effecting node can
        dedupe its write across attempts. Runtime-provided (compiled graphs are
        cached across runs), so it reads the run identity from ``state``.
      node_defs: node-id -> node definition, used to resolve the SOURCE node's
        ``idempotent`` flag for the edge-retry fail-closed check.

    Returns an async callable with the same ``(state) -> dict`` contract.
    """
    effective_policy = rc.resolve_node_retry(node_def, pipeline_retry_policy)
    outgoing_edges = outgoing_edges or []
    incoming_edges = incoming_edges or []
    node_defs = node_defs or {}

    def _source_fail_closed(edge: dict[str, Any]) -> bool:
        source_id = rc._string_or_default(edge.get("source", edge.get("source_node_id")))
        return rc.node_is_fail_closed(node_defs.get(source_id))

    async def _invoke_with_key(
        fn: Callable[[dict[str, Any]], Any],
        key_node_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke a node callable, stamping the node-scoped idempotency key first.

        The key is stamped onto a COPY of ``state`` (never the caller's snapshot)
        so a side-effecting node can dedupe its write across retry/edge/
        compensation re-executions. This is the single choke point that wires the
        documented ``_NODE_IDEMPOTENCY_KEY`` guarantee onto EVERY re-execution
        path — the node's own retry, the per-edge SOURCE re-execution, the
        edge-retry target re-run, and the compensation-target invocation
        (FAR-402 MAJOR-3). The runtime provides ``idempotency_key`` so it can read
        the run identity from ``state``.
        """
        key = idempotency_key(key_node_id, state) if idempotency_key is not None else None
        if key is not None:
            state = dict(state)
            state[_NODE_IDEMPOTENCY_KEY] = key
        return await _await_result(fn, state)

    async def _invoke_raw(state: dict[str, Any]) -> dict[str, Any]:
        # Stamp the node-scoped idempotency key before the node body runs so a
        # side-effecting node can dedupe its write across retry attempts.
        return await _invoke_with_key(raw_fn, node_id, state)

    async def _edge_retry(state: dict[str, Any], failed_event: str | None) -> dict[str, Any] | None:
        """Re-execute an incoming edge's SOURCE node; return the re-run result.

        Per-edge retry fires ONLY when the node's own retry budget is exhausted
        and an incoming edge declares a transition ``retry``. It re-executes the
        SOURCE node (not the target). Fail-closed: a non-idempotent source is
        never re-executed.

        ``failed_event`` is the retry event of the watched node's OWN failure (the
        exception that landed us here). It is classified BEFORE any source is
        re-executed (FAR-402 MAJOR-2): if the watched node failed on a
        never-retryable terminal fault (e.g. ``ScriptFailedError``), re-executing
        the source and re-running the target would only reproduce the same
        exactly-once fault — so we fall through to compensation / normal failure
        WITHOUT re-running a side-effecting source.

        When the edge budget is exhausted (or the edge failure is non-retryable)
        the edge is skipped and the next incoming edge is tried; if no eligible
        edge produces a result we return ``None`` so ``_wrapped`` proceeds to
        compensation (FAR-402 MAJOR-1 — the trailing fall-through must be live,
        not dead code behind a raised exception).
        """
        # Never-retryable watched-node failure → do NOT re-run the source.
        if failed_event is None:
            return None
        for edge in incoming_edges:
            if not rc.edge_retry_reattempts_source(edge):
                continue
            if _source_fail_closed(edge):
                continue
            if raw_fn_resolver is None:
                continue
            source_id = rc._string_or_default(edge.get("source", edge.get("source_node_id")))
            source_fn = raw_fn_resolver(source_id)
            if source_fn is None:
                continue
            edge_policy = rc.parse_edge_retry(edge.get("retry")) or effective_policy
            attempts = 0
            # Re-execute the source (with its own idempotency key), then re-run
            # THIS node against the fresh source output. The edge budget bounds
            # the source re-executions.
            while attempts < edge_policy.max_attempts:
                attempts += 1
                try:
                    await _sleep(attempts, edge_policy)
                    src_out = await _invoke_with_key(source_fn, source_id, state)
                    merged = dict(state)
                    if isinstance(src_out, dict):
                        merged.update(src_out)
                    return await _invoke_with_key(raw_fn, node_id, merged)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    event = failure_event(exc)
                    if event is None or not rc.node_retries_on(edge_policy, event):
                        break
            # Edge budget exhausted / non-retryable → fall through to the next
            # edge, or to compensation / normal failure. Do NOT raise here, or the
            # compensation branch becomes unreachable (FAR-402 MAJOR-1).
            continue
        return None

    async def _wrapped(state: dict[str, Any]) -> dict[str, Any]:
        attempts = 0
        while True:
            attempts += 1
            try:
                result = await _invoke_raw(state)
                # "stall" is a returned marker, not an exception.
                if (
                    _stall_event(result)
                    and rc.node_retries_on(effective_policy, "stall")
                    and attempts < effective_policy.max_attempts
                ):
                    await _sleep(attempts, effective_policy)
                    continue
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                event = failure_event(exc)
                # Per-node retry: only when the event is retryable, on the
                # policy's event set, and budget remains.
                if (
                    event is not None
                    and rc.node_retries_on(effective_policy, event)
                    and attempts < effective_policy.max_attempts
                ):
                    await _sleep(attempts, effective_policy)
                    continue
                # Per-edge retry: node-retry exhausted → re-execute the source.
                # Pass the watched node's own failure event so the edge retry can
                # skip a side-effecting source re-execution for a never-retryable
                # terminal fault (FAR-402 MAJOR-2).
                edge_result = await _edge_retry(state, event)
                if edge_result is not None:
                    return edge_result
                # Compensation edge: terminal failure with an on_failure_target.
                # Control-flow terminal faults (RunCancelledError / GraphInterrupt /
                # EvalBlockedError / SupersededNodeError / OutputRejectedError /
                # RunawayRunError) MUST reach the run-level terminal path, NOT be
                # swallowed by a compensation edge that would continue the run. The
                # edge-retry branch above is guarded by the same invariant
                # (failure_event returns None for these), but the compensation branch
                # was not — so a watched node cancelled / interrupted / eval-blocked /
                # superseded / output-rejected / runaway would have its control-flow
                # exception absorbed and the run wrongly CONTINUED. Re-raise so the
                # executor can cancel / interrupt / eval-fail / supersede as designed.
                # NOTE: the script-execution terminal faults (Script*Error) are
                # intentionally NOT in this set — a script that failed exactly-once is
                # a genuine TERMINAL node failure a compensation edge may continue from
                # (see test_per_edge_retry_does_not_reexecute_source_for_never_retryable_major2).
                if _is_control_flow_fault(exc):
                    raise
                comp_target = resolve_compensation_target(node_id, outgoing_edges)
                if comp_target and raw_fn_resolver is not None:
                    comp_fn = raw_fn_resolver(comp_target)
                    if comp_fn is not None:
                        try:
                            await _invoke_with_key(comp_fn, comp_target, state)
                            return _compensated_marker(node_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception as comp_exc:
                            if _is_never_retryable(comp_exc):
                                raise
                            raise CompensationFailedError(
                                node_id=node_id,
                                compensation_target=comp_target,
                                cause=comp_exc,
                            ) from comp_exc
                # No retry / edge / compensation applies → normal failure.
                raise

    _wrapped.__name__ = f"retry_{node_id}"
    return _wrapped


async def _sleep(attempt_n: int, policy: NodeRetryPolicy) -> None:
    """Sleep the retry backoff (no-op when the policy schedule is 0)."""
    delay = _backoff_seconds(policy, attempt_n)
    if delay > 0:
        await asyncio.sleep(delay)


class CompensationFailedError(RuntimeError):
    """A watched node's compensation node itself failed.

    The wrapper raises this (in place of the watched node's own failure) so the
    executor can terminalize the run as ``COMPENSATION_FAILED`` — the status P5
    added. Carries the watched node id and the compensation target so the
    terminal detail readout is meaningful.
    """

    def __init__(self, *, node_id: str, compensation_target: str, cause: BaseException) -> None:
        super().__init__(f"Compensation node '{compensation_target}' for watched node '{node_id}' failed: {cause}")
        self.node_id = node_id
        self.compensation_target = compensation_target
        self.cause = cause
