"""Error-code registry for run/agent failure classification (agent-failure UX, phase 1).

Single source of truth for the dotted error-code taxonomy
(``<namespace>.<reason>``) described in the agent-failure-ux-proposal (§1, §3,
§15.16). This module implements the write-time legacy→dotted mapping plus the
registry lookups shared by ``_retry_after_policy``, the alert-rule matcher, and
the notifier ``event_mapper`` — one table, three consumers, no drift (§3.2
hard rules).

The module is intentionally dependency-free (no DB, no settings import) so unit
tests are fast and the registry is importable from any consumer.

It also owns the shared error-text sanitizer (:func:`sanitize_error_text`) and
the read-surface presenter (:func:`present_error`) used by the API/MCP layers
and the SAQ task_failure writer — one redaction primitive, no drift.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from modulo.core.secret_patterns import AWS_ACCESS_KEY_PATTERN, GITHUB_PAT_PATTERN

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorCodeSpec:
    """One registry entry: classification tag, retry default, alert severity, guidance."""

    error_class: str
    retryable: bool
    alert_severity: str | None
    guidance: str


# Canonical dotted codes referenced from multiple places in this module
# (registry keys, LEGACY_ALIASES targets, and the unmapped-code fallback).
# Constants keep the registry and the alias table provably in sync — a
# spelling change is a one-line edit (S1192).
_CODE_HARNESS_UNKNOWN = "harness.unknown"
_CODE_HARNESS_EXECUTOR_FAILED = "harness.executor_failed"
_CODE_HARNESS_DISPATCH_FAILED = "harness.dispatch_failed"
_CODE_NODE_TIMEOUT = "node.timeout"
_CODE_NODE_RUNAWAY = "node.runaway"
_CODE_EVAL_BLOCKED = "eval.blocked"
_CODE_CONTRACT_SCHEMA = "contract.schema"
_CODE_SANDBOX_RATE_LIMITED = "sandbox.rate_limited"
_CODE_SANDBOX_QUEUE_TIMEOUT = "sandbox.queue_timeout"
# FAR-510: the finalize-time downgrade code for a sandbox_agent node whose
# synthetic failure envelope was masked as a completed output.
_CODE_SANDBOX_AGENT_FAILED = "sandbox.agent_failed"
# FAR-592 (D6): provision-time per-agent runner-binding resolution failure —
# retryable config error; the D6 rollback trigger reads this code's rate.
_CODE_SANDBOX_BINDING_RESOLUTION = "sandbox.binding_resolution"
# FAR-592 (D6): the Local (host-subprocess) provider tier refused a
# bindings-carrying agent without an explicit opt-in (D7-refusal posture).
_CODE_SANDBOX_TIER_REFUSED = "sandbox.tier_refused"
_CODE_CAPACITY_ORG = "capacity.org"
# FAR-410: a connector write was cancelled mid-send (per-attempt timeout), so
# the upstream side-effect state is unknowable. This is a DISTINCT terminal
# outcome — never collapsed into generic failure (it must surface for manual
# confirm and be re-runnable with the same persisted idempotency key). The
# dotted spelling mirrors the ``script.side_effect_unknown`` taxonomy pattern
# ("side-effect state unknown; never retried").
_CODE_CONNECTOR_UNKNOWN = "connector.side_effect_unknown"
_CODE_SCOPE_VIOLATION = "scope.violation"


ERROR_CODE_REGISTRY: dict[str, ErrorCodeSpec] = {
    # --- agent (work verdict) codes -------------------------------------
    "agent.failed": ErrorCodeSpec(
        error_class="agent",
        retryable=False,
        alert_severity="critical",
        guidance="The agent reported it failed.",
    ),
    "agent.no_op": ErrorCodeSpec(
        error_class="agent",
        retryable=False,
        alert_severity="warning",
        guidance="Completed, but no verifiable work.",
    ),
    "agent.stall": ErrorCodeSpec(
        error_class="agent",
        retryable=False,
        alert_severity="warning",
        guidance="Run claimed by a worker but never dispatched a node (wedged worker); recovered by re-dispatch.",
    ),
    # --- contract (output) codes ----------------------------------------
    _CODE_CONTRACT_SCHEMA: ErrorCodeSpec(
        error_class="contract",
        retryable=False,
        alert_severity="warning",
        guidance="Output didn't match the schema.",
    ),
    "contract.no_output": ErrorCodeSpec(
        error_class="contract",
        retryable=False,
        alert_severity="warning",
        guidance="Node produced no usable output.",
    ),
    # --- script (script-mode sandbox) codes ------------------------------
    # FAR-296 Phase 2 stage-split contract: once a script-mode node's script
    # PROCESS has started (the fencing lease is claimed), every fault is
    # TERMINAL (never retryable) — re-dispatching could double-execute a
    # side-effecting script. These are the post-claim terminal codes.
    # ``script.schema_failed`` / ``script.no_output`` canonicalize to the
    # existing contract.* codes (one string per failure class) — see
    # LEGACY_ALIASES.
    "script.failed": ErrorCodeSpec(
        error_class="script",
        retryable=False,
        alert_severity="critical",
        guidance="Script-mode sandbox failed after the script process started (post-claim, terminal).",
    ),
    "script.invalid_output": ErrorCodeSpec(
        error_class="script",
        retryable=False,
        alert_severity="warning",
        guidance="Script-mode sandbox produced invalid output after the script process started (post-claim, terminal).",
    ),
    "script.side_effect_unknown": ErrorCodeSpec(
        error_class="script",
        retryable=False,
        alert_severity="critical",
        guidance="Script terminated mid-execution, side-effect state unknown; never retried (needs human).",
    ),
    "script.session_lost": ErrorCodeSpec(
        error_class="script",
        retryable=False,
        alert_severity="critical",
        guidance="Script-mode sandbox session was lost after the script process started (post-claim, terminal).",
    ),
    "script.budget_killed": ErrorCodeSpec(
        error_class="script",
        retryable=False,
        alert_severity="critical",
        guidance="Script-mode sandbox exceeded its resource limits and was killed by "
        "the platform-side runtime killer (post-claim, terminal).",
    ),
    # --- harness (machinery) codes ---------------------------------------
    # ``harness.unknown`` is the fallback for unmapped legacy codes — any code
    # that has no alias and no registry entry resolves here so presentation
    # always has a resolvable code (§3.2). Non-retryable by default: an
    # unclassified failure is never auto-retried (fail-safe default).
    _CODE_HARNESS_UNKNOWN: ErrorCodeSpec(
        error_class="harness",
        retryable=False,
        alert_severity="warning",
        guidance="Unclassified harness failure.",
    ),
    "harness.db.connection_lost": ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Database connection lost.",
    ),
    "harness.state_serialization": ErrorCodeSpec(
        error_class="harness",
        retryable=False,
        alert_severity="warning",
        guidance="Checkpoint state could not be serialized.",
    ),
    "harness.sdk_task_cancelled": ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Sandbox SDK task was cancelled.",
    ),
    _CODE_HARNESS_EXECUTOR_FAILED: ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Executor failed during dispatch.",
    ),
    "harness.executor_heartbeat_lost": ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Executor heartbeat was lost.",
    ),
    _CODE_HARNESS_DISPATCH_FAILED: ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Run was never dispatched.",
    ),
    "harness.worker_failed": ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Worker task failed.",
    ),
    "harness.node_cancelled": ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="Node was cancelled by the harness.",
    ),
    "harness.gate_creation_failed": ErrorCodeSpec(
        error_class="harness",
        retryable=True,
        alert_severity="warning",
        guidance="A HITL gate could not be created.",
    ),
    "harness.late_write": ErrorCodeSpec(
        error_class="harness",
        retryable=False,
        alert_severity="warning",
        guidance="A node wrote output after the run terminalized.",
    ),
    "harness.idempotency_gate": ErrorCodeSpec(
        error_class="harness",
        retryable=False,
        alert_severity="warning",
        guidance="Delivery already sent; transient retry suppressed by the idempotency gate.",
    ),
    # --- connector (generic REST) codes ----------------------------------
    # FAR-410: write-timeout / mid-send cancellation is a DISTINCT terminal
    # state that must NOT masquerade as generic failure. It surfaces to the run
    # viewer / HITL for manual confirm and is re-runnable with the SAME
    # persisted idempotency key (never a fresh random per run).
    _CODE_CONNECTOR_UNKNOWN: ErrorCodeSpec(
        error_class="connector",
        retryable=False,
        alert_severity="critical",
        guidance=(
            "Connector write was cancelled mid-send; upstream side-effect state unknown. "
            "Re-run with the same idempotency key or confirm manually."
        ),
    ),
    # --- sandbox codes ---------------------------------------------------
    "sandbox.no_output_json": ErrorCodeSpec(
        error_class="sandbox",
        retryable=True,
        alert_severity="warning",
        guidance="Sandbox produced no parseable output.",
    ),
    "sandbox.spawn": ErrorCodeSpec(
        error_class="sandbox",
        retryable=True,
        alert_severity="warning",
        guidance="Sandbox could not be provisioned.",
    ),
    "sandbox.network": ErrorCodeSpec(
        error_class="sandbox",
        retryable=True,
        alert_severity="warning",
        guidance="Sandbox network failure.",
    ),
    _CODE_SANDBOX_RATE_LIMITED: ErrorCodeSpec(
        error_class="sandbox",
        retryable=True,
        alert_severity="warning",
        guidance="E2B provisioner rate-limited sandbox creation (429); the run will be retried.",
    ),
    _CODE_SANDBOX_QUEUE_TIMEOUT: ErrorCodeSpec(
        error_class="sandbox",
        retryable=True,
        alert_severity="warning",
        guidance=(
            "Sandbox provisioning was retried but the rate-limit retry budget"
            " was exhausted within the node timeout window."
        ),
    ),
    # FAR-510: a sandbox_agent node whose synthetic failure path (generic
    # exception, schema validation) RETURNED the stamped failure envelope
    # (instead of raising) is downgraded from "complete" to "failed" at
    # finalization. The executor writes this exact dotted spelling into
    # ``runs.error_code`` (a registry key passes through ``map_legacy_code``
    # unchanged) — otherwise the honest downgrade would present as
    # ``harness.unknown``.
    _CODE_SANDBOX_AGENT_FAILED: ErrorCodeSpec(
        error_class="sandbox",
        retryable=False,
        alert_severity="critical",
        guidance="Sandbox agent execution failed; the run was downgraded from complete at finalization.",
    ),
    # FAR-592 (D6): retryable provision-time binding-resolution failure
    # (backend gone/unhealthy/decrypt/malformed). The D6 rollback trigger
    # monitors this code's rate via the error dashboard.
    _CODE_SANDBOX_BINDING_RESOLUTION: ErrorCodeSpec(
        error_class="config",
        retryable=True,
        alert_severity="warning",
        guidance="Agent runner binding could not be resolved at provision time; re-configure the binding.",
    ),
    # FAR-592 (D6): terminal — the Local host tier refuses standing-credential
    # injection without an explicit profile opt-in.
    _CODE_SANDBOX_TIER_REFUSED: ErrorCodeSpec(
        error_class="config",
        retryable=False,
        alert_severity="warning",
        guidance=(
            "Local provider tier refused runner bindings; opt in via the profile's "
            "allow_runner_env_bindings flag or switch tiers."
        ),
    ),
    # --- node guard codes ------------------------------------------------
    _CODE_NODE_TIMEOUT: ErrorCodeSpec(
        error_class="node",
        retryable=True,
        alert_severity="warning",
        guidance="Hit the timeout guard.",
    ),
    "node.deadline_exceeded": ErrorCodeSpec(
        error_class="node",
        retryable=False,
        alert_severity="warning",
        guidance=(
            "A node did not complete within its configured timeout_seconds. "
            "Distinct from the short setup-grace executor_stalled: the node "
            "started executing but never finished (the idle-watchdog could not "
            "catch a half-alive SSE stall)."
        ),
    ),
    _CODE_NODE_RUNAWAY: ErrorCodeSpec(
        error_class="node",
        retryable=False,
        alert_severity="warning",
        guidance="Hit the token budget.",
    ),
    "node.cancelled": ErrorCodeSpec(
        error_class="node",
        retryable=True,
        alert_severity="warning",
        guidance="Node was cancelled.",
    ),
    # --- run-level codes -------------------------------------------------
    "run.superseded": ErrorCodeSpec(
        error_class="run",
        retryable=False,
        alert_severity=None,
        guidance="Superseded by a newer run.",
    ),
    # --- connector codes -------------------------------------------------
    "connector.invalid_key": ErrorCodeSpec(
        error_class="connector",
        retryable=False,
        alert_severity="critical",
        guidance="Connector credentials are invalid.",
    ),
    # FAR-418: node-level capability_scope violation — a node used a connector /
    # tool / run_context key excluded by its scope (deny-by-default). Permanent
    # (re-dispatching would reproduce the same violation), so never retryable.
    _CODE_SCOPE_VIOLATION: ErrorCodeSpec(
        error_class="scope",
        retryable=False,
        alert_severity="critical",
        guidance="Node used a capability excluded by its capability_scope (connector/tool/context).",
    ),
    "connector.permission": ErrorCodeSpec(
        error_class="connector",
        retryable=False,
        alert_severity="critical",
        guidance="Connector lacks permission.",
    ),
    "connector.rate_limit": ErrorCodeSpec(
        error_class="connector",
        retryable=True,
        alert_severity="warning",
        guidance="Connector is temporarily rate limited.",
    ),
    "connector.network": ErrorCodeSpec(
        error_class="connector",
        retryable=True,
        alert_severity="warning",
        guidance="Connector network failure.",
    ),
    # --- provider (model backend) codes -----------------------------------
    # Raw exception class names that executor's generic catch publishes
    # (``type(exc).__name__``) for LLM-node failures. ``provider.authentication``
    # is permanent (a bad API key); the others are transient infra states and
    # match the analogous connector.transient retryable conventions.
    "provider.unavailable": ErrorCodeSpec(
        error_class="provider",
        retryable=True,
        alert_severity="warning",
        guidance="The model provider is unavailable (gateway outage or upstream 5xx).",
    ),
    "provider.authentication": ErrorCodeSpec(
        error_class="provider",
        retryable=False,
        alert_severity="critical",
        guidance="The model provider rejected the API key.",
    ),
    "provider.rate_limited": ErrorCodeSpec(
        error_class="provider",
        retryable=True,
        alert_severity="warning",
        guidance="The model provider rate-limited the request.",
    ),
    "provider.connection": ErrorCodeSpec(
        error_class="provider",
        retryable=True,
        alert_severity="warning",
        guidance="A connection to the model provider failed.",
    ),
    # --- model (stdout-scanned provider failure) codes --------------------
    # FAR-734: model-backend errors detected by scanning the retained agent
    # stdout for terminal JSONL ``"type":"error"`` signatures.  These are
    # DISTINCT from the provider.* codes (which map Python exception class
    # names from the executor's generic catch) — the same underlying failure
    # (e.g. a timeout) surfaces as provider.unavailable when caught as a
    # Python exception, but as model.provider_timeout when detected post-hoc
    # in retained stdout.  Retryability matches the provider.* counterpart.
    "model.provider_timeout": ErrorCodeSpec(
        error_class="model",
        retryable=True,
        alert_severity="warning",
        guidance="The model backend timed out mid-session (detected from retained stdout).",
    ),
    "model_disabled": ErrorCodeSpec(
        error_class="model",
        retryable=False,
        alert_severity="critical",
        guidance="The model is disabled for the API key (detected from retained stdout).",
    ),
    "model.connection": ErrorCodeSpec(
        error_class="model",
        retryable=True,
        alert_severity="warning",
        guidance="A connection to the model backend failed (detected from retained stdout).",
    ),
    "model.rate_limited": ErrorCodeSpec(
        error_class="model",
        retryable=True,
        alert_severity="warning",
        guidance="The model backend rate-limited the request (detected from retained stdout).",
    ),
    # --- capacity codes --------------------------------------------------
    _CODE_CAPACITY_ORG: ErrorCodeSpec(
        error_class="capacity",
        retryable=True,
        alert_severity=None,
        guidance="Queued — waiting for org capacity.",
    ),
    "capacity.pipeline": ErrorCodeSpec(
        error_class="capacity",
        retryable=True,
        alert_severity=None,
        guidance="Queued — waiting for pipeline capacity.",
    ),
    "capacity.claim": ErrorCodeSpec(
        error_class="capacity",
        retryable=True,
        alert_severity=None,
        guidance="Claim capacity exhausted.",
    ),
    "capacity.timeout": ErrorCodeSpec(
        error_class="capacity",
        retryable=True,
        alert_severity=None,
        guidance="Capacity wait timed out.",
    ),
    # --- hitl codes --------------------------------------------------------
    # FAR-648: the dispatcher_reconcile expired-HITL-gate terminalizer writes
    # the raw ``hitl_gate_expired`` code — registered here (and aliased in
    # LEGACY_ALIASES) so it never resolves through the ``harness.unknown``
    # fallback and analytics buckets it as its own cancel class rather than
    # "Unknown error". Terminal like the sibling terminalizer codes
    # (``run.superseded``): the gate expired unanswered, never retried, and
    # routine hygiene — no alert.
    "hitl.gate_expired": ErrorCodeSpec(
        error_class="hitl",
        retryable=False,
        alert_severity=None,
        guidance="HITL gate expired unclaimed; run terminalized by dispatcher_reconcile to free its concurrency slot.",
    ),
    # --- eval codes ------------------------------------------------------
    _CODE_EVAL_BLOCKED: ErrorCodeSpec(
        error_class="eval",
        retryable=False,
        alert_severity="warning",
        guidance="Work done, but evals blocked or failed.",
    ),
    "eval.failed": ErrorCodeSpec(
        error_class="eval",
        retryable=False,
        alert_severity="warning",
        guidance="Eval suite failed.",
    ),
    # --- config codes ----------------------------------------------------
    "config.error": ErrorCodeSpec(
        error_class="config",
        retryable=False,
        alert_severity="warning",
        guidance="Pipeline configuration is invalid.",
    ),
    "config.invalid": ErrorCodeSpec(
        error_class="config",
        retryable=False,
        alert_severity="warning",
        guidance="Pipeline configuration is invalid.",
    ),
}


LEGACY_ALIASES: dict[str, str] = {
    # Agent verdict / work-truth (executor.run_failed publishes).
    "executor_stalled": "agent.stall",
    # Node guards.
    "node_timeout": _CODE_NODE_TIMEOUT,
    "TimeoutError": _CODE_NODE_TIMEOUT,
    "node_deadline_exceeded": "node.deadline_exceeded",
    "runaway": _CODE_NODE_RUNAWAY,
    "runaway.tokens_exceeded": _CODE_NODE_RUNAWAY,
    "node_cancelled": "node.cancelled",
    # Run-level.
    "executor_superseded": "run.superseded",
    # Contract.
    "output_rejected": _CODE_CONTRACT_SCHEMA,
    # Executor maps manual/agent output schema validation failures to this
    # domain code (PRD §8.9 error table) instead of a raw "ValueError".
    "schema_validation_failure": _CODE_CONTRACT_SCHEMA,
    # FAR-296 Phase 2: script-mode stage-split aliases. These canonicalize to
    # ONE string per failure class — ``script.schema_failed`` is the same
    # contract.schema class, ``script.no_output`` the same contract.no_output
    # class. A script exception class name that the executor's generic catch
    # publishes (``type(exc).__name__``) also resolves to its canonical code.
    "script.schema_failed": _CODE_CONTRACT_SCHEMA,
    "script.no_output": "contract.no_output",
    "ScriptFailedError": "script.failed",
    "ScriptInvalidOutputError": "script.invalid_output",
    "ScriptSideEffectUnknownError": "script.side_effect_unknown",
    "ScriptBudgetKilledError": "script.budget_killed",
    # Harness machinery (§3.2). ``TypeError``/``OperationalError`` are the
    # raw exception class names that executor's generic catch publishes.
    "OperationalError": "harness.db.connection_lost",
    "TypeError": "harness.state_serialization",
    "NodeCancelledError": "harness.sdk_task_cancelled",
    "SandboxNodeFailedError": "sandbox.no_output_json",
    # FAR-296 Phase 4a: E2B concurrent-sandbox rate limits (429 / resource
    # exhausted) are transient. The executor's generic catch publishes the raw
    # exception class name (``SandboxRateLimitedError`` — our retryable wrapper,
    # or the un-retried e2b ``RateLimitException``), both of which must resolve
    # to the retryable ``sandbox.rate_limited`` code — never the permanent
    # ``harness.unknown`` fallback.
    "SandboxRateLimitedError": _CODE_SANDBOX_RATE_LIMITED,
    "RateLimitException": _CODE_SANDBOX_RATE_LIMITED,
    # FAR-296 Phase 4b: rate-limit retry exhaustion maps to the distinct
    # ``sandbox.queue_timeout`` code (the "queue" for capacity timed out).
    "SandboxQueueTimeoutError": _CODE_SANDBOX_QUEUE_TIMEOUT,
    "SandboxRateLimitExhaustedError": _CODE_SANDBOX_QUEUE_TIMEOUT,
    # FAR-296 Phase 4b: dispatch-time capacity gate maps to ``capacity.org``.
    "SandboxCapacityExceededError": _CODE_CAPACITY_ORG,
    # FAR-592 (D6): provision-time binding-resolution failures map to the
    # retryable ``sandbox.binding_resolution`` code (D6 rollback-trigger
    # signal); both the typed wrapper and the raw core class name publish.
    # The dotted spellings are registry keys — map_legacy_code's alias lookup
    # misses them and the registry check then passes them through unchanged,
    # so no dotted alias entry is needed.
    "SandboxBindingResolutionError": _CODE_SANDBOX_BINDING_RESOLUTION,
    "AgentBindingResolutionError": _CODE_SANDBOX_BINDING_RESOLUTION,
    # FAR-592 (D6): the Local tier refusal maps to the terminal
    # ``sandbox.tier_refused`` code. Both the node-runner wrapper (what the
    # executor sees via ``type(exc).__name__``) and the raw core class name
    # publish.
    "SandboxTierRefusedError": _CODE_SANDBOX_TIER_REFUSED,
    "LocalProviderBindingsRefusedError": _CODE_SANDBOX_TIER_REFUSED,
    "executor_setup_failed": _CODE_HARNESS_EXECUTOR_FAILED,
    "executor_failed": _CODE_HARNESS_EXECUTOR_FAILED,
    "executor_heartbeat_lost": "harness.executor_heartbeat_lost",
    "never_dispatched": _CODE_HARNESS_DISPATCH_FAILED,
    "dispatch_failed": _CODE_HARNESS_DISPATCH_FAILED,
    "worker_lost": _CODE_HARNESS_DISPATCH_FAILED,
    "task_failure": "harness.worker_failed",
    "gate_creation_failed": "harness.gate_creation_failed",
    # FAR-228 raw code used by the executor's retry-suppression write.
    "idempotency_gate": "harness.idempotency_gate",
    # FAR-410: connector write-timeout / mid-send cancellation → distinct
    # ``connector.side_effect_unknown`` (never collapsed into generic failure).
    # ``connector_unknown`` is the raw exception/snake_case spelling;
    # ``ConnectorUnknownError`` is the exception class name the executor's
    # generic catch publishes (``type(exc).__name__``); ``connector.unknown`` is
    # the prior dotted spelling kept for backward-compat.
    "connector_unknown": _CODE_CONNECTOR_UNKNOWN,
    "ConnectorUnknownError": _CODE_CONNECTOR_UNKNOWN,
    "connector.unknown": _CODE_CONNECTOR_UNKNOWN,
    # Provider (model backend) exception class names published by executor's
    # generic catch (``type(exc).__name__``) on LLM-node failures.
    "RateLimitError": "provider.rate_limited",
    "ProviderUnavailableError": "provider.unavailable",
    "AuthenticationError": "provider.authentication",
    "APIConnectionError": "provider.connection",
    # Eval.
    "eval_blocked": _CODE_EVAL_BLOCKED,
    "eval_suite_blocked": _CODE_EVAL_BLOCKED,
    # Config.
    "configuration_error": "config.error",
    # Capacity.
    "claim_cap_exhausted": "capacity.claim",
    # FAR-648: the dispatcher_reconcile expired-HITL-gate terminalizer writes
    # the raw code — canonicalized to the ``hitl.gate_expired`` registry entry
    # beside its sibling terminalizer aliases above (never ``harness.unknown``).
    "hitl_gate_expired": "hitl.gate_expired",
    "pipeline_capacity": "capacity.pipeline",
    "org_capacity_limited": _CODE_CAPACITY_ORG,
    "capacity_timeout": "capacity.timeout",
    # FAR-418: scope violation (legacy snake_case spelling canonicalized to
    # scope.violation).
    "scope_violation": _CODE_SCOPE_VIOLATION,
    "ScopeViolationError": _CODE_SCOPE_VIOLATION,
    # FAR-734: model-backend errors detected from retained stdout (snake_case
    # spellings for backward compat — these are written by the stdout scanner
    # and must resolve to the dotted registry entries above, never
    # ``harness.unknown``).  ``model_disabled`` is already a registry key so
    # no alias entry is needed (map_legacy_code passes it through unchanged).
    "model_provider_timeout": "model.provider_timeout",
    "model_connection": "model.connection",
    "model_rate_limited": "model.rate_limited",
}


# ---------------------------------------------------------------------------
# Unmapped-fallback signal (FAR-589 D3b) + map-key uniqueness guard
# ---------------------------------------------------------------------------

# The ``harness.unknown`` fallback choke point emits a DISTINCT, queryable
# signal carrying the unmapped code (usually an exception class name the
# executor's generic catch published). Analytics canonicalizes unknown classes
# away, so the log event is the only place the raw name survives. Emitted once
# per distinct code per process: read surfaces (analytics bucketing, runs list,
# present_error) hit ``map_legacy_code`` per row, so an unconditional warning
# would flood the log — the FIRST occurrence per code per process is the
# signal, and a bounded seen-set keeps memory flat.
_UNMAPPED_CODE_SIGNAL_MAX = 256
_unmapped_code_signal_seen: set[str] = set()
_unmapped_code_signal_lock = threading.Lock()


def _emit_unmapped_code_signal(code: str) -> None:
    """Emit ``harness.unknown.fallback`` once per distinct unmapped code.

    The event extra carries the unmapped code verbatim (the class name a
    map-miss would otherwise lose) so the fallback is queryable and the
    rollbacks' ``unmapped-fallback events > 0`` tripwires have a defined
    signal to read.
    """
    with _unmapped_code_signal_lock:
        if code in _unmapped_code_signal_seen or len(_unmapped_code_signal_seen) >= _UNMAPPED_CODE_SIGNAL_MAX:
            return
        _unmapped_code_signal_seen.add(code)
    _log.warning(
        "harness.unknown.fallback",
        extra={"unmapped_code": code},
    )


def _reset_unmapped_code_signal_for_tests() -> None:
    """Clear the per-process seen-set (test isolation only)."""
    with _unmapped_code_signal_lock:
        _unmapped_code_signal_seen.clear()


def error_code_map_conflicts() -> list[str]:
    """Bare-name uniqueness guard over the registry + alias maps (FAR-589 D3b).

    The registry and alias tables map both dotted codes and BARE names
    (exception class names / legacy snake_case spellings). Python silently
    drops a duplicated literal key at import, and :func:`map_legacy_code`
    checks ``LEGACY_ALIASES`` FIRST — so one bare name claimed by both maps
    with different targets changes meaning depending on lookup order, and a
    bare name must never be silently ambiguous. Returns human-readable
    conflict descriptions; an EMPTY list is a clean map:

    1. a key present in BOTH maps whose canonical resolutions differ
       (the alias silently shadows the registry entry);
    2. an alias key that duplicates a registry key even to the SAME target
       (the bare name is ambiguous about which table owns it);
    3. an alias target that does not resolve through the registry
       (``class_for``/``is_retryable`` would silently degrade to
       ``"unknown"`` / non-retryable).

    The unit-test suite asserts this stays empty on every change to either
    map — a guard test, not a runtime raise (an import-time raise would turn
    a map edit into a fleet-wide import failure).
    """
    conflicts = [
        f"key {key!r} exists in both ERROR_CODE_REGISTRY and LEGACY_ALIASES "
        f"(registry passthrough vs alias target {LEGACY_ALIASES[key]!r})"
        for key in sorted(set(ERROR_CODE_REGISTRY) & set(LEGACY_ALIASES))
    ]
    conflicts.extend(
        f"alias {alias!r} -> {target!r} is not a registry key"
        for alias, target in sorted(LEGACY_ALIASES.items())
        if target not in ERROR_CODE_REGISTRY
    )
    return conflicts


def map_legacy_code(code: str | None) -> str:
    """Map a (legacy or already-dotted) error code to its canonical dotted code.

    Legacy codes are resolved through :data:`LEGACY_ALIASES`; already-dotted
    registry codes pass through unchanged. Unmapped codes fall back to
    ``harness.unknown`` (§3.2) so presentation always has a resolvable code —
    and each FIRST unmapped code per process emits the distinct
    ``harness.unknown.fallback`` signal carrying the raw name (usually the
    exception class name analytics canonicalizes away).
    """
    if not code:
        return _CODE_HARNESS_UNKNOWN
    resolved = LEGACY_ALIASES.get(code)
    if resolved is not None:
        return resolved
    if code in ERROR_CODE_REGISTRY:
        return code
    _emit_unmapped_code_signal(code)
    return _CODE_HARNESS_UNKNOWN


def class_for(code: str | None) -> str:
    """Return the error class tag for a code (``"agent"``, ``"harness"``, ...).

    Unmapped codes resolve through ``harness.unknown`` to the ``harness`` class;
    ``"unknown"`` is returned only if the registry entry itself is missing.
    """
    canonical = map_legacy_code(code)
    spec = ERROR_CODE_REGISTRY.get(canonical)
    if spec is None:
        return "unknown"
    return spec.error_class


def is_retryable(code: str | None) -> bool:
    """Return the registry's default retryability for a code (default False)."""
    canonical = map_legacy_code(code)
    spec = ERROR_CODE_REGISTRY.get(canonical)
    if spec is None:
        return False
    return spec.retryable


def expand_code_variants(code: str) -> set[str]:
    """All raw DB values equivalent to *code* (dotted, legacy, or exception class name).

    The API presents canonical dotted codes while ``runs.error_code`` /
    ``run_daily_facts.error_code`` are written raw (legacy snake_case / class
    names), so a filter must match every spelling that maps to the same
    canonical code.
    """
    canonical = map_legacy_code(code)
    variants = {code, canonical}
    for legacy, dotted in LEGACY_ALIASES.items():
        if dotted == canonical:
            variants.add(legacy)
    return variants


def known_error_codes() -> set[str]:
    """All raw DB code spellings that resolve to a KNOWN canonical code.

    The union of every registry key and every legacy alias. Any raw code NOT in
    this set is exactly what :func:`map_legacy_code` falls back to
    ``harness.unknown`` — i.e. the raw rows the analytics "Unknown error"
    dimension slice shows (``bucket_rows`` canonicalizes unmapped raw codes
    into that slice, and the facts table stores raw codes, never the literal
    dotted aggregate).

    ``harness.unknown`` itself IS in the set — it is a registry key and passes
    through ``map_legacy_code`` unchanged. Consumers that need the EXACT raw
    rows the unknown slice shows must subtract it
    (``known_error_codes() - {"harness.unknown"}``): a raw literal
    ``harness.unknown`` row is bucketed into the unknown slice (registry
    passthrough) and must therefore still match the unknown filter, while
    every other known spelling is excluded.
    """
    return set(ERROR_CODE_REGISTRY) | set(LEGACY_ALIASES)


# ---------------------------------------------------------------------------
# Shared error-text sanitizer + read-surface presenter (run-failure UX)
# ---------------------------------------------------------------------------

# Hard cap BEFORE any regex runs — bounds the ReDoS surface (an attacker who can
# reach error_detail must not be able to feed an unbounded string into the
# pattern engine). ``runs.error_detail`` is ``Text`` (widened from String(5000)
# by migration 0199), so error-detail writes pass ``limit=None``; this default
# still caps every other sanitizer caller (stack traces, context JSON, ...).
_ERROR_DETAIL_HARD_LIMIT = 5000

# Redaction patterns — char-class-only, NO alternations with nested quantifiers
# (the codebase's own (a|b)+ ReDoS lesson). Each pattern is a single anchored
# literal prefix + a flat char class + a flat quantifier, so worst-case work is
# linear in the (capped) input. The AWS-key and GitHub fine-grained-PAT formats
# are sourced from the canonical shared list in
# :mod:`modulo.core.secret_patterns` so the secret-format knowledge is never
# duplicated or drifted across redaction sites.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"),
    # The body class includes ``-`` so the hyphenated vendor segments match:
    # after the ``sk-`` prefix the current OpenAI key shapes run
    # ``sk-proj-…``, ``sk-svcacct-…``, ``sk-None-…`` and OpenRouter runs
    # ``sk-or-v1-…`` — a plain-alphanumeric body stops at the first hyphen
    # and would leak those keys unredacted (FAR-613: persisted briefing
    # content passes through this sanitizer). Short innocuous strings are
    # unaffected: the 8-char minimum still applies to the whole body.
    re.compile(r"sk-[A-Za-z0-9-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk_live_[A-Za-z0-9]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[ousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{8,}"),
    AWS_ACCESS_KEY_PATTERN,
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    # ``r`` (Slack rotateable tokens) aligned with the canonical shared list
    # in :mod:`modulo.core.secret_patterns` (``xox[baprs]-``) — the two
    # sites had drifted apart and a ``xoxr-`` token escaped this sanitizer.
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"://[^:\s@]+:[^@\s@]+@"),
    re.compile(r"secret_[A-Za-z0-9]{16,}"),
    re.compile(r"npm_[A-Za-z0-9]{20,}"),
    GITHUB_PAT_PATTERN,
    # Vendor connector credential formats (FAR-513 defense-in-depth). These
    # mirror the credential values the vendor connectors hold so a token that
    # escapes a connector boundary is still caught by the sanitizer even when
    # the value-based connector redaction was not applied.
    re.compile(r"lin_api_[A-Za-z0-9]{20,}"),
    re.compile(r"(?:DD-API-KEY|DD-APPLICATION-KEY)[\s:=]+[0-9A-Fa-f]{32}"),
    re.compile(r"(?i)n8n[\s_-]*(?:api[\s_-]*)?(?:key|token)[\s:=]+[A-Za-z0-9]{20,}"),
    re.compile(r"u\+[0-9A-Fa-f]{20,}"),
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)sentry[_-]?(?:auth[_-]?token|token|dsn)[\s:=]+[A-Za-z0-9]{32,}"),
)

# Hard control characters (NUL, bell, vertical tab, form feed, C0 except
# \n \t \r, DEL). Printable text, newlines, tabs and carriage returns pass
# through untouched — the sanitizer is a NO-OP for clean strings.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_error_text(text: Any, limit: int | None = _ERROR_DETAIL_HARD_LIMIT) -> str:
    """Control-char strip + secret-pattern redaction for error detail.

    Idempotent and a NO-OP for clean strings (the redacted replacement never
    matches a secret pattern). Input is capped at *limit* code points (default
    :data:`_ERROR_DETAIL_HARD_LIMIT`) BEFORE any regex runs — a ReDoS defense
    that bounds the pattern-engine input. Pass ``limit=None`` to skip the cap
    (used for the ``runs.error_detail`` column, which was widened to ``Text`` by
    migration 0199). Non-str input is coerced via ``str()`` — never raises.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    capped = text if limit is None else text[:limit]
    sanitized = _CONTROL_CHARS.sub("", capped)
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("<redacted>", sanitized)
    return sanitized


def present_error(code: str | None, detail: Any, limit: int) -> tuple[str | None, str | None]:
    """Present one run's error for a read surface (sanitize + truncate).

    * ``code`` is canonicalized to the dotted taxonomy via
      :func:`map_legacy_code` so every read surface presents a resolvable
      dotted code (legacy ``executor_stalled`` → ``agent.stall``, unmapped
      codes → ``harness.unknown``). ``None`` stays ``None`` — a missing code
      is never turned into ``harness.unknown`` (callers rely on error_code
      being absent).
    * ``detail``: ``None`` → ``None``; otherwise :func:`sanitize_error_text`
      then a code-point-safe truncate to *limit* with a ``…`` suffix when cut.
      Python ``str`` slicing never splits a multi-byte character.
    * Never raises on a non-str detail (coerced via ``str()``).

    Returns ``(code, detail)`` ready for the response dict.
    """
    if code is not None:
        code = map_legacy_code(code)
    if detail is None:
        return code, None
    cleaned = sanitize_error_text(detail)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return code, cleaned
