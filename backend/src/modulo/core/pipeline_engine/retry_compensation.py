"""Per-node / per-edge retry + compensation edges (FAR-402 P5, §4F / §10).

This module is the *authoring + schema + validation* layer for failure handling
(design §F3: "an authoring + schema + validation layer"). It does NOT re-architect
the LangGraph executor — the runtime relies on the existing FAR-295 idempotency
harness (``executor._graph_is_idempotent``) and the FAR-410 idempotency-key
primitive (``stable_idempotency_key``).

What lives here:
  * The pure decision helpers for per-node retry configuration — how a node's own
    ``retry`` block overrides the pipeline ``retry_policy`` default, and which
    node events (``timeout`` / ``error`` / ``stall``) a node will retry on.
  * The pure per-edge transition-retry semantics: an edge retry re-runs its
    SOURCE node, is MUTUALLY EXCLUSIVE with a compensation / ``on_failure_target``
    edge, wraps the node's own retries, and is fail-closed when the source node
    is ``idempotent=false``.
  * The node-scoped idempotency key (``run + node + index``) derived from the
    FAR-410 ``stable_idempotency_key`` primitive, plus the run-level
    ``idempotency_key`` contract for operator re-runs.
  * The COMPILE-TIME validation routines used by ``GraphValidator``: retry-config
    shape, edge-retry/compensation mutual exclusion, and the ACYCLIC compensation
    check (a cyclic compensation graph is rejected with a typed error).

Nothing here imports the executor or the ORM — it is DB-free and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modulo.core.pipeline_engine.idempotency import stable_idempotency_key

__all__ = [
    "RETRY_BACKOFF_CAP_SECONDS",
    "RETRY_EVENTS",
    "RETRY_MAX_ATTEMPTS_BOUND",
    "NodeRetryPolicy",
    "RetryConfigError",
    "build_run_ref",
    "detect_compensation_cycle",
    "edge_has_compensation",
    "edge_retry_and_compensation_conflict",
    "edge_retry_reattempts_source",
    "node_idempotency_key",
    "node_is_fail_closed",
    "node_retries_on",
    "parse_edge_retry",
    "parse_node_retry",
    "resolve_node_retry",
    "run_idempotency_key",
    "validate_compensation_acyclic",
    "validate_compensation_target_exists",
    "validate_edge_mutual_exclusion",
    "validate_edge_retry_config",
    "validate_node_retry_config",
]


# The events a node (or edge transition) may retry on. The design (§4F) names
# them ``on: [timeout, error, stall]`` — distinct from the RUN-level
# ``retry_policy`` events (``stall`` / ``timeout`` / ``failure`` / ``eval_failed``)
# which describe whether a whole RUN is re-dispatched. A node retry is a finer-grained,
# within-run re-execution trigger.
RETRY_EVENTS: frozenset[str] = frozenset({"timeout", "error", "stall"})

# Hard bound on per-node / per-edge ``max_attempts`` (mirrors the run-level
# ``_RETRY_POLICY_MAX_RETRIES`` bound of 5).
RETRY_MAX_ATTEMPTS_BOUND = 5

# Maximum sane backoff (seconds) for a node / edge retry. Kept conservative so a
# malformed ``backoff`` (e.g. ``1e9``) cannot pin a worker in a sleep loop.
RETRY_BACKOFF_CAP_SECONDS = 300.0


class RetryConfigError(ValueError):
    """Raised when a ``retry`` configuration block is structurally invalid.

    The GraphValidator surfaces these as typed ``result.error`` entries rather
    than raising directly; this exception exists for the pure helpers so callers
    outside the validator (tests, the executor) can rely on a typed signal.
    """

    def __init__(self, message: str, code: str = "RETRY_CONFIG_MALFORMED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NodeRetryPolicy:
    """Resolved retry policy for a single node execution.

    ``max_attempts`` is the ceiling on TOTAL execution attempts (>=1). An
    ``events`` value of ``frozenset()`` means the node never retries (a retry
    config with all events empty is treated as "no retry").
    """

    max_attempts: int
    backoff_seconds: float
    events: frozenset[str]

    @property
    def retries(self) -> int:
        """The number of RETRIES implied by the attempt ceiling (max_attempts-1)."""
        return max(self.max_attempts - 1, 0)


def _coerce_events(events: Any) -> frozenset[str] | None:
    """Validate/normalise a ``retry`` ``on`` list to the RETRY_EVENTS set.

    Returns ``None`` when invalid (non-list, non-string entries, or unknown
    values), ``frozenset()`` for an empty list.
    """
    if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
        return None
    if set(events) - RETRY_EVENTS:
        return None
    return frozenset(events)


def parse_node_retry(raw: Any) -> NodeRetryPolicy | None:
    """Parse a node's ``retry`` block into a :class:`NodeRetryPolicy`.

    ``None`` (absent) and ``{}`` (empty) both mean "no node-level retry" — the
    node inherits the pipeline default. Raises :class:`RetryConfigError` for a
    structurally malformed block (wrong types / out-of-bounds budget). An empty
    ``on`` list is accepted as "no retry" (max_attempts clamped to 1).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RetryConfigError("node 'retry' must be an object like {'max_attempts': N, 'backoff': S, 'on': [...]}")
    max_attempts = raw.get("max_attempts", 1)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise RetryConfigError("retry 'max_attempts' must be an integer")
    if not 1 <= max_attempts <= RETRY_MAX_ATTEMPTS_BOUND:
        raise RetryConfigError(
            f"retry 'max_attempts' must be between 1 and {RETRY_MAX_ATTEMPTS_BOUND}",
            "RETRY_CONFIG_MALFORMED",
        )
    events = _coerce_events(raw.get("on", []))
    if events is None:
        raise RetryConfigError(
            "retry 'on' must be a list of strings from ['stall', 'error', 'timeout']",
            "RETRY_CONFIG_MALFORMED",
        )
    backoff = raw.get("backoff", 0.0)
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)):
        raise RetryConfigError("retry 'backoff' must be a number of seconds", "RETRY_CONFIG_MALFORMED")
    # A blanket attempt ceiling of 1 (or 0) means "no retry".
    if max_attempts <= 1:
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    # Nevers retry when the event set is empty.
    if not events:
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    backoff_sec = min(float(max(backoff, 0.0)), RETRY_BACKOFF_CAP_SECONDS)
    return NodeRetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff_sec, events=events)


def resolve_node_retry(node: dict[str, Any] | None, pipeline_retry_policy: Any) -> NodeRetryPolicy:
    """Resolve the EFFECTIVE retry policy for a node.

    A node's own ``retry`` block overrides the pipeline-level ``retry_policy``
    default; a node with no ``retry`` config inherits the pipeline default. The
    run-level ``retry_policy`` uses the ``{on: [stall|timeout|failure|eval_failed],
    max_retries}`` shape; node overrides use ``{max_attempts, backoff,
    on: [timeout|error|stall]}``. The two vocabularies are reconciled here so the
    executor never has to branch on both shapes.
    """
    # Fail-closed: a node explicitly declared non-idempotent is NEVER auto-retried
    # (retrying would re-run a side-effecting node).
    if node is not None and node.get("idempotent") is False:
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    node_retry = node.get("retry") if isinstance(node, dict) else None
    if node_retry is not None:
        parsed = parse_node_retry(node_retry)
        if parsed is not None:
            return parsed
    return _policy_from_pipeline_default(pipeline_retry_policy)


def _policy_from_pipeline_default(pipeline_retry_policy: Any) -> NodeRetryPolicy:
    """Translate a run-level ``retry_policy`` to a :class:`NodeRetryPolicy`.

    A run-level policy that says retry on ``stall``/``timeout``/``failure``/``eval_failed``
    becomes a node-level retry on the corresponding node events (``stall`` maps
    to ``stall``, ``timeout`` to ``timeout``, ``failure`` to ``error``; ``eval_failed``
    is handled by the executor-level guardrail path, not by node-level retries). The
    run-level ``max_retries`` is the retry budget, so the attempt ceiling is
    ``max_retries + 1``.
    """
    if not isinstance(pipeline_retry_policy, dict):
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    events_raw = pipeline_retry_policy.get("on")
    if not isinstance(events_raw, list):
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    node_events: set[str] = set()
    for e in events_raw:
        if e == "stall":
            node_events.add("stall")
        elif e == "timeout":
            node_events.add("timeout")
        elif e in ("failure", "error"):
            node_events.add("error")
    max_retries = pipeline_retry_policy.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries <= 0:
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    if not node_events:
        return NodeRetryPolicy(max_attempts=1, backoff_seconds=0.0, events=frozenset())
    max_attempts = min(max_retries + 1, RETRY_MAX_ATTEMPTS_BOUND)
    backoff = pipeline_retry_policy.get("backoff", 0.0)
    backoff_sec = min(float(max(backoff, 0.0)), RETRY_BACKOFF_CAP_SECONDS) if isinstance(backoff, (int, float)) else 0.0
    return NodeRetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff_sec, events=frozenset(node_events))


def node_retries_on(policy: NodeRetryPolicy, event: str) -> bool:
    """True when ``policy`` retries on ``event`` and a retry budget remains."""
    return event in policy.events and policy.max_attempts > 1


def node_is_fail_closed(node: dict[str, Any] | None) -> bool:
    """True when the node must not be auto-retried (``idempotent=false``)."""
    return bool(node is not None and node.get("idempotent") is False)


# ---------------------------------------------------------------------------
# Per-edge retry semantics (§4F)
# ---------------------------------------------------------------------------


def parse_edge_retry(raw: Any) -> NodeRetryPolicy | None:
    """Parse a transition-edge ``retry`` block.

    An edge retry re-executes the edge's SOURCE node; its config shares the node
    ``max_attempts``/``backoff``/``on`` shape. ``None``/{}/absent → no edge retry.
    """
    return parse_node_retry(raw)


def edge_retry_reattempts_source(edge: dict[str, Any]) -> bool:
    """True when the edge declares a transition retry (re-executes its source)."""
    retry = edge.get("retry")
    if retry is None:
        return False
    parsed = parse_edge_retry(retry)
    return parsed is not None and parsed.max_attempts > 1 and bool(parsed.events)


def edge_has_compensation(edge: dict[str, Any]) -> bool:
    """True when the edge declares an ``on_failure_target`` compensation edge."""
    value = edge.get("on_failure_target")
    return value not in (None, "")


def edge_retry_and_compensation_conflict(edge: dict[str, Any]) -> bool:
    """True when an edge declares BOTH a transition retry and a compensation target.

    A failure is either edge-retried OR routed to a compensation/``on_failure``
    target, never both (design §4F). The GraphValidator emits a typed error.
    """
    return edge_retry_reattempts_source(edge) and edge_has_compensation(edge)


# ---------------------------------------------------------------------------
# Idempotency keys (§4F / §10 R7 — two keys)
# ---------------------------------------------------------------------------


def node_idempotency_key(
    *,
    run_ref: str,
    node_ref: str,
    index: int | str | None = None,
    payload: str | bytes | None = None,
) -> str:
    """The NODE-SCOPED idempotency key ``run + node + index`` (within-run dedupe).

    Wraps the FAR-410 ``stable_idempotency_key`` primitive. ``run_ref`` MUST be
    the stable logical identity ``<pipeline_id>:<run_number>`` (recomputed on a
    re-run), never the per-replay ``run_id`` — a fresh ``run_id`` would mint a
    fresh key and defeat scatter/retry dedupe. ``index`` is the fan-out /
    cardinality position (``None`` for a single-execution node).

    Used to dedupe within-run scatter/retry re-executions: a node that already
    has a completed output for its ``run+node+index`` key is not executed again.
    """
    return stable_idempotency_key(run_ref=run_ref, node_ref=node_ref, index=index, payload=payload)


#: Sentinel ``node_ref`` for the RUN-LEVEL key (the whole operator re-run).
_RUN_KEY_NODE = "__run__"


def build_run_ref(pipeline_id: str | int, run_number: int) -> str:
    """The stable logical run identity ``<pipeline_id>:<run_number>``.

    Recomputed on an operator re-run from the pipeline identity — NEVER the
    per-replay ``run_id``. This is the ``run_ref`` that must be handed to the
    node-scoped and run-level key derivations so a re-run reuses the same key.
    """
    return f"{pipeline_id}:{run_number}"


def run_idempotency_key(*, run_ref: str, payload: str | bytes | None = None) -> str:
    """The RUN-LEVEL idempotency key (FAR-410) for an operator re-run.

    Derived deterministically from the stable logical run identity
    (``<pipeline_id>:<run_number>``) with an empty node dimension. An operator
    re-run of an ``UNKNOWN`` run recomputes the SAME run-level key (a fresh
    ``run_id`` is ignored because it is not part of the derivation), so a write
    that may have reached the upstream is not re-applied as a fresh operation.
    """
    return stable_idempotency_key(run_ref=run_ref, node_ref=_RUN_KEY_NODE, payload=payload)


# ---------------------------------------------------------------------------
# Compile-time validation (consumed by ``GraphValidator``)
# ---------------------------------------------------------------------------


def validate_node_retry_config(node: dict[str, Any], nid: str, result: Any) -> None:
    """Emit a typed error when a node's ``retry`` block is malformed."""
    raw = node.get("retry")
    if raw is None:
        return
    try:
        parse_node_retry(raw)
    except RetryConfigError as exc:
        result.error(exc.code, f"Node '{nid}': {exc}", node_id=nid)


def validate_edge_retry_config(edge: dict[str, Any], nid: str, result: Any) -> None:
    """Emit a typed error when a transition edge's ``retry`` block is malformed."""
    source = edge.get("source", edge.get("source_node_id"))
    raw = edge.get("retry")
    if raw is None:
        return
    try:
        parse_edge_retry(raw)
    except RetryConfigError as exc:
        result.error(exc.code, f"Edge from '{_string_or_default(source)}': {exc}", node_id=_string_or_default(source))


def validate_edge_mutual_exclusion(edge: dict[str, Any], result: Any) -> None:
    """Emit a typed error when an edge declares BOTH a retry and a compensation.

    Design §4F: ``on_failure_target`` and a transition ``retry`` are mutually
    exclusive per failure — a failure is either edge-retried OR routed to a
    compensation / on_failure target, never both.
    """
    source = edge.get("source", edge.get("source_node_id"))
    if edge_retry_and_compensation_conflict(edge):
        result.error(
            "EDGE_RETRY_COMPENSATION_EXCLUSIVE",
            f"Edge from '{_string_or_default(source)}': a failure is either edge-retried OR routed to an "
            "'on_failure_target', never both ('retry' and 'on_failure_target' are mutually exclusive).",
            node_id=_string_or_default(source),
        )


def validate_compensation_target_exists(edges: list[dict[str, Any]], nodes: list[dict[str, Any]], result: Any) -> None:
    """Emit a typed error when an edge's ``on_failure_target`` is not a node."""
    node_ids = {_string_or_default(n.get("id")) for n in nodes if isinstance(n, dict)}
    for edge in edges:
        target = edge.get("on_failure_target")
        if target in (None, ""):
            continue
        source = edge.get("source", edge.get("source_node_id"))
        if _string_or_default(target) not in node_ids:
            result.error(
                "COMPENSATION_TARGET_NOT_FOUND",
                f"Edge from '{_string_or_default(source)}': on_failure_target '{target}' does not match any node id.",
                node_id=_string_or_default(source),
            )


def detect_compensation_cycle(graph_json: dict[str, Any]) -> list[list[str]]:
    """Return every compensation-edge cycle in the graph (empty = acyclic).

    The compensation relation is ``source -> on_failure_target`` for each edge
    carrying an ``on_failure_target``. A cycle among these edges means the
    compensation path can loop infinitely (a compensation node's failure routes
    back to an upstream compensation), which the design FORBIDS at compile time.
    Nested / composite sub-pipelines are validated at their own boundary; this
    detects cycles in the current graph's compensation edges.
    """
    nodes = graph_json.get("nodes", []) if isinstance(graph_json, dict) else []
    edges = graph_json.get("edges", []) if isinstance(graph_json, dict) else []
    node_ids = {_string_or_default(n.get("id")) for n in nodes if isinstance(n, dict)}

    # Build the compensation adjacency (only edges whose on_failure_target is a
    # real node). Use an insertion-ordered dict so results are deterministic.
    adj: dict[str, list[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = _string_or_default(edge.get("source", edge.get("source_node_id")))
        target = edge.get("on_failure_target")
        if target in (None, "") or _string_or_default(target) not in node_ids:
            continue
        target = _string_or_default(target)
        adj.setdefault(source, []).append(target)

    # DFS with an explicit "in current stack" marker. Iterative to avoid recursion
    # depth issues on wide graphs (there is no recursion limit concern here, but
    # iterative is simpler to keep deterministic order).
    cycles: list[list[str]] = []
    state: dict[str, int] = {}  # 0=unvisited, 1=in-stack, 2=done

    def _dfs(start: str, stack: list[str]) -> bool:
        # Returns True if a cycle was found rooted through ``start``.
        state[start] = 1
        stack.append(start)
        for nxt in adj.get(start, []):
            if state.get(nxt, 0) == 1:
                # cycle: from nxt back through the current stack
                idx = stack.index(nxt)
                cycles.append([*stack[idx:], nxt])
                continue
            if state.get(nxt, 0) == 0:
                _dfs(nxt, stack)
        stack.pop()
        state[start] = 2
        return bool(cycles)

    for node in node_ids:
        if state.get(node, 0) == 0:
            _dfs(node, [])

    # De-duplicate cycles (rotation-invariant) and cap the count to keep the
    # error message bounded.
    seen: set[str] = set()
    unique: list[list[str]] = []
    for cycle in cycles:
        canonical = min(cycle)
        start_at = cycle.index(canonical)
        normalized = tuple(cycle[start_at:] + cycle[:start_at])
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        unique.append(list(normalized))
    return unique


def validate_compensation_acyclic(graph_json: dict[str, Any], result: Any) -> None:
    """Emit a typed error when the compensation graph contains a cycle (§4F)."""
    cycles = detect_compensation_cycle(graph_json)
    # The detector may surface the same loop with different rotation for multiple
    # entries; but it de-dupes. Bound the message.
    cycle_desc = "; ".join(" -> ".join(c) for c in cycles[:3])
    if cycles:
        result.error(
            "COMPENSATION_CYCLE",
            f"Compensation graph must be ACYCLIC; found cycle(s): {cycle_desc}. "
            "A compensation node's failure cannot route back to an upstream compensation node.",
        )


def _string_or_default(value: Any) -> str:
    return str(value) if value is not None else ""
