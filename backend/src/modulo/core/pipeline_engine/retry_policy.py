"""Run-level retry policy decision logic (FAR-136 / FAR-2076 / FAR-525 / FAR-2006).

This module contains the PURE decision functions for the pipeline-level
``retry_policy`` — whether a terminal run outcome qualifies for a re-dispatch,
what the retry budget ceiling is, and the jittered/backoff schedule between
re-dispatches. It is DB-free, ORM-free, and executor-free so it can be
imported by the executor, the graph validator, the watchdog, and the
dispatcher_reconcile backstop without creating import cycles.

The EXECUTOR-SPECIFIC orchestration (fenced pending-reset, hub cleanup,
SAQ re-raise) stays in ``executor.py`` and CALLS these pure functions.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from langgraph.errors import NodeCancelledError
from opentelemetry import metrics as _otel_metrics

from modulo.core.pipeline_engine.error_codes import (
    _CODE_SANDBOX_NO_OUTPUT_JSON,
    ERROR_CODE_REGISTRY,
    LEGACY_ALIASES,
    map_legacy_code,
)
from modulo.core.pipeline_engine.model_backend_errors import NON_RETRYABLE_PROVIDER_CODES
from modulo.core.pipeline_engine.retry_compensation import (
    RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS,
    RETRY_SCHEDULE_DEFAULT_MULTIPLIER,
    resolve_backoff_schedule,
    sanitize_retry_policy_snippet,
)
from modulo.core.retry_budget import (
    retry_max_executions,
    retry_policy_max_retries,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry policy event vocabulary (must stay in sync with the API schema and graph validator)
# ---------------------------------------------------------------------------
# A policy can retry on:
#   - "stall":    run ended "stalled" / error_code "executor_stalled" / "agent.stall"
#   - "timeout":  error_code "node_timeout" / "TimeoutError" / "node_deadline_exceeded"
#                 / "node.deadline_exceeded" / "node.runaway"
#   - "failure":  any other "failed" terminal status (excluding sandbox-agent
#                 hang deaths — error_code "node_cancelled" + "likely hung")
#   - "eval_failed": run terminalized "eval_failed" / error_code "eval_blocked"
#                 / "eval.blocked" — safe to re-dispatch because the FAR-228
#                 idempotency gate (guard A) skips an already-delivered node.
_RETRY_POLICY_EVENTS = frozenset({"stall", "timeout", "failure", "eval_failed"})

# ---------------------------------------------------------------------------
# Backoff schedule for a retry_policy re-dispatch (FAR-136 / FAR-525).
# ---------------------------------------------------------------------------
# A policy-triggered retry must NOT re-fire back-to-back — the run is
# re-dispatched only after a jittered, capped exponential delay. ``base`` is
# the first-attempt wait; the delay grows per attempt and is capped at ``cap``.
# Jitter spreads re-dispatch across the fleet (a herd of failing pipelines
# must not all re-fire together).
# FAR-525: the DEFAULT base/multiplier are SINGLE-SOURCED to
# retry_compensation.RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS/_MULTIPLIER —
# the executor-local aliases below exist only as named references and must
# never drift from the rc constants (pinned by
# test_retry_backoff_defaults_single_sourced_from_retry_compensation).
_RETRY_BACKOFF_BASE_SECONDS = float(RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS)
_RETRY_BACKOFF_CAP_SECONDS = 300.0
# Jitter range as a fraction of the current schedule value; uniform in
# [0, fraction * delay] so the schedule keeps its exponential shape while
# still decorrelating concurrent retries. Capped against ``_RETRY_BACKOFF_CAP_SECONDS``.
_RETRY_BACKOFF_JITTER_FRACTION = 0.25
# FAR-525: the growth factor of the in-job sleep schedule — single-sourced to
# the resolver's default (2.0 matches the pre-FAR-525 hardcoded doubling so
# "present with defaults" == "absent"); a run-level ``backoff_schedule`` may
# override it in [1.0, 10.0] (fixed delay at 1.0).
_RETRY_BACKOFF_DEFAULT_MULTIPLIER = float(RETRY_SCHEDULE_DEFAULT_MULTIPLIER)

# ---------------------------------------------------------------------------
# Canonical dotted error codes written at terminalization and matched by the
# retry policy. Constants are pure aliases so the retry policy, the failure
# write, and log names cannot drift.
# ---------------------------------------------------------------------------
_ERROR_CODE_AGENT_FAILED = "agent.failed"
_ERROR_CODE_NODE_TIMEOUT = "node.timeout"
_ERROR_CODE_NODE_DEADLINE_EXCEEDED = "node.deadline_exceeded"
_ERROR_CODE_EVAL_BLOCKED = "eval.blocked"
_ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN = "script.side_effect_unknown"
_ERROR_CODE_HARNESS_IDEMPOTENCY_GATE = "harness.idempotency_gate"
# FAR-592 (D6 F2): the Local-tier refusal is TERMINAL (deterministic config
# fault) — written directly by the run-level handler before the transient
# machinery so the refusal is never requeued.
_ERROR_CODE_SANDBOX_TIER_REFUSED = "sandbox.tier_refused"

# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# FAR-1800 / FAR-2006: provider BILLING faults are non-retryable by nature — the
# provider account is out of funds (HTTP 402) or the model is disabled for the
# key, so every retry re-hits the same wall and only burns a sandbox dispatch.
# Excluded from ``failure`` policy retries exactly like the script-mode terminal
# codes.
#
# DELIBERATELY NARROW: this is NOT "every registry-non-retryable code". The
# registry also marks ``agent.stall`` and ``agent.failed`` non-retryable, yet a
# run-level ``failure``/``stall`` policy is allowed to retry those (the operator
# explicitly asked for it, and a fixed prompt/backend can recover). Only the
# billing/provisioning refusal class is refused outright.
_NEVER_RETRYABLE_PROVIDER_CODES: frozenset[str] = frozenset(
    {
        "model.insufficient_funds",
        "model_disabled",
    }
)


# ---------------------------------------------------------------------------
# RunRetryPolicyError — subclasses NodeCancelledError so the caller
# (run_executor_with_watchdog) treats the re-raise as a transient retry — the
# SAQ job is re-dispatched and the run (already reset to ``pending``) is
# claimed and executed again.
# ---------------------------------------------------------------------------


class RunRetryPolicyError(NodeCancelledError):
    """A run ended in a state the pipeline's ``retry_policy`` says to retry."""

    def __init__(self, status: str, max_retries: int) -> None:
        super().__init__(node="retry_policy", message=f"retry_policy: status={status!r}, budget={max_retries}")
        self.status = status
        self.max_retries = max_retries


# ---------------------------------------------------------------------------
# Pure decision function: does a terminal outcome match the retry policy?
# ---------------------------------------------------------------------------
def _stall_event_matches(event_set: set[Any], final_status: str, code: str, mapped: str) -> bool:
    """``stall`` event matches a stalled outcome or an agent.stall code."""
    return "stall" in event_set and (final_status == "stalled" or code == "executor_stalled" or mapped == "agent.stall")


def _timeout_event_matches(event_set: set[Any], code: str, mapped: str) -> bool:
    """``timeout`` event matches a node.timeout / node.runaway / node.deadline_exceeded code.

    FAR-369: the absolute node-deadline watchdog terminalizes with
    ``node_deadline_exceeded`` (raw) / ``node.deadline_exceeded`` (dotted) —
    both resolve to ``node.deadline_exceeded`` through ``map_legacy_code``, so a
    ``{on: ["timeout"]}`` policy re-dispatches a deadline death exactly like a
    per-node timeout.
    """
    return "timeout" in event_set and (
        code in ("node_timeout", "TimeoutError")
        or mapped in (_ERROR_CODE_NODE_TIMEOUT, "node.runaway", _ERROR_CODE_NODE_DEADLINE_EXCEEDED)
    )


def _eval_failed_event_matches(event_set: set[Any], final_status: str, code: str, mapped: str) -> bool:
    """``eval_failed`` event matches a guardrail/eval-blocked outcome.

    The executor terminalizes an ``EvalBlockedError`` as final_status
    ``"eval_failed"`` with raw error_code ``"eval_blocked"`` (which maps to the
    canonical ``eval.blocked``), so all three spellings match the event.
    """
    return "eval_failed" in event_set and (
        final_status == "eval_failed" or code == "eval_blocked" or mapped == _ERROR_CODE_EVAL_BLOCKED
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
    hung" in ``error_detail`` - re-dispatching would burn a full node timeout
    with zero recovery probability, so it is excluded from ``"failure"`` retries.
    """
    if (
        "failure" not in event_set
        or final_status != "failed"
        # Timeout is a distinct event - a "failure"-only policy must not retry
        # a timeout outcome, and a stall is not a generic failure. The
        # absolute node-deadline watchdog code resolves to the timeout event
        # too (map_legacy_code: node_deadline_exceeded -> node.deadline_exceeded),
        # so both spellings are excluded the same way.
        or code in ("node_timeout", "TimeoutError", "executor_stalled", "node_deadline_exceeded")
        or mapped in (_ERROR_CODE_NODE_TIMEOUT, "node.runaway", "agent.stall", _ERROR_CODE_NODE_DEADLINE_EXCEEDED)
    ):
        return False
    # FAR-296 Phase 2: never-retryable script-mode terminal codes are excluded
    # from ``failure`` retries regardless of the policy — re-dispatching a
    # script-mode node whose process started could double-execute a side effect.
    # This covers both the canonical mapped code and the raw exception-class
    # spelling the generic catch publishes.
    if mapped in _NEVER_RETRYABLE_SCRIPT_CODES or code in _NEVER_RETRYABLE_SCRIPT_RAW:
        return False
    # FAR-2006: a provider credential / funding refusal (401 / 402) is
    # permanent. A ``failure`` policy must never re-dispatch it — three
    # attempts on a 402 is pure waste (~10 min each) and a whole night ran at
    # zero throughput that way. The shared constant is the same set the two
    # retry paths use, so this exclusion cannot drift from them.
    # FAR-1800: main's narrower billing/provisioning set (``model_disabled``)
    # is kept alongside it, and matched on the raw code spelling too.
    if (
        mapped in NON_RETRYABLE_PROVIDER_CODES
        or mapped in _NEVER_RETRYABLE_PROVIDER_CODES
        or code in _NEVER_RETRYABLE_PROVIDER_CODES
    ):
        return False
    # FAR-136 Gap 2: exclude sandbox-agent hang deaths.
    is_cancel = code == "node_cancelled" or mapped == "node.cancelled"
    return not (is_cancel and error_detail and "likely hung" in error_detail)


def retry_after_policy(
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
                        / ``node.deadline_exceeded`` (legacy ``node_timeout`` /
                        ``TimeoutError`` included; the FAR-369 absolute
                        node-deadline watchdog code ``node_deadline_exceeded``
                        and its dotted registry spelling both resolve to
                        ``node.deadline_exceeded``)
      - ``"failure"``:  ``final_status == "failed"`` and not a stall/timeout outcome
      - ``"eval_failed"``: ``final_status == "eval_failed"`` or the code resolves
                        to ``eval.blocked`` (legacy ``eval_blocked`` included).
                        Re-dispatch is safe for delivery nodes: the FAR-228
                        idempotency gate (guard A) returns the skipped envelope
                        when a delivery_done-marked node re-executes.

    Codes are matched BOTH literally (legacy codes stay backward compatible) and
    through the shared ``map_legacy_code`` alias table, so dotted registry codes
    (e.g. an ``agent.failed`` A1 elevation) and legacy codes behave identically.

    ``error_detail`` (optional) refines the ``"failure"`` match: a sandbox-agent
    HANG death terminalizes as ``error_code="node_cancelled"`` with
    ``error_detail`` containing ``"likely hung"`` (node_runner). Re-dispatching
    a hang would burn a full node timeout with zero recovery probability, so it
    is excluded from ``"failure"`` retries — while TRANSIENT ``node_cancelled``
    (no hang marker) stays retryable via the existing NodeCancelledError path.

    Watchdog kills reach this decision too (FAR-690 / FAR-693): the FAR-369
    absolute node-deadline watchdog consults this function — via the shared
    ``pipeline_engine.watchdog_retry`` module — with final_status ``failed``
    and the raw ``node_deadline_exceeded`` code before terminal-failing, and
    the executor-level zombie-watchdog consults it with final_status
    ``stalled`` and ``executor_stalled``. When the budget allows, both
    re-dispatch the run through the SAME mechanism the in-execute path uses
    (fenced pending-reset + backoff + ``RunRetryPolicyError`` re-raise → SAQ
    job retry); with no coverage or an exhausted budget they keep the
    unconditional terminal fail. The ``"timeout"`` / ``"stall"`` aliases above
    therefore cover both the in-execute outcome spellings AND the raw watchdog
    codes (both resolve through ``map_legacy_code``).

    Nodeless zombie deaths also consult this matcher (FAR-733): the
    dispatcher_reconcile cron path's ``_should_redispatch_nodeless`` calls
    this function with ``final_status="stalled"`` and
    ``error_code="executor_stalled"`` so that a stall-covered policy
    re-dispatches nodeless zombies on the SAME cycle the watchdog would allow.
    FAR-2076: the attempt count on every path is the durable
    ``runs.execution_attempt_count`` (via
    ``modulo.core.retry_budget.retry_executions_consumed``) — never
    ``claim_count``, which grows on capacity-demoted and resume claims.

    FAR-649: an ABSENT ``on`` key (key missing or explicitly ``null``) with a
    valid ``max_retries`` > 0 means ALL retryable events — every matcher below
    evaluates (the intuitive default: enabling a budget opts the pipeline into
    all four retryable outcomes). An explicit non-empty ``on`` list stays
    granular (unchanged); an explicit EMPTY list (``on: []``) stays "no retry"
    (backward compatible — the FAR-525 GUI's inert no-op panel save shape).
    The budget validation runs BEFORE the event-shape branch so an all-events
    policy still fail-closes on a malformed budget.

    An absent/malformed policy or a 0 budget yields None (no retry) — the
    current behaviour is unchanged for pipelines without a policy.
    """
    if not isinstance(policy, dict):
        return None
    max_retries = retry_policy_max_retries(policy)
    if max_retries is None or max_retries == 0:
        return None
    events = policy.get("on")
    if events is None:
        # FAR-649: absent (key missing) or explicitly-null `on` with a valid
        # budget > 0 = ALL retryable events — evaluate every matcher below.
        event_set: set[Any] = set(_RETRY_POLICY_EVENTS)
    elif isinstance(events, list) and events:
        event_set = set(events)
    else:
        # Explicit ``on: []`` (or a malformed non-list value) = no retry,
        # fail-closed — unchanged.
        return None
    code = error_code or ""
    mapped = map_legacy_code(code) if code else ""
    if _stall_event_matches(event_set, final_status, code, mapped):
        return max_retries
    if _timeout_event_matches(event_set, code, mapped):
        return max_retries
    if _eval_failed_event_matches(event_set, final_status, code, mapped):
        return max_retries
    if _failure_event_matches(event_set, final_status, code, mapped, error_detail):
        return max_retries
    return None


# ---------------------------------------------------------------------------
# Retry policy applicability guards (FAR-295 / FAR-210 / FAR-2025)
# ---------------------------------------------------------------------------
def retry_policy_applies(
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


def can_retry_after_policy(
    execution_attempt_count: int,
    retry_budget: int,
    superseded: bool,
    script_retry_probe_ok: bool,
    *,
    attempt_consumed_budget: bool = False,
) -> bool:
    """Can the retry_policy re-dispatch this run (FAR-136 / FAR-296 Phase 2)?

    ``attempt_consumed_budget`` (FAR-2025) refuses the retry when the attempt
    that just finished already spent the graph's whole node-budget allowance —
    an over-budget task, whose retry is a guaranteed repeat. False by default
    so every caller that has not been taught the guard keeps today's behaviour.
    """
    return (
        execution_attempt_count <= retry_budget
        and not superseded
        and script_retry_probe_ok
        and not attempt_consumed_budget
    )


# ---------------------------------------------------------------------------
# Jittered, capped exponential backoff delay for a retry_policy retry (FAR-136 / FAR-525).
# ---------------------------------------------------------------------------
def retry_backoff_seconds(
    attempt_n: int,
    *,
    base: float = RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS,
    cap: float = _RETRY_BACKOFF_CAP_SECONDS,
    jitter_fraction: float = _RETRY_BACKOFF_JITTER_FRACTION,
    multiplier: float = RETRY_SCHEDULE_DEFAULT_MULTIPLIER,
) -> float:
    """Jittered, capped exponential backoff delay for a retry_policy retry.

    ``attempt_n`` is the 1-based node execution attempt count (attempt 1 = the
    first real execution). The deterministic schedule is
    ``min(base * multiplier ** (attempt_n - 1), cap)`` and a uniform jitter term in
    ``[0, jitter_fraction * delay]`` is added (clamped so the total never
    exceeds ``cap``). A 0/negative ``attempt_n`` is clamped to 1.
    ``base``/``multiplier`` (FAR-525) default to the SINGLE-SOURCED
    ``retry_compensation.RETRY_SCHEDULE_DEFAULT_DELAY_SECONDS`` /
    ``RETRY_SCHEDULE_DEFAULT_MULTIPLIER`` (45.0 / 2.0) so "present with
    defaults" is behaviourally identical to "absent" — the pre-FAR-525
    exponential schedule.

    The schedule is bounded by the retry budget at the decision site: the
    executor only re-dispatches while ``execution_attempt_count <=
    max_retries``, so the last scheduled attempt (attempt == budget) cannot
    extend beyond the policy's ``max_retries``. This is a pure function of the
    attempt number — unit-testable without touching the async retry path.
    """
    n = max(int(attempt_n), 1)
    exponential: float = min(float(base) * float(multiplier) ** (n - 1), cap)
    # Jitter is NOT crypto — it only decorrelates concurrent retries (S311).
    jitter = float(random.uniform(0.0, exponential * max(jitter_fraction, 0.0)))  # noqa: S311  # nosec B311 — non-cryptographic retry jitter only
    return min(exponential + jitter, cap)


# ---------------------------------------------------------------------------
# FAR-525 observability: retry re-dispatch counter
# ---------------------------------------------------------------------------
# Inline-lazy OTel counter (house pattern: classify._record_classification_failure)
# with a `_get_meter`-style indirection seam (rest_metrics pattern) so tests can
# inject a fake meter. Never raises — a metrics failure must never prevent the
# re-raise that re-dispatches the run.

_RETRY_REDISPATCH_COUNTER_NAME = "runs_retry_redispatch_total"


def _get_otel_meter() -> Any:
    """Indirection seam for tests: returns the ``modulo.pipeline_engine`` meter.

    No-ops (returns None) without a meter provider; never raises.
    """
    try:
        provider = _otel_metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.pipeline_engine", version="0.1.0")
    except Exception:
        return None


def _retry_delay_bucket(delay_seconds: float) -> str:
    """Bucket a resolved post-cap post-jitter sleep for counter cardinality."""
    if delay_seconds < 1.0:
        return "<1s"
    if delay_seconds < 10.0:
        return "1-10s"
    if delay_seconds < 60.0:
        return "10-60s"
    if delay_seconds < 300.0:
        return "60-300s"
    return "300s+"


def record_retry_redispatch(*, reason: str, schedule_state: str, delay_seconds: float) -> None:
    """Best-effort counter increment after a CONFIRMED fenced pending-reset.

    Attributes: ``reason`` (the terminal event that triggered the retry),
    ``schedule_state`` ("absent" | "valid" | "failopen"), and ``delay_bucket``
    (coarse bucket of the effective post-cap post-jitter sleep). Emission is
    wrapped no-throw with a warning log (a metrics failure must never prevent
    the re-raise).
    """
    try:
        meter = _get_otel_meter()
        if meter is None:
            return
        counter = meter.create_counter(
            name=_RETRY_REDISPATCH_COUNTER_NAME,
            description="Run-level retry_policy re-dispatches, by reason/schedule-state/delay bucket",
            unit="1",
        )
        counter.add(
            1,
            {
                "reason": str(reason)[:120],
                "schedule_state": schedule_state,
                "delay_bucket": _retry_delay_bucket(delay_seconds),
            },
        )
    except Exception:
        _log.warning("pipeline.retry_policy.metrics_unavailable", exc_info=True)


# ---------------------------------------------------------------------------
# Executor-facing orchestration helpers (pure functions, no executor state).
# These are called by executor.py which provides the necessary context.
# ---------------------------------------------------------------------------
async def maybe_retry_after_policy(
    *,
    run_id: Any,
    org_id: Any,
    pipeline_retry_policy: dict[str, Any] | None,
    final_status: str,
    error_code: str | None,
    error_detail: str | None,
    is_correction_run: bool,
    graph_idempotent: bool,
    graph_json: dict[str, Any],
    model_backend_hub: Any | None,
    connector_hub: Any | None,
    broker: Any,
    session_factory: Any,
    claim_token: str | None,
    fenced_pending_reset_fn: Any,
    cleanup_run_resources_fn: Any,
    record_attempt_reset_fn: Any,
    probe_script_lease_fn: Any,
    read_retry_attempt_state_fn: Any,
    graph_has_script_mode_fn: Any,
    node_timeouts: Any,
) -> str:
    """Apply the pipeline retry_policy after a terminal outcome.

    This is a PURE orchestration function — all side effects (fenced reset,
    hub cleanup, SAQ re-raise) are delegated to the caller-provided functions.
    The executor.py implementations of those functions handle the actual
    executor state (claim_token, hubs, etc.).

    Returns ``"retried"`` (the fenced pending-reset + backoff +
    ``RunRetryPolicyError`` re-raise happened inside this helper — control
    never returns), ``"side_effect_unknown"`` (terminal-failed; the
    ``run_failed`` publish happened here and the caller sets the
    code/detail), or ``"none"`` (nothing to do).
    """
    retry_budget = retry_after_policy(pipeline_retry_policy, final_status, error_code, error_detail)
    # FAR-295 / FAR-210: a non-idempotent graph or a correction run is
    # NEVER re-dispatched by the retry_policy (re-running would re-execute
    # a side-effecting node / a correction's budget is owned elsewhere).
    if not retry_policy_applies(retry_budget, is_correction_run, graph_idempotent):
        return "none"
    # _retry_policy_applies guarantees a budget (not None) here; a defensive
    # narrowing keeps mypy strict-clean without introducing an ``assert``.
    if retry_budget is None:
        return "none"
    execution_attempt_count, current_claim_token, attempt_started_at = await read_retry_attempt_state_fn(org_id, run_id)
    superseded = claim_token is not None and current_claim_token is not None and current_claim_token != claim_token
    # FAR-2025: an attempt that already consumed the graph's whole node-budget
    # allowance is an over-budget TASK, not a transient failure — re-running it
    # re-consumes the same budgets and ends the same way. Refuse the retry so the
    # ladder cannot multiply occupancy for no chance of success.
    attempt_consumed_budget = _attempt_consumed_node_budget(
        _attempt_elapsed_seconds(attempt_started_at),
        node_timeouts,
    )
    if attempt_consumed_budget:
        _log.warning(
            "pipeline.retry_policy_budget_exhausted",
            extra={
                "run_id": str(run_id),
                "error_code": error_code,
                "attempt": execution_attempt_count,
                "budget": retry_budget,
                "node_budget_seconds": sum(t for t in node_timeouts if isinstance(t, int) and t > 0),
            },
        )
    # FAR-2076: ``execution_attempt_count`` counts EXECUTIONS of the graph
    # (one per admitted ``execute_run``), so ``<= retry_budget`` means the
    # budget counts actual RETRIES: budget=1 permits execution 1
    # (1 <= 1) and refuses execution 2 (2 <= 1 is false) — one retry, two
    # executions. ``claim_count`` (the old pre-node measure) and
    # ``node_attempt_count`` are NOT execution counters: the former grows on
    # capacity-demoted / resume claims, the latter records nothing for a
    # pre-node death.
    # FAR-296 Phase 2: before ANY requeue of a script-mode run, prove
    # no script process could still be alive (stale-claim lease probe).
    script_retry_probe_ok = True
    if graph_has_script_mode_fn(graph_json):
        script_retry_probe_ok = await probe_script_lease_fn(run_id=run_id, org_id=org_id)
    if can_retry_after_policy(
        execution_attempt_count,
        retry_budget,
        superseded,
        script_retry_probe_ok,
        attempt_consumed_budget=attempt_consumed_budget,
    ):
        # pipeline_retry_policy is not None here (checked by can_retry_after_policy)
        assert pipeline_retry_policy is not None
        await _redispatch_after_policy(
            run_id=run_id,
            org_id=org_id,
            final_status=final_status,
            error_code=error_code,
            execution_attempt_count=execution_attempt_count,
            retry_budget=retry_budget,
            pipeline_retry_policy=pipeline_retry_policy,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
            fenced_pending_reset_fn=fenced_pending_reset_fn,
            cleanup_run_resources_fn=cleanup_run_resources_fn,
            record_attempt_reset_fn=record_attempt_reset_fn,
        )
    if not script_retry_probe_ok:
        return _publish_script_side_effect_unknown(broker, run_id)
    return "none"


async def _redispatch_after_policy(
    *,
    run_id: Any,
    org_id: Any,
    final_status: str,
    error_code: str | None,
    execution_attempt_count: int,
    retry_budget: int,
    pipeline_retry_policy: dict[str, Any],
    model_backend_hub: Any | None,
    connector_hub: Any | None,
    fenced_pending_reset_fn: Any,
    cleanup_run_resources_fn: Any,
    record_attempt_reset_fn: Any,
) -> None:
    """Fenced pending-reset + backoff + ``RunRetryPolicyError`` re-dispatch.

    Control never returns: the re-raise propagates out of the caller BEFORE
    the post-stream try/finally, so the run's cleanup runs HERE (clear the
    cancellation check + hubs and close the run's broker so the retry
    re-entry gets a fresh broker and no stale contextvars).
    """
    from modulo.settings import get_settings

    # FAR-525: resolve the run-level backoff_schedule EARLY — the pure
    # computation MUST happen BEFORE the fenced pending-reset so a
    # resolver defect can never strand a run that was already reset.
    # Total fail-open: an invalid/out-of-bounds schedule falls back to
    # the hardcoded default schedule (45s x 2.0, cap 300, jitter).
    schedule_present, schedule_delay, schedule_multiplier, schedule_reason = resolve_backoff_schedule(
        pipeline_retry_policy
    )
    if schedule_reason is not None:
        _log.warning(
            "pipeline.retry_policy_schedule_fail_open",
            extra={
                "run_id": str(run_id),
                "reason": schedule_reason,
                "offending": sanitize_retry_policy_snippet(
                    pipeline_retry_policy.get("backoff_schedule") if isinstance(pipeline_retry_policy, dict) else None
                ),
            },
        )
    if not schedule_present:
        schedule_state = "absent"
    elif schedule_reason:
        schedule_state = "failopen"
    else:
        schedule_state = "valid"
    # ONE jitter draw: the delay is computed ONCE here and threaded to
    # BOTH the log line and the asyncio.sleep below (no re-resolution).
    effective_sleep = retry_backoff_seconds(
        execution_attempt_count, base=schedule_delay, multiplier=schedule_multiplier
    )
    _log.warning(
        "pipeline.retry_policy",
        extra={
            "run_id": str(run_id),
            "status": final_status,
            "error_code": error_code,
            "attempt": execution_attempt_count,
            "budget": retry_budget,
            # FAR-525 observability: the effective post-cap post-jitter
            # sleep, the schedule state, and the SAQ delay component.
            "sleep_seconds": round(effective_sleep, 3),
            "schedule_state": schedule_state,
            "saq_retry_delay": int(getattr(get_settings(), "saq_retry_delay", 0)),
        },
    )
    reset_rowcount = await fenced_pending_reset_fn(run_id=run_id, org_id=org_id)
    if reset_rowcount is None:
        # FAR-2076: the reset TIMED OUT — the run was NOT handed back to
        # ``pending``; it stays ``running`` under our claim token and is
        # left for the age backstop. The re-raise below still cannot
        # re-execute it (the SAQ retry's claim only takes ``pending`` or a
        # STALE running row), and it is never terminalised here.
        _log.warning(
            "pipeline.retry_policy_reset_timed_out run=%s status=%s — the fenced "
            "pending-reset timed out; the row is left running for the nodeless/age "
            "backstop (no confirmed reset, no counter)",
            run_id,
            final_status,
        )
    await cleanup_run_resources_fn(model_backend_hub=model_backend_hub, connector_hub=connector_hub, run_id=run_id)
    # FAR-136 Gap 1: jittered, capped backoff before the re-dispatch.
    # Without it a policy-triggered retry re-fires back-to-back,
    # hammering the queue/gateway on a persistent failure. The delay
    # grows with the attempt count and is bounded by the retry
    # budget (the caller only re-dispatches while
    # execution_attempt_count <= retry_budget), so the schedule can never
    # extend beyond max_retries. `retry_backoff_seconds` is a pure
    # function of the attempt number — covered by unit tests.
    # FAR-525: the delay honours the run-level ``backoff_schedule``
    # (resolved EARLY above; fail-open to the hardcoded default).
    if reset_rowcount:
        # FAR-525 observability: count only CONFIRMED resets — a fence
        # loss (rowcount 0) means a successor already owns the run, and
        # ``None`` means the reset timed out (unconfirmed).
        record_retry_redispatch(reason=final_status, schedule_state=schedule_state, delay_seconds=effective_sleep)
        # FAR-2078: close the attempt that ended in this confirmed reset with
        # the outcome the reset just cleared from ``runs.error_code``.
        await record_attempt_reset_fn(run_id=run_id, org_id=org_id, error_code=error_code)
    import asyncio

    await asyncio.sleep(effective_sleep)
    raise RunRetryPolicyError(final_status, retry_budget)


def _publish_script_side_effect_unknown(broker: Any, run_id: Any) -> str:
    """FAR-296 Phase 2: terminal-fail with ``script.side_effect_unknown``.

    The lease probe blocked the requeue — a script process may have run
    with unknown side-effect state. Terminal-fail (never retried) so the
    run reaches a needs-human state instead of silently looping or being
    left stuck in ``running``.
    """
    _log.warning(
        "script.lease_probe.terminal_side_effect_unknown",
        extra={"run_id": str(run_id), "error_code": _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN},
    )
    error_detail_value = _sanitize_detail(
        "Script-mode sandbox has an unresolved execution claim (side effect unknown); "
        "not retried — needs human review.",
        limit=5000,
    )
    broker.publish("run_failed", {"error": _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN, "detail": error_detail_value})
    return "side_effect_unknown"


# ---------------------------------------------------------------------------
# Helper functions used by the orchestration layer (pure, unit-testable).
# ---------------------------------------------------------------------------
def _attempt_started_at(run: Any) -> Any | None:
    """This attempt's start time from a run row, or None when it cannot be known.

    ``started_at`` is set once, when the FIRST attempt begins executing, while
    ``dispatched_at`` is rewritten on every claim / re-claim. So the LATER of the
    two is the current attempt's start: for attempt 1 that is ``started_at``
    (which excludes any queue wait before the worker claimed the run — using
    ``dispatched_at`` there would count the wait as consumed budget, a false
    positive); for a re-dispatch it is the rewritten ``dispatched_at`` (the
    original ``started_at`` would otherwise make every retry look exhausted).
    """
    from datetime import UTC, datetime

    def _as_utc_datetime(value: Any) -> Any | None:
        if not isinstance(value, datetime):
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    candidates = [
        value
        for value in (
            _as_utc_datetime(getattr(run, "dispatched_at", None)),
            _as_utc_datetime(getattr(run, "started_at", None)),
        )
        if value is not None
    ]
    return max(candidates) if candidates else None


def _attempt_elapsed_seconds(started_at: Any | None, *, now: Any | None = None) -> float | None:
    """Seconds since this execution attempt was dispatched, or None if unknown.

    Pure (``now`` is injectable for tests). A naive timestamp is treated as UTC
    so a driver that hands back a naive datetime cannot raise here — the retry
    decision must never be the reason a run cannot reach a terminal state.
    """
    from datetime import UTC, datetime

    if started_at is None:
        return None
    if not isinstance(started_at, datetime):
        return None
    reference = now if now is not None else datetime.now(UTC)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return max(0.0, (reference - started_at).total_seconds())


# FAR-2025: a retry must not re-run an attempt that has already consumed the
# graph's whole node-budget allowance. Such an attempt is an OVER-BUDGET TASK,
# not a transient failure: the retry re-runs the identical workload against the
# same per-node budgets, so it is guaranteed to end the same way and only
# multiplies slot occupancy (measured: 12 of 69 Branch Fixer deadline runs over
# 30h were a retry that re-consumed the budget, 24.8 of 77 slot-hours).
#
# The slack absorbs everything that legitimately happens inside a node's wall
# clock other than the agent's own work: sandbox provisioning, the post-node
# envelope write / cost self-report merge, and a task's OWN end-of-budget
# reserve (a sandbox command that hands itself a timeout a little under the
# node budget so it can still write its envelope). An attempt within this
# window of its ceiling has spent the allowance for retry purposes.
_NODE_BUDGET_RETRY_SLACK_SECONDS = 300


def _attempt_consumed_node_budget(elapsed_seconds: float | None, node_timeouts: Any) -> bool:
    """True when the finished attempt already used its node-budget allowance.

    ``node_timeouts`` are the graph's resolved per-node budgets. Their SUM is
    the most a single attempt of this graph could legitimately spend, so it is
    the safe upper bound for "this attempt is over budget"; using the maximum
    would refuse a legitimate retry of a multi-node graph that spent its
    allowance across several nodes.

    Fail OPEN (return False) when either input is unknown — a missing elapsed
    time, or no resolved node budget (``_node_timeouts`` is only populated once
    the executor prepares the stream, so a PRE-node hang has no allowance to
    check and must keep its retry). A retry decision must never be the reason a
    run cannot reach a terminal state, and today's behaviour is to allow it.
    """
    if elapsed_seconds is None:
        return False
    budget_seconds = sum(t for t in node_timeouts if isinstance(t, int) and t > 0)
    if budget_seconds <= 0:
        return False
    return elapsed_seconds >= budget_seconds - _NODE_BUDGET_RETRY_SLACK_SECONDS


def _resolved_node_timeouts(graph_json: dict[str, Any] | None) -> dict[str, int]:
    """Resolve each graph node's effective ``timeout_seconds`` (FAR-369).

    The absolute node-deadline watchdog holds every node to its own hard
    deadline, falling back to the pipeline-level default when a node does not
    configure one.

    Absent and explicitly-``null`` are the SAME state here: the API
    ``PipelineGraphNode`` model defaults ``timeout_seconds`` to ``None`` and
    serialises the key, so graphs authored through the API carry an explicit
    ``"timeout_seconds": null``. Resolving that with ``dict.get(key, default)``
    would return the persisted ``None`` (the key IS present) rather than the
    default, and ``int(None)`` raises a TypeError that fails the run before any
    node starts. Guard ``None`` explicitly so the default is always applied.
    """
    resolved: dict[str, int] = {}
    for node in (graph_json or {}).get("nodes", []) or []:
        if not isinstance(node, dict) or "id" not in node:
            continue
        configured = node.get("timeout_seconds")
        resolved[str(node["id"])] = int(configured if configured is not None else 300)
    return resolved


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


def _sanitize_detail(detail: Any, limit: int | None = 5000) -> str:
    """Sanitize an error detail at a write surface (FAR-163).

    Redaction runs before any truncation so a secret straddling the cut point is
    still removed. Pass ``limit=None`` to skip the cap. Never raises.
    """
    from modulo.core.pipeline_engine.error_codes import sanitize_error_text

    return sanitize_error_text(detail, limit)


def _transient_retry_allowance(configured: int, retry_policy: Any) -> int:
    """Bound the in-execute transient retry by the pipeline's policy (FAR-2076).

    ``_can_fenced_requeue`` is fed ``runs.execution_attempt_count`` (via
    ``modulo.core.retry_budget.retry_executions_consumed``) and ``retries`` used
    to be ``settings.saq_run_retries`` ONLY — so the in-execute transient path
    could grant executions the pipeline's ``retry_policy`` had already refused
    (the run-level policy path and the transport were each counting a different
    budget).

    Returns ``min(configured, execution ceiling)`` where the ceiling is
    ``1 + max_retries`` (the total executions the policy authorises, this
    attempt included). ``execution_attempt_count`` — the value compared against
    the returned bound — counts executions, so ``consumed < 1 + max_retries``
    permits exactly ``1 + max_retries`` executions in total. A policy with no
    ceiling (absent / empty) returns the configured value unchanged: no policy
    means existing behaviour.
    """
    max_executions = retry_max_executions(retry_policy)
    if max_executions is None:
        return configured
    return min(configured, max_executions)


def _is_non_retryable_error_code(code: str | None) -> bool:
    """True when *code* is a REGISTERED error code its registry marks non-retryable.

    A code with NO registry entry has no retryability verdict to honour, so it
    is NOT treated as non-retryable — the caller's existing behaviour is
    preserved (the same fail-open posture the provider classifier takes). Legacy
    spellings resolve through ``LEGACY_ALIASES`` first, so a raw
    ``model_insufficient_funds`` is judged as its canonical code.
    """
    if not code:
        return False
    spec = ERROR_CODE_REGISTRY.get(LEGACY_ALIASES.get(code, code))
    if spec is None:
        return False
    return not spec.retryable


def _can_fenced_requeue(
    execution_attempt_count: int,
    retries: int,
    superseded: bool,
    stalled: bool,
    script_lease_ok: bool,
    graph_idempotent: bool,
    error_code: str | None = None,
) -> bool:
    """Can a transient node failure be fenced back to ``pending`` for a retry?

    False when the retry budget is exhausted, the run is owned by a successor
    (superseded), the zombie watchdog stalled it, a script lease is still live,
    the graph contains a non-idempotent node (FAR-295), or — FAR-1800 — the
    failure class is REGISTERED NON-RETRYABLE. A billing/credential fault (HTTP
    402 -> ``model.insufficient_funds``) re-hits the same wall on every attempt,
    so re-dispatching it only burns sandbox dispatches until the SAQ retry
    budget is spent; the run must terminal-fail on the FIRST attempt instead.
    ``error_code`` is the failure's already-classified code (``None`` when no
    signature matched, which leaves the outcome unchanged). This NARROWS when
    the fence fires — it never weakens it: every existing invariant (claim
    token, fencing, script lease, idempotency) still applies.
    """
    if _is_non_retryable_error_code(error_code):
        return False
    return execution_attempt_count < retries and not superseded and not stalled and script_lease_ok and graph_idempotent


def _non_retryable_failure_detail(
    non_retryable_code: str,
    exc: Exception,
    execution_attempt_count: int,
    retries: int,
) -> str:
    """Terminal detail for a failure whose error class cannot be retried (FAR-1800).

    A billing/credential refusal (HTTP 402 -> ``model.insufficient_funds``)
    re-hits the same wall on every attempt, so the requeue gate refuses it. The
    wording must not claim a retry was attempted when budget remained: the run
    is a FIRST-attempt terminal in that case, and a retries-exhausted terminal
    only when the budget was already spent.
    """
    if execution_attempt_count < retries:
        return _sanitize_detail(
            f"Sandbox node failed with non-retryable error {non_retryable_code}; "
            "not retried because a retry re-hits the same fault: " + str(exc),
            limit=5000,
        )
    return _sanitize_detail(
        "Sandbox node failed after retries exhausted; the last failure is "
        f"non-retryable ({non_retryable_code}) and a further retry would "
        "re-hit the same fault: " + str(exc),
        limit=5000,
    )


def sandbox_failure_error_code(exc: Any) -> str:
    """The caught sandbox failure's OWN canonical code, else the generic one.

    Every ``SandboxNodeFailedError`` role that outlives the retry budget used
    to be written as ``sandbox.no_output_json`` regardless of its subclass,
    which hid the distinct retryable classes from the Error Dashboard and
    daily facts (FAR-1887: an exhausted E2B rate-limit retry budget —
    ``SandboxQueueTimeoutError`` — was recorded as "no output json", so
    ``sandbox.queue_timeout`` / ``sandbox.rate_limited`` never appeared in
    analytics). ``LEGACY_ALIASES`` keys these subclasses by exception class
    name, so resolve through it and fall back to ``sandbox.no_output_json``
    only for the BASE class or a subclass the alias table does not map —
    never to ``harness.unknown``, which would be a regression for an
    unclassified sandbox failure.
    """
    return LEGACY_ALIASES.get(type(exc).__name__, _CODE_SANDBOX_NO_OUTPUT_JSON)


__all__ = [
    "_ERROR_CODE_AGENT_FAILED",
    "_ERROR_CODE_EVAL_BLOCKED",
    "_ERROR_CODE_HARNESS_IDEMPOTENCY_GATE",
    "_ERROR_CODE_NODE_DEADLINE_EXCEEDED",
    "_ERROR_CODE_NODE_TIMEOUT",
    "_ERROR_CODE_SANDBOX_TIER_REFUSED",
    "_ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN",
    "_NEVER_RETRYABLE_PROVIDER_CODES",
    "_NEVER_RETRYABLE_SCRIPT_CODES",
    "_NEVER_RETRYABLE_SCRIPT_RAW",
    "_RETRY_BACKOFF_BASE_SECONDS",
    "_RETRY_BACKOFF_CAP_SECONDS",
    "_RETRY_BACKOFF_DEFAULT_MULTIPLIER",
    "_RETRY_BACKOFF_JITTER_FRACTION",
    "_RETRY_POLICY_EVENTS",
    "RunRetryPolicyError",
    "_attempt_consumed_node_budget",
    "_attempt_elapsed_seconds",
    "_attempt_started_at",
    "_can_fenced_requeue",
    "_eval_failed_event_matches",
    "_failure_event_matches",
    "_graph_has_script_mode",
    "_non_retryable_failure_detail",
    "_publish_script_side_effect_unknown",
    "_redispatch_after_policy",
    "_resolved_node_timeouts",
    "_sanitize_detail",
    "_stall_event_matches",
    "_timeout_event_matches",
    "_transient_retry_allowance",
    "can_retry_after_policy",
    "maybe_retry_after_policy",
    "record_retry_redispatch",
    "retry_after_policy",
    "retry_backoff_seconds",
    "retry_policy_applies",
    "sandbox_failure_error_code",
]
