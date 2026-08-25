"""PipelineExecutor — orchestrates a single run end-to-end.

Responsibilities:
  - Seed initial LangGraph state from snapshot.run_context_defaults + input_payload
  - Obtain/compile the StateGraph (via graph_cache)
  - Enforce per-pipeline max_concurrent_runs, the per-org sandbox
    concurrency cap, and the per-org run-concurrency cap via count-based
    capacity checks (no FOR UPDATE; runs declined at capacity are demoted
    back to ``pending`` with a reason marker and recovered by
    ``dispatcher_reconcile`` (cron_helpers) when a slot frees, with
    ``stale_run_recovery_sweep``'s stranded re-dispatch as the durable
    liveness backstop — plan F3b: there is NO in-process retry loop)
  - Consume astream_events() and publish to the per-run RunEventBroker
  - Set up AsyncPostgresSaver as LangGraph checkpointer
  - Stream graph execution, updating Run status on transitions
  - Mark run complete/failed/cancelled/awaiting_human/eval_failed in DB

Handles GraphInterrupt by transitioning the run to awaiting_human.
Does NOT handle WebSocket fan-out, HITL claim/approve/reject, or webhook triggers (phases 3+).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import socket
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.errors import GraphInterrupt, NodeCancelledError
from opentelemetry import context as context_api
from opentelemetry.trace import set_span_in_context
from sqlalchemy import Boolean, Uuid, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.audit_logger import append_audit_event
from modulo.core.connector_hub.locking import _uuid_to_lock_keys
from modulo.core.cost_controller.finalize import derive_node_type_map, finalize_cost
from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalEngine,
    EvalSuiteBlockedError,
    SuiteEvalResult,
    evaluate_suite,
)
from modulo.core.eval_engine import (
    EvalDefinition as EvalDefDTO,
)
from modulo.core.eval_engine import (
    EvalResult as EngineEvalResult,
)
from modulo.core.graph_validator import GraphValidator
from modulo.core.graph_validator._types import ValidationResult
from modulo.core.hitl_manager import HITLManager
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.node_output_split import (
    DEFAULT_NODE_TYPE,
    SPLITTABLE_NODE_TYPES,
    resolve_node_contract_output,
)
from modulo.core.notifier import EVENT_HITL_AWAITING
from modulo.core.pipeline_engine.decorator import (
    RunCancelledError,
    set_audit_hook,
    set_cancellation_check,
    set_connector_hub,
    set_model_backend_hub,
)
from modulo.core.pipeline_engine.error_codes import map_legacy_code, sanitize_error_text
from modulo.core.pipeline_engine.event_broker import RunEventBroker, get_registry
from modulo.core.pipeline_engine.evidence import (
    EvidenceProvider,
    build_default_evidence_provider,
    compute_work_intact,
    evidence_enabled,
    node_declared_success,
    run_evidence_probe,
)
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json, get_or_compile
from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver
from modulo.core.pipeline_engine.node_runner import (
    OutputSchemaValidationError,
    SandboxNodeFailedError,
    SupersededNodeError,
    _idempotency_gate_skipped_envelope,
    _marker_delivery_done_for_node,
    set_conformance_ctx,
)
from modulo.core.pipeline_engine.output_filter import OutputRejectedError
from modulo.core.pipeline_engine.port_resolver import compute_port_topology_hash
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from modulo.core.trigger_engine.agent_signal import fire_agent_signal
from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.run import (
    ERROR_CODE_ORG_CAPACITY_LIMITED,
    ERROR_CODE_PIPELINE_CAPACITY,
    _graph_contains_sandbox_agent,
    count_active_runs_for_org,
    count_active_runs_for_pipeline,
    count_active_sandbox_runs_for_org,
    get_org_run_concurrency_limit,
    get_run,
    get_sandbox_concurrency_limit,
    update_run_status,
)
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import ACTIVE_RUN_STATUSES, TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.otel_bridge import LangGraphOtelBridge, trace_id_for_thread

_WORKER_ID: str = f"{socket.gethostname()}:{os.getpid()}"

# Statuses a run may still be admitted from when a retry's backoff elapses.
# Any terminal status (complete/failed/cancelled/eval_failed) means the run
# was already finalised while the retry loop slept — it must never be
# resurrected back to ``running``. Single-sourced from
# ``db.models.run.ACTIVE_RUN_STATUSES`` (the never-entered ``waiting_for_lock``
# sub-state was excised in migration 0074/0075).
_ADMISSIBLE_STATUSES = ACTIVE_RUN_STATUSES

# Terminal statuses. A run in one of these is already finalised; it must never
# be resurrected AND must never spawn (or hold) a retry task. Single-sourced
# from ``db.models.run.TERMINAL_STATUSES`` (includes ``stalled`` since #1011).
_TERMINAL_STATUSES = TERMINAL_STATUSES

_SANDBOX_AGENT_CACHE: OrderedDict[str, bool] = OrderedDict()
_SANDBOX_AGENT_CACHE_MAX = 512

_log = logging.getLogger(__name__)

# Pipeline retry_policy events (must stay in sync with the API schema in
# api/routes/pipelines.py and the graph validator). A policy can retry on:
#   - "stall":    run ended "stalled" / error_code "executor_stalled"
#   - "timeout":  error_code "node_timeout" / "TimeoutError"
#   - "failure":  any other "failed" terminal status (excluding sandbox-agent
#                 hang deaths — error_code "node_cancelled" + "likely hung" in
#                 error_detail — see ``_retry_after_policy``, FAR-136)
_RETRY_POLICY_EVENTS = frozenset({"stall", "timeout", "failure"})
_RETRY_POLICY_MAX_RETRIES = 5

# Canonical dotted error codes written at terminalization (agent-failure UX) and
# matched by the retry policy. Constants are pure aliases so the retry policy,
# the failure write, and log names cannot drift (S1192).
_ERROR_CODE_AGENT_FAILED = "agent.failed"
_ERROR_CODE_NODE_TIMEOUT = "node.timeout"
_ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN = "script.side_effect_unknown"
_ERROR_CODE_HARNESS_IDEMPOTENCY_GATE = "harness.idempotency_gate"

# Backoff schedule for a retry_policy re-dispatch (FAR-136). A policy-triggered
# retry must NOT re-fire back-to-back — the run is re-dispatched only after a
# jittered, capped exponential delay. ``base`` is the first-attempt wait; the
# delay doubles per attempt and is capped at ``cap``. Jitter spreads re-dispatch
# across the fleet (a herd of failing pipelines must not all re-fire together).
_RETRY_BACKOFF_BASE_SECONDS = 45.0
_RETRY_BACKOFF_CAP_SECONDS = 300.0
# Jitter range as a fraction of the current schedule value; uniform in
# [0, fraction * delay] so the schedule keeps its exponential shape while
# still decorrelating concurrent retries. Capped against ``_RETRY_BACKOFF_CAP_SECONDS``.
_RETRY_BACKOFF_JITTER_FRACTION = 0.25


def _sanitize_detail(detail: Any, limit: int = 5000) -> str:
    """Sanitize an error detail at a write surface, THEN truncate (FAR-163).

    Redaction runs before truncation so a secret straddling the cut point is
    still removed. Never raises — :func:`sanitize_error_text` coerces any input
    via ``str()`` and is a NO-OP for clean strings.
    """
    return sanitize_error_text(detail)[:limit]


def _traceback_detail(exc: BaseException, limit: int = 2000) -> str:
    """Sanitized, bounded traceback text for a caught exception.

    Formats the full traceback of *exc* into a single string, sanitizes it
    (FAR-163 redaction runs before truncation), and caps it at *limit* chars.
    Shared by the generic ``except Exception`` handlers in ``execute`` /
    ``resume`` / ``_stream_graph`` so each stays a one-liner.
    """
    import traceback

    return _sanitize_detail(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        limit=limit,
    )


def _retry_backoff_seconds(
    attempt_n: int,
    *,
    base: float = _RETRY_BACKOFF_BASE_SECONDS,
    cap: float = _RETRY_BACKOFF_CAP_SECONDS,
    jitter_fraction: float = _RETRY_BACKOFF_JITTER_FRACTION,
) -> float:
    """Jittered, capped exponential backoff delay for a retry_policy retry.

    ``attempt_n`` is the 1-based node execution attempt count (attempt 1 = the
    first real execution). The deterministic schedule is
    ``min(base * 2 ** (attempt_n - 1), cap)`` and a uniform jitter term in
    ``[0, jitter_fraction * delay]`` is added (clamped so the total never
    exceeds ``cap``). A 0/negative ``attempt_n`` is clamped to 1.

    The schedule is bounded by the retry budget at the decision site: the
    executor only re-dispatches while ``node_attempt_count <= max_retries``,
    so the last scheduled attempt (attempt == budget) cannot extend beyond the
    policy's ``max_retries``. This is a pure function of the attempt number —
    unit-testable without touching the async retry path.
    """
    n = max(int(attempt_n), 1)
    exponential: float = min(float(base) * float(2 ** (n - 1)), cap)
    # Jitter is NOT crypto — it only decorrelates concurrent retries (S311).
    jitter = float(random.uniform(0.0, exponential * max(jitter_fraction, 0.0)))  # noqa: S311  # nosec B311 — non-cryptographic retry jitter only
    return min(exponential + jitter, cap)


class RunRetryPolicyError(NodeCancelledError):
    """A run ended in a state the pipeline's ``retry_policy`` says to retry.

    Subclasses ``NodeCancelledError`` so the caller
    (``run_executor_with_watchdog``) treats the re-raise as a transient retry
    — the SAQ job is re-dispatched and the run (already reset to ``pending``)
    is claimed and executed again.
    """

    def __init__(self, status: str, max_retries: int) -> None:
        super().__init__(node="retry_policy", message=f"retry_policy: status={status!r}, budget={max_retries}")
        self.status = status
        self.max_retries = max_retries


def _retry_after_policy(
    policy: Any,
    final_status: str,
    error_code: str | None,
    error_detail: str | None = None,
) -> int | None:
    """Return the retry budget (``max_retries``) for a terminal outcome, or None.

    Matching rules:
      - ``"stall"``:    ``final_status == "stalled"`` or the code resolves to
                        ``agent.stall`` (legacy ``executor_stalled`` included)
      - ``"timeout"``:  the code resolves to ``node.timeout`` / ``node.runaway``
                        (legacy ``node_timeout`` / ``TimeoutError`` included)
      - ``"failure"``:  ``final_status == "failed"`` and not a stall/timeout outcome

    Codes are matched BOTH literally (legacy codes stay backward compatible) and
    through the shared ``map_legacy_code`` alias table, so dotted registry codes
    (e.g. an ``agent.failed`` A1 elevation) and legacy codes behave identically
    (§3.2: one alias table shared by retry/alert/notifier consumers).

    ``error_detail`` (optional) refines the ``"failure"`` match: a sandbox-agent
    HANG death terminalizes as ``error_code="node_cancelled"`` with
    ``error_detail`` containing ``"likely hung"`` (node_runner). Re-dispatching
    a hang would burn a full node timeout with zero recovery probability, so it
    is excluded from ``"failure"`` retries — while TRANSIENT ``node_cancelled``
    (no hang marker) stays retryable via the existing NodeCancelledError path.

    Known limitation of the ``"stall"`` event: it covers the **node-idle stall**
    path only — a node returns a stalled output dict (``stall_reason``) in
    ``_stream_graph``, which reaches this decision. The **executor-level
    zombie-watchdog stall** (``execute_run`` watchdog terminal-fails the run and
    cancels ``execute()``; the ``CancelledError`` is re-raised at the top of the
    stream block before this decision runs) is NOT retried. See ``docs/prd.md``
    §8.9.

    An absent/malformed policy or a 0 budget yields None (no retry) — the
    current behaviour is unchanged for pipelines without a policy.
    """
    if not isinstance(policy, dict):
        return None
    events = policy.get("on")
    if not isinstance(events, list) or not events:
        return None
    max_retries = policy.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        return None
    if not 0 <= max_retries <= _RETRY_POLICY_MAX_RETRIES:
        return None
    if max_retries == 0:
        return None
    event_set = set(events)
    code = error_code or ""
    mapped = map_legacy_code(code) if code else ""
    if _stall_event_matches(event_set, final_status, code, mapped):
        return max_retries
    if _timeout_event_matches(event_set, code, mapped):
        return max_retries
    if _failure_event_matches(event_set, final_status, code, mapped, error_detail):
        return max_retries
    return None


def _stall_event_matches(event_set: set[Any], final_status: str, code: str, mapped: str) -> bool:
    """``stall`` event matches a stalled outcome or an agent.stall code."""
    return "stall" in event_set and (final_status == "stalled" or code == "executor_stalled" or mapped == "agent.stall")


def _timeout_event_matches(event_set: set[Any], code: str, mapped: str) -> bool:
    """``timeout`` event matches a node.timeout / node.runaway code."""
    return "timeout" in event_set and (
        code in ("node_timeout", "TimeoutError") or mapped in (_ERROR_CODE_NODE_TIMEOUT, "node.runaway")
    )


# FAR-296 Phase 2: never-retryable script-mode terminal codes. Once a script-mode
# node's script PROCESS has started (fencing lease claimed), a fault can never be
# retried — re-dispatching could double-execute a side-effecting script. These
# MUST be excluded from EVERY retry path (run-level ``_retry_after_policy`` AND
# node-level A-series fenced reset).
#
# This set holds ONLY the script-mode CANONICAL codes. The shared contract codes
# ``contract.schema`` / ``contract.no_output`` (canonicalized from the raw
# ``script.schema_failed`` / ``script.no_output`` spellings) are DELIBERATELY NOT
# listed here: they are also produced by NON-script paths (LLM-mode agent output
# schema validation, manual-node resume, any node's output rejection), and
# ``_failure_event_matches`` applies this set unconditionally — blacklisting the
# shared targets would silently disable ``failure``-policy retries for ALL node
# types. Script-mode coverage is preserved via the raw spellings in
# ``_NEVER_RETRYABLE_SCRIPT_RAW`` below.
_NEVER_RETRYABLE_SCRIPT_CODES: frozenset[str] = frozenset(
    {
        "script.failed",
        "script.invalid_output",
        _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN,
        "script.session_lost",
        "script.budget_killed",
    }
)
_NEVER_RETRYABLE_SCRIPT_RAW: frozenset[str] = frozenset(
    {
        "ScriptFailedError",
        "ScriptInvalidOutputError",
        "ScriptSideEffectUnknownError",
        "ScriptBudgetKilledError",
        "script.schema_failed",
        "script.no_output",
    }
)


def _failure_event_matches(
    event_set: set[Any],
    final_status: str,
    code: str,
    mapped: str,
    error_detail: str | None,
) -> bool:
    """``failure`` event matches a failed outcome, excluding hang deaths.

    A sandbox-agent HANG death terminalizes as ``node_cancelled`` + "likely
    hung" in ``error_detail`` — re-dispatching would burn a full node timeout
    with zero recovery probability, so it is excluded from ``"failure"`` retries.
    """
    if (
        "failure" not in event_set
        or final_status != "failed"
        # Timeout is a distinct event — a "failure"-only policy must not retry
        # a timeout outcome, and a stall is not a generic failure.
        or code in ("node_timeout", "TimeoutError", "executor_stalled")
        or mapped in (_ERROR_CODE_NODE_TIMEOUT, "node.runaway", "agent.stall")
    ):
        return False
    # FAR-296 Phase 2: never-retryable script-mode terminal codes are excluded
    # from ``failure`` retries regardless of the policy — re-dispatching a
    # script-mode node whose process started could double-execute a side effect.
    # This covers both the canonical mapped code and the raw exception-class
    # spelling the generic catch publishes.
    if mapped in _NEVER_RETRYABLE_SCRIPT_CODES or code in _NEVER_RETRYABLE_SCRIPT_RAW:
        return False
    # FAR-136 Gap 2: exclude sandbox-agent hang deaths.
    is_cancel = code == "node_cancelled" or mapped == "node.cancelled"
    return not (is_cancel and error_detail and "likely hung" in error_detail)


class RunNotFoundError(KeyError):
    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(str(run_id))
        self.run_id = run_id


class SandboxCapacityExceededError(Exception):
    """Raised when the org sandbox concurrency cap blocks a resume."""

    def __init__(self, org_id: uuid.UUID) -> None:
        self.org_id = org_id
        super().__init__(
            f"Sandbox concurrency limit reached for org {org_id}; gate left undecided. Retry when capacity frees up."
        )


async def _teardown_hub(hub: Any) -> None:
    """Await a hub's ``__aexit__`` shielded against cancellation.

    A cancellation arriving during cleanup must not abort the hub teardown —
    ``asyncio.shield`` keeps the ``__aexit__`` coroutine running to completion
    even when the awaiting task is cancelled (a second CancelledError cannot
    abort the cleanup). The shielded future is re-awaited on cancellation so no
    "Task was destroyed but it is pending" warning is emitted at loop close.
    """
    shielded = asyncio.shield(hub.__aexit__(None, None, None))
    try:
        await shielded
    except asyncio.CancelledError:
        with suppress(Exception):
            await shielded
        raise
    except Exception:
        _log.exception("pipeline.hub_cleanup_failed")


class GraphValidationError(ValueError):
    """Raised when pre-run graph validation fails with blocking errors."""

    def __init__(
        self,
        issues: list[Any],
        run_id: uuid.UUID,
    ) -> None:
        messages = [f"[{i.code}] {i.message}" for i in issues]
        super().__init__(f"Graph validation failed for run {run_id}: {'; '.join(messages)}")
        self.run_id = run_id
        self.issues = issues


def _seed_state(
    snapshot: PipelineSnapshot,
    input_payload: dict[str, Any],
    variant_config_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the initial LangGraph state for a run.

    If *input_payload* contains a ``_feedback_correction`` key, it is
    promoted to the top-level ``run_context`` as ``feedback_correction``
    and removed from the input dict so the pipeline agents never see it
    as part of their normal input.

    ``_run_overrides`` is a SYSTEM-RESERVED TOP-LEVEL run_context key. It is
    seeded ONLY from the run's frozen ``variant_config_snapshot`` (captured at
    fire time) — NEVER from caller input. A normal run has no variant config, so
    a caller-supplied ``_run_overrides`` inside ``input_payload`` stays DATA in
    ``run_context["input"]`` and can never reach the node_runner's override
    boundary (FAR-342 injection surface).
    """
    # Copy input_payload to avoid mutating the caller's dict.
    payload = dict(input_payload)
    run_context_defaults: dict[str, Any] = snapshot.run_context_defaults or {}
    run_context: dict[str, Any] = {
        **run_context_defaults,
        "cancelled": False,
        "input": payload,
    }
    # Promote feedback_correction from input_payload to run_context
    # so the entire graph can access rejection metadata.
    feedback_correction = payload.pop("_feedback_correction", None)
    if feedback_correction:
        run_context["feedback_correction"] = feedback_correction
    # Seed autonomy from snapshot-level default so gate nodes can resolve it.
    if snapshot.default_autonomy_level:
        run_context["_pipeline_default_autonomy"] = snapshot.default_autonomy_level
    # Seed the system-reserved override namespace from the run's FROZEN variant
    # config only. This is the ONLY path that populates it — a caller-supplied
    # ``_run_overrides`` in the input payload is never promoted here.
    if isinstance(variant_config_snapshot, dict):
        overrides = variant_config_snapshot.get("_run_overrides")
        if isinstance(overrides, dict):
            run_context["_run_overrides"] = overrides
    return {
        "run_context": run_context,
        "artifacts": [],
        # Seeded so the loop-edge counter node starts from an explicit dict
        # (the counter node returns the incremented value as a real state
        # update — LangGraph discards router-side in-place mutations, so the
        # counter must live on a node, not a router).
        "_iteration_counts": {},
    }


def _map_lg_event(
    lg_event: dict[str, Any],
    node_ids: set[str],
) -> tuple[str, dict[str, Any]] | None:
    """Map a LangGraph astream_events event to (event_type, payload) or None."""
    event_kind = lg_event.get("event", "")
    name = lg_event.get("name", "")

    if name not in node_ids:
        return None

    if event_kind == "on_chain_start":
        return "node_started", {"node_id": name}
    if event_kind == "on_chain_end":
        return "node_completed", {"node_id": name}
    if event_kind == "on_chain_error":
        data = lg_event.get("data")
        error = data.get("error", "") if isinstance(data, dict) else ""
        return "node_failed", {"node_id": name, "error": str(error)}
    return None


def _streamed_interrupts(lg_event: dict[str, Any]) -> tuple[Any, ...]:
    """Extract native LangGraph interrupts from a top-level stream event."""
    if lg_event.get("event") != "on_chain_stream":
        return ()
    data = lg_event.get("data")
    chunk = data.get("chunk") if isinstance(data, dict) else None
    interrupts = chunk.get("__interrupt__") if isinstance(chunk, dict) else None
    if isinstance(interrupts, (list, tuple)):
        return tuple(interrupts)
    return (interrupts,) if interrupts is not None else ()


@asynccontextmanager
async def _checkpointer_scope(
    conn_string: str,
    organisation_id: uuid.UUID,
    fernet_key: str | None = None,
) -> AsyncIterator[ModuloPostgresSaver]:
    """Create a ModuloPostgresSaver for the duration of a single run execution."""
    async with ModuloPostgresSaver.from_conn_string(
        conn_string,
        organisation_id=organisation_id,
        fernet_key=fernet_key,
    ) as saver:
        yield saver


def _sandbox_agent_for_snapshot(snapshot_id: uuid.UUID, graph_json: dict[str, Any] | None) -> bool:
    """Bounded per-snapshot cache over the (immutable) snapshot graph JSON."""
    key = str(snapshot_id)
    cached = _SANDBOX_AGENT_CACHE.get(key)
    if cached is not None:
        return cached
    result = _graph_contains_sandbox_agent(graph_json)
    if len(_SANDBOX_AGENT_CACHE) >= _SANDBOX_AGENT_CACHE_MAX:
        _SANDBOX_AGENT_CACHE.clear()
    _SANDBOX_AGENT_CACHE[key] = result
    return result


def _graph_is_idempotent(graph_json: dict[str, Any] | None) -> bool:
    """True when every node in the graph is safe to re-run (FAR-295).

    A node is idempotent unless it EXPLICITLY declares ``idempotent=false``:
    the field defaults to ``true`` on the ``PipelineGraphNode`` schema, and a
    missing/None value (legacy graphs persisted before the field existed) is
    treated as idempotent so old graphs keep their retry behaviour unchanged.
    Returns False when ANY node is non-idempotent — a retry re-executes the
    whole run, so a single side-effecting node makes re-running the run unsafe.
    """
    if not isinstance(graph_json, dict):
        return True
    for node in graph_json.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        if node.get("idempotent") is False:
            return False
    return True


def _graph_has_script_mode(graph_json: dict[str, Any] | None) -> bool:
    """True when the graph contains a ``sandbox_agent`` node in ``mode="script"``.

    FAR-296 Phase 2: the fencing lease + stage-split contract apply ONLY to
    script-mode nodes. Any script-mode sandbox node in the graph means a retry
    of this run must first prove no script process could still be alive (the
    lease probe) before a requeue.
    """
    if not isinstance(graph_json, dict):
        return False
    for node in graph_json.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        if (
            str(node.get("node_type", "")).strip() == "sandbox_agent"
            and str(node.get("mode") or "").strip() == "script"
        ):
            return True
    return False


# Node types that can PAUSE a run for human input and therefore REQUIRE a
# LangGraph checkpointer to be able to resume. Any pipeline containing one is
# "interactive". Everything else (agent / connector / sandbox_agent) is a
# batch pipeline that never pauses, so its checkpoints are pure overhead.
_INTERACTIVE_NODE_TYPES: frozenset[str] = frozenset({"manual"})


def _graph_is_interactive(graph_json: dict[str, Any] | None) -> bool:
    """True when the graph needs checkpoint persistence (HITL/interactive).

    A checkpoint is only useful for a run that can PAUSE and RESUME. Two graph
    shapes make a pipeline interactive, matching exactly the LangGraph paths
    that call ``interrupt()`` (node_runner):
      - an edge carrying ``hitl_gate_config`` (a HITL gate is inserted between
        source and target and interrupts for human approval / review), and
      - a node with ``node_type == "manual"`` (a manual-input node that
        interrupts and waits for human output).

    Batch pipelines (pure ``agent`` / ``connector`` / ``sandbox_agent`` nodes,
    no HITL gates) never pause, so persisting a checkpoint after every
    superstep is pure overhead (FAR-432: drive persist-off for them).

    When the graph cannot be inspected (None / not a dict) we are conservative
    and return True so a genuinely interactive pipeline is never broken by
    lacking a checkpointer — the only caller (``_execute_stream``) always has a
    real ``graph_json``.
    """
    if not isinstance(graph_json, dict):
        return True
    any_interactive_node = any(
        isinstance(node, dict) and str(node.get("node_type", "")).strip() in _INTERACTIVE_NODE_TYPES
        for node in graph_json.get("nodes", []) or []
    )
    any_hitl_gate_edge = any(
        isinstance(edge, dict) and edge.get("hitl_gate_config") for edge in graph_json.get("edges", []) or []
    )
    return any_interactive_node or any_hitl_gate_edge


async def _script_lease_probe_ok(
    session_factory: Callable[..., Any] | None,
    run_id: str,
    org_id: Any,
    claim_token: str | None,
) -> bool:
    """FAR-296 Phase 2 stale-claim probe: is it SAFE to requeue this run?

    Reads ``runs.sandbox_dispatch_state`` for a live ``script_executing`` lease
    (execution claimed, completion marker pending) that belongs to THIS claim.
    A stale lease means a script PROCESS could still have been alive — requeue
    is forbidden (exactly-once). Returns True (safe to requeue) when:
      - no session factory / claim / org (fail-open to the normal path), or
      - the dispatch state carries NO ``script_executing`` lease (no process
        could have been alive for this claim).
    Returns False (DO NOT requeue) only when a live script lease exists.
    NEVER auto-retries a stale claim — the caller escalates to needs-human.
    """
    if session_factory is None or not claim_token:
        return True
    org_uuid: uuid.UUID | None = None
    try:
        org_uuid = uuid.UUID(str(org_id)) if org_id else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return True
    try:
        from sqlalchemy import text as _sql_text

        from modulo.db.rls import set_rls_execution_context, set_rls_org

        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await set_rls_execution_context(session)
            row = (
                await session.execute(
                    _sql_text(
                        "SELECT sandbox_dispatch_state FROM runs WHERE id=:rid "
                        "AND organisation_id=:oid AND claim_token=:tok"
                    ),
                    {"rid": run_id, "oid": str(org_uuid), "tok": claim_token},
                )
            ).fetchone()
            if row is None:
                return True
            raw = row[0]
            if not raw:
                return True
            try:
                state = json.loads(raw)
            except (TypeError, ValueError):
                return True
            return not (isinstance(state, dict) and state.get("state") == "script_executing")
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "script.lease_probe_failed",
            extra={"run_id": str(run_id), "exc_type": "probe"},
            exc_info=True,
        )
        # Fail-closed on a probe error: a script-mode run whose lease we cannot
        # read is NEVER requeued (a hidden live process would double-execute).
        return False


def _resolve_post_node_eval_target(
    node_id: str,
    envelope: dict[str, Any],
    node_type_map: dict[str, str] | None,
) -> Any:
    """Resolve the eval target for a completed node envelope (FAR-311).

    Returns the node's CONTRACT output — the same pure return users see as the
    node return — so evals validate what the agent actually produced, not the
    telemetry-style outer ``output`` envelope. For a sandbox_agent that means
    ``artifacts[0].output.output_json`` (which carries ``pr_url`` /
    ``changed_files``). Unknown node types keep the legacy ``envelope["output"]``
    read, falling back to the whole envelope if it is not a dict — preserving
    historical behaviour for graphs without a resolved node type.
    """
    node_type = (node_type_map or {}).get(node_id) or DEFAULT_NODE_TYPE
    found, contract_output = resolve_node_contract_output(envelope, node_type)
    if found:
        return contract_output
    if node_type in SPLITTABLE_NODE_TYPES:
        # Splittable type but no dict contract output (e.g. a sandbox_agent
        # that produced no output_json) — fail closed on the whole envelope
        # rather than silently validating telemetry.
        _log.debug(
            "post_node_eval.missing_contract_output",
            extra={"node_id": node_id, "node_type": node_type},
        )
        return envelope
    inner_output = envelope.get("output")
    if isinstance(inner_output, dict):
        return inner_output
    return envelope


def _node_output_stall_reason(node_output: Any) -> str | None:
    """Return the stall_reason carried by a captured sandbox-agent node output.

    Sandbox-agent nodes return ``{"artifacts": [...], "output": {...}}`` where
    the inner ``output`` dict carries ``stall_reason`` when the idle watchdog
    fired (FAR-98). Returns None for non-dict / garbage output and for nodes
    that did not stall, so non-sandbox node outputs are never misread.
    """
    if not isinstance(node_output, dict):
        return None
    inner = node_output.get("output")
    if not isinstance(inner, dict):
        return None
    reason = inner.get("stall_reason")
    return reason if isinstance(reason, str) and reason else None


def _node_output_agent_failure(node_output: Any) -> str | None:
    """Return a reason string when a captured node output self-reported failure.

    Sandbox-agent nodes return ``{"artifacts": [...], "output": {...}}`` where
    the inner ``output`` dict carries ``agent_status``/``agent_outcome`` —
    the agent's RAW verdict surfaced verbatim from output.json. A1 elevation
    fires when ``agent_status == "failed"`` OR ``outcome == "failed"``: a
    self-declared failure must NEVER land the run ``complete``, regardless of
    the exit code (§15.4). Returns None for non-dict / non-failed output, so
    non-sandbox node outputs are never misread.
    """
    if not isinstance(node_output, dict):
        return None
    inner = node_output.get("output")
    if not isinstance(inner, dict):
        return None
    if inner.get("agent_status") != "failed" and inner.get("agent_outcome") != "failed":
        return None
    reason = inner.get("error") or inner.get("summary")
    if not isinstance(reason, str) or not reason:
        return "agent self-reported failure"
    return reason


def _node_output_sandbox_session_lost(node_output: Any) -> str | None:
    """Return a reason string when a captured node output carries the FAR-227
    session-lost marker.

    node_runner stamps ``sandbox_session_lost: True`` on the output dict ONLY
    for the E2B wrapper's fallback echo (a dead opencode session). It is NOT an
    agent verdict — the agent never wrote output.json — so it must be routed to
    the retryable ``sandbox.no_output_json``, never the non-retryable
    ``agent.failed`` A1 elevation. Returns None for non-dict / non-marked
    output, so ordinary node outputs are never misread.
    """
    if not isinstance(node_output, dict):
        return None
    inner = node_output.get("output")
    if not isinstance(inner, dict):
        return None
    if inner.get("sandbox_session_lost") is not True:
        return None
    reason = inner.get("error") or inner.get("summary")
    if not isinstance(reason, str) or not reason:
        return "No output from agent - session interrupted"
    return reason


def _should_skip_retry(node_id: str | None, markers: Any, run_id: Any) -> bool:
    """FAR-228 guard B decision: should a transient node failure be suppressed
    as a retry because the run's delivery was already made?

    ``node_id`` is ``exc.node_id`` (only ``SandboxNodeFailedError`` carries it)
    or None — a None node_id (or a plain ``NodeCancelledError``) disables the
    gate. ``markers`` is the run's ALREADY-LOADED ``raw_output_markers`` (never
    a fresh SELECT). Fires only when any marker whose attempt_key embeds
    ``:node:<node_id>:`` and ``:run:<run_id>:`` (delimiters, never substring)
    carries ``delivery_done is True``. Non-dict ``markers`` (including
    MagicMock test doubles) are ignored — the gate stays silent.
    """
    if node_id is None:
        return False
    return _marker_delivery_done_for_node(markers, run_id, node_id)


def _node_output_has_idempotency_gate(node_output: Any) -> bool:
    """True when a captured node output carries the FAR-228 idempotency-gate
    skip marker (``output_json.idempotency_gate``) — such a node must NOT
    re-fire agent_signal triggers.

    Keyed ONLY on the marker — never on ``status == "skipped"`` (template-error
    skips fire today and must keep firing). Handles both the outer
    ``{"output": {...}}`` envelope and the artifact-wrapped envelope returned by
    the sandbox node body (guard A / guard B shape).
    """
    if not isinstance(node_output, dict):
        return False
    candidates: list[Any] = [node_output.get("output")]
    artifacts = node_output.get("artifacts")
    if isinstance(artifacts, list):
        candidates.extend(a.get("output") for a in artifacts if isinstance(a, dict))
    for inner in candidates:
        if not isinstance(inner, dict):
            continue
        output_json = inner.get("output_json")
        if isinstance(output_json, dict) and output_json.get("idempotency_gate"):
            return True
    return False


async def _apply_work_intact(
    session: AsyncSession,
    run_id: uuid.UUID,
    work_intact: bool,
    *,
    claim_token: str | None,
) -> None:
    """Fenced UPDATE of ``runs.work_intact`` — mirrors ``update_run_status``
    fencing (F3a): a superseded executor's token no longer matches and the
    write is a no-op, so it cannot stamp work_intact on a successor's run.
    Runs INSIDE the caller's terminalization transaction (FAR-152 §15.3).

    ``run_id`` is bound with the ``Uuid`` type so the raw SQL matches the
    stored id on every backend (SQLite stores the 32-hex form, not the
    dashed ``str(uuid)``).
    """
    if claim_token is None:
        await session.execute(
            text("UPDATE runs SET work_intact = :wi WHERE id = :rid").bindparams(
                bindparam("rid", type_=Uuid()),
                bindparam("wi", type_=Boolean()),
            ),
            {"wi": work_intact, "rid": run_id},
        )
        return
    await session.execute(
        text("UPDATE runs SET work_intact = :wi WHERE id = :rid AND claim_token = :tok").bindparams(
            bindparam("rid", type_=Uuid()),
            bindparam("wi", type_=Boolean()),
        ),
        {"wi": work_intact, "rid": run_id, "tok": claim_token},
    )


async def _reclassify_after_work_intact(session: AsyncSession, run_id: uuid.UUID) -> None:
    """FAR-189 round-2 FIX 3: refresh + re-persist the classification record
    after the ``work_intact`` write so the record carries the real value.

    ``finalize_cost`` triggers the terminal status write → inline classify
    BEFORE ``_apply_work_intact`` runs, so the record persists
    ``work_intact=None`` for every executor-terminalized run — and the
    reconciliation sweep SKIPS already-classified rows, so it never corrects
    them. This is the only path that fixes the executor-written records.

    Runs INSIDE the caller's terminalization transaction, AFTER
    ``_apply_work_intact`` succeeded. The re-read forces a real row load
    (``populate_existing=True``) because the work_intact write bypassed the ORM
    identity map (a raw UPDATE); ``classify_and_persist_run`` then recomputes
    the verdict from the fresh row and upserts it. Best-effort and NEVER
    raises — mirrors the ``work_intact.write_failed`` wrapper so a classify or
    persist failure can never block or fail terminalization.
    """
    try:
        from modulo.core.pipeline_engine.classify import classify_and_persist_run

        run = await session.get(Run, run_id, populate_existing=True)
        if run is not None:
            await classify_and_persist_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("work_intact.classify_refresh_failed", extra={"run_id": str(run_id)})


async def org_sandbox_capacity_free(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> bool:
    """True when the org's sandbox cap (if configured) can admit *run_id*.

    Used by HITL routes as a PRE-approval check: the gate decision must not be
    committed when the org is at sandbox capacity. Fail-open — any error reads
    as ``True`` (admit) with a warning, never raises.
    """
    try:
        run = await get_run(session, run_id)
        if run is None or run.snapshot_id is None:
            return True
        snap_result = await session.execute(
            select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == run.snapshot_id)
        )
        graph_json = snap_result.scalar_one_or_none()
        if not _graph_contains_sandbox_agent(graph_json):
            return True
        cap = await get_sandbox_concurrency_limit(session, org_id)
        if cap is None:
            return True
        active = await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
        return active < cap
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "hitl.sandbox_capacity_check_failed",
            extra={"org_id": str(org_id), "run_id": str(run_id)},
        )
        return True


def _compute_otel_run_context(
    config: dict[str, Any],
    bridge: Any,
) -> tuple[str | None, Any | None]:
    """FAR-198: compute the run's deterministic OTel trace id and root span.

    The root span is created via the bridge (spans created by the bridge
    inherit it). The ``context_api.attach`` is deferred to the caller so the
    ``context_api.detach`` in the caller's ``finally`` stays balanced with the
    attach.
    """
    thread_id = (config.get("configurable") or {}).get("thread_id")
    run_trace_id = trace_id_for_thread(thread_id) if thread_id else None
    run_root_span = None
    if thread_id:
        run_root_span = bridge.start_run_root(thread_id)
    return run_trace_id, run_root_span


def _record_chain_end_output(
    completed_node_outputs: dict[str, Any] | None,
    name: str,
    data: Any,
    run_trace_id: str | None,
    bridge: Any,
    lg_run_id: Any,
) -> Any | None:
    """FAR-198: stamp and store a completed node's output envelope.

    Stamps the node's real OTel span id (and the run's trace id) onto a
    shallow copy of the envelope, stores it into ``completed_node_outputs``
    and returns the (possibly stamped) output so the caller can extract the
    stall / agent-failure / session-lost markers. Returns ``None`` when there
    is no envelope to capture (nothing stored).
    """
    if completed_node_outputs is None:
        return None
    output = data.get("output") if isinstance(data, dict) else None
    if output is None:
        return None
    # FAR-198: stamp the node's real OTel span id (and the run's trace id)
    # onto a shallow copy of the envelope. The node_output_split splitter
    # folds unknown top-level envelope keys into the node_telemetry entry, so
    # these surface in the per-node span column — never in the pure return.
    node_span_id = bridge.span_id_for_run(lg_run_id)
    if isinstance(output, dict) and (node_span_id or run_trace_id):
        stamped = dict(output)
        if node_span_id:
            stamped["otel_span_id"] = node_span_id
        if run_trace_id:
            stamped["otel_trace_id"] = run_trace_id
        output = stamped
    completed_node_outputs[name] = output
    return output


def _record_node_markers(
    output: Any,
    broker: RunEventBroker,
    name: str,
) -> tuple[str | None, str | None, str | None]:
    """Extract stall / agent-failure / session-lost markers from a node output.

    Publishes ``run_stalled`` when the output carries a stall reason and
    returns the three marker reasons so the caller can latch them onto the
    run's terminal status.
    """
    stall_reason = _node_output_stall_reason(output)
    if stall_reason:
        broker.publish(
            "run_stalled",
            {
                "node_id": name,
                "stall_reason": _sanitize_detail(stall_reason, limit=5000),
            },
        )
    agent_failure = _node_output_agent_failure(output)
    session_lost = _node_output_sandbox_session_lost(output)
    return stall_reason, agent_failure, session_lost


def _accumulate_node_token_usage(
    node_name: str,
    token_usage: dict[str, Any],
    node_token_usage: dict[str, dict[str, int]],
    guard: RunawayGuard | None,
    node_token_budgets: dict[str, int] | None,
) -> None:
    """Accumulate token usage for a single node and enforce the per-node budget."""
    node_data = node_token_usage.setdefault(node_name, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    pt = token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0
    ct = token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0
    tt = token_usage.get("total_tokens", 0) or 0
    node_data["input_tokens"] += pt
    node_data["output_tokens"] += ct
    node_data["total_tokens"] += tt
    if guard is not None:
        guard.record_tokens(tt)
    if node_token_budgets is not None:
        node_budget = node_token_budgets.get(node_name)
        if node_budget is not None and node_data["total_tokens"] > node_budget:
            raise RunawayRunError("token_budget", node_data["total_tokens"], node_budget)


def _extract_chat_model_token_usage(output: Any) -> dict[str, Any]:
    """Extract token usage from a chat model end event output."""
    usage_metadata = getattr(output, "usage_metadata", None) if isinstance(output, BaseMessage) else None
    if usage_metadata is not None:
        return {
            "input_tokens": usage_metadata.get("input_tokens", 0) or 0,
            "output_tokens": usage_metadata.get("output_tokens", 0) or 0,
            "total_tokens": usage_metadata.get("total_tokens", 0) or 0,
        }
    if isinstance(output, dict):
        # Legacy fallback: llm_output.token_usage
        llm_output = output.get("llm_output", {})
        return llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}
    return {}


def _extract_llm_token_usage(output: Any) -> dict[str, Any]:
    """Extract token usage from an llm end event output (legacy interface)."""
    llm_output = output.get("llm_output", {}) if isinstance(output, dict) else {}
    return llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}


def _accumulate_chat_model_tokens(
    lg_event: Any,
    node_token_usage: dict[str, dict[str, int]],
    guard: RunawayGuard | None,
    node_token_budgets: dict[str, int] | None,
) -> None:
    """Accumulate token usage from an ``on_chat_model_end`` event.

    Reads ``usage_metadata`` (with the legacy ``llm_output.token_usage``
    fallback), records cumulative tokens on the guard, and enforces the
    per-node token budget (raising ``RunawayRunError`` on breach).
    """
    metadata = lg_event.get("metadata") or {}
    node_name = metadata.get("langgraph_node")
    if node_name:
        data = lg_event.get("data", {})
        output = data.get("output", {}) if isinstance(data, dict) else {}
        token_usage = _extract_chat_model_token_usage(output)
        if isinstance(token_usage, dict):
            _accumulate_node_token_usage(node_name, token_usage, node_token_usage, guard, node_token_budgets)


def _accumulate_llm_tokens(
    lg_event: Any,
    node_token_usage: dict[str, dict[str, int]],
    guard: RunawayGuard | None,
    node_token_budgets: dict[str, int] | None,
) -> None:
    """Accumulate token usage from an ``on_llm_end`` event (legacy interface).

    Reads ``llm_output.token_usage``, records cumulative tokens on the guard,
    and enforces the per-node token budget (raising ``RunawayRunError`` on
    breach).
    """
    metadata = lg_event.get("metadata") or {}
    node_name = metadata.get("langgraph_node")
    if node_name:
        data = lg_event.get("data", {})
        output = data.get("output", {}) if isinstance(data, dict) else {}
        token_usage = _extract_llm_token_usage(output)
        if isinstance(token_usage, dict):
            _accumulate_node_token_usage(node_name, token_usage, node_token_usage, guard, node_token_budgets)


def _terminal_failure(
    broker: RunEventBroker,
    status: str,
    code: str,
    detail: str,
    node_token_usage: dict[str, Any] | None,
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """Publish ``run_failed`` and return the terminal failure 4-tuple."""
    broker.publish("run_failed", {"error": code, "detail": detail})
    return status, code, detail, node_token_usage or None


@dataclass
class _StreamState:
    """Mutable per-run stream accumulators for ``_stream_graph``.

    Groups the state the ``astream_events`` loop reads and mutates so the
    per-event helper takes a single bundle instead of many loose parameters
    (CodeScene: excess function arguments / complex method). A pure refactor —
    no behaviour, ordering, or state-transition change.
    """

    node_token_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    segments_completed: int = 0
    first_node_signalled: bool = False
    # Set when a captured sandbox-agent node output carries stall_reason — a
    # stalled node RETURNS a failed output dict instead of raising, so the run
    # must be recorded as 'stalled', not 'complete' (FAR-98).
    stall_reason: str | None = None
    # Set when a captured sandbox-agent node output self-reported failure
    # (agent_status=failed OR outcome=failed) — A1 elevation (agent-failure UX,
    # phase 1): such a run must NEVER land 'complete'.
    agent_failure_reason: str | None = None
    # FAR-227: set when a captured sandbox-agent node output carries the
    # sandbox-session-lost marker (the E2B wrapper's fallback echo — a dead
    # opencode session). Routes to retryable ``sandbox.no_output_json``.
    session_lost_reason: str | None = None


@dataclass
class _StreamContext:
    """Immutable per-run stream context shared by the ``astream_events`` handlers.

    Bundles the fixed per-run values every per-event handler needs so the
    loop body and helpers take a single bundle instead of many loose
    parameters (CodeScene: excess function arguments). A pure refactor — no
    behaviour, ordering, or state-transition change.
    """

    node_ids: set[str]
    guard: RunawayGuard | None
    completed_node_outputs: dict[str, Any] | None
    run_trace_id: str | None
    broker: RunEventBroker
    run_id: uuid.UUID
    pipeline_id: uuid.UUID | None
    org_id: uuid.UUID | None
    node_token_budgets: dict[str, int] | None
    eval_definitions_by_node: dict[str, list[EvalDefDTO]] | None
    node_type_map: dict[str, str] | None


def _stream_terminal_reason(
    state: _StreamState,
    broker: RunEventBroker,
    run_id: uuid.UUID,
) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
    """Return the terminal 4-tuple for a captured stall / agent-failure /
    session-lost node, or ``None`` when the run completed normally.

    Extracted from ``_stream_graph``'s post-loop tail (pure refactor). A dead
    sandbox session routes to the retryable ``sandbox.no_output_json``, a
    self-reported agent failure elevates to ``agent.failed``, and a node that
    stalled takes priority (existing precedence preserved). ``None`` means the
    caller publishes ``run_completed`` and returns ``complete``.
    """
    usage = state.node_token_usage or None
    if state.session_lost_reason and not state.stall_reason:
        return _terminal_failure(
            broker,
            "failed",
            "sandbox.no_output_json",
            _sanitize_detail(state.session_lost_reason, limit=5000),
            usage,
        )
    if state.agent_failure_reason and not state.stall_reason:
        # A1 elevation (agent-failure UX, phase 1, §15.4): a node that
        # self-reported failure must NEVER land the run 'complete'.
        # Fail-open — any elevation computation error logs a warning and falls
        # back to today's path (§2.3.6). If the node also stalled, the stall
        # terminalization below wins (existing behaviour).
        try:
            from modulo.settings import get_settings

            if get_settings().modulo_agent_failure_elevation_enabled:
                return _terminal_failure(
                    broker,
                    "failed",
                    _ERROR_CODE_AGENT_FAILED,
                    _sanitize_detail(state.agent_failure_reason, limit=5000),
                    usage,
                )
        except Exception:
            _log.warning(
                "agent_failure_elevation.failed_open",
                extra={"run_id": str(run_id), "detail": state.agent_failure_reason},
                exc_info=True,
            )
    if state.stall_reason:
        return _terminal_failure(
            broker,
            "stalled",
            "executor_stalled",
            _sanitize_detail(state.stall_reason, limit=5000),
            usage,
        )
    return None


def _all_capacities_ok(pipeline_ok: bool, sandbox_ok: bool, run_ok: bool) -> bool:
    """All three capacity gates (pipeline / sandbox / org-run) admit the run."""
    return pipeline_ok and sandbox_ok and run_ok


def _can_fenced_requeue(
    node_attempt_count: int,
    retries: int,
    superseded: bool,
    stalled: bool,
    script_lease_ok: bool,
    graph_idempotent: bool,
) -> bool:
    """Can a transient node failure be fenced back to ``pending`` for a retry?

    False when the retry budget is exhausted, the run is owned by a successor
    (superseded), the zombie watchdog stalled it, a script lease is still live,
    or the graph contains a non-idempotent node (FAR-295).
    """
    return node_attempt_count < retries and not superseded and not stalled and script_lease_ok and graph_idempotent


def _can_retry_after_policy(
    node_attempt_count: int,
    retry_budget: int,
    superseded: bool,
    script_retry_probe_ok: bool,
) -> bool:
    """Can the retry_policy re-dispatch this run (FAR-136 / FAR-296 Phase 2)?"""
    return node_attempt_count <= retry_budget and not superseded and script_retry_probe_ok


def _retry_policy_applies(
    retry_budget: int | None,
    is_correction_run: bool,
    graph_idempotent: bool,
) -> bool:
    """Should the retry_policy be considered at all for this terminal run?

    False when there is no budget (no policy / exhausted), the run is a
    correction run (FAR-210: its retry budget is owned by the correction path),
    or the graph contains a non-idempotent node (FAR-295: re-running would
    re-execute a side-effecting step).
    """
    return retry_budget is not None and not is_correction_run and graph_idempotent


class PipelineExecutor:
    """Execute a single pipeline run (HITL-aware, supports parallel fan-out).

    ``astream_events`` delivers every node's events (including nodes running in
    parallel branches, FAR-171) through ONE async generator in superstep
    completion order, so ``_stream_graph`` needs no per-branch task management:
    ``completed_node_outputs`` is keyed by node_id (no clobbering), token usage
    accumulates per node name, and the RunawayGuard counters are incremented
    once per completed node / token report (each parallel node counts once;
    duration is wall-clock). Parallel fan-out itself is compiled natively by
    ``graph_cache.build_graph_from_json`` (multiple ``add_edge`` calls from one
    source → LangGraph runs them in the same superstep).

    A HITL interrupt raised in ONE parallel branch pauses the whole run; the
    already-completed sibling tasks in that superstep keep their state, and on
    ``resume`` the interrupted gate re-runs and any join nodes downstream of it
    re-run with the gate's decision (see ``_stream_graph`` interrupt handling).

    Args:
        db_engine: SQLAlchemy async engine for run CRUD operations.
        checkpointer_conn_string: psycopg-compatible connection string for
            LangGraph's AsyncPostgresSaver. If None, no checkpointer is used
            (runs will not persist checkpoints and HITL interrupts will not work).

    """

    def __init__(
        self,
        db_engine: AsyncEngine,
        *,
        checkpointer_conn_string: str | None = None,
        evidence_provider: EvidenceProvider | None = None,
        notifier: Any | None = None,
    ) -> None:
        self._engine = db_engine
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        self._checkpointer_conn_string = checkpointer_conn_string
        self._otel_bridge = LangGraphOtelBridge()
        # Evidence seam (FAR-152 §15.3): injected FakeEvidenceProvider in tests;
        # None selects the production E2B+DB-backed provider per run.
        self._evidence_provider = evidence_provider
        # Zombie-run protection hook (2026-08-05): wired by
        # ``pipeline_execution.run_executor_with_watchdog`` to an asyncio.Event.
        # Called when the FIRST node dispatches so the execute_run watchdog can
        # distinguish "hung in pre-node setup" (no progress) from a legitimate
        # long-running node (progress already signalled → watchdog stands down).
        self.on_first_progress: Callable[[], None] | None = None
        # Absolute node-deadline watchdog hooks (FAR-369, defense-in-depth): wired
        # by ``pipeline_execution.run_executor_with_watchdog`` to asyncio.Events.
        # Called when EACH node starts / completes (by node_id) so the watchdog
        # can measure each node's execution against its own ``timeout_seconds``
        # independently of idle/activity — catching a half-alive SSE stall that
        # defeats the idle-watchdog. Unlike ``on_first_progress`` (fires once),
        # these fire once per node.
        self.on_node_started: Callable[[str], None] | None = None
        self.on_node_completed: Callable[[str], None] | None = None
        # Per-node configured ``timeout_seconds`` (node_id -> seconds), populated
        # from the graph JSON before streaming so the watchdog can read it as a
        # shared dict reference. Empty until ``_prepare_and_stream`` runs.
        self._node_timeouts: dict[str, int] = {}
        # Fenced-lease authority (dist/runtime-core A1): the claim token
        # captured at execute/resume start. Every terminal/demotion write by
        # this executor is fenced against it so a superseded original cannot
        # write out from under a successor. Seeded into LangGraph state as
        # ``_claim_lease`` for the sandbox dispatch marker.
        self._claim_token: str | None = None
        # Cancellation-intent signals wired by run_executor_with_watchdog so the
        # NodeCancelledError retry handler can tell a watchdog stall / supersession
        # from a genuine transient node cancellation and skip the pending-reset.
        self._stall_requested: asyncio.Event | None = None
        self._superseded: asyncio.Event | None = None
        # HITL-awaiting notifier seam (team-hitl-gates Known Gap, 2026-08-16):
        # inject the Notifier so the run lifecycle can dispatch the
        # ``hitl_awaiting`` webhook/in-app notification. Injected by the SAQ
        # execute/resume path (which already builds a Notifier for the system
        # worker crons); None skips dispatch (fail-open, never blocks the run).
        self._notifier: Any | None = notifier

    async def _check_capacity(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        graph_json: dict[str, Any] | None = None,
        snapshot_id: uuid.UUID | None = None,
    ) -> Run:
        """Non-blocking capacity check using count-based comparison.

        Soft-cap, no advisory lock. If the pipeline's ``max_concurrent_runs``
        limit OR the organisation's sandbox concurrency cap (when the graph
        contains a ``sandbox_agent`` node and a cap is configured) OR the
        organisation's run-concurrency cap (``run_concurrency_limit``, when
        configured) is reached, the run is atomically demoted back to
        ``pending`` with a reason marker on ``error_code``
        (``pipeline_capacity`` / ``org_capacity_limited``) and recovered by
        ``dispatcher_reconcile`` (cron_helpers) / the stale-run sweep
        (pipeline_execution) — plan F3b: no in-process retry loop.

        The org run-concurrency cap is the CLAIM-TIME backstop for the
        dispatch-time admission gate in ``dispatch_run``. Dispatch-time
        admission counts active runs in one transaction but enqueues later;
        newly-enqueued runs stay ``pending`` (invisible to the count) until a
        worker claims them, so a burst of dispatches can each see
        ``active < limit`` and all enqueue — exceeding the org cap by the
        batch size. Re-checking the org run cap here, at claim time, closes
        that TOCTOU window (mirroring the sandbox-cap claim-time check).

        When *max_concurrent* is 0 or negative the pipeline is unlimited, but
        the organisation caps (sandbox + run, if configured) still apply.

        Fail-open, loud: any error reading the org settings, counting org
        runs, or scanning the graph logs a warning and ADMITS the run (treats
        it as no-cap) rather than raising.
        """
        org_sandbox_cap: int | None = await self._read_org_sandbox_cap(org_id, graph_json, snapshot_id)
        org_run_limit: int | None = await self._read_org_run_concurrency_limit(org_id)

        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            if run.cancellation_requested:
                await update_run_status(session, run_id, "cancelled")
                cancelled_run = await get_run(session, run_id)
                if cancelled_run is None:
                    raise RunNotFoundError(run_id)
                return cancelled_run

            pipeline_capacity_ok = True
            active_count = 0
            if max_concurrent > 0:
                active_count = await count_active_runs_for_pipeline(
                    session, pipeline_id, include_pending=False, exclude_run_id=run_id
                )
                pipeline_capacity_ok = active_count < max_concurrent

            org_sandbox_count = 0
            if org_sandbox_cap is not None:
                org_sandbox_count = await self._org_sandbox_active_count(session, org_id, run_id)
            org_sandbox_cap_ok = org_sandbox_cap is None or org_sandbox_count < org_sandbox_cap

            org_run_count = 0
            if org_run_limit is not None:
                org_run_count = await self._org_run_active_count(session, org_id, run_id)
            org_run_cap_ok = org_run_limit is None or org_run_count < org_run_limit

            if _all_capacities_ok(pipeline_capacity_ok, org_sandbox_cap_ok, org_run_cap_ok):
                if run.status not in _ADMISSIBLE_STATUSES:
                    # The run went terminal (or hold) while a retry was backing
                    # off — never resurrect it. Return it untouched so the
                    # caller does not resume execution.
                    return run
                return await self._claim_run_and_audit(
                    session=session,
                    run_id=run_id,
                    org_id=org_id,
                    pipeline_id=pipeline_id,
                )

            decline_code, decline_detail = self._capacity_decline(
                max_concurrent=max_concurrent,
                active_count=active_count,
                pipeline_capacity_ok=pipeline_capacity_ok,
                org_sandbox_cap=org_sandbox_cap,
                org_count=org_sandbox_count,
                org_capacity_ok=org_sandbox_cap_ok,
                org_run_limit=org_run_limit,
                org_run_count=org_run_count,
                org_run_capacity_ok=org_run_cap_ok,
            )
            # Demote to pending + reason marker so the recovery sweeps
            # (dispatcher_reconcile / stale-run) pick it up. FENCED to this
            # executor's claim token and only from ``running`` (A1): a
            # superseded original (token rotated by a successor) cannot demote
            # the successor's running row back to pending.
            await update_run_status(
                session,
                run_id,
                "pending",
                error_code=decline_code,
                error_detail=decline_detail,
                claim_token=self._claim_token,
                from_status="running",
            )
            pending_run = await get_run(session, run_id)
            if pending_run is None:
                raise RunNotFoundError(run_id)
            return pending_run

    async def _claim_run_and_audit(
        self,
        *,
        session: AsyncSession,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> Run:
        """Claim a capacity-admitted run (pending→running) and fire ``run_started``.

        Extracted from ``_check_capacity`` (pure refactor). PRD §8.12: a run
        "starts" exactly once — the pending→running claim transition in the
        execute() path. The resume() path sets ``running`` directly (no
        _check_capacity call) so the event fires once per run, not once per
        resume. Failure-isolated: a broken audit append must never block run
        admission (the savepoint rollback undoes only the audit write).
        """
        await update_run_status(
            session,
            run_id,
            "running",
            claimed_by=_WORKER_ID,
            clear_error_code=True,
        )
        running_run = await get_run(session, run_id)
        if running_run is None:
            raise RunNotFoundError(run_id)
        try:
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="run_started",
                resource_type="run",
                resource_id=run_id,
                payload_json={"pipeline_id": str(pipeline_id)},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.run_started_audit_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id)},
            )
        return running_run

    async def _org_sandbox_active_count(self, session: AsyncSession, org_id: uuid.UUID, run_id: uuid.UUID) -> int:
        """Count the org's active sandbox runs (fail-open to 0 on read error)."""
        try:
            return await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_org_count_failed",
                extra={"org_id": str(org_id), "run_id": str(run_id)},
            )
            return 0

    async def _org_run_active_count(self, session: AsyncSession, org_id: uuid.UUID, run_id: uuid.UUID) -> int:
        """Count the org's active runs (fail-open to 0 on read error)."""
        try:
            return await count_active_runs_for_org(session, org_id, include_pending=False, exclude_run_id=run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.org_run_count_failed",
                extra={"org_id": str(org_id), "run_id": str(run_id)},
            )
            return 0

    async def _read_org_sandbox_cap(
        self,
        org_id: uuid.UUID,
        graph_json: dict[str, Any] | None,
        snapshot_id: uuid.UUID | None,
    ) -> int | None:
        """Read the org's sandbox cap (``None`` = no cap / no sandbox / fail-open).

        Short-circuits BEFORE any DB read: a graph with no ``sandbox_agent``
        node skips the settings read entirely. Each fail-open path logs a
        warning and treats the run as uncapped.
        """
        try:
            if snapshot_id is not None:
                has_sandbox = _sandbox_agent_for_snapshot(snapshot_id, graph_json)
            else:
                has_sandbox = _graph_contains_sandbox_agent(graph_json)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_graph_scan_failed",
                extra={"org_id": str(org_id)},
            )
            return None
        if not has_sandbox:
            return None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                return await get_sandbox_concurrency_limit(session, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_cap_read_failed",
                extra={"org_id": str(org_id)},
            )
            return None

    async def _read_org_run_concurrency_limit(self, org_id: uuid.UUID) -> int | None:
        """Read the org's run-concurrency cap (``None`` = no cap / fail-open).

        Mirrors :meth:`_read_org_sandbox_cap` but for the org-wide
        ``run_concurrency_limit`` (applies to EVERY run, not just sandbox
        graphs). Fail-open: any read error logs a warning and treats the org
        as uncapped.
        """
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                return await get_org_run_concurrency_limit(session, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.org_run_cap_read_failed",
                extra={"org_id": str(org_id)},
            )
            return None

    @staticmethod
    def _capacity_decline(
        *,
        max_concurrent: int,
        active_count: int,
        pipeline_capacity_ok: bool,
        org_sandbox_cap: int | None,
        org_count: int,
        org_capacity_ok: bool,
        org_run_limit: int | None = None,
        org_run_count: int = 0,
        org_run_capacity_ok: bool = True,
    ) -> tuple[str, str]:
        """Pick the capacity reason marker + sanitized human detail."""
        if not org_capacity_ok and org_sandbox_cap is not None:
            return (
                ERROR_CODE_ORG_CAPACITY_LIMITED,
                f"Org sandbox concurrency limit reached: {org_count} active, cap {org_sandbox_cap}",
            )
        if not org_run_capacity_ok and org_run_limit is not None:
            return (
                ERROR_CODE_ORG_CAPACITY_LIMITED,
                f"Org run concurrency limit reached: {org_run_count} active, cap {org_run_limit}",
            )
        return (
            ERROR_CODE_PIPELINE_CAPACITY,
            f"Pipeline max_concurrent_runs reached: {active_count} active, limit {max_concurrent}",
        )

    async def _load_claimed_conformance_guardrails(
        self,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> tuple[list[Any], bool]:
        """FAR-215: hoisted per-run claim discovery for the conformance re-check.

        Computed ONCE at run start (both ``execute`` and ``resume``) and seeded
        into the run-scoped conformance context, so the per-node check pays
        zero DB round-trips when the pipeline has no conformance claims and one
        query per run when it does. Returns ``(claimed, load_failed)`` — a
        load failure sets ``load_failed=True`` so the node gate fails CLOSED
        (unknown blocks) rather than silently skipping claims.
        """
        from modulo.core.guardrails.conformance import load_claimed_guardrails

        return await load_claimed_guardrails(
            self._session_factory,
            org_id=org_id,
            pipeline_id=pipeline_id,
        )

    async def _load_eval_defs_for_pipeline(
        self,
        session: AsyncSession,
        pipeline_id: uuid.UUID,
    ) -> list[EvalDefinition]:
        """Load eval definitions for a pipeline that are scoped to a node."""
        eval_stmt = select(EvalDefinition).where(
            EvalDefinition.pipeline_id == pipeline_id,
            EvalDefinition.node_id.isnot(None),
        )
        return list((await session.execute(eval_stmt)).scalars().all())

    @staticmethod
    def _build_eval_defs_by_node(
        eval_rows: list[EvalDefinition],
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> dict[str, list[EvalDefDTO]]:
        """Convert eval definition ORM rows to a dict keyed by node id."""
        eval_defs_by_node: dict[str, list[EvalDefDTO]] = {}
        for e in eval_rows:
            node_key = str(e.node_id) if e.node_id else ""
            if node_key:
                eval_defs_by_node.setdefault(node_key, []).append(
                    EvalDefDTO(
                        id=e.id,
                        org_id=org_id,
                        pipeline_id=e.pipeline_id,
                        node_id=node_key,
                        name=e.name,
                        eval_type=e.eval_type,
                        config=e.config_json,
                        failure_behaviour=e.failure_behaviour,
                        pass_threshold=e.pass_threshold,
                        suite_id=e.suite_id,
                        version=e.version,
                    )
                )
        return eval_defs_by_node

    async def _run_post_node_evals(
        self,
        node_id: str,
        envelope: dict[str, Any],
        eval_definitions_by_node: dict[str, list[EvalDefDTO]],
        run_id: uuid.UUID,
        org_id: uuid.UUID | None,
        node_type_map: dict[str, str] | None = None,
    ) -> None:
        """FAR-305: run node-scoped evals for a completed node (standalone path).

        This is the non-HITL counterpart to ``make_hitl_gate_fn``'s
        eval-before-interrupt: it evaluates each of the node's eval definitions
        against the node's CONTRACT output — the agent's actual return (what
        users see as the node return; for a sandbox_agent that is
        ``artifacts[0].output.output_json``), matching what the HITL gate
        evaluates against state after FAR-311 — and persists the results to the
        ``eval_results`` table so post-run suite-level threshold checks can
        read them.

        If a ``block`` eval fails, ``EvalBlockedError`` propagates to
        ``_stream_graph``'s existing handler, transitioning the run to
        ``eval_failed`` with ``error_code="eval_blocked"``.
        """
        eval_defs = eval_definitions_by_node.get(node_id)
        if not eval_defs:
            return
        # The captured ``output`` is the envelope ``{"artifacts": [...],
        # "output": {...}}``. Validate the node's CONTRACT output (what the
        # agent produced — ``artifacts[0].output.output_json`` for a
        # sandbox_agent), NOT the telemetry-style outer ``output`` envelope
        # (FAR-311: the outer output carries status/summary/cost but no
        # pr_url / changed_files).
        eval_target = _resolve_post_node_eval_target(node_id, envelope, node_type_map)

        engine = EvalEngine()
        results: dict[str, EngineEvalResult] = {}
        for eval_def in eval_defs:
            eval_result = engine.evaluate(eval_target, eval_def, run_id=run_id)
            results[eval_def.name] = eval_result
            _log.info(
                "post_node_eval.result",
                extra={
                    "node_id": node_id,
                    "eval_name": eval_def.name,
                    "eval_id": str(eval_def.id),
                    "passed": eval_result.passed,
                    "score": eval_result.score,
                    "detail": eval_result.detail,
                },
            )

        # Persist eval results to the eval_results table so post-run
        # suite-level threshold checks can read them.
        if self._session_factory is not None and org_id is not None:
            try:
                async with self._session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    await set_rls_execution_context(session)
                    for eval_def in eval_defs:
                        eval_result = results[eval_def.name]
                        node_uuid: uuid.UUID | None = uuid.UUID(eval_def.node_id) if eval_def.node_id else None
                        session.add(
                            EvalResult(
                                organisation_id=org_id,
                                run_id=run_id,
                                node_id=node_uuid,
                                eval_id=eval_def.id,
                                eval_definition_version=eval_def.version,
                                passed=eval_result.passed,
                                score=eval_result.score,
                                detail=eval_result.detail,
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("post_node_eval.persist_failed", extra={"node_id": node_id})

    async def _init_model_backend_hub(self, org_id: uuid.UUID) -> ModelBackendHub | None:
        """Load active model backends for the org and initialise ModelBackendHub.

        Sets the hub on the current ContextVar so node_runner can access it.
        Returns the hub (or None if no backends are configured) for cleanup
        in the caller's finally block.
        """
        hub: ModelBackendHub | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                backend_rows = (
                    (
                        await session.execute(
                            select(ModelBackend).where(
                                ModelBackend.organisation_id == org_id,
                                ModelBackend.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(backend_rows, list) and backend_rows:
                    from modulo.settings import get_settings

                    _settings = get_settings()
                    from modulo.core.secrets_backend import create_secrets_backend

                    secrets_backend = create_secrets_backend(
                        fernet_key=_settings.fernet_key,
                        session=session,
                    )
                    hub = ModelBackendHub()
                    await hub.__aenter__()
                    await hub.initialise(backend_rows, secrets_backend=secrets_backend)
                    set_model_backend_hub(hub)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.model_backend_hub_init_failed")
            if hub is not None:
                await _teardown_hub(hub)
            hub = None
        return hub

    async def _init_connector_hub(
        self,
        org_id: uuid.UUID,
        allowed_connectors: Sequence[str] | None = None,
    ) -> Any | None:
        """Load active ConnectorInstance rows for the org and initialise ConnectorHub.

        Sets the hub on the current ContextVar so make_connector_fn can access it.
        Returns the hub (or None if no connectors are configured).

        *allowed_connectors* (FAR-418) wires fetch-time capability scoping into the
        production run path: when every node in the run is connector-scoped the hub
        decrypts ONLY those connectors, so out-of-scope credentials are never
        disclosed. Pass ``None`` (the default) to fetch every active org connector,
        preserving the pre-scope behaviour (used for compensation and unscoped runs).
        """
        hub: Any | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                from sqlalchemy import select

                from modulo.db.models.connector_instance import ConnectorInstance

                rows = (
                    (
                        await session.execute(
                            select(ConnectorInstance).where(
                                ConnectorInstance.organisation_id == org_id,
                                ConnectorInstance.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(rows, list) and rows:
                    from modulo.core.connector_hub import ConnectorHub
                    from modulo.core.pipeline_engine.decorator import set_connector_hub
                    from modulo.core.runtime_provider import create_default_hub
                    from modulo.core.secrets_backend import create_secrets_backend
                    from modulo.settings import get_settings

                    _settings = get_settings()
                    secrets_backend = create_secrets_backend(
                        fernet_key=_settings.fernet_key,
                        session=session,
                    )
                    runtime_hub = create_default_hub()
                    hub = ConnectorHub(
                        secrets_backend=secrets_backend,
                        runtime_provider=runtime_hub,
                    )
                    await hub.__aenter__()
                    await hub.initialise(rows, allowed_connectors=allowed_connectors)
                    set_connector_hub(hub)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.connector_hub_init_failed")
            if hub is not None:
                await _teardown_hub(hub)
            hub = None
        return hub

    async def _compensate_blocked_run_best_effort(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        executed_nodes: dict[str, Any],
    ) -> None:
        """Run-termination compensation for a guardrail-blocked mid-run terminalization (FAR-291).

        A run whose earlier nodes performed connector side effects (pushed a PR,
        flipped a Linear status) before a later node's output was
        guardrail-blocked must have those side effects compensated. Invoked from
        ``_finalize_run_after_stream`` AFTER the terminal status write
        (``finalize_cost``), so both the ``execute()`` and ``resume()`` paths
        share this single wiring point.

        Uses its OWN fresh session (``set_rls_org`` inside ``session.begin``)
        and a FRESH connector hub for the compensation window — it never touches
        the claim_token-fenced ``finalize_cost`` transaction, and the hub
        teardown cannot interfere with the run's own hub lifecycle. Best-effort
        + failure-isolated (guard-the-guard): every raise is logged here and
        never propagates into terminalization.
        """
        hub: Any | None = None
        try:
            hub = await self._init_connector_hub(org_id)
            # The compensation + summary write commit INSIDE this session
            # transaction (so the blocked_partial summary is durable) BEFORE
            # the hub is torn down. If teardown failed inside the transaction
            # block it would roll the summary back with it — guard-the-guard:
            # teardown must never lose an already-committed compensation.
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                run = await get_run(session, run_id)
                if run is None:
                    _log.warning("guardrails.compensation.run_not_found run=%s", run_id)
                    return
                from modulo.core.guardrails.compensation import compensate_blocked_run

                await compensate_blocked_run(
                    session,
                    run,
                    guardrail_block=run.error_detail or "",
                    connector_hub=hub,
                    executed_nodes=executed_nodes,
                    blocking_eval_name="",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("guardrails.compensation.error run=%s", run_id)
        finally:
            if hub is not None:
                await _teardown_hub(hub)
            set_connector_hub(None)

    def _check_db_cancellation(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> Callable[[], Awaitable[bool]]:
        """Build a DB-backed cancellation check closure for a run."""

        async def _check() -> bool:
            try:
                return await asyncio.wait_for(
                    self._do_db_cancellation_check(org_id, run_id),
                    timeout=5.0,
                )
            except TimeoutError:
                _log.warning(
                    "run_context.cancellation_db_timeout",
                    extra={"run_id": str(run_id)},
                )
                return False

        return _check

    async def _do_db_cancellation_check(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> bool:
        """Execute the DB cancellation check query."""
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            run = await get_run(session, run_id)
            return run is not None and run.cancellation_requested

    def _dispatch_context_write_audit(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """Build the audit hook that records non-context-setter run_context writes.

        Mirrors ``_check_db_cancellation``: opens a fresh session, applies RLS, and
        appends a ``context_write_by_non_setter`` event to the org's audit chain
        (§8.18). Failures and timeouts are logged and swallowed so the hook can
        never mask the ContextSetterViolationError raised by the decorator.
        """

        async def _audit(payload: dict[str, Any]) -> None:
            try:
                await asyncio.wait_for(
                    self._do_context_write_audit(org_id, run_id, payload),
                    timeout=5.0,
                )
            except TimeoutError:
                _log.warning(
                    "run_context.audit_hook_timeout",
                    extra={"run_id": str(run_id)},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "run_context.audit_hook_failed",
                    extra={"run_id": str(run_id)},
                )

        return _audit

    async def _do_context_write_audit(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        """Append the ``context_write_by_non_setter`` audit event for a run."""
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="context_write_by_non_setter",
                resource_type="run",
                resource_id=run_id,
                payload_json={
                    "node_id": payload.get("node_id"),
                    "role": payload.get("role"),
                    "attempted_keys": list(payload.get("attempted_keys") or []),
                },
            )

    @staticmethod
    def _log_accumulation_state(
        run_id: uuid.UUID,
        segments_completed: int,
        node_token_usage: dict[str, dict[str, int]] | None,
    ) -> None:
        """Distinguish a genuinely-empty accumulator from an upstream wiring
        regression (§4.2). A ``{}``/``None`` accumulator is legitimate when ZERO
        segments streamed; an EMPTY dict after ≥1 segment streamed means the
        on_chain_end / on_chat_model_end handlers stopped populating it.
        """
        if segments_completed > 0 and not node_token_usage:
            _log.warning(
                "cost_components_accumulation_broken",
                extra={"run_id": str(run_id), "segments_completed": segments_completed},
            )
        elif segments_completed == 0:
            _log.info(
                "cost_components_zero_nodes",
                extra={"run_id": str(run_id)},
            )

    @staticmethod
    def _compute_token_costs(
        node_token_usage: dict[str, dict[str, int]] | None,
        input_rate: Decimal,
        output_rate: Decimal,
    ) -> tuple[int | None, Decimal | None, dict[str, Any] | None]:
        """Compute total tokens, total cost, and per-node cost from node token usage."""
        if node_token_usage is None:
            return None, None, None

        total_tokens = sum(n.get("total_tokens") or 0 for n in node_token_usage.values())
        total_cost = Decimal(0)
        result_usage: dict[str, dict[str, Any]] = {}
        for node_id, n_data in node_token_usage.items():
            input_tokens = n_data.get("input_tokens")
            output_tokens = n_data.get("output_tokens")
            n_cost = Decimal(str(input_tokens if input_tokens is not None else 0)) * input_rate
            n_cost += Decimal(str(output_tokens if output_tokens is not None else 0)) * output_rate
            result_usage[node_id] = {**n_data, "cost_usd": float(n_cost)}
            total_cost += n_cost

        return total_tokens, total_cost, result_usage

    @staticmethod
    def _aggregate_sandbox_cost(completed_node_outputs: dict[str, Any] | None) -> Decimal:
        """Sum per-node sandbox-agent cost estimates into a single Decimal.

        Each completed node's captured output is ``{"artifacts": [...],
        "output": {...}}``; the inner ``output`` dict carries the numeric
        ``cost_estimate_usd`` attached by
        :func:`modulo.core.pipeline_engine.node_runner._compute_sandbox_cost`.
        Non-dict outputs, missing estimates, non-positive, and non-finite
        values (NaN/inf, which would otherwise corrupt the run total) contribute
        zero. Kept as a small pure helper so cost parity is testable and shared
        between :meth:`execute` and :meth:`resume`.
        """
        if not completed_node_outputs:
            return Decimal(0)
        sandbox_cost = Decimal(0)
        for node_output in completed_node_outputs.values():
            if not isinstance(node_output, dict):
                continue
            out = node_output.get("output")
            if not isinstance(out, dict):
                continue
            est = out.get("cost_estimate_usd")
            if isinstance(est, (int, float)) and est > 0 and math.isfinite(est):
                sandbox_cost += Decimal(str(est))
        return sandbox_cost

    def _get_evidence_provider(self, org_id: uuid.UUID) -> EvidenceProvider:
        """The injected evidence provider (tests) or the production
        E2B+DB-backed provider wired for this run's org.
        """
        if self._evidence_provider is not None:
            return self._evidence_provider
        return build_default_evidence_provider(self._session_factory, org_id)

    def _compute_run_work_intact(
        self,
        final_status: str,
        error_code: str | None,
        completed_node_outputs: dict[str, Any],
        node_ids: set[str],
    ) -> bool | None:
        """FAR-152 §15.3 — work_intact computed at terminalization from
        completed-node artifacts + the full DAG ran. NOT from the async
        evidence probe (restores the false-failure banner for #1/#3).

        Returns None for non-terminal statuses (nothing to write). An
        A1-elevated run (``failed`` + ``agent.failed``) is NOT complete — its
        honest work verdict is False (§15.4), so the zero-work elevation banner
        is what renders. Same for a ``sandbox.no_output_json`` session-lost run
        (FAR-227): the agent produced no output at all, so it can never be
        "work intact".
        """
        if final_status not in _TERMINAL_STATUSES:
            return None
        if final_status == "failed" and error_code in ("agent.failed", "sandbox.no_output_json"):
            return False
        return compute_work_intact(completed_node_outputs, node_ids)

    async def _run_post_terminal_evidence_probes(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        final_status: str,
        completed_node_outputs: dict[str, Any],
    ) -> None:
        """FAR-152 §15.3 — post-commit async evidence probe for no-op-eligible
        nodes (declared ``outcome:success``) on a complete run. Runs off the
        critical path (after terminalization commits), bounded ≤3s per probe,
        gated by the EvidenceProvider seam. Fail-open — a probe failure never
        affects the run.
        """
        if final_status != "complete" or not evidence_enabled():
            return
        evidence_nodes = [node_id for node_id, out in completed_node_outputs.items() if node_declared_success(out)]
        if not evidence_nodes:
            return
        provider = self._get_evidence_provider(org_id)
        results = await asyncio.gather(
            *[
                run_evidence_probe(
                    provider=provider,
                    session_factory=self._session_factory,
                    run_id=run_id,
                    node_id=node_id,
                )
                for node_id in evidence_nodes
            ],
            return_exceptions=True,
        )
        for node_id, res in zip(evidence_nodes, results, strict=False):
            if isinstance(res, Exception):
                _log.warning(
                    "heuristic.probe_task_failed",
                    extra={"run_id": str(run_id), "node_id": node_id},
                    exc_info=res,
                )

    async def _enforce_resume_sandbox_capacity(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
    ) -> None:
        """Atomic sandbox-capacity enforcement for a resume (FAR-1306 TOCTOU fix).

        The pre-check in the HITL route is a fast-fail optimisation; THIS is
        the real gate — serialised per-org via advisory lock so two concurrent
        approvals cannot both pass. Raises ``SandboxCapacityExceededError`` when
        the org's sandbox cap is reached.
        """
        graph_json = (
            await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))
        ).scalar_one_or_none()
        if not _graph_contains_sandbox_agent(graph_json):
            return
        cap = await get_sandbox_concurrency_limit(session, org_id)
        if cap is None:
            return
        # pg_advisory_xact_lock(k1, k2) — the two int4 keys are derived
        # from a deterministic md5 of the org UUID (same pattern as the
        # break-glass migration), so all workers/machines for the same
        # org hash to the SAME key (unlike hash(str(org_id)), which
        # PYTHONHASHSEED salts per-process). xact-scoped, so it is
        # released automatically exactly at this transaction's commit /
        # rollback — never leaked onto a pooled connection.
        k1, k2 = _uuid_to_lock_keys(org_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
            {"k1": k1, "k2": k2},
        )
        active = await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
        if active >= cap:
            raise SandboxCapacityExceededError(org_id)

    async def resume(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        resume_data: dict[str, Any],
        claim_token: str | None = None,
        check_sandbox_capacity: bool = True,
    ) -> Run:
        """Resume a run that was interrupted for HITL review.

        Loads the checkpointed graph state, injects *resume_data* as
        ``_hitl_decision``, and streams the graph until completion or the
        next interrupt. *claim_token* is the fenced-lease authority captured at
        resume start (see :meth:`execute`).
        """
        self._claim_token = claim_token
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            await update_run_status(session, run_id, "running", claimed_by=_WORKER_ID)

            # Atomic sandbox-capacity enforcement (FAR-1306 TOCTOU fix).
            # Skipped for reject/terminate paths (check_sandbox_capacity=False)
            # because those routes do not resume sandbox execution.
            if check_sandbox_capacity:
                await self._enforce_resume_sandbox_capacity(
                    session,
                    org_id=org_id,
                    run_id=run_id,
                    snapshot_id=run.snapshot_id,
                )

            snapshot_result = await session.execute(
                select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id)
            )
            snapshot = snapshot_result.scalar_one()
            graph_json: dict[str, Any] = snapshot.graph_json

            # FROZEN node-type map — captured ONCE per run at run start from the
            # snapshot's graph_json (the graph is immutable per snapshot) and
            # passed into finalize_cost at every pause and resume. A mid-run
            # graph edit cannot change sandbox_by_map mid-run (§1.6).
            node_type_map = derive_node_type_map(graph_json)

            # Re-validate the snapshot before resuming — the pipeline
            # config may have changed since the original run started.
            validation = await GraphValidator().validate_for_run(snapshot, {}, session)
            if not validation.is_valid:
                raise GraphValidationError(validation.issues, run_id)

            # Load eval definitions while session is active.
            eval_rows = await self._load_eval_defs_for_pipeline(session, run.pipeline_id)
            eval_defs_by_node = self._build_eval_defs_by_node(eval_rows, org_id, run.pipeline_id)

        pipeline_id = run.pipeline_id
        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id

        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)

            # Load pipeline for runaway protection limits.
            pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
            pipeline = pipeline_result.scalar_one_or_none()
            if pipeline is None:
                raise RunNotFoundError(run_id)

            guard = RunawayGuard(
                max_duration_seconds=pipeline.max_duration_seconds,
                max_steps=pipeline.max_steps,
                token_budget=pipeline.token_budget,
            )

        if not self._checkpointer_conn_string:
            raise RuntimeError("Cannot resume without a checkpointer configured")

        compiled = get_or_compile(
            pipeline_id,
            snapshot_id,
            lambda: build_graph_from_json(
                graph_json,
                session_factory=self._session_factory,
                org_id=org_id,
                pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
            ),
            pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
            graph_struct_hash=compute_port_topology_hash(graph_json),
        )

        config = {"configurable": {"thread_id": thread_id}}
        node_ids = {str(n["id"]) for n in graph_json.get("nodes", [])}
        node_token_budgets: dict[str, int] = {
            str(n["id"]): n["token_budget"] for n in graph_json.get("nodes", []) if n.get("token_budget") is not None
        }

        # FAR-215: seed the run-scoped conformance context on resume too, so a
        # node re-check fires after the run pauses/reviews (the manifest may
        # have changed between the original run and the resume). The claimed
        # guardrail list is hoisted ONCE per run; the live capability manifest
        # is still read per node at node start.
        claimed, claims_load_failed = await self._load_claimed_conformance_guardrails(org_id, pipeline_id)
        set_conformance_ctx(
            self._session_factory,
            org_id,
            snapshot.environment_profile_id,
            pipeline_id,
            claimed,
            claims_load_failed,
        )

        final_status: str = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        completed_node_outputs: dict[str, Any] = {}
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(self._check_db_cancellation(org_id, run_id))
        set_audit_hook(self._dispatch_context_write_audit(org_id, run_id))
        self._otel_bridge.set_run_context(str(org_id), str(pipeline_id))

        # Load model backends for this run's org.
        model_backend_hub = await self._init_model_backend_hub(org_id)

        try:
            from modulo.settings import get_settings

            _settings = get_settings()
            async with _checkpointer_scope(
                self._checkpointer_conn_string,
                organisation_id=org_id,
                fernet_key=_settings.fernet_key,
            ) as saver:
                compiled.checkpointer = saver
                await compiled.aupdate_state(
                    config,
                    {"_hitl_decision": resume_data, "_claim_lease": self._claim_token},
                )
                final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                    compiled,
                    None,
                    config,
                    node_ids,
                    broker,
                    run_id,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    completed_node_outputs=completed_node_outputs,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                    eval_definitions_by_node=eval_defs_by_node,
                    node_type_map=node_type_map,
                )
        except RuntimeError as exc:
            if "checkpointer" in str(exc):
                _log.warning("pipeline.resume_no_checkpointer", extra={"run_id": str(run_id)})
                final_status = "failed"
                error_code = "configuration_error"
                error_detail = "Pipeline configuration is invalid (checkpointer unavailable)."
            else:
                raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_detail = _traceback_detail(exc, limit=2000)
            _log.exception("pipeline.resume_error", extra={"run_id": str(run_id)})
            final_status = "failed"
            error_code = type(exc).__name__
        finally:
            set_cancellation_check(None)
            set_audit_hook(None)
            set_model_backend_hub(None)
            if model_backend_hub is not None:
                await _teardown_hub(model_backend_hub)
            if final_status != "awaiting_human":
                get_registry().close(run_id)

        # Mark terminal/awaiting_human — the SINGLE finalization path (PR A2).
        # (The eval_blocked audit + work_intact + finalize_cost tail live in
        # ``_finalize_run_after_stream``.)
        return await self._finalize_run_after_stream(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            node_type_map=node_type_map,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            completed_node_outputs=completed_node_outputs,
            node_ids=node_ids,
        )

    async def _record_eval_blocked_audit(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        error_detail: str | None,
    ) -> None:
        """Record an ``eval.blocked`` audit event — failure-isolated.

        Runs in its own session/transaction so a blocked-eval audit failure
        never interrupts terminalization. The payload carries the sanitized
        detail plus the run's pipeline id so the event links back to the graph.
        """
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            try:
                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type="eval.blocked",
                    resource_type="run",
                    resource_id=run_id,
                    payload_json={
                        "pipeline_id": str(pipeline_id),
                        "error_detail": _sanitize_detail(error_detail, limit=5000),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("audit.eval_blocked_failed", extra={"run_id": str(run_id)})

    async def _finalize_run_after_stream(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_type_map: dict[str, str],
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        node_token_usage: dict[str, Any] | None,
        completed_node_outputs: dict[str, Any],
        node_ids: set[str],
    ) -> Run:
        """Finalize a streamed run (execute + resume share this tail).

        Computes ``work_intact``, runs ``finalize_cost``, records the
        compensating daily fact when needed, fetches the final row, and fires
        the post-terminal evidence probes. ``final_status`` reflects the
        terminal/awaiting_human outcome of the stream.
        """
        # Record audit events for block failures on resume.
        eval_blocked = final_status == "eval_failed" and error_code == "eval_blocked"
        if eval_blocked:
            await self._record_eval_blocked_audit(
                org_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                error_detail=error_detail,
            )

        # Resume recomputes from LIVE components over the cumulative merged set
        # (two-state rule, §4.4); finalize_cost reads the stored cumulative set,
        # merges the resumed segment (segment-wins), and recomputes.
        # FAR-152 §15.3 — work_intact computed at terminalization (same rule as
        # execute()).
        work_intact = self._compute_run_work_intact(final_status, error_code, completed_node_outputs, node_ids)
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await finalize_cost(
                session,
                run_id=run_id,
                org_id=org_id,
                status=final_status,
                segment_node_token_usage=node_token_usage,
                segment_completed_node_outputs=completed_node_outputs,
                node_type_map=node_type_map,
                error_code=error_code,
                error_detail=error_detail,
                is_terminal=final_status in _TERMINAL_STATUSES,
                session_factory=self._session_factory,
                claim_token=self._claim_token,
            )
            if work_intact is not None:
                try:
                    await _apply_work_intact(session, run_id, work_intact, claim_token=self._claim_token)
                    # FAR-189 round-2 FIX 3: finalize_cost's inline classify ran
                    # BEFORE this write (work_intact still NULL at classify
                    # time). Re-persist the classification with the real value —
                    # the sweep skips already-classified rows, so this is the
                    # only correction for executor-terminalized runs.
                    await _reclassify_after_work_intact(session, run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("work_intact.write_failed", extra={"run_id": str(run_id)})

        # FAR-291: run-termination compensation for a guardrail-blocked
        # MID-RUN terminalization. The terminal status write (finalize_cost
        # above) has already landed; a run whose earlier nodes pushed a PR /
        # flipped a Linear status before a later node's output was
        # guardrail-blocked now gets those side effects compensated. Best-effort
        # + failure-isolated (guard-the-guard): a compensation failure never
        # crashes terminalization. Uses its own fresh session + hub.
        if eval_blocked:
            await self._compensate_blocked_run_best_effort(
                org_id=org_id,
                run_id=run_id,
                executed_nodes=completed_node_outputs,
            )

        # Fetch the final run in a fresh session — finalize_cost's ledger block
        # may have aborted the transaction in the whole-tx-abort reduced-escape
        # path (the run row was re-terminalized there).
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            final_run = await get_run(session, run_id)

        if final_run is None:
            raise RunNotFoundError(run_id)

        # FAR-152 §15.3 — post-commit async evidence probe (same rule as
        # execute()). Bounded ≤3s per node, gated by the EvidenceProvider seam.
        await self._run_post_terminal_evidence_probes(
            run_id=run_id,
            org_id=org_id,
            final_status=final_status,
            completed_node_outputs=completed_node_outputs,
        )
        # FAR-296 Phase 3b: revoke the per-run runner-role API key (if any was
        # minted for a script-mode sandbox). Failure-isolated — a revocation
        # failure never crashes terminalization (the key's short-TTL expiry in
        # validate_api_key is the backstop).
        try:
            await self._revoke_run_api_key(run_id=run_id, org_id=org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("run_api_key.revoke_failed", extra={"run_id": str(run_id)})
        return final_run

    async def _revoke_run_api_key(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
        """Revoke the per-run runner-role API key minted for a script-mode sandbox.

        Failure-isolated at the call site — a revocation failure never crashes
        terminalization (the key's short-TTL expiry in validate_api_key is the
        backstop).
        """
        if self._session_factory is None:
            return
        try:
            from modulo.auth.api_key import revoke_run_api_key

            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                await revoke_run_api_key(session, run_id=run_id, org_id=org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("run_api_key.revoke_failed", extra={"run_id": str(run_id)})

    async def execute(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
        claim_token: str | None = None,
    ) -> Run:
        """Execute the run to completion. Returns the final Run row.

        *claim_token* is the fenced-lease authority captured at execute start:
        it guards the capacity demotion, the NodeCancelledError pending-reset,
        and is seeded into LangGraph state as ``_claim_lease`` for the sandbox
        dispatch marker. A superseded executor (whose token was rotated by a
        successor) cannot demote/complete the run out from under it.

        A capacity-blocked run is returned ``pending`` (with its reason marker)
        — there is NO in-process retry loop (plan F3b); recovery is owned by
        ``dispatcher_reconcile`` (cron_helpers) and ``stale_run_recovery_sweep``.
        """
        self._claim_token = claim_token
        # Load run + pipeline + snapshot in one short-lived transaction.
        run, pipeline, snapshot, graph_json, node_type_map = await self._load_execution_context(
            run_id=run_id,
            org_id=org_id,
            input_payload=input_payload,
        )
        variant_config_snapshot = run.variant_config_snapshot
        scalars = self._capture_execution_scalars(pipeline, run)
        pipeline_id = scalars["pipeline_id"]
        max_concurrent = scalars["max_concurrent"]
        pipeline_retry_policy = scalars["pipeline_retry_policy"]
        guard = scalars["guard"]
        snapshot_id = scalars["snapshot_id"]
        thread_id = scalars["thread_id"]
        is_correction_run = scalars["is_correction_run"]

        # Load eval definitions for conditional HITL gating (eval-before-interrupt).
        eval_defs_by_node: dict[str, list[EvalDefDTO]] = {}
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            eval_rows = await self._load_eval_defs_for_pipeline(session, pipeline_id)
        eval_defs_by_node = self._build_eval_defs_by_node(eval_rows, org_id, pipeline_id)

        # Non-blocking capacity check — if at limit the run is demoted back to
        # pending (with a reason marker) and recovered by dispatcher_reconcile /
        # the stale-run sweep (plan F3b).
        capacity_run = await self._check_capacity(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            graph_json=graph_json,
            snapshot_id=snapshot_id,
        )
        if capacity_run.status != "running":
            # Capacity-blocked or terminal (plan F3b): return the run as-is.
            # There is NO in-process ``_retry_pending`` loop — a capacity-blocked
            # run stays ``pending`` with its reason marker and is recovered by
            # ``dispatcher_reconcile`` (cron_helpers) when a slot frees, with
            # ``stale_run_recovery_sweep``'s stranded re-dispatch as the durable
            # liveness backstop. A terminal run (cancelled/completed while the
            # capacity check ran) is never resurrected.
            return capacity_run

        final_status: str = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        completed_node_outputs: dict[str, Any] = {}
        # Initialised so the terminalization work_intact computation is safe even
        # when the stream never started (compile/pre-stream failure) — a run with
        # no executed nodes is never work-intact.
        node_ids: set[str] = set()
        model_backend_hub, connector_hub, broker, single_sandbox_node = await self._init_run_environment(
            org_id=org_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            graph_json=graph_json,
        )
        # FAR-295: computed ONCE per run — a graph containing ANY node declared
        # non-idempotent (idempotent=false) suppresses every retry path below
        # (the node-level transient retry and the run-level retry_policy
        # re-dispatch), because either retry re-executes the whole run and
        # would re-run the side-effecting node.
        graph_idempotent = _graph_is_idempotent(graph_json)
        # FAR-228: set when guard B suppressed a transient retry — gates the
        # eval-suite/fire_agent_signal block and publishes run_completed.
        gate_suppressed = False

        try:
            final_status, error_code, error_detail, node_token_usage = await self._prepare_and_stream(
                run_id=run_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                snapshot=snapshot,
                input_payload=input_payload,
                variant_config_snapshot=variant_config_snapshot,
                graph_json=graph_json,
                eval_defs_by_node=eval_defs_by_node,
                thread_id=thread_id,
                node_ids=node_ids,
                completed_node_outputs=completed_node_outputs,
                guard=guard,
                node_type_map=node_type_map,
                pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
                broker=broker,
            )
        except asyncio.CancelledError:
            raise
        except (NodeCancelledError, SandboxNodeFailedError) as exc:
            # Transient node cancellation / sandbox-infra failure (e.g. an E2B
            # sandbox command wait cancelled from outside, a stall, or a command
            # timeout). Do NOT terminal-fail: the run is still retryable.
            # Bounded by the SAQ node-attempt count (NOT the claim count —
            # capacity-deferred / non-executing claims must not consume the
            # retry budget). Uses the ORIGINAL executor's captured claim token
            # for the fenced pending-reset; a superseded original, a watchdog
            # stall, or a requested cancellation SKIP the reset (the run is
            # owned elsewhere / terminal / cancelled — never demote it).
            # The gate/requeue/superseded/terminal decision chain lives in
            # ``_decide_transient_failure`` — this handler is a thin dispatcher.
            # For requeue/superseded the helper already performed the fenced
            # pending-reset + cleanup, and the bare re-raise below propagates out
            # of execute() BEFORE the post-stream try/finally, exactly as today.
            _log.warning(
                "pipeline.node_cancelled_transient",
                extra={"run_id": str(run_id), "exc_type": type(exc).__name__},
            )
            transient = await self._decide_transient_failure(
                exc=exc,
                run_id=run_id,
                org_id=org_id,
                graph_json=graph_json,
                graph_idempotent=graph_idempotent,
                single_sandbox_node=single_sandbox_node,
                model_backend_hub=model_backend_hub,
                connector_hub=connector_hub,
                broker=broker,
                stall_requested=self._stall_requested,
            )
            gate_suppressed, final_status, error_code, error_detail = self._apply_transient_decision(
                transient,
                completed_node_outputs=completed_node_outputs,
                gate_suppressed=gate_suppressed,
            )
        except Exception as exc:
            _tb = _traceback_detail(exc, limit=2000)
            _log.exception("pipeline.execution_error", extra={"run_id": str(run_id)})
            final_status = "failed"
            error_code = type(exc).__name__
            error_detail = _tb

        # Retry policy: if the run ended in a state the pipeline's
        # retry_policy says to retry and the attempt budget remains, reset the
        # run to pending + release the dispatch lease + re-raise so SAQ
        # re-dispatches it as a new attempt — the same fenced pattern as the
        # NodeCancelledError path above. The E2B dispatch fence was retired in
        # favour of ``runs.claim_token`` fencing (settings.py F3a note), so the
        # fenced pending-reset below IS the fence release.
        retry_decision = await self._maybe_retry_after_policy(
            run_id=run_id,
            org_id=org_id,
            pipeline_retry_policy=pipeline_retry_policy,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            is_correction_run=is_correction_run,
            graph_idempotent=graph_idempotent,
            graph_json=graph_json,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
            broker=broker,
        )
        if retry_decision == "side_effect_unknown":
            # FAR-296 Phase 2: the lease probe blocked the requeue — a script
            # process may have run with unknown side-effect state. Terminal-fail
            # with ``script.side_effect_unknown`` (never retried) so the run
            # reaches a needs-human state. The run_failed publish happened inside
            # ``_maybe_retry_after_policy``; here we surface the code/detail so
            # the terminalization write matches.
            final_status = "failed"
            error_code = _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN
            error_detail = _sanitize_detail(
                "Script-mode sandbox has an unresolved execution claim (side effect unknown); "
                "not retried — needs human review.",
                limit=5000,
            )

        # The post-stream tail (eval-suite checks, agent_signal firing, gated
        # run_completed publish) owns its own try/except
        # (pipeline.post_stream_error); it can also mutate final_status /
        # error_code / error_detail (EvalSuiteBlockedError), so its returned
        # values are rebound here. The tail + the resource teardown
        # (contextvars, hubs, broker registry close) live in
        # ``_run_post_stream_and_teardown`` — the teardown runs in a finally so
        # it fires even when the tail re-raises (e.g. CancelledError), matching
        # the original inline block.
        final_status, error_code, error_detail = await self._run_post_stream_and_teardown(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            completed_node_outputs=completed_node_outputs,
            broker=broker,
            gate_suppressed=gate_suppressed,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
        )

        # Mark complete/failed/cancelled/awaiting_human — the SINGLE
        # finalization path (PR A2). finalize_cost merges the accumulated
        # segment sets into the stored cumulative sets (segment-wins), builds
        # the enriched union + breakdown (total == sum), and runs the
        # terminal-only ledger block.
        # FAR-152 §15.3 — work_intact computed at terminalization from
        # completed-node artifacts + the full DAG ran. NOT from the async
        # evidence probe. Written atomically inside the same terminalization
        # transaction (restores the false-failure banner for #1/#3).
        return await self._finalize_run_after_stream(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            node_type_map=node_type_map,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            completed_node_outputs=completed_node_outputs,
            node_ids=node_ids,
        )

    async def _decide_transient_failure(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        graph_json: dict[str, Any],
        graph_idempotent: bool,
        single_sandbox_node: bool,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        broker: RunEventBroker,
        stall_requested: asyncio.Event | None,
    ) -> dict[str, Any]:
        """Decide how a transient node-cancelled / sandbox-failed run recovers.

        Extracted from ``execute``'s ``except (NodeCancelledError,
        SandboxNodeFailedError)`` handler. Loads the transient state, computes
        superseded / stalled / gate_ok / script_lease_ok, then returns a
        decision dict. The caller (``execute``) performs the side effects that
        must stay in its scope — the idempotency-gate envelope write and the
        bare re-raise for requeue / superseded (which propagates out of
        execute() before the post-stream try/finally). Mutations that are safe
        to perform here (the fenced pending-reset + cleanup) happen in this
        method so the re-raise path stays identical to the inline chain.

        Returns one of:
        - ``{"decision": "gate", ...}`` — idempotency gate suppressed the
          transient retry (delivery already sent); the run completes COMPLETE.
        - ``{"decision": "requeue"}`` — retry budget remains; the run was
          fenced back to pending and the execution environment cleaned up so
          the SAQ job re-entry gets a fresh broker.
        - ``{"decision": "superseded"}`` — the run is owned by a successor or
          was already terminal-failed by the zombie watchdog; cleaned up, never
          reset, never terminal-failed here.
        - ``{"decision": "terminal", ...}`` — retries exhausted; the run_failed
          event was published and the caller terminal-fails with the returned
          code/detail.
        """
        from modulo.settings import get_settings

        retries = int(get_settings().saq_run_retries)
        node_attempt_count, current_token, run_markers, cancellation_requested = await self._load_transient_state(
            run_id=run_id, org_id=org_id
        )

        superseded = self._claim_token is not None and current_token is not None and current_token != self._claim_token
        stalled = bool(stall_requested is not None and stall_requested.is_set())

        # FAR-228 guard B (retry-suppression — THE INCIDENT FIX): before any
        # state mutation, check whether the run ALREADY delivered (a prior
        # attempt's marker carries delivery_done=True) for THIS node. If so,
        # a transient retry is suppressed: the run completes COMPLETE with
        # error_code harness.idempotency_gate instead of burning the retry
        # budget and re-sending the side effect. ORDERING INVARIANT: computed
        # AFTER superseded/stalled (above) and BEFORE any mutation —
        # final_status/error_code are first mutated only in the
        # retries-exhausted branch below. `markers` is the ALREADY-LOADED
        # current_run.raw_output_markers — no fresh SELECT.
        gate_ok = self._idempotency_gate_ok(
            exc=exc,
            run_markers=run_markers,
            run_id=run_id,
            superseded=superseded,
            stalled=stalled,
            cancellation_requested=cancellation_requested,
            single_sandbox_node=single_sandbox_node,
        )
        # Only SandboxNodeFailedError carries a node_id — the gate is
        # keyed on it, so a None node_id (plain NodeCancelledError) can
        # never suppress.
        _gated_node_id = exc.node_id if isinstance(exc, SandboxNodeFailedError) else None
        # FAR-296 Phase 2: before ANY requeue of a script-mode run, prove no
        # script process could still be alive (stale-claim lease probe).
        # A stale ``script_executing`` lease means a script may have run —
        # requeue is forbidden (exactly-once). Only when the probe proves no
        # live lease does the run stay eligible for the fenced reset below.
        # The probe is computed BEFORE the gate/retry chain below so
        # the idempotency gate stays the FIRST branch (it must suppress the
        # pending-reset when a delivery marker is present), and so the
        # pending-reset branch remains reachable for BOTH script-mode and
        # non-script-mode graphs (a stale lease simply disqualifies it).
        script_lease_ok = await self._script_lease_ok(run_id=run_id, org_id=org_id, graph_json=graph_json)
        if gate_ok and _gated_node_id is not None:
            _log.warning(
                "pipeline.idempotency_gate.suppressed_retry",
                extra={"run_id": str(run_id), "node_id": _gated_node_id},
            )
            return {
                "decision": "gate",
                "final_status": "complete",
                "error_code": _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE,
                "error_detail": "delivery already sent; transient retry suppressed by idempotency gate",
                "gated_node_id": _gated_node_id,
            }
        if _can_fenced_requeue(
            node_attempt_count,
            retries,
            superseded,
            stalled,
            script_lease_ok,
            graph_idempotent,
        ):
            # Fenced pending-reset: a conditional UPDATE guarded by OUR
            # captured claim token + status='running' so a superseded
            # original cannot demote the successor's running row, a stalled
            # (watchdog-cancelled) executor cannot resurrect a run the
            # watchdog just failed, and a cancellation cannot be reversed.
            await self._fenced_pending_reset(run_id=run_id, org_id=org_id)
            # The caller's re-raise propagates out of execute() BEFORE the
            # post-stream try/finally, so run its cleanup here: clear the
            # cancellation check + hubs and close the run's broker so the
            # retry re-entry gets a fresh broker and no stale contextvars.
            await self._cleanup_run_resources(
                model_backend_hub=model_backend_hub, connector_hub=connector_hub, run_id=run_id
            )
            return {"decision": "requeue"}
        if superseded or stalled:
            # Superseded or watchdog-stalled: the run is owned by a
            # successor or was already terminal-failed by the zombie
            # watchdog — never reset it to pending and never terminal-fail
            # it here. Clean up so the SAQ job retry re-entry gets a fresh
            # broker and no stale contextvars (the caller re-raises).
            await self._cleanup_run_resources(
                model_backend_hub=model_backend_hub, connector_hub=connector_hub, run_id=run_id
            )
            return {"decision": "superseded"}
        # Retries exhausted — terminal failure with a MEANINGFUL code
        # (not the raw langgraph class name). Publish the run_failed
        # event so WS subscribers get a live failure notification,
        # consistent with every other terminal-failure path in this
        # file.
        #
        # Write cap: 5000, NOT 500 — the transient node-cancelled
        # detail is the only place the FAR-197 no-output.json
        # diagnostic (stdout/stderr tails, the E2B log tail where the
        # kill reason lives) reaches the user. It is bounded by the
        # builder to fit the 5000-char sanitizer/column cap
        # (runs.error_detail is String(5000)), and every detail read
        # surface (run-detail REST + MCP) presents at limit=5000; list
        # surfaces truncate to 200 by design. A 500-char write cap cut
        # the stderr + log tails entirely for large-output failures.
        error_code, error_detail = self._transient_failure_detail(
            exc=exc,
            script_lease_ok=script_lease_ok,
            graph_idempotent=graph_idempotent,
            node_attempt_count=node_attempt_count,
            retries=retries,
        )
        broker.publish("run_failed", {"error": error_code, "detail": error_detail})
        return {
            "decision": "terminal",
            "final_status": "failed",
            "error_code": error_code,
            "error_detail": error_detail,
        }

    def _apply_transient_decision(
        self,
        transient: dict[str, Any],
        *,
        completed_node_outputs: dict[str, Any],
        gate_suppressed: bool,
    ) -> tuple[bool, str, str | None, str | None]:
        """Apply a transient-failure decision dict to execute()'s locals.

        Returns ``(gate_suppressed, final_status, error_code, error_detail)``.
        For ``requeue``/``superseded`` the bare ``raise`` propagates the
        original exception out of ``execute()`` so the SAQ job retry path
        re-dispatches — the fenced pending-reset + cleanup already happened
        inside ``_decide_transient_failure``.
        """
        if transient["decision"] == "gate":
            # SKIP the pending-reset, the re-raise and the run_failed publish
            # — fall through to the existing finalization with
            # final_status="complete". run_completed is published after the
            # eval-skip point, while the broker is still open.
            completed_node_outputs[transient["gated_node_id"]] = _idempotency_gate_skipped_envelope(
                transient["gated_node_id"]
            )
            return True, transient["final_status"], transient["error_code"], transient["error_detail"]
        if transient["decision"] in ("requeue", "superseded"):
            raise
        # terminal (retries exhausted) — or gate, whose decision dict carries
        # the same final_status / error_code / error_detail it set above.
        return gate_suppressed, transient["final_status"], transient["error_code"], transient["error_detail"]

    async def _load_execution_context(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
    ) -> tuple[Run, Pipeline, PipelineSnapshot, dict[str, Any], dict[str, str]]:
        """Load the run + pipeline + snapshot in one short-lived transaction.

        Runs the pre-run GraphValidator (incl. the malformed-retry-policy
        check) and derives the frozen node-type map. Queries directly to avoid
        SQLAlchemy async lazy-load (MissingGreenlet). Raises RunNotFoundError /
        GraphValidationError exactly as the original inline block did.
        """
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == run.pipeline_id))
            pipeline = pipeline_result.scalar_one()
            snapshot_result = await session.execute(
                select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id)
            )
            snapshot = snapshot_result.scalar_one()
            graph_json: dict[str, Any] = snapshot.graph_json

            # FROZEN node-type map — captured ONCE per run at run start (§1.6)
            # and passed into finalize_cost at every pause and resume.
            node_type_map = derive_node_type_map(graph_json)

            # Pre-run validation — blocks execution on errors.
            validation = await GraphValidator().validate_for_run(snapshot, input_payload, session)
            if not validation.is_valid:
                raise GraphValidationError(validation.issues, run_id)

            # A malformed pipeline retry_policy must not silently disable retries
            # at run time — surface it as a hard validation error (default no-policy
            # is unaffected). Only dict values carry a policy; None/non-dicts (e.g.
            # legacy rows) are the no-policy default and _retry_after_policy
            # already fail-safes them to no retry.
            retry_policy_check = ValidationResult()
            _retry_policy_value = getattr(pipeline, "retry_policy", None)
            if isinstance(_retry_policy_value, dict):
                GraphValidator.check_retry_policy(_retry_policy_value, retry_policy_check)
                if not retry_policy_check.is_valid:
                    raise GraphValidationError(retry_policy_check.issues, run_id)
        return run, pipeline, snapshot, graph_json, node_type_map

    def _capture_execution_scalars(self, pipeline: Pipeline, run: Run) -> dict[str, Any]:
        """Capture scalar attributes from the run + pipeline before sessions close."""
        pipeline_id = run.pipeline_id
        max_concurrent = pipeline.max_concurrent_runs
        pipeline_retry_policy: dict[str, Any] = {}
        try:
            raw_policy = getattr(pipeline, "retry_policy", None)
            if isinstance(raw_policy, dict):
                pipeline_retry_policy = raw_policy
        except Exception:
            # A malformed/legacy retry_policy must never crash the run —
            # default to no retry.
            pipeline_retry_policy = {}
        guard = RunawayGuard(
            max_duration_seconds=pipeline.max_duration_seconds,
            max_steps=pipeline.max_steps,
            token_budget=pipeline.token_budget,
        )

        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id
        # FAR-210: correction runs are EXCLUDED from retry_policy re-dispatch.
        # A single-node correction has a fixed bounded retry budget owned by the
        # correction path itself; the pipeline retry policy must never
        # re-dispatch a correction run (no chained corrections).
        run_trigger_type = getattr(run, "trigger_type", "") or ""
        is_correction_run = run_trigger_type == "correction"

        return {
            "pipeline_id": pipeline_id,
            "max_concurrent": max_concurrent,
            "pipeline_retry_policy": pipeline_retry_policy,
            "guard": guard,
            "snapshot_id": snapshot_id,
            "thread_id": thread_id,
            "is_correction_run": is_correction_run,
        }

    async def _init_run_environment(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        graph_json: dict[str, Any],
    ) -> tuple[ModelBackendHub | None, Any | None, RunEventBroker, bool]:
        """Set up the run-scoped execution environment (broker + hubs + otel).

        Returns ``(model_backend_hub, connector_hub, broker, single_sandbox_node)``.
        """
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(self._check_db_cancellation(org_id, run_id))
        set_audit_hook(self._dispatch_context_write_audit(org_id, run_id))
        self._otel_bridge.set_run_context(str(org_id), str(pipeline_id))

        # Load model backends for this run's org — provides LLM access to agent nodes.
        model_backend_hub = await self._init_model_backend_hub(org_id)
        # Load connector hub for this run's org — provides connector access to connector nodes.
        # FAR-418: wire fetch-time capability scoping — when every graph node is
        # connector-scoped the hub decrypts ONLY the union of their allowed
        # connectors, so out-of-scope credentials are never disclosed.
        from modulo.core.capability_scope import compute_run_fetch_scope

        connector_hub = await self._init_connector_hub(
            org_id,
            allowed_connectors=compute_run_fetch_scope(graph_json),
        )

        # FAR-228: the idempotency gate is inert on multi-node graphs — it only
        # fires for a SINGLE sandbox_agent node (guard A in the node body and
        # guard B below both require this).
        single_sandbox_node: bool = (
            sum(1 for n in graph_json.get("nodes", []) if str(n.get("node_type", "")).strip() == "sandbox_agent") == 1
        )
        return model_backend_hub, connector_hub, broker, single_sandbox_node

    async def _prepare_and_stream(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        snapshot: PipelineSnapshot,
        input_payload: dict[str, Any],
        variant_config_snapshot: dict[str, Any] | None,
        graph_json: dict[str, Any],
        eval_defs_by_node: dict[str, list[EvalDefDTO]],
        thread_id: str,
        node_ids: set[str],
        completed_node_outputs: dict[str, Any],
        guard: RunawayGuard,
        node_type_map: dict[str, str],
        pipeline_node_timeout_seconds: int,
        broker: RunEventBroker,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Compile (or retrieve) the StateGraph and stream it to completion.

        Returns ``(final_status, error_code, error_detail, node_token_usage)``.
        ``node_ids`` is mutated in place (the caller owns the empty set) so the
        finalization path sees the same ids the original inline rebinding
        produced.
        """
        # Compile (or retrieve from cache) the StateGraph.
        compiled = get_or_compile(
            pipeline_id,
            snapshot_id,
            lambda: build_graph_from_json(
                graph_json,
                eval_definitions_by_node=eval_defs_by_node,
                session_factory=self._session_factory,
                org_id=org_id,
                pipeline_node_timeout_seconds=pipeline_node_timeout_seconds,
            ),
            pipeline_node_timeout_seconds=pipeline_node_timeout_seconds,
            graph_struct_hash=compute_port_topology_hash(graph_json),
        )

        initial_state = _seed_state(snapshot, input_payload, variant_config_snapshot)
        initial_state.update(
            {
                "_run_id": run_id,
                "_org_id": org_id,
                "_claim_lease": self._claim_token,
            }
        )
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        node_ids.clear()
        node_ids.update({str(n["id"]) for n in graph_json.get("nodes", [])})
        # FAR-369: expose each node's configured ``timeout_seconds`` so the
        # absolute node-deadline watchdog (run_executor_with_watchdog) can hold
        # every node to its own hard deadline. Defaults to the pipeline-level
        # node timeout when a node omits it. Mutate in place (NOT reassign) so
        # the dict object passed to the watchdog stays the live reference that
        # the watchdog reads once populated.
        self._node_timeouts.clear()
        self._node_timeouts.update(
            {
                str(n["id"]): int(n.get("timeout_seconds", pipeline_node_timeout_seconds))
                for n in graph_json.get("nodes", [])
            }
        )
        node_token_budgets: dict[str, int] = {
            str(n["id"]): n["token_budget"] for n in graph_json.get("nodes", []) if n.get("token_budget") is not None
        }

        # FAR-215: seed the run-scoped conformance context so every node
        # re-validates its bound guardrail conformance at node start. The
        # claimed guardrail list is hoisted ONCE per run (one query); the
        # live capability manifest is still read per node at node start.
        claimed, claims_load_failed = await self._load_claimed_conformance_guardrails(org_id, pipeline_id)
        set_conformance_ctx(
            self._session_factory,
            org_id,
            snapshot.environment_profile_id,
            pipeline_id,
            claimed,
            claims_load_failed,
        )

        # Count this REAL node-execution attempt (post capacity-check,
        # post compile, pre-stream). The NodeCancelledError retry budget
        # below is bounded by node_attempt_count, NOT claim_count —
        # claim_count increments on every SAQ claim including
        # capacity-deferred / non-executing claims, which would otherwise
        # consume the retry budget before any real execution attempt
        # (postmortem FAR-121).
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await session.execute(
                text("UPDATE runs SET node_attempt_count = node_attempt_count + 1 WHERE id = :rid"),
                {"rid": str(run_id)},
            )

        # FAR-432 (item 4a): a checkpointer is ONLY attached when the pipeline
        # is HITL/interactive. The decision is DERIVED from the graph at
        # execution time (a HITL gate edge or a ``manual`` input node → resume
        # must work → persist). Batch pipelines never pause, so their
        # per-superstep checkpoints are pure overhead — derive persistence off.
        # ``compiled`` is shared via the graph cache, so the checkpointer is
        # explicitly cleared in the non-interactive branch to avoid leaking a
        # previously-attached saver into a batch run of the same snapshot.
        if self._checkpointer_conn_string and _graph_is_interactive(graph_json):
            from modulo.settings import get_settings

            _settings = get_settings()
            async with _checkpointer_scope(
                self._checkpointer_conn_string,
                organisation_id=org_id,
                fernet_key=_settings.fernet_key,
            ) as saver:
                compiled.checkpointer = saver
                final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                    compiled,
                    initial_state,
                    config,
                    node_ids,
                    broker,
                    run_id,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    completed_node_outputs=completed_node_outputs,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                    eval_definitions_by_node=eval_defs_by_node,
                    node_type_map=node_type_map,
                )
        else:
            compiled.checkpointer = None
            final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                compiled,
                initial_state,
                config,
                node_ids,
                broker,
                run_id,
                pipeline_id=pipeline_id,
                org_id=org_id,
                completed_node_outputs=completed_node_outputs,
                guard=guard,
                node_token_budgets=node_token_budgets,
                eval_definitions_by_node=eval_defs_by_node,
                node_type_map=node_type_map,
            )
        return final_status, error_code, error_detail, node_token_usage

    async def _load_transient_state(
        self, *, run_id: uuid.UUID, org_id: uuid.UUID
    ) -> tuple[int, str | None, dict[str, Any] | None, bool]:
        """Reload the run's attempt markers + claim + cancellation state.

        Returns ``(node_attempt_count, current_token, run_markers, cancellation_requested)``.
        """
        node_attempt_count = 0
        current_token: str | None = None
        run_markers: dict[str, Any] | None = None
        cancellation_requested = False
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            current_run = await get_run(session, run_id)
            if current_run is not None:
                node_attempt_count = int(current_run.node_attempt_count or 0)
                current_token = current_run.claim_token
                run_markers = current_run.raw_output_markers
                cancellation_requested = bool(current_run.cancellation_requested)
        return node_attempt_count, current_token, run_markers, cancellation_requested

    def _idempotency_gate_ok(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_markers: dict[str, Any] | None,
        run_id: uuid.UUID,
        superseded: bool,
        stalled: bool,
        cancellation_requested: bool,
        single_sandbox_node: bool,
    ) -> bool:
        """FAR-228 guard B — should a transient retry be suppressed by the idempotency gate?"""
        from modulo.settings import get_settings

        gate_ok = False
        try:
            gate_ok = (
                _should_skip_retry(getattr(exc, "node_id", None), run_markers, str(run_id))
                and not superseded
                and not stalled
                and not cancellation_requested
                and getattr(get_settings(), "modulo_idempotency_gate_enabled", True)
                and single_sandbox_node
            )
        except Exception:
            _log.warning("pipeline.idempotency_gate.check_failed", extra={"run_id": str(run_id)})
            gate_ok = False
        return gate_ok

    async def _script_lease_ok(self, *, run_id: uuid.UUID, org_id: uuid.UUID, graph_json: dict[str, Any]) -> bool:
        """FAR-296 Phase 2 — prove no script process could still be alive.

        Returns True when the graph has no script mode, or when the stale-claim
        lease probe proves no live lease. The probe-failure and blocked-requeue
        warnings stay here (same messages as the original inline block).
        """
        script_lease_ok = True
        if _graph_has_script_mode(graph_json):
            try:
                script_lease_ok = await _script_lease_probe_ok(
                    self._session_factory, str(run_id), org_id, self._claim_token
                )
            except Exception:
                _log.warning(
                    "script.lease_probe_eval_failed",
                    extra={"run_id": str(run_id)},
                    exc_info=True,
                )
                script_lease_ok = False
            if not script_lease_ok:
                _log.warning(
                    "script.lease_probe.blocked_requeue",
                    extra={"run_id": str(run_id), "reason": "stale script_executing lease"},
                )
        return script_lease_ok

    async def _fenced_pending_reset(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
        """Fenced pending-reset: a conditional UPDATE guarded by OUR captured
        claim token + status='running' so a superseded original cannot demote
        the successor's running row, a stalled (watchdog-cancelled) executor
        cannot resurrect a run the watchdog just failed, and a cancellation
        cannot be reversed.
        """
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await session.execute(
                text(
                    "UPDATE runs SET status='pending', error_code=NULL, error_detail=NULL "
                    "WHERE id=:rid AND claim_token=:tok AND status='running' "
                    "AND cancellation_requested = false"
                ),
                {"rid": str(run_id), "tok": self._claim_token},
            )

    async def _cleanup_run_resources(
        self,
        *,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        run_id: uuid.UUID,
    ) -> None:
        """Clear the contextvars + tear down hubs + close the run's broker.

        Runs BEFORE a re-raise that propagates out of execute() — the
        post-stream try/finally is not entered on that path, so the cleanup
        must happen here for the retry re-entry to get a fresh broker and no
        stale contextvars.
        """
        set_cancellation_check(None)
        set_audit_hook(None)
        set_model_backend_hub(None)
        set_connector_hub(None)
        if model_backend_hub is not None:
            await _teardown_hub(model_backend_hub)
        if connector_hub is not None:
            await _teardown_hub(connector_hub)
        get_registry().close(run_id)

    def _transient_failure_detail(
        self,
        *,
        exc: Exception,
        script_lease_ok: bool,
        graph_idempotent: bool,
        node_attempt_count: int,
        retries: int,
    ) -> tuple[str, str]:
        """Terminal failure code/detail for a retries-exhausted transient node failure."""
        if not script_lease_ok:
            # FAR-296 Phase 2: a stale script_executing lease blocked the
            # requeue — a script process may have run, so the side-effect
            # state is unknown. Terminal (never retried) with the
            # needs-human ``script.side_effect_unknown`` code.
            error_code = _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN
            error_detail = _sanitize_detail(
                "Script-mode sandbox has an unresolved execution claim (side effect unknown); "
                "not retried — needs human review: " + str(exc),
                limit=5000,
            )
        else:
            error_code = "node_cancelled"
            if not graph_idempotent and node_attempt_count < retries:
                # FAR-295: the run was NOT retried because a node in the
                # graph is non-idempotent (idempotent=false) — a retry
                # would re-execute a side-effecting step. Distinguish
                # this from the retries-exhausted message so the run
                # detail readout explains WHY it terminal-failed on the
                # first attempt.
                error_detail = _sanitize_detail(
                    "Sandbox node failed (transient); retry suppressed because a node in the "
                    "graph is non-idempotent (idempotent=false) and re-running could double-execute "
                    "a side effect: " + str(exc),
                    limit=5000,
                )
            elif isinstance(exc, NodeCancelledError):
                error_detail = _sanitize_detail(
                    "Sandbox node cancelled (transient) after retries exhausted: " + str(exc), limit=5000
                )
            else:
                # SandboxNodeFailedError: the FAR-197 no-output diagnostic
                # is a fully bounded message designed to survive this
                # surface in full — keep the limit at the sanitizer/column
                # cap (5000), not the 500 used for the short
                # NodeCancelledError string, or the kill-reason log tail
                # would be the first thing truncated (FAR-197 review).
                error_detail = _sanitize_detail(
                    "Sandbox node failed (transient) after retries exhausted: " + str(exc), limit=5000
                )
        return error_code, error_detail

    async def _maybe_retry_after_policy(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_retry_policy: dict[str, Any],
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        is_correction_run: bool,
        graph_idempotent: bool,
        graph_json: dict[str, Any],
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        broker: RunEventBroker,
    ) -> str:
        """Apply the pipeline retry_policy after a terminal outcome.

        Returns ``"retried"`` (the fenced pending-reset + backoff +
        ``RunRetryPolicyError`` re-raise happened inside this helper — control
        never returns), ``"side_effect_unknown"`` (terminal-failed; the
        ``run_failed`` publish happened here and the caller sets the
        code/detail), or ``"none"`` (nothing to do).
        """
        retry_budget = _retry_after_policy(pipeline_retry_policy, final_status, error_code, error_detail)
        # FAR-295 / FAR-210: a non-idempotent graph or a correction run is
        # NEVER re-dispatched by the retry_policy (re-running would re-execute
        # a side-effecting node / a correction's budget is owned elsewhere).
        if not _retry_policy_applies(retry_budget, is_correction_run, graph_idempotent):
            return "none"
        # _retry_policy_applies guarantees a budget (not None) here; a defensive
        # narrowing keeps mypy strict-clean without introducing an ``assert``.
        if retry_budget is None:
            return "none"
        node_attempt_count = 0
        current_claim_token: str | None = None
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            current_run = await get_run(session, run_id)
            if current_run is not None:
                node_attempt_count = int(current_run.node_attempt_count or 0)
                current_claim_token = current_run.claim_token
        superseded = (
            self._claim_token is not None
            and current_claim_token is not None
            and current_claim_token != self._claim_token
        )
        # ``node_attempt_count`` is incremented to 1 on the FIRST real
        # execution attempt (above), so ``<= retry_budget`` means the
        # budget counts actual RETRIES: budget=1 retries attempt 1
        # (1 <= 1) and is terminal after attempt 2 (2 <= 1 is false) —
        # 1 retry, 2 attempts. ``< retry_budget`` would have yielded N
        # total attempts (N-1 retries), contradicting the API's
        # "max_retries means retries" contract.
        # FAR-296 Phase 2: before ANY requeue of a script-mode run, prove
        # no script process could still be alive (stale-claim lease probe).
        script_retry_probe_ok = True
        if _graph_has_script_mode(graph_json):
            try:
                script_retry_probe_ok = await _script_lease_probe_ok(
                    self._session_factory, str(run_id), org_id, self._claim_token
                )
            except Exception:
                _log.warning(
                    "script.lease_probe_eval_failed_retry_policy",
                    extra={"run_id": str(run_id)},
                    exc_info=True,
                )
                script_retry_probe_ok = False
            if not script_retry_probe_ok:
                _log.warning(
                    "script.lease_probe.blocked_retry_policy",
                    extra={"run_id": str(run_id), "reason": "stale script_executing lease"},
                )
        if _can_retry_after_policy(node_attempt_count, retry_budget, superseded, script_retry_probe_ok):
            _log.warning(
                "pipeline.retry_policy",
                extra={
                    "run_id": str(run_id),
                    "status": final_status,
                    "error_code": error_code,
                    "attempt": node_attempt_count,
                    "budget": retry_budget,
                },
            )
            await self._fenced_pending_reset(run_id=run_id, org_id=org_id)
            # The re-raise below propagates out of execute() BEFORE the
            # post-stream try/finally, so run its cleanup here: clear the
            # cancellation check + hubs and close the run's broker so the
            # retry re-entry gets a fresh broker and no stale contextvars.
            await self._cleanup_run_resources(
                model_backend_hub=model_backend_hub, connector_hub=connector_hub, run_id=run_id
            )
            # FAR-136 Gap 1: jittered, capped backoff before the re-dispatch.
            # Without it a policy-triggered retry re-fires back-to-back,
            # hammering the queue/gateway on a persistent failure. The delay
            # grows with the attempt count and is bounded by the retry
            # budget (the loop above only re-dispatches while
            # node_attempt_count <= retry_budget), so the schedule can never
            # extend beyond max_retries. `_retry_backoff_seconds` is a pure
            # function of the attempt number — covered by unit tests.
            await asyncio.sleep(_retry_backoff_seconds(node_attempt_count))
            raise RunRetryPolicyError(final_status, retry_budget)
        if not script_retry_probe_ok:
            # FAR-296 Phase 2: the lease probe blocked the requeue — a
            # script process may have run with unknown side-effect state.
            # Terminal-fail with ``script.side_effect_unknown`` (never
            # retried) so the run reaches a needs-human state instead of
            # silently looping or being left stuck in ``running``.
            _log.warning(
                "script.lease_probe.terminal_side_effect_unknown",
                extra={"run_id": str(run_id), "error_code": _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN},
            )
            error_detail_value = _sanitize_detail(
                "Script-mode sandbox has an unresolved execution claim (side effect unknown); "
                "not retried — needs human review.",
                limit=5000,
            )
            broker.publish(
                "run_failed", {"error": _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN, "detail": error_detail_value}
            )
            return "side_effect_unknown"
        return "none"

    async def _run_post_stream_tail(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        broker: RunEventBroker,
        gate_suppressed: bool,
    ) -> tuple[str, str | None, str | None]:
        """Post-stream tail: eval-suite checks, agent_signal firing, gated publish.

        Preserves the outer try/except (``pipeline.post_stream_error``) that
        wrapped the original inline block. Returns the (possibly mutated)
        ``(final_status, error_code, error_detail)`` — the eval-suite-blocked
        branch terminal-fails the run, and that must reach ``_finalize_run_after_stream``.
        The caller's ``finally`` owns the resource cleanup.
        """
        try:
            # (The eval_blocked audit for this run is recorded in
            # ``_finalize_run_after_stream`` — the shared finalization tail that
            # both execute() and resume() use, so it fires exactly once.)
            # If the run completed, check for eval suite thresholds. FAR-228: a
            # gated run (error_code harness.idempotency_gate) is excluded — the
            # delivery was already made by a PRIOR attempt; running evals /
            # firing agent_signal against the skip envelope would be wrong.
            if final_status == "complete" and error_code != _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE:
                async with self._session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    await set_rls_execution_context(session)
                    final_status, error_code, error_detail = await self._check_eval_suites_for_run(
                        session=session,
                        run_id=run_id,
                        org_id=org_id,
                        pipeline_id=pipeline_id,
                        final_status=final_status,
                        error_code=error_code,
                        error_detail=error_detail,
                        broker=broker,
                    )
                # Fire agent_signal triggers for each completed node.
                async with self._session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    await set_rls_execution_context(session)
                    await self._fire_agent_signals(
                        session=session,
                        org_id=org_id,
                        run_id=run_id,
                        pipeline_id=pipeline_id,
                        completed_node_outputs=completed_node_outputs,
                    )
            if gate_suppressed:
                # FAR-228: a gated run completed WITHOUT re-executing the node
                # (guard B suppressed the transient retry). Publish run_completed
                # after the eval-skip point and BEFORE the post-stream cleanup
                # closes the broker below — the broker is provably open here
                # (run_failed publishes at the retries-exhausted branch today).
                broker.publish("run_completed", {})
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.post_stream_error", extra={"run_id": str(run_id)})
        return final_status, error_code, error_detail

    async def _run_post_stream_and_teardown(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        broker: RunEventBroker,
        gate_suppressed: bool,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
    ) -> tuple[str, str | None, str | None]:
        """Run the post-stream tail then tear down the run's execution environment.

        The teardown (contextvars, hub teardown, broker registry close) runs in
        a ``finally`` so it fires even when the tail re-raises (e.g. a
        ``CancelledError``), preserving the original inline try/finally
        semantics in ``execute``. Returns the (possibly mutated) triplet from
        the tail.
        """
        try:
            final_status, error_code, error_detail = await self._run_post_stream_tail(
                run_id=run_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                final_status=final_status,
                error_code=error_code,
                error_detail=error_detail,
                completed_node_outputs=completed_node_outputs,
                broker=broker,
                gate_suppressed=gate_suppressed,
            )
        finally:
            # Close broker after all post-stream work (suite checks, signals).
            set_cancellation_check(None)
            set_audit_hook(None)
            set_model_backend_hub(None)
            set_connector_hub(None)
            if model_backend_hub is not None:
                await _teardown_hub(model_backend_hub)
            if connector_hub is not None:
                await _teardown_hub(connector_hub)
            if final_status != "awaiting_human":
                get_registry().close(run_id)
        return final_status, error_code, error_detail

    async def _check_eval_suites_for_run(
        self,
        *,
        session: AsyncSession,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        broker: RunEventBroker,
    ) -> tuple[str, str | None, str | None]:
        """Check eval-suite thresholds for a completed run, terminal-failing when blocked.

        Runs only for completed, non-gated runs (FAR-228: a gated run's
        delivery was already made by a PRIOR attempt; running evals against the
        skip envelope would be wrong). On ``EvalSuiteBlockedError`` the run is
        terminal-failed as ``eval_suite_blocked`` with the audit event recorded
        here. Returns the (possibly mutated) triplet.
        """
        if final_status != "complete" or error_code == _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE:
            return final_status, error_code, error_detail
        try:
            await self._check_eval_suites(session, run_id, pipeline_id)
        except EvalSuiteBlockedError as exc:
            final_status = "failed"
            error_code = "eval_suite_blocked"
            error_detail = _sanitize_detail(exc, limit=5000)
            broker.publish("run_failed", {"error": "eval_suite_blocked", "detail": error_detail})
            _log.warning(
                "eval.suite_blocked",
                extra={
                    "run_id": str(run_id),
                    "suite_id": exc.suite_id,
                    "score": exc.score,
                },
            )
            try:
                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type="eval.suite_blocked",
                    resource_type="run",
                    resource_id=run_id,
                    payload_json={
                        "error_detail": _sanitize_detail(error_detail, limit=5000),
                        "suite_id": exc.suite_id,
                        "score": exc.score,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("audit.eval_suite_blocked_failed", extra={"run_id": str(run_id)})
        return final_status, error_code, error_detail

    async def _fire_agent_signals(
        self,
        *,
        session: AsyncSession,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        completed_node_outputs: dict[str, Any],
    ) -> None:
        """Fire agent_signal triggers for each completed node.

        FAR-228: a node whose output_json carries the idempotency_gate marker
        is a SKIPPED delivery (guard A/B) — it must not re-fire child
        pipelines. Keyed ONLY on the marker, never on status == "skipped"
        (template-error skips fire today).
        """
        for node_id, node_output in completed_node_outputs.items():
            if _node_output_has_idempotency_gate(node_output):
                continue
            await self._fire_agent_signal_for_node(
                session=session,
                org_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                node_output=node_output,
            )

    async def _fire_agent_signal_for_node(
        self,
        *,
        session: AsyncSession,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_id: str,
        node_output: Any,
    ) -> None:
        """Fire one node's agent_signal triggers, failure-isolated + logged."""
        try:
            signal_results = await fire_agent_signal(
                session,
                org_id=org_id,
                source_run_id=run_id,
                source_pipeline_id=pipeline_id,
                completed_node_id=node_id,
                node_output=node_output,
            )
            for sr in signal_results:
                _log.info(
                    "agent_signal.%s trigger=%s run=%s",
                    sr["status"],
                    sr.get("trigger_id", "?"),
                    sr.get("run_id", "?"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "agent_signal.failed",
                extra={
                    "run_id": str(run_id),
                    "node_id": node_id,
                },
            )

    async def _check_eval_suites(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> list[SuiteEvalResult]:
        """Check all eval suites with pass_threshold for a completed run.

        Queries eval definitions for the pipeline that belong to a suite
        with a pass_threshold, aggregates their results, and returns
        SuiteEvalResult for each suite.
        """
        stmt = select(EvalDefinition).where(
            EvalDefinition.pipeline_id == pipeline_id,
            EvalDefinition.suite_id.isnot(None),
            EvalDefinition.pass_threshold.isnot(None),
            EvalDefinition.eval_type != "guardrail",
        )
        result = await session.execute(stmt)
        suite_defs = result.scalars().all()

        if not suite_defs:
            return []

        suite_ids = list({d.suite_id for d in suite_defs if d.suite_id})
        results: list[SuiteEvalResult] = []
        for suite_id in suite_ids:
            eval_stmt = select(EvalDefinition).where(
                EvalDefinition.suite_id == suite_id,
                EvalDefinition.pipeline_id == pipeline_id,
            )
            eval_result = await session.execute(eval_stmt)
            defs_in_suite = eval_result.scalars().all()
            if not defs_in_suite:
                continue
            eval_ids = [d.id for d in defs_in_suite]

            result_stmt = select(EvalResult).where(
                EvalResult.run_id == run_id,
                EvalResult.eval_id.in_(eval_ids),
            )
            result_result = await session.execute(result_stmt)
            eval_results = result_result.scalars().all()

            threshold_raw = next(
                (d.pass_threshold for d in defs_in_suite if d.pass_threshold is not None),
                None,
            )
            threshold = float(threshold_raw) if threshold_raw is not None else None

            suite_result_raw = evaluate_suite(
                eval_results=[
                    EngineEvalResult(
                        id=r.id,
                        run_id=r.run_id,
                        node_id=str(r.node_id) if r.node_id else "",
                        eval_id=r.eval_id,
                        passed=r.passed,
                        score=r.score,
                        detail=r.detail or "",
                        evaluated_at=r.evaluated_at,
                    )
                    for r in eval_results
                ],
                suite_id=suite_id,
                pass_threshold=threshold,
            )

            suite_result = SuiteEvalResult(
                suite_id=suite_id,
                total_evals=len(eval_results),
                passed_evals=sum(1 for r in eval_results if r.passed),
                aggregate_score=suite_result_raw.aggregate_score,
                passed=suite_result_raw.passed,
                blocking_failures=suite_result_raw.blocking_failures,
            )
            if threshold is not None and not suite_result.passed:
                raise EvalSuiteBlockedError(suite_id, suite_result.aggregate_score, threshold)
            results.append(suite_result)

        return results

    async def _handle_graph_interrupt(
        self,
        interrupts: Any,
        state: _StreamState,
        ctx: _StreamContext,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Create the HITL gate and publish the awaiting event for an interrupt.

        PR A signature change (§4.2): the handler ACCEPTS the accumulated
        ``node_token_usage`` / ``completed_node_outputs`` (via ``state`` /
        ``ctx``, which live in ``_stream_graph``'s scope) and RETURNS them in
        the ``awaiting_human`` 4-tuple, so the pause persists the CURRENT
        segment's sets MERGED into the stored cumulative set — NOT ``None``,
        NOT segment-only. The empty-accumulator case (``{}`` → ``None``)
        normalizes so ``finalize_cost``'s merge leaves the stored set untouched.
        """
        first_interrupt = interrupts[0] if interrupts else None
        value = getattr(first_interrupt, "value", None)
        gate_payload = value if isinstance(value, dict) else {}
        gate_id = gate_payload.get("gate_id", "")
        required_team_id_str = gate_payload.get("required_team_id")
        required_team_id = uuid.UUID(required_team_id_str) if required_team_id_str else None
        node_token_usage = state.node_token_usage
        broker = ctx.broker
        run_id = ctx.run_id
        pipeline_id = ctx.pipeline_id
        org_id = ctx.org_id

        if pipeline_id is not None and org_id is not None:
            mgr = HITLManager()
            pipeline_name: str | None = None
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                await mgr.create_gate(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    required_team_id=required_team_id,
                )
                try:
                    pipeline = await get_pipeline(session, pipeline_id)
                    pipeline_name = pipeline.name if pipeline is not None else None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning(
                        "hitl_gate.pipeline_name_lookup_failed",
                        extra={"pipeline_id": str(pipeline_id), "org_id": str(org_id)},
                    )
            broker.publish(
                "hitl_awaiting",
                {
                    "gate_payload": gate_payload,
                    "team_id": str(required_team_id) if required_team_id else None,
                },
            )
            await self._dispatch_hitl_awaiting(
                org_id=org_id,
                run_id=run_id,
                gate_id=gate_id,
                pipeline_name=pipeline_name,
                team_id=required_team_id,
            )
            return "awaiting_human", None, None, node_token_usage or None

        _log.warning(
            "hitl_gate.cannot_create",
            extra={"run_id": str(run_id), "pipeline_id": str(pipeline_id), "org_id": str(org_id)},
        )
        broker.publish("run_failed", {"error": "gate_creation_failed", "detail": "Pipeline or org ID is None"})
        return (
            "failed",
            "configuration_error",
            "Missing pipeline_id or org_id for HITL gate creation",
            node_token_usage or None,
        )

    async def _dispatch_hitl_awaiting(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        gate_id: str,
        pipeline_name: str | None,
        team_id: uuid.UUID | None,
    ) -> None:
        """Dispatch the ``hitl_awaiting`` webhook/in-app notification.

        Closes the team-hitl-gates Known Gap (PRD §8.8 flow step 2): previously
        the run lifecycle only emitted the WebSocket broker ``hitl_awaiting``
        event — the HMAC-signed webhook / in-app notification path was never
        triggered from the executor. Routed through the injected Notifier with
        ``team_id`` so team-scoped gates reach team notification endpoints
        first (falling back to org-wide). Failure-isolated: a broken notifier
        or dispatch is logged and never blocks the run pause.
        """
        if self._notifier is None:
            return
        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "gate_id": gate_id,
            "team_id": str(team_id) if team_id else None,
        }
        if pipeline_name is not None:
            payload["pipeline_name"] = pipeline_name
        try:
            await self._notifier.dispatch_event(
                org_id=org_id,
                event_type=EVENT_HITL_AWAITING,
                payload=payload,
                run_id=run_id,
                team_id=team_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "hitl_gate.awaiting_notification_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id), "gate_id": gate_id},
            )

    async def _handle_chain_end_event(
        self,
        *,
        state: _StreamState,
        ctx: _StreamContext,
        lg_event: Any,
    ) -> None:
        """Handle one ``on_chain_end`` event: capture + stamp the completed
        node's output and latch its stall / agent-failure / session-lost markers.

        Extracted from ``_stream_graph``'s event loop (pure refactor) so the
        loop body stays small and single-responsibility. Also fires the
        FAR-305 standalone post-node eval path against the node's contract
        output. ``ctx.completed_node_outputs`` / ``state`` are mutated in place.
        """
        name = lg_event.get("name", "")
        if name not in ctx.node_ids:
            return
        state.segments_completed += 1
        if ctx.guard is not None:
            ctx.guard.record_step()
        output = _record_chain_end_output(
            ctx.completed_node_outputs,
            name,
            lg_event.get("data", {}),
            ctx.run_trace_id,
            self._otel_bridge,
            lg_event.get("run_id"),
        )
        if output is None:
            return
        # FAR-305: standalone post-node eval path — run any node-scoped evals
        # for this node against its inner output dict. This is independent of
        # HITL gates: a plain node (no gate) now gets its node-scoped evals
        # evaluated too. If a ``block`` eval fails, ``EvalBlockedError``
        # propagates and the existing ``except EvalBlockedError`` below
        # transitions the run to ``eval_failed`` with ``error_code="eval_blocked"``.
        # If this node ALSO feeds a HITL gate with eval-before-interrupt,
        # the evals run twice (once here post-node, once in the gate) —
        # acceptable for now, the gate's eval is a separate node.
        if ctx.eval_definitions_by_node:
            await self._run_post_node_evals(
                name,
                output,
                ctx.eval_definitions_by_node,
                ctx.run_id,
                ctx.org_id,
                node_type_map=ctx.node_type_map,
            )
        stall_reason, agent_failure, session_lost = _record_node_markers(
            output,
            ctx.broker,
            name,
        )
        if stall_reason:
            state.stall_reason = stall_reason
        if agent_failure:
            state.agent_failure_reason = agent_failure
        if session_lost:
            state.session_lost_reason = session_lost

    async def _handle_stream_event(
        self,
        state: _StreamState,
        ctx: _StreamContext,
        lg_event: Any,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
        """Handle one ``astream_events`` event; returns a terminal 4-tuple or None.

        Extracted from ``_stream_graph``'s event loop (pure refactor). Maps a
        HITL interrupt to the awaiting_human 4-tuple, publishes the node
        lifecycle event (with first-node zombie-watchdog signalling), fires the
        chain-end capture handler, and accumulates token usage. Returns None
        when the loop must keep streaming.
        """
        interrupts = _streamed_interrupts(lg_event)
        if interrupts:
            return await self._handle_graph_interrupt(interrupts, state, ctx)
        mapped = _map_lg_event(lg_event, ctx.node_ids)
        if mapped is not None:
            event_type, payload = mapped
            # Zombie-run protection: the first real node dispatch is the
            # signal that pre-node setup finished — stands down the
            # execute_run watchdog (pipeline_execution.zombie_watchdog).
            if not state.first_node_signalled:
                state.first_node_signalled = True
                if self.on_first_progress is not None:
                    self.on_first_progress()
            # FAR-369 absolute node-deadline watchdog: signal per-node
            # start/completion (by node_id) so the deadline watchdog can
            # hold each node to its own timeout_seconds independent of
            # idle/activity. ``on_node_started`` runs for BOTH the first
            # and every later node (unlike on_first_progress).
            if event_type == "node_started" and self.on_node_started is not None:
                self.on_node_started(str(payload.get("node_id", "")))
            elif event_type == "node_completed" and self.on_node_completed is not None:
                self.on_node_completed(str(payload.get("node_id", "")))
            ctx.broker.publish(event_type, payload)
        event_kind = lg_event.get("event", "")
        # Capture node output for agent_signal trigger firing.
        if event_kind == "on_chain_end":
            await self._handle_chain_end_event(state=state, ctx=ctx, lg_event=lg_event)
        if event_kind == "on_chat_model_end":
            _accumulate_chat_model_tokens(lg_event, state.node_token_usage, ctx.guard, ctx.node_token_budgets)
        elif event_kind == "on_llm_end":
            _accumulate_llm_tokens(lg_event, state.node_token_usage, ctx.guard, ctx.node_token_budgets)
        return None

    async def _stream_exception_outcome(
        self,
        exc: BaseException,
        *,
        state: _StreamState,
        ctx: _StreamContext,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Map a stream exception to a terminal 4-tuple, or re-raise it.

        Extracted from ``_stream_graph``'s exception chain (pure refactor) so
        the stream method keeps a single ``except BaseException`` clause. Every
        mapped outcome is identical to the original chain; cancellation and
        transient node failures are re-raised unchanged.
        """
        broker = ctx.broker
        run_id = ctx.run_id
        node_token_usage = state.node_token_usage or None
        if isinstance(exc, GraphInterrupt):
            interrupts = exc.args[0] if exc.args else []
            return await self._handle_graph_interrupt(interrupts, state, ctx)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, (NodeCancelledError, SandboxNodeFailedError)):
            # Transient node cancellation / sandbox-infra failure (langgraph
            # wraps a node body's asyncio.CancelledError; a stall or command
            # timeout raises SandboxNodeFailedError). Do NOT swallow into a
            # terminal failure tuple here — propagate so execute() can decide:
            # retry (fenced reset to pending + re-raise) or terminal-fail once
            # retries are exhausted.
            raise
        if isinstance(exc, EvalBlockedError):
            return _terminal_failure(
                broker,
                "eval_failed",
                "eval_blocked",
                _sanitize_detail(exc, limit=5000),
                node_token_usage,
            )
        if isinstance(exc, OutputRejectedError):
            # C4: ``output_rejected`` violates the ``ck_runs_status`` CHECK
            # constraint as a STATUS — it is an error CODE on a ``failed`` run.
            return _terminal_failure(
                broker,
                "failed",
                "output_rejected",
                _sanitize_detail(exc, limit=5000),
                node_token_usage,
            )
        if isinstance(exc, RunCancelledError):
            broker.publish("run_cancelled", {})
            self._log_accumulation_state(run_id, state.segments_completed, state.node_token_usage)
            return "cancelled", None, None, node_token_usage
        if isinstance(exc, RunawayRunError):
            error_detail = _sanitize_detail(exc, limit=5000)
            _log.warning(
                "runaway.terminated",
                extra={
                    "run_id": str(run_id),
                    "guard": exc.guard,
                    "current": exc.current,
                    "limit": exc.limit,
                },
            )
            return _terminal_failure(broker, "failed", "runaway", error_detail, node_token_usage)
        if isinstance(exc, TimeoutError):
            error_detail = _sanitize_detail(exc, limit=5000)
            _log.warning(
                _ERROR_CODE_NODE_TIMEOUT,
                extra={"run_id": str(run_id), "detail": error_detail},
            )
            return _terminal_failure(broker, "failed", "node_timeout", error_detail, node_token_usage)
        if isinstance(exc, SupersededNodeError):
            # A6: the sandbox dispatch marker was denied — a superseded claim
            # or a run no longer running. Terminal ``superseded`` failure; the
            # token-guarded finalize write is a no-op if a successor already
            # owns the run. NEVER a completed run with zero work.
            scrubbed = _sanitize_detail(exc, limit=5000)
            _log.warning(
                "pipeline.node_superseded",
                extra={"run_id": str(run_id), "detail": scrubbed[:500]},
            )
            return _terminal_failure(
                broker,
                "failed",
                "executor_superseded",
                scrubbed,
                node_token_usage,
            )
        if isinstance(exc, OutputSchemaValidationError):
            # Manual-node resume output (or agent output) failed validation
            # against output_schema_json. Domain-specific error code per §8.9 —
            # never a raw ``ValueError``.
            scrubbed = _sanitize_detail(exc, limit=5000)
            _log.warning(
                "pipeline.output_schema_validation_failed",
                extra={"run_id": str(run_id), "detail": scrubbed[:500]},
            )
            return _terminal_failure(
                broker,
                "failed",
                "schema_validation_failure",
                scrubbed,
                node_token_usage,
            )
        _tb = _traceback_detail(exc, limit=5000)
        return _terminal_failure(
            broker,
            "failed",
            type(exc).__name__,
            _tb,
            state.node_token_usage or None,
        )

    async def _stream_graph(
        self,
        compiled: Any,
        initial_state: dict[str, Any] | None,
        config: dict[str, Any],
        node_ids: set[str],
        broker: RunEventBroker,
        run_id: uuid.UUID,
        *,
        pipeline_id: uuid.UUID | None = None,
        org_id: uuid.UUID | None = None,
        completed_node_outputs: dict[str, Any] | None = None,
        guard: RunawayGuard | None = None,
        node_token_budgets: dict[str, int] | None = None,
        eval_definitions_by_node: dict[str, list[EvalDefDTO]] | None = None,
        node_type_map: dict[str, str] | None = None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Stream graph execution, mapping events to broker publishes.

        If *completed_node_outputs* is provided (a mutable dict), it will be
        populated with ``{node_id: output_data}`` for each completed node.

        If *guard* is provided, runaway run protection checks are enforced
        before each event and on node completion / token usage.

        If *node_token_budgets* is provided (``{node_id: token_budget}``),
        per-node token budgets are enforced after each LLM call — if a node's
        cumulative tokens exceed its budget a ``RunawayRunError`` is raised.

        Returns (final_status, error_code, error_detail, node_token_usage).
        """
        state = _StreamState()
        ctx = _StreamContext(
            node_ids=node_ids,
            guard=guard,
            completed_node_outputs=completed_node_outputs,
            run_trace_id=None,
            broker=broker,
            run_id=run_id,
            pipeline_id=pipeline_id,
            org_id=org_id,
            node_token_budgets=node_token_budgets,
            eval_definitions_by_node=eval_definitions_by_node,
            node_type_map=node_type_map,
        )
        lg_config = {**config, "callbacks": [self._otel_bridge]}
        # FAR-198: seed the OTel context with the run's deterministic trace id
        # so every span exported during graph execution carries the SAME
        # trace_id the API reports on RunResponse. The root span is stored on
        # the bridge (its spans inherit it) AND attached to the current
        # context (spans created outside the bridge inherit it too). Both are
        # cleaned up in the finally block below.
        run_trace_id, run_root_span = _compute_otel_run_context(
            config,
            self._otel_bridge,
        )
        ctx.run_trace_id = run_trace_id
        run_root_token = context_api.attach(set_span_in_context(run_root_span)) if run_root_span is not None else None
        try:
            async for lg_event in compiled.astream_events(initial_state, lg_config, version="v2"):
                if ctx.guard is not None:
                    ctx.guard.check_duration()
                outcome = await self._handle_stream_event(state, ctx, lg_event)
                if outcome is not None:
                    return outcome

            terminal = _stream_terminal_reason(state, broker, run_id)
            if terminal is not None:
                return terminal
            broker.publish("run_completed", {})
            return "complete", None, None, state.node_token_usage or None
        except Exception as exc:
            # CancelledError / NodeCancelledError / SandboxNodeFailedError
            # propagate via _stream_exception_outcome's re-raise branches so
            # execute()'s retry machinery still decides.
            return await self._stream_exception_outcome(exc, state=state, ctx=ctx)
        finally:
            # FAR-198: tear down the seeded OTel context + run root span on
            # every exit path (returns, exceptions, cancellation).
            if run_root_token is not None:
                context_api.detach(run_root_token)
            self._otel_bridge.end_run_root()
