"""Scatter (fan-out) + Join (fan-in) node semantics — FAR-402 P3 / FAR-417.

Implements FAR-402 design §4 B:

* **Scatter (fan-out):** a node with ``fan_out`` splits a source port's iterable
  payload into N branches. Compiles to **N DISTINCT child node identities**
  (unique ``node_id = parent + index``) so audit / claim / feedback keys stay
  unique. A hard ceiling enforces fail-closed behaviour (no unbounded
  materialisation). Branch execution is sequential in P3; the contract only
  exposes knobs that the runtime actually honours (see ``FanOutConfig``).
* **Join (fan-in):** a ``join`` node collects upstream branch outputs and
  aggregates them (``concat`` | ``merge_by_key`` | ``map``). The collected
  outputs are merged with ``cost_controller.finalize._merge_stored_outputs``
  (design §4 B) before aggregation, so the merge semantics match the rest of
  the cost-controller output handling.

This module is the pure, fully-unit-testable core. The runtime LangGraph node
functions (``make_scatter_node_fn`` / ``make_join_node_fn``) are thin adapters
that call into here with injected child-execution and event hooks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Literal

import jmespath
from pydantic import BaseModel, Field, ValidationError

from modulo.core.cost_controller.finalize import _merge_stored_outputs

_log = logging.getLogger("modulo.core.pipeline_engine.scatter_join")

# Per-expression JMESPath compile cache (design §4 B R1): compile once, reuse
# across every collected branch so fan-out over many items stays cheap.
_JMESPATH_CACHE: dict[str, Any] = {}

# Default hard ceiling on fan-out cardinality (design §4 B: "Hard ceiling
# required (no unbounded materialization)").
FANOUT_DEFAULT_MAX = 1000

JoinAggregateKind = Literal["concat", "merge_by_key", "map", "custom_function"]
JoinPartialPolicy = Literal["collect_and_proceed", "fail"]


class FanOutConfig(BaseModel):
    """Declares a scatter (fan-out) on an agent / sandbox_agent node.

    Only the ``split`` source and the ``max_items`` cardinality ceiling are
    honoured by the runtime — there is no batching/parallelism in P3, so those
    knobs are intentionally absent from the contract rather than accepted and
    silently ignored.
    """

    split: str = Field(description="Name of the source port / state key to split.")
    max_items: int | None = Field(
        default=None,
        ge=1,
        description="Hard ceiling on fan-out cardinality. Defaults to FANOUT_DEFAULT_MAX.",
    )


class JoinCollectSpec(BaseModel):
    """One upstream branch a join node collects from.

    Only the parent ``node`` is read by the runtime (it locates the scatter
    manifest and merges every child output of that parent). The ``port`` knob
    was removed from the contract because no code path selected a specific
    output port; collecting all child outputs of the parent is the supported
    semantics.
    """

    node: str = Field(description="Parent (scatter) node id this branch belongs to.")


class JoinAggregateSpec(BaseModel):
    """Aggregation applied by a join node to its collected branches."""

    kind: JoinAggregateKind
    key: str | None = Field(
        default=None,
        description="Field name for merge_by_key (read from each collected item's output).",
    )
    map_expression: str | None = Field(default=None, description="JMESPath expression for map aggregation.")


class ScatterJoinError(Exception):
    """Base class for scatter/join typed errors."""


class FanOutCapExceededError(ScatterJoinError):
    """Raised when the split source exceeds the configured fan-out cap."""


class JoinAggregateUnsupportedError(ScatterJoinError):
    """Raised when a node requests an aggregate not supported in P3."""


class JoinConfigurationError(ScatterJoinError):
    """Raised when a scatter/join node is misconfigured."""


def scatter_child_node_id(parent_node_id: str, index: int) -> str:
    """Correlation token for child ``index`` of a scatter node.

    Unique across the run so audit / claim / feedback keys stay distinct
    (design: scatter→Join correlation token = the parent+index identity).
    """
    return f"{parent_node_id}__scatter_{index}"


def effective_fan_out_cap(config: FanOutConfig) -> int:
    """Resolve the effective fan-out cap for a config."""
    return config.max_items if config.max_items is not None else FANOUT_DEFAULT_MAX


def validate_fan_out_count(count: int, cap: int) -> None:
    """Fail-closed: raise ``FanOutCapExceededError`` BEFORE any request when count > cap."""
    if count > cap:
        raise FanOutCapExceededError(f"Fan-out cardinality {count} exceeds the configured cap of {cap}")


def expand_scatter_nodes(node_def: dict[str, Any], items: list[Any]) -> list[dict[str, Any]]:
    """Compile a scatter node into **N DISTINCT child node defs**.

    Each child is a clone of ``node_def`` with a unique ``id``
    (= ``scatter_child_node_id(parent, index)``) and the per-index item attached
    under ``scatter_item``. The scatter node itself is NOT reused for multiple
    branches, satisfying the design's "N distinct graph nodes" requirement.

    Raises:
        FanOutCapExceededError: when ``len(items)`` exceeds the configured cap
            (fail-closed, before any branch is produced).
        JoinConfigurationError: when the node has no ``fan_out`` config.
    """
    raw = node_def.get("fan_out")
    if raw is None:
        raise JoinConfigurationError("expand_scatter_nodes requires a fan_out config")
    cfg = raw if isinstance(raw, FanOutConfig) else FanOutConfig.model_validate(raw)
    cap = effective_fan_out_cap(cfg)
    validate_fan_out_count(len(items), cap)

    parent_id = str(node_def["id"])
    children: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        child = dict(node_def)
        child["id"] = scatter_child_node_id(parent_id, index)
        child["scatter_parent"] = parent_id
        child["scatter_index"] = index
        child["scatter_item"] = item
        # The child is a concrete execution, not a scatter itself.
        child.pop("fan_out", None)
        children.append(child)
    return children


def _collect_outputs(collected: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge collected child outputs into a single node_id→output dict.

    Reuses ``cost_controller.finalize._merge_stored_outputs`` (design §4 B) so the
    merge semantics match the rest of the cost-controller output handling. Each
    collected entry is keyed by its (already-unique) child node id.
    """
    merged_outputs: dict[str, Any] = {}
    merged_telemetry: dict[str, Any] = {}
    node_type_map: dict[str, str] = {}
    for entry in collected:
        node_id = str(entry.get("node_id", ""))
        output = entry.get("output")
        stored_out = {node_id: output} if output is not None else {}
        stored_tel = {node_id: {"status": entry.get("status", "succeeded")}}
        _merge_stored_outputs(merged_outputs, merged_telemetry, stored_out, stored_tel, node_type_map, None)
    return merged_outputs


def _apply_map(output: Any, expression: str) -> Any:
    """Apply a JMESPath ``map_expression`` to a single collected item.

    Compiles the expression once and caches it per expression string so a
    scatter→join with many branches only pays the JMESPath compile cost a
    single time (design §4 B R1).
    """
    try:
        compiled = _JMESPATH_CACHE.get(expression)
        if compiled is None:
            compiled = jmespath.compile(expression)
            _JMESPATH_CACHE[expression] = compiled
    except jmespath.exceptions.JMESPathError as exc:  # pragma: no cover - defensive
        raise JoinConfigurationError(f"Invalid map_expression JMESPath: {exc}") from exc
    return compiled.search(output if isinstance(output, (dict, list)) else {"value": output})


def aggregate_join_results(
    collected: list[dict[str, Any]],
    spec: JoinAggregateSpec,
    *,
    partial_policy: JoinPartialPolicy = "collect_and_proceed",
) -> dict[str, Any]:
    """Aggregate collected branch results per ``spec``.

    ``collected`` is a list of per-branch dicts:
        ``{"node_id", "output", "status": "succeeded"|"failed"|"timed_out", "error"?}``

    Returns a structured result carrying the aggregated value, a per-child status
    map (failed branches are marked, design §4 B), and an ``empty`` flag for the
    typed-empty case.
    """
    if spec.kind == "custom_function":
        # Deferred in P3 (design §4 B). Fall back to concat and surface the note.
        raise JoinAggregateUnsupportedError(
            "custom_function aggregate is deferred in P3 (follow-up). Use concat | merge_by_key | map."
        )

    if not collected:
        # Design: empty-collection → typed empty result.
        return {"aggregated": None, "branches": [], "empty": True, "status": "empty"}

    failed = [c for c in collected if c.get("status") != "succeeded"]

    if partial_policy == "fail" and failed:
        raise JoinConfigurationError(f"Join over {len(failed)} failed branch(es) with partial_policy='fail'.")

    merged = _collect_outputs(collected)

    if spec.kind == "concat":
        aggregated: Any = [merged.get(str(c.get("node_id"))) for c in collected]
    elif spec.kind == "merge_by_key":
        if not spec.key:
            raise JoinConfigurationError("merge_by_key requires an explicit 'key'.")
        aggregated = {}
        for c in collected:
            out = merged.get(str(c.get("node_id")))
            if not isinstance(out, dict):
                raise JoinConfigurationError("merge_by_key requires dict outputs.")
            key_val = out.get(spec.key)
            aggregated[key_val] = out
    elif spec.kind == "map":
        if not spec.map_expression:
            raise JoinConfigurationError("map requires a 'map_expression'.")
        aggregated = [_apply_map(merged.get(str(c.get("node_id"))), spec.map_expression) for c in collected]
    else:  # pragma: no cover - guarded by Literal
        raise JoinConfigurationError(f"unknown aggregate kind {spec.kind!r}")

    status = "completed" if not failed else "partial"
    return {
        "aggregated": aggregated,
        "branches": [{"node_id": c.get("node_id"), "status": c.get("status", "succeeded")} for c in collected],
        "empty": False,
        "status": status,
    }


def child_teardown_dedup_key(run_id: str, node_id: str, index: int) -> str:
    """Idempotent teardown dedupe key (design §4 B: ``run+node+index``).

    Emitted into the ``scatter.complete`` observability event as a stable key
    that a future teardown/cancellation hook can use to dedupe child cleanup
    across run cancellation, join completion, and scatter-level failure. There
    is no consumer in P3 yet — this is a forward-compatibility hook, not an
    active cancellation mechanism.
    """
    return f"{run_id}::{node_id}::{index}"


def emit_scatter_event(event: str, **attrs: Any) -> None:
    """Emit a structured scatter/join observability event.

    Events: ``scatter.start``, ``scatter.complete``, ``join.partial``,
    ``join.failed``, ``join.completed`` (design §4 B observability). Uses the
    same structured ``extra=`` logging convention as the cost-controller finalize
    module so downstream OTel/audit pipelines pick them up uniformly.
    """
    _log.info(event, extra={"scatter_join_event": event, **attrs})


# --------------------------------------------------------------------------- #
# Runtime adapters (thin: call into the pure core with injected hooks).
# --------------------------------------------------------------------------- #


async def run_scatter_node(
    node_def: dict[str, Any],
    *,
    items: list[Any],
    execute_child: Callable[[dict[str, Any]], Any],
    emit_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute a scatter node against materialised ``items``.

    Produces N child branch executions (each with a unique correlation id used as
    the audit/claim/feedback key) via ``execute_child`` and returns a state
    fragment carrying each child's output keyed by its child node id.

    ``execute_child`` may be sync OR async (returning a coroutine) — the latter is
    the LangGraph runtime path where each child is a real async node function.
    Either way the child's result is resolved before ``results`` is populated, so
    fan-out branches actually execute (a sync-only call would store un-awaited
    coroutines and never run the children).

    An empty ``items`` iterator succeeds vacuously with no child calls (design
    §4 B).
    """
    emit = emit_event or emit_scatter_event
    parent_id = str(node_def["id"])
    cfg = node_def.get("fan_out")
    cfg = cfg if isinstance(cfg, FanOutConfig) else FanOutConfig.model_validate(cfg)
    cap = effective_fan_out_cap(cfg)
    emit("scatter.start", node_id=parent_id, cap=cap)

    if not items:
        emit("scatter.complete", node_id=parent_id, count=0, vacuous=True)
        return {}

    children = expand_scatter_nodes(node_def, items)
    results: dict[str, Any] = {}
    teardown_keys: list[str] = []
    for child in children:
        child_id = str(child["id"])
        output = execute_child(child)
        if asyncio.iscoroutine(output):
            output = await output
        results[child_id] = output
        # Idempotent teardown dedupe key (design §4 B): ties child teardown to
        # run cancellation AND join completion AND scatter-level failure.
        teardown_keys.append(child_teardown_dedup_key(parent_id, child_id, child["scatter_index"]))
    emit(
        "scatter.complete",
        node_id=parent_id,
        count=len(children),
        vacuous=False,
        teardown_keys=teardown_keys,
    )
    return results


def run_join_node(
    node_def: dict[str, Any],
    *,
    collected: list[dict[str, Any]],
    emit_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute a join node over already-resolved ``collected`` branch results."""
    emit = emit_event or emit_scatter_event
    node_id = str(node_def["id"])
    raw = node_def.get("aggregate")
    spec = raw if isinstance(raw, JoinAggregateSpec) else JoinAggregateSpec.model_validate(raw)
    partial_policy: JoinPartialPolicy = node_def.get("join_partial_policy", "collect_and_proceed")

    try:
        result = aggregate_join_results(collected, spec, partial_policy=partial_policy)
    except JoinAggregateUnsupportedError:
        emit("join.failed", node_id=node_id, reason="custom_function_unsupported")
        raise
    except JoinConfigurationError as exc:
        emit("join.failed", node_id=node_id, reason=str(exc))
        raise

    if result.get("status") == "empty":
        emit("join.completed", node_id=node_id, empty=True)
    elif result.get("status") == "partial":
        emit("join.partial", node_id=node_id, branches=len(collected))
    else:
        emit("join.completed", node_id=node_id, branches=len(collected))
    return result


def validate_scatter_join_node(node_def: dict[str, Any]) -> None:
    """Validate a single graph node's scatter/join configuration (compile-time).

    Raises ``JoinConfigurationError`` (a typed error) on a malformed config so
    graph validation can fail closed.
    """
    node_type = node_def.get("node_type")
    fan_out = node_def.get("fan_out")
    collect = node_def.get("collect")
    aggregate = node_def.get("aggregate")

    if node_type == "join":
        if not collect or not aggregate:
            raise JoinConfigurationError("join node requires both 'collect' and 'aggregate'.")
        # Validate shapes.
        try:
            [JoinCollectSpec.model_validate(c) for c in collect]
        except ValidationError as exc:
            raise JoinConfigurationError(f"invalid join collect spec: {exc}") from exc
        try:
            spec = (
                aggregate if isinstance(aggregate, JoinAggregateSpec) else JoinAggregateSpec.model_validate(aggregate)
            )
        except ValidationError as exc:
            raise JoinConfigurationError(f"invalid join aggregate spec: {exc}") from exc
        if spec.kind == "merge_by_key" and not spec.key:
            raise JoinConfigurationError("join aggregate merge_by_key requires 'key'.")
        if spec.kind == "map" and not spec.map_expression:
            raise JoinConfigurationError("join aggregate map requires 'map_expression'.")
        return

    if fan_out is not None:
        try:
            FanOutConfig.model_validate(fan_out)
        except ValidationError as exc:
            raise JoinConfigurationError(f"invalid fan_out config: {exc}") from exc
        allowed = node_def.get("node_type") in ("agent", "sandbox_agent")
        if not allowed:
            raise JoinConfigurationError("fan_out is only allowed on agent / sandbox_agent nodes.")
