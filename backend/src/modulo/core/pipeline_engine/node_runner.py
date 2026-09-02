"""Factory that builds a cancellable LangGraph node function from a node definition.

Node types:
  - standard (agent):  agent/connector node; runs the node body, then checks for
                        outgoing HITL gate edges (handled externally via
                        intermediate gate nodes).
  - hitl_gate:         intermediate node inserted by build_graph_from_json for
                        every edge that carries a hitl_gate_config.  Calls
                        interrupt(gate_payload) and blocks until a human reviews
                        it, unless the effective autonomy level (from run_context
                        or pipeline default) bypasses the gate.  Also supports
                        conditional gating via a JMESPath ``condition`` on the
                        gate config, and eval-before-interrupt for node-scoped
                        eval definitions.
  - manual:            placeholder node for SDLC modeling.  No AI agent, no
                        connector binding, no model backend required.  The human
                        provides output directly via the HITL review UI.  Output
                        is validated against output_schema_id before the run
                        continues.  A log entry is recorded on completion.
                        Fields required: id, output_schema_id (optional).
                        Fields NOT required: agent_id, connector_binding,
                        model_backend_id.

Autonomy integration:
  - ``manual_approval`` (default):  gate interrupts for human review.
  - ``notify_on_complete``:         gate auto-approves and records an artifact;
                                    no interrupt is raised.
  - ``fully_autonomous``:           gate is silently skipped.
  - ``human_only`` on gate config:  overrides autonomy —  always interrupts.

Conditional gating ((Section 8.17):
  - ``condition`` on ``hitl_gate_config``:  JMESPath expression evaluated
    against the current state (upstream node output).  If falsy the gate is
    skipped.  If truthy or absent the gate proceeds to autonomy checks.

Eval-before-interrupt ((Section 8.17):
  - ``eval_definitions``:  list of ``EvalDefinition`` DTOs scoped to the
    upstream node.  Evaluated *after* the condition check but *before* the
    interrupt.  If any eval with ``failure_behaviour='block'`` fails, an
    ``EvalBlockedError`` is raised instead of a ``GraphInterrupt``.
"""

import asyncio
import base64
import concurrent.futures
import difflib
import hashlib
import json
import logging
import math
import os
import re as _re
import socket
import time
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, TypeGuard

import jinja2
from jinja2.sandbox import SandboxedEnvironment
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from modulo.connectors.base import DEFAULT_ON_UNKNOWN, ON_UNKNOWN_MODES
from modulo.core.secret_patterns import AWS_ACCESS_KEY_PATTERN, GITHUB_PAT_PATTERN

if TYPE_CHECKING:
    from e2b import AsyncSandbox

from modulo.core.capability_scope import filter_run_context_scope
from modulo.core.cost_controller.breakdown.constants import (
    MAX_REPORTABLE_BAND_USD,
    MAX_REPORTABLE_USD_MIN,
)
from modulo.core.cost_controller.breakdown.metrics import record_out_of_band
from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult, EvalType
from modulo.core.guardrails.loop_intercept import LoopInterceptConfig
from modulo.core.node_output_split import (
    DEFAULT_NODE_TYPE,
    SPLITTABLE_NODE_TYPES,
    resolve_node_contract_output,
)
from modulo.core.pipeline_engine.decorator import cancellable_node
from modulo.core.pipeline_engine.errors import RouterNoMatchError
from modulo.core.pipeline_engine.event_broker import RunEventBroker, get_registry
from modulo.core.pipeline_engine.idempotency import (
    node_idempotency_key,
    read_before_write_ambiguous,
    read_before_write_suppression,
)
from modulo.core.pipeline_engine.input_truncation import truncate_input
from modulo.core.pipeline_engine.jmespath_eval import (
    compile_jmespath,
    evaluate_jmespath_condition,
)
from modulo.core.pipeline_engine.sandbox_mode import _validate_sandbox_mode_config
from modulo.core.run_context.autonomy import (
    effective_autonomy_level,
    should_notify_on_complete,
    should_skip_hitl_gate,
)
from modulo.core.run_context.autonomy_telemetry import emit_autonomy_telemetry
from modulo.db.models.eval_result import EvalResult as EvalResultModel
from modulo.db.rls import set_rls_execution_context, set_rls_org

_log = logging.getLogger(__name__)


def _normalize_required_team_id(gate_id: str, raw: Any) -> str | None:
    """Validate a HITL gate's ``required_team_id`` and return a canonical UUID
    string, or None when absent/invalid.

    The executor parses the interrupt payload's ``required_team_id`` with
    ``uuid.UUID(...)``; an unparseable value would raise there and fail the
    run. The config normally arrives UUID-typed via ``HitlGateConfig``
    validation, but a corrupted snapshot / raw-DB gate config can carry an
    arbitrary string — normalise here so the executor never sees an invalid
    value. An invalid value is logged and treated as "no team restriction"
    (the gate degrades to org-wide) rather than raising.
    """
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return str(raw)
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        _log.warning(
            "hitl_gate.invalid_required_team_id",
            extra={"gate_id": gate_id, "required_team_id_raw": str(raw)},
        )
        return None


class SandboxNodeFailedError(Exception):
    """A sandbox-agent node failed due to sandbox infrastructure (retryable).

    Raised for a stall (idle watchdog), a command timeout, or a non-zero exit
    code with no parseable ``output.json``. The executor maps this to the
    retryable path (fenced reset to ``pending`` + SAQ retry) instead of a
    silent wrong-success completion.

    ``node_id`` is carried so the executor's FAR-228 idempotency gate (guard B)
    can resolve which node failed without re-deriving it from the message.
    Omitting ``node_id`` (e.g. ``SandboxNodeFailedError("msg")`` in tests)
    disables guard B — the transient retry proceeds exactly as before.
    """

    def __init__(self, message: str = "", *, node_id: str | None = None) -> None:
        super().__init__(message)
        self.node_id = node_id


class SupersededNodeError(Exception):
    """The sandbox dispatch marker was denied (claim superseded / not running).

    Raised when the DB-atomic dispatch marker UPDATE matched zero rows — a
    successor rotated the run's claim token or the run is no longer running, so
    a sandbox MUST NOT be created. The executor maps this to a terminal
    ``superseded`` failure (a token-guarded no-op if a successor already owns
    the run) — never a completed run with zero work.
    """


class ScriptModeError(Exception):
    """Base for FAR-296 Phase 2 script-mode TERMINAL faults.

    Script mode is exactly-once: once the script PROCESS has started (the
    fencing lease is claimed), re-dispatching could double-execute a
    side-effecting script, so every post-claim fault is TERMINAL — the executor
    must never fenced-reset / retry these. ``SandboxNodeFailedError`` (the
    retryable sandbox-infra failure) is deliberately NOT a parent — the two
    retryability classes must be disjoint so the executor's retry machinery can
    never confuse them.
    """


class ScriptSideEffectUnknownError(ScriptModeError):
    """The script process was terminated mid-execution with exit undetermined.

    Raised on a timeout / budget / watchdog kill while the process is alive or
    its exit is undetermined — the side effect may or may not have happened, so
    this maps to the never-retryable ``script.side_effect_unknown`` code and is
    treated as needs-human.
    """


class ScriptFailedError(ScriptModeError):
    """The script-mode sandbox failed after the process started (post-claim).

    Raised for a non-zero exit / missing output AFTER the script process
    started — maps to the never-retryable ``script.failed`` code. Re-dispatching
    is forbidden (exactly-once).
    """


class ScriptInvalidOutputError(ScriptModeError):
    """The script-mode output was invalid after the process started (post-claim).

    Raised for invalid / oversized / unparseable output.json AFTER the script
    process started — maps to the never-retryable ``script.invalid_output``
    code. Re-dispatching is forbidden (exactly-once).
    """


class ScriptBudgetKilledError(ScriptModeError):
    """The script's sandbox was killed by the platform-side resource-cap
    killer (FAR-296 Phase 3b-3). Post-claim, TERMINAL (never retryable)."""


class SandboxRateLimitedError(SandboxNodeFailedError):
    """E2B rate-limited the sandbox creation (429 / concurrent-sandbox limit).

    Raised for a SINGLE rate-limit event when the retry loop is NOT enabled
    (the legacy path). Subclassing ``SandboxNodeFailedError`` maps it to the
    retryable ``sandbox.no_output_json`` family by default; the executor's
    LEGACY_ALIASES routes ``SandboxRateLimitedError`` to the dedicated
    retryable ``sandbox.rate_limited`` code.
    """


class SandboxQueueTimeoutError(SandboxNodeFailedError):
    """E2B rate-limit retry budget exhausted — sandbox queue timed out (FAR-296 Phase 4b).

    Raised when ``AsyncSandbox.create`` exhausts the bounded retry/backoff loop
    (FAR-296 Phase 4a). The sandbox was NEVER created and the script PROCESS was
    NEVER started, so this is a PRE-CLAIM failure — retryable. Maps to the
    distinct ``sandbox.queue_timeout`` code via the executor's LEGACY_ALIASES,
    separate from the single-event ``sandbox.rate_limited`` code.
    """


class SandboxCapacityExceededError(SandboxNodeFailedError):
    """Org sandbox concurrency cap reached — dispatch-time capacity gate (FAR-296 Phase 4b).

    Raised BEFORE sandbox provisioning when the org's active sandbox lease
    count equals or exceeds the configured cap. The sandbox was NEVER created
    and the script PROCESS was NEVER started, so this is a PRE-CLAIM failure —
    retryable. Maps to ``capacity.org`` via the executor's LEGACY_ALIASES.
    """


def _script_budget_killed_message(node_id: str) -> str:
    """Message for a platform-side budget kill (ScriptBudgetKilledError).

    Covers BOTH kill sources: the resource-cap killer (FAR-296 Phase 3b-3) and
    the wall-clock spend budget killer (FAR-296 Phase 4a).
    """
    return (
        f"Script-mode sandbox exceeded its budget (resource limits or wall-clock spend) "
        f"for node '{node_id}' — killed by platform-side runtime killer"
    )


class OutputSchemaValidationError(ValueError):
    """A node's output failed validation against its output_schema_json.

    Raised by ``_validate_against_schema`` for manual-node resume output and
    agent-node output. The executor maps this to the domain-specific
    ``schema_validation_failure`` error code (§8.9 error table — "The output
    did not match the expected format.") instead of a raw ``ValueError``.
    """


# Single source of truth for JMESPath guard truthiness — `bool(result)`.
# Test-pinned in tests/unit/pipeline_engine/test_conditional_transitions.py.
_is_truthy = bool

# Cap for the stored artifact stdout/stderr blobs. 512KB keeps storage bounded
# while capturing realistic sessions (real runs stream 364KB+; the old 100KB
# cap made every long run look cut mid-JSON). Consumers can tell stored
# truncation from a genuine cut via the stdout_length/stderr_length fields.
_MAX_ARTIFACT_LOG = 512000
_MAX_OTEL_LOG_ATTR = 32768
_MAX_ERROR_MSG = 500

# FAR-197: bounds for the no-output.json diagnostic message. The raised
# SandboxNodeFailedError message must survive the executor's terminal-fail
# surface AFTER retries exhausted — `_sanitize_detail("Sandbox node failed
# (transient) after retries exhausted: " + msg, limit=5000)` — and the
# `runs.error_detail` String(5000) column, so every section stays small and
# the COMBINED message (sections + headers + truncation markers) stays well
# under 5000 chars. Sections are ordered by diagnostic value: the E2B log
# tail (the only place the kill reason lives) FIRST, then the captured agent
# stderr and stdout (agent errors typically sit at the END of stderr), and
# any raw bytes read back from output.json (the invalid-JSON case) LAST.
# Section caps are sized so the whole message stays under the sanitizer's
# hard cap (5000 chars) AND the executor's terminal-fail write surface
# (`_sanitize_detail(..., limit=5000)`) and the `runs.error_detail`
# String(5000) column, so the diagnostic is never truncated away.
_NO_OUTPUT_LOG_TAIL = 1024
_NO_OUTPUT_STDERR_TAIL = 1536
_NO_OUTPUT_STDOUT_TAIL = 1024
_NO_OUTPUT_RAW_SNIPPET = 512

_OUTPUT_READ_TIMEOUT = 30.0  # max seconds to wait for sandbox output after command times out
# FAR-487: the E2B sandbox LIFETIME must outlast the runner's command timeout.
# Both used to be set from the same ``sandbox_timeout`` value, so the platform
# killed the sandbox (``endAt``) at the same instant the runner's command
# timeout fired. In that race the SDK's command event stream closes first and
# ``handle.wait()`` resolves a zero-exit CommandResult (no exit event ever
# arrives — the process died with the sandbox), so the runner took the
# "completed, exit 0" path and then tried to read /home/user/output.json from
# a DEAD sandbox: the read raised, ``output_json`` stayed None, and the node
# failed as "Sandbox agent produced no parseable output.json (exit code 0)" —
# 15+ production PR-Reviewer runs on 2026-08-29. The grace window makes the
# runner's OWN timeout path (clean kill + correct stall/timeout
# classification) win the race deterministically.
# FAR-489: this MUST be an int. The E2B create API (Go) unmarshals
# ``NewSandbox.timeout`` into an int32 — a float payload ("360.0") is
# rejected with HTTP 400 and every sandbox create fails instantly. The
# int() cast at the create call site is the load-bearing guard; keep both.
_SANDBOX_LIFETIME_GRACE_S = 120
_DECORATOR_GRACE = 5.0  # scheduling + finally-block margin for decorator safety net
# FAR-188 (QA round 1): the raw-output retention DB write is bounded to fit
# inside the node decorator's grace budget (_DECORATOR_GRACE = 5.0s) so a hung
# DB fails open with a log BEFORE the safety-net timer can convert the
# retryable SandboxNodeFailedError into a terminal node_timeout.
_RAW_OUTPUT_MARKER_PERSIST_TIMEOUT = 5.0
# FAR-228: bounded fenced SELECT for the idempotency gate's guard-A marker read
# (3s — fail-open to provision normally on a hung DB, never block dispatch).
_IDEMPOTENCY_GATE_READ_TIMEOUT = 3.0
# FAR-228: best-effort marker persist bounded inside a caught CancelledError
# (5s — the node is being cancelled, the write must not delay the re-raise).
_IDEMPOTENCY_GATE_CANCEL_PERSIST_TIMEOUT = 5.0
# FAR-458: the per-connector-per-write ``on_unknown`` modes and default live in
# ONE place — ``modulo.connectors.base`` (a stdlib-only leaf imported by both
# the pipeline engine's gate read and the REST connector's config validation)
# so the mode set can never drift between the two. The default
# ``DEFAULT_ON_UNKNOWN`` (``"fail_open"``) re-fires the write on ambiguity
# (possible duplicate, usually recoverable); ``fail_closed`` SUPPRESSES it
# (possible silent miss; the operator reconciles); ``off`` bypasses the gate
# entirely. A CONFIRMED-delivered write (delivery_done + matching key) is
# ALWAYS suppressed regardless of the mode (that is the point of dedup).
_SANDBOX_IO_TIMEOUT = 30.0  # max seconds for a single sandbox file read/write
_SANDBOX_IDLE_TIMEOUT = 300.0  # max seconds of agent silence before treating the command as stalled (FAR-97)
_STREAM_FLUSH_INTERVAL = 1.0  # min seconds between live stdout/stderr chunk publishes per node (FAR-98)
# FAR-97 pipe-buffer fix: the agent command's stdout/stderr are redirected to a
# log file inside the sandbox so the process can never block on a full stdout
# pipe (a long session emitting >64KB before completion would otherwise stall on
# write). A periodic drain probe reads that file and uses its success as the
# idle watchdog's liveness signal — the sandbox connection — instead of the
# fragile RPC output stream.
_SANDBOX_LOG_PATH = "/home/user/agent.log"
_SANDBOX_TAIL_INTERVAL = 5.0  # seconds between sandbox log drain probes
_SANDBOX_TAIL_READ_TIMEOUT = 10.0  # per-drain probe wait_for timeout
# FAR-296 Phase 3b-3: platform-side resource-cap killer cadence. The killer
# polls sandbox.get_metrics() once every N _tick invocations; _tick runs every
# _SANDBOX_TAIL_INTERVAL (5s), so 6 ticks = a 30s poll cadence.
_SANDBOX_BUDGET_POLL_INTERVAL_TICKS = 6
# Bounds for the killer's get_metrics / kill calls (never let the SDK hang a
# tick forever; the shield keeps SDK internal tasks alive).
_SANDBOX_METRICS_POLL_TIMEOUT = 10.0
_SANDBOX_KILL_TIMEOUT = 15.0
# FAR-212 PR B: per-host DNS resolution bound for the selected-mode egress
# allowlist (the iptables rules bind concrete IPs, never DNS names).
_SANDBOX_EGRESS_RESOLVE_TIMEOUT = 5.0

# FAR-510: the summary stamped on the sandbox_agent synthetic failure
# envelopes (the generic-exception path and the schema-validation path RETURN
# a failed envelope instead of raising). Kept as the human-readable failure
# detail — the executor's downgrade predicate keys on the machine marker
# below, never on this text. Single source of truth so runner and executor
# cannot drift.
SANDBOX_AGENT_FAILED_SUMMARY = "Sandbox agent execution failed"

# FAR-510: machine marker stamped on BOTH sandbox_agent synthetic failure
# envelopes (generic-exception + schema-validation). The executor's
# finalize-time downgrade predicate requires ``status == "failed"`` AND this
# field True — marker-based, so an agent-authored failure shape (which never
# carries the runner's internal marker) can never collide. Single source of
# truth so runner and executor cannot drift.
MODULO_SYNTHETIC_FAILURE_MARKER = "modulo_synthetic_failure"


async def _resolve_egress_allowlist(
    egress_allowlist: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Pre-resolve selected-mode egress allowlist hostnames to IPv4 addresses.

    iptables rules can only match numeric addresses, not DNS names, so node_runner
    resolves each allowlisted host before the policy step builds the rules. The
    resolution is BEST-EFFORT and FAIL-CLOSED: a host that cannot be resolved is
    simply left without a ``_resolved_ip`` key, and the resulting iptables rule
    for it falls through to the raw hostname (which iptables cannot match, so the
    host stays denied). Combined with the ``allow_internet_access=False`` sandbox
    flag, an unresolved host is unreachable — never accidentally opened. Hosts
    that are already numeric are passed through untouched. Entries carry the
    resolved address under ``_resolved_ip`` (never a secret — just a public IP).
    """
    if not egress_allowlist:
        return egress_allowlist

    async def _resolve_one(entry: dict[str, Any]) -> dict[str, Any]:
        host = entry.get("host")
        if not isinstance(host, str) or not host:
            return entry
        if _re.match(r"^[\d.]+$", host) or ":" in host:
            return entry
        try:
            infos = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    None,
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                ),
                timeout=_SANDBOX_EGRESS_RESOLVE_TIMEOUT,
            )
        except Exception:
            # Unresolvable host -> no rule -> host stays denied (fail-closed).
            return entry
        if infos:
            ip = infos[0][4][0]
            return {**entry, "_resolved_ip": ip}
        return entry

    return [await _resolve_one(e) for e in egress_allowlist]


# FAR-296 Phase 4a: E2B concurrent-sandbox rate-limit (429 / resource exhausted)
# retry. ``AsyncSandbox.create`` can be rate-limited by the E2B provisioner; a
# 429 is TRANSIENT. Retry with exponential backoff, bounded by the create-timeout
# window and the node timeout. Exhausting the retries fails RETRYABLY
# (sandbox.rate_limited) — never the permanent ``harness.unknown`` fallback.
_SANDBOX_RATE_LIMIT_MAX_RETRIES = 3
_SANDBOX_RATE_LIMIT_BASE_BACKOFF_S = 5


# The raw returned value is a non-metric Python number (int/float, not bool).
def _is_real_number(value: Any) -> TypeGuard[int | float]:
    """True when *value* is a usable numeric metric (int/float, never bool).

    Guards the resource-cap killer against non-numeric / missing metric
    fields (an SDK or mock that reports None or an absent attribute must not
    be treated as an exceeded cap).
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# D3 drain-probe bound: the E2B files API has NO offset/range read (only
# path/format/user/timeout), so every drain re-transfers the whole log — O(n)
# per tick, O(n^2) total on a long agent run. Bounding the retained/processed
# window to the last _MAX_DRAIN_WINDOW bytes keeps per-tick memory + slicing
# constant (and matches the artifact cap, so nothing is lost that would have
# survived the artifact truncation anyway). Absolute file offsets (from
# get_info size) drive the new-bytes slice, so window truncation can neither
# lose nor double-emit — the emitted chunk is always a suffix of the file.
_MAX_DRAIN_WINDOW = _MAX_ARTIFACT_LOG

# The raw_reported display clamp for the node-output surface: the RAW value
# rides for audit, the SEPARATE clamped display field is what the UI/money
# formatter renders.
_NODE_OUTPUT_DISPLAY_CLAMP = 1e6


def _dispatch_marker_json(attempt_key: str) -> str:
    """Structured ``runs.sandbox_dispatch_state`` value (dist/cleanup-idempotency D5).

    The marker is extended from a bare ``'dispatching'`` literal to
    ``{"state": "dispatching", "attempt_key": "<run:run_id:node:node_id:claim_count>"}``
    so the per-node, per-claim-attempt idempotency key rides on the SAME DB-atomic
    dispatch marker that already fences superseded executors. ``runs.sandbox_id``
    stays in its own column (heartbeat-lost kill path reads it by id).
    """
    return json.dumps({"state": "dispatching", "attempt_key": attempt_key})


def _claim_token_attempt_suffix(claim_lease: str | None) -> str:
    """Fallback attempt-key discriminator when no DB claim_count is available.

    The DB-atomic dispatch path derives the attempt key from ``runs.claim_count``;
    the fail-open path (no session factory / no claim lease) has no run row to
    read, so it derives the discriminator from the claim token instead. The token
    is a fence credential, so only a truncated SHA-256 is surfaced — never the
    token itself — and it still rotates per claim, so attempts are distinguishable.
    """
    if not claim_lease:
        return "claim-unknown"
    return hashlib.sha256(claim_lease.encode("utf-8")).hexdigest()[:16]


def _effective_self_reported_cap() -> float:
    """The per-node clamp ceiling (Settings knob, min-capped at the column cap).

    devtools' ``read_opencode_cost`` uses the CONSTANTS default via this name;
    the backend node_runner clamp is AUTHORITATIVE — the executor re-applies
    the Settings-knob clamp (effective value min-capped at the column cap) when
    it extracts ``model_cost_usd`` from the node output, so a devtools-side
    default drift can never bypass the knob.
    """
    try:
        from modulo.settings import get_settings

        return float(get_settings().effective_max_self_reported_usd)
    except Exception:
        _log.debug("sandbox_cost.self_reported_cap_lookup_failed; using default", exc_info=True)
        from modulo.core.cost_controller.breakdown.constants import MAX_SELF_REPORTED_USD

        return float(MAX_SELF_REPORTED_USD)


def _extract_reported_cost(
    output_json: Any,
    *,
    max_reportable_usd_min: float | None = None,
    max_reportable_band_usd: float | None = None,
    per_node_cap: float | None = None,
) -> tuple[float, float, bool, bool] | None:
    """Tri-state + BAND extraction — the SINGLE extraction authority.

    Returns ``(raw, clamped, was_clamped, out_of_band_high)`` ONLY for a
    POSITIVE finite numeric ``model_cost_usd`` (> 0). ``None`` for absent key,
    non-dict, non-numeric, NaN/Inf, negative, zero, or bool (bool rejected
    explicitly). ``None`` => the key is NOT written.

    The raw input is read from ``model_cost_raw_usd`` WHEN PRESENT (the
    producer's pre-clamp value — devtools writes it), falling back to
    ``model_cost_usd`` for legacy producers. The flags derive from the TRUE raw.

    CLAMP ORDER (pinned): the value is clamped at the per-node cap
    (``_effective_self_reported_cap()``, min-capped at the column cap) AND at
    the BAND CEILING (``MAX_REPORTABLE_BAND_USD`` = 50.0). Because band <
    per-node cap (50 < 10000), ``min(min(raw, cap), band) == min(min(raw,
    band), cap)`` — the final value is IDENTICAL regardless of clamp order.
    ``was_clamped = clamped != raw`` (ANY clamp — band OR per-node);
    ``out_of_band_high = raw > band``.

    SCHEMA-DRIFT FLAG READ AT THE TOP: the devtools-emitted ``schema_drift``
    producer-wire key (the FATAL minimal dict ``{"schema_drift": true}``
    forwarded by write_output) returns ``None`` (no report) when truthy — a
    drifted-schema node reports NO cost. The COUNTER INCREMENT does NOT happen
    here (the provenance gate is evaluated in ``_enrich_union``, PR A2, where
    the frozen node-type map is in scope).
    """
    if not isinstance(output_json, dict):
        return None
    if output_json.get("schema_drift"):
        return None
    val = output_json.get("model_cost_raw_usd")
    if val is None:
        val = output_json.get("model_cost_usd")
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    try:
        val_f = float(val)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(val_f) and val_f > 0):
        return None
    floor = float(max_reportable_usd_min) if max_reportable_usd_min is not None else float(MAX_REPORTABLE_USD_MIN)
    if val_f < floor:
        return None
    raw = val_f
    cap = per_node_cap if per_node_cap is not None else _effective_self_reported_cap()
    clamped = min(raw, cap)
    band = float(max_reportable_band_usd) if max_reportable_band_usd is not None else float(MAX_REPORTABLE_BAND_USD)
    out_of_band_high = False
    if clamped > band:
        clamped = band
        out_of_band_high = True
        record_out_of_band("cost_out_of_band_high")
        _log.warning(
            "cost_components_out_of_band_high",
            extra={"direction": "cost_out_of_band_high", "raw": raw, "clamped": clamped},
        )
    was_clamped = clamped != raw
    return raw, clamped, was_clamped, out_of_band_high


#: Producer output.json ``token_usage`` key -> node-output field. The producer
#: contract (FAR-491) pins ``token_usage: {input, output, total, cache_read?,
#: cache_write?}`` — producer semantics are ``total = input + output`` and the
#: cache keys appear only when the sandbox's opencode.db exposes the columns.
_TOKEN_USAGE_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("model_tokens_input", "input"),
    ("model_tokens_output", "output"),
    ("model_tokens_total", "total"),
    ("model_tokens_cache_read", "cache_read"),
    ("model_tokens_cache_write", "cache_write"),
)


def _build_token_usage_fields(output_json: Any) -> dict[str, Any]:
    """Build the node-output agent-reported token-usage fields (FAR-491).

    Reads ``token_usage`` from the sandbox agent's output.json (the same
    ``cost_source`` the self-reported cost extraction reads) and extracts
    ``model_tokens_input`` / ``model_tokens_output`` / ``model_tokens_total``
    / ``model_tokens_cache_read`` / ``model_tokens_cache_write``. A truthy
    producer ``schema_drift`` flag returns ``{}`` (no report) — a
    drifted-schema node reports NO tokens, mirroring
    ``_extract_reported_cost``. Tri-state per key: absent / non-int / bool /
    negative → the key is OMITTED (never a ``0`` or ``null`` placeholder —
    mirrors ``_build_model_cost_fields``). A valid ``0`` report is a real
    report and IS written. These fields are DISPLAY-ONLY: they feed
    ``node_telemetry_json`` and the union's ``reported_*`` analytics fields,
    never an input to the system's built-in money math (operator-defined
    formulas may reference them).
    """
    if not isinstance(output_json, dict):
        return {}
    if output_json.get("schema_drift"):
        return {}
    usage = output_json.get("token_usage")
    if not isinstance(usage, dict):
        return {}
    fields: dict[str, Any] = {}
    for field_name, usage_key in _TOKEN_USAGE_FIELD_MAP:
        value = usage.get(usage_key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 0:
            continue
        fields[field_name] = value
    return fields


def _build_model_cost_fields(output_json: Any) -> dict[str, Any]:
    """Build the node-output model-cost fields (audit + display + flags).

    Returns an EMPTY dict when the node carries no report (the keys are ABSENT
    — ``0.0`` is NEVER written as a report). When a report exists the fields
    are: ``model_cost_usd`` (clamped), ``model_cost_raw_usd`` (pre-clamp, for
    audit), ``model_cost_display_usd`` (clamped-at-1e6 — the UI/money formatter
    renders THIS field, so the raw value never reaches the money path),
    ``model_cost_clamped`` and ``model_cost_out_of_band_high`` (BOTH written
    UNCONDITIONALLY — true/false explicitly, derived from the TRUE raw so a
    legacy or hostile marker already on the node output can never survive).
    """
    extracted = _extract_reported_cost(output_json)
    if extracted is None:
        return {}
    raw, clamped, was_clamped, out_of_band_high = extracted
    display = min(clamped, _NODE_OUTPUT_DISPLAY_CLAMP)
    return {
        "model_cost_usd": clamped,
        "model_cost_raw_usd": raw,
        "model_cost_display_usd": display,
        "model_cost_clamped": was_clamped,
        "model_cost_out_of_band_high": out_of_band_high,
    }


# Per-run agent runtime cost: E2B sandbox hourly rate (USD) used to estimate
# sandbox_agent node cost from wall-clock time. E2B bills per-second sandbox
# uptime, so (elapsed_seconds / 3600) x rate is a faithful cost estimate.
# Default reflects the dashboard-confirmed opencode template = 2 vCPU / 2 GiB
# at E2B per-second rates (~$0.133/hr). Operators can override via
# E2B_SANDBOX_USD_PER_HOUR. This fallback only applies when settings
# cannot be imported; keep it in sync with settings.py's
# `e2b_sandbox_usd_per_hour` default.
_E2B_SANDBOX_USD_PER_HOUR = 0.13
try:
    from modulo.settings import get_settings

    _E2B_SANDBOX_USD_PER_HOUR = float(get_settings().e2b_sandbox_usd_per_hour)
except Exception:
    _log.debug("sandbox_cost.e2b_rate_lookup_failed; using default", exc_info=True)


def _e2b_rate_runtime() -> float:
    """The E2B hourly rate read at RUNTIME via ``get_settings()`` (§3.3).

    Routing the rate through ``get_settings()`` at RUNTIME (instead of the
    import-time read) is a REAL code change: an env override of
    ``E2B_SANDBOX_USD_PER_HOUR`` must move the boundary everywhere — including
    this legacy fallback path — without a process restart. Falls back to the
    module default when Settings is unavailable (never raises).
    """
    try:
        from modulo.settings import get_settings

        return float(get_settings().e2b_sandbox_usd_per_hour)
    except Exception:
        _log.debug("sandbox_cost.e2b_rate_runtime_lookup_failed; using default", exc_info=True)
        return _E2B_SANDBOX_USD_PER_HOUR


def _compute_sandbox_cost(elapsed_seconds: float, output_json: Any) -> float:
    """Estimate the USD cost of a sandbox_agent dispatch.

    Combines Modulo's own sandbox uptime estimate (wall-clock seconds at the
    RUNTIME Settings E2B hourly rate) with the agent's self-reported cost
    estimate (``cost_estimate_usd`` in its structured output contract, written
    by the agent to /home/user/output.json). Non-finite estimates (NaN/inf) are
    discarded. Returns a plain JSON-serialisable float.
    """
    rate = _e2b_rate_runtime()
    sandbox_cost = round((elapsed_seconds / 3600.0) * rate, 6)
    agent_reported_cost = 0.0
    if isinstance(output_json, dict):
        try:
            agent_reported_cost = float(output_json.get("cost_estimate_usd") or 0)
        except (TypeError, ValueError):
            agent_reported_cost = 0.0
        if not math.isfinite(agent_reported_cost):
            agent_reported_cost = 0.0
    total = sandbox_cost + agent_reported_cost
    if not math.isfinite(total):
        return 0.0
    return round(total, 6)


async def _fetch_sandbox_log_tail(sandbox_id: str | None, limit: int = 60) -> str:
    """Fetch the tail of an E2B sandbox's logs — the only place the kill reason lives.

    Uses GET https://api.e2b.app/sandboxes/{sandbox_id}/logs?limit={limit} with
    header X-API-KEY: <MODULO_E2B_API_KEY or E2B_API_KEY>. Returns a bounded
    string (last ~limit log lines) or "" if unavailable/disabled. Never raises.
    """
    if not isinstance(sandbox_id, str) or not sandbox_id:
        return ""
    api_key = os.environ.get("MODULO_E2B_API_KEY") or os.environ.get("E2B_API_KEY")
    if not api_key:
        return ""

    def _fetch_bytes() -> bytes:
        _req = urllib.request.Request(
            f"https://api.e2b.app/sandboxes/{sandbox_id}/logs?limit={limit}",
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
        )
        # URL is a hard-coded https endpoint, not caller-controlled.
        with urllib.request.urlopen(_req, timeout=8) as _resp:  # noqa: S310  # nosec B310
            return bytes(_resp.read())

    try:
        raw = (await asyncio.to_thread(_fetch_bytes)).decode("utf-8", errors="replace")
    except Exception:
        return ""
    try:
        payload = json.loads(raw)
        entries = payload.get("logEntries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return raw[:4000]
        combined = _combine_log_entries(entries, limit)
        return "\n".join(combined)[-6000:]
    except Exception:
        return raw[:4000]


def _combine_log_entries(entries: list[Any], limit: int) -> list[str]:
    """Split E2B log entries into preferred-level and rest, then tail the union.

    Entries at informative levels (info/warn/warning/error) sort ahead of the
    remainder so the most actionable lines survive the ``limit`` window.
    """
    preferred: list[str] = []
    rest: list[str] = []
    preferred_levels = {"info", "warn", "warning", "error"}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = _log_entry_text(entry)
        if not text:
            continue
        if isinstance(entry.get("level"), str) and entry["level"].lower() in preferred_levels:
            preferred.append(text)
        else:
            rest.append(text)
    return (preferred + rest)[-limit:]


def _log_entry_text(entry: dict[str, Any]) -> str:
    """Extract the human-readable text of one E2B log entry."""
    msg = entry.get("message")
    if msg is None:
        msg = entry.get("fields")
    if not msg:
        return ""
    return str(msg)


def _bounded_tail(text: str, limit: int) -> str:
    """Return the last ``limit`` chars of *text* with a clear truncation marker.

    Empty input yields "" (no marker); a short tail reads verbatim. When cut,
    a marker line naming how many chars were dropped precedes the retained
    suffix so consumers can tell truncation from a genuine short tail.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"...[truncated {len(text) - limit} chars]...\n{text[-limit:]}"


def _classify_no_output_cause(*, read_error: str, read_raw: str) -> str:
    """Name WHY output.json was unparseable (FAR-487).

    The runner reads AND parses output.json inside ONE try block, so a JSON
    parse failure surfaces here as a ``JSONDecodeError`` read error with the
    raw bytes still captured. Classification order: a parse failure of bytes
    that were read (invalid JSON), a missing-file read error, any other read
    failure (dead sandbox, network), a successful read whose bytes parse to
    JSON null. The schema-rejection case is a DIFFERENT code path (validated
    dicts) and is not classified here. Returns "" when the evidence cannot
    classify.
    """
    if read_error:
        if "JSONDecodeError" in read_error:
            return "output.json was written but is NOT valid JSON"
        if "NotFound" in read_error or "FileNotFound" in read_error:
            return "output.json was MISSING (the agent never wrote it)"
        return f"output.json could not be read ({read_error[:200]})"
    if read_raw:
        try:
            parsed = json.loads(read_raw)
        except (ValueError, TypeError):
            return "output.json was written but is NOT valid JSON"
        if parsed is None:
            return "output.json was JSON null"
    return ""


def _build_no_output_message(
    *,
    exit_code: int,
    stdout_raw: str,
    stderr_raw: str,
    sandbox_id: str | None,
    read_raw: str = "",
    read_error: str = "",
    log_tail: str = "",
) -> str:
    """Compose the FAR-197 diagnostic for an unparseable/missing output.json.

    A compact, bounded message: a prefix naming the exit code and sandbox id,
    then a cause line naming WHY the output was unparseable (missing file /
    unreadable / invalid JSON — FAR-487), then bounded tails ordered by
    diagnostic value — the best-effort E2B log
    tail FIRST (the only place the kill reason lives), then the captured agent
    stderr and stdout (agent errors typically sit at the END of stderr), and
    any raw bytes read back from output.json (the invalid-JSON case) LAST.
    Section caps are sized so the whole message stays under the sanitizer's
    hard cap (5000 chars) AND the executor's terminal-fail write surface
    (`_sanitize_detail(..., limit=5000)`) and the `runs.error_detail`
    String(5000) column, so the diagnostic is never truncated away.
    Downstream error-detail sanitization (``sanitize_error_text``) strips control
    chars and redacts secrets; this just keeps the WHY visible within the
    sanitizer's hard cap.
    """
    stdout_raw = str(stdout_raw)
    stderr_raw = str(stderr_raw)
    read_raw = str(read_raw)
    log_tail = str(log_tail)

    parts = [f"Sandbox agent produced no parseable output.json (exit code {exit_code})"]
    cause = _classify_no_output_cause(read_error=read_error, read_raw=read_raw)
    if cause:
        parts.append(cause)
    if isinstance(sandbox_id, str) and sandbox_id:
        parts.append(f"sandbox id: {sandbox_id}")
    log_tail_cap = _bounded_tail(log_tail, _NO_OUTPUT_LOG_TAIL)
    if log_tail_cap:
        parts.append("--- sandbox log tail ---")
        parts.append(log_tail_cap)
    stderr_tail = _bounded_tail(stderr_raw, _NO_OUTPUT_STDERR_TAIL)
    if stderr_tail:
        parts.append("--- stderr tail ---")
        parts.append(stderr_tail)
    stdout_tail = _bounded_tail(stdout_raw, _NO_OUTPUT_STDOUT_TAIL)
    if stdout_tail:
        parts.append("--- stdout tail ---")
        parts.append(stdout_tail)
    read_snippet = _bounded_tail(read_raw, _NO_OUTPUT_RAW_SNIPPET)
    if read_snippet:
        parts.append(f"--- output.json read ({len(read_raw)} chars) ---")
        parts.append(read_snippet)

    return "\n".join(parts)


_PR_URL_PATTERN = _re.compile(r"https?://github\.com/[A-Za-z\d_.-]+/[A-Za-z\d_.-]+/pull/\d+")

# Credential redaction for retained raw output (FAR-188 QA round 2): sandbox
# commands run with OPENCODE_API_KEY and GITHUB_TOKEN (a PAT) injected, and
# agent ``git push``/``git clone``/``gh`` output routinely embeds tokenized
# URLs like ``https://x-access-token:<PAT>@github.com/...``. The stored
# ``raw_output`` field is scrubbed BEFORE persistence so credentials never
# enter it unmasked. ``_TOKENIZED_GIT_URL_PATTERN`` strips the userinfo from
# any ``http(s)://...@host`` URL; ``_TOKEN_VALUE_PATTERN`` defensively masks
# bare token values that follow known credential labels.
_TOKENIZED_GIT_URL_PATTERN = _re.compile(r"(https?://)[^@\s/]+@")
# Label-based masking (covers short tokens including github_pat_ <50 chars)
# plus the canonical bare-value patterns for fine-grained GitHub PATs and AWS
# access keys, shared from the sensitive_mask canonical list.
_TOKEN_VALUE_PATTERN = _re.compile(
    r"(x-access-token:|gh[pous]_|github_pat_|Bearer\s+|token=)[^\s\"'<>]+"
    r"|" + GITHUB_PAT_PATTERN.pattern + r"|" + AWS_ACCESS_KEY_PATTERN.pattern
)


def _extract_pr_url(raw_text: str) -> str:
    """Best-effort GitHub PR URL extraction from raw sandbox output.

    FAR-188: when ``output.json`` fails to parse, the run record retains the RAW
    content so a ``pr_url`` the agent created inside the sandbox is never lost.
    Classification (FAR-189) reads the marker directly instead of re-parsing.
    """
    if not raw_text:
        return ""
    match = _PR_URL_PATTERN.search(raw_text)
    return match.group(0) if match else ""


def _redact_raw_output(raw_text: str) -> str:
    """Best-effort scrub of credentials from retained raw sandbox output.

    FAR-188 (QA round 2): the stored ``raw_output`` marker field must never
    contain live credentials. Both tokenized git URLs (scheme preserved, e.g.
    ``https://x-access-token:<PAT>@github.com/...`` → ``https://<redacted>@github.com/...``)
    and bare token values following known credential labels (``x-access-token:``,
    ``ghp_``/``gho_``/``ghu_``/``ghs_``, ``github_pat_``, ``Bearer ``,
    ``token=``) are masked. NEVER raises: a redaction failure returns the
    original text so retention is never blocked (best-effort, fail open).
    """
    if not isinstance(raw_text, str) or not raw_text:
        return raw_text
    try:
        scrubbed = _TOKENIZED_GIT_URL_PATTERN.sub(r"\1<redacted>@", raw_text)
        return _TOKEN_VALUE_PATTERN.sub(r"\1<redacted>", scrubbed)
    except (_re.error, TypeError, ValueError):
        _log.warning("sandbox_agent.raw_output_redact_failed")
        return raw_text


def _normalize_marker_text(raw: Any) -> str:
    """Normalize a raw-output source to text — bytes decoded exactly once."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


# FAR-228: the delivery sentinel must match as a FULL line, never a substring
# (a mid-line occurrence in unrelated log prose must not fabricate a delivery).
# ``^<sentinel>\r?$`` with MULTILINE + re.escape — the sentinel is a literal the
# pipeline author chose, not a pattern. Compiled once at module load.
def _compile_delivery_sentinel_pattern(sentinel: str | None) -> _re.Pattern[str] | None:
    """Compile the full-line sentinel matcher for *sentinel* (or None)."""
    if not sentinel or not isinstance(sentinel, str):
        return None
    try:
        return _re.compile(rf"^{_re.escape(sentinel)}\r?$", _re.MULTILINE)
    except (_re.error, TypeError):
        _log.warning("sandbox_agent.delivery_sentinel_pattern_failed")
        return None


def _uncancel_current_task() -> None:
    """Clear one pending cancellation on the CURRENT task (FAR-228).

    ``uncancel()`` is a ``Task`` method, never a module-level function —
    ``asyncio.uncancel()`` raises ``AttributeError``. Must be called from
    within the task being uncancelled (the running task is the only one whose
    pending-cancellation count we are permitted to mutate).
    """
    task = asyncio.current_task()
    if task is not None:
        task.uncancel()


def _source_contains_delivery_sentinel(text: Any, sentinel: str | None) -> bool:
    """True when *text* contains *sentinel* as a FULL LINE (FAR-228).

    Full-line match only — ``re.search(r'^<sentinel>\r?$', text, re.M)``. A
    mid-line occurrence (e.g. the sentinel embedded inside a JSON summary or a
    log line) never counts, so the gate cannot be tripped by incidental prose.
    Never raises on non-str input (coerced via ``_normalize_marker_text``).
    """
    pattern = _compile_delivery_sentinel_pattern(sentinel)
    if pattern is None:
        return False
    return pattern.search(_normalize_marker_text(text)) is not None


def _marker_delivery_done_for_node(markers: Any, run_id: Any, node_id: str) -> bool:
    """True when any raw-output marker for ``(run_id, node_id)`` carries
    ``delivery_done is True`` (FAR-228 — shared by guard A in the node body and
    guard B in the executor).

    The attempt_key is ``run:{run_id}:node:{node_id}:{claim_count}``. Matching
    uses the delimited fragments ``run:{run_id}:`` (the key PREFIX) and
    ``:node:{node_id}:`` (mid-key, trailing ``:``) so ``run-1`` never matches
    ``run-11`` and ``node-a`` never matches ``node-a11`` (delimiter trap).
    Non-dict markers / non-dict ``markers`` are ignored.
    """
    if not isinstance(markers, dict):
        return False
    run_tag = f"run:{run_id}:"
    node_tag = f":node:{node_id}:"
    for marker in markers.values():
        if not isinstance(marker, dict) or marker.get("delivery_done") is not True:
            continue
        key = marker.get("attempt_key")
        if isinstance(key, str) and run_tag in key and node_tag in key:
            return True
    return False


async def _retain_raw_output_marker(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    attempt_key: str | None,
    summary: str,
    source: Any,
    parse_error: str,
    exit_code: int,
    stdout_length: int,
    stderr_length: int,
    delivery_sentinel: str | None = None,
    status: str = "failed",
    index: int | str | None = None,
    payload: str | bytes | None = None,
) -> None:
    """Single builder + persist for a raw-output retention marker (FAR-188).

    Both failure branches (no-parseable output.json and stall/timeout) funnel
    through this ONE helper so the marker shape cannot drift between them.
    *status* defaults to ``"failed"`` (the failure branches); the FAR-228
    success path passes ``"completed"`` so a delivery_done marker written for a
    successful run records its real status, never a misleading failure.

    ``source`` is the FULL pre-truncation evidence: for the no-parseable branch
    it is the union of the file content AND the captured stdout; for the
    stall/timeout branch it is whatever raw source is available (the drained
    tail, which the drain window bounds to the last ``_MAX_DRAIN_WINDOW``
    (512KB) bytes — the retained ``raw_output`` therefore reflects that
    bounded tail, never a multi-MB log). Bytes are decoded exactly once;
    ``pr_url`` is extracted from the FULL UNREDACTED source, then the stored
    ``raw_output`` copy is scrubbed of credentials (``_redact_raw_output``)
    and ONLY THEN truncated to ``_MAX_ARTIFACT_LOG`` for storage.

    FAR-228: when *delivery_sentinel* is non-empty AND the pre-truncation
    ``source`` contains the sentinel as a FULL LINE, ``delivery_done: True`` is
    stamped onto the marker — the run's side-effecting delivery (e.g. an email)
    already happened even though the node is failing/retrying. The persist is
    monotone at the same-key write (see ``_persist_raw_output_marker``): a
    retry marker without the sentinel never unsets an existing
    ``delivery_done``.

    ``index`` / ``payload`` (FAR-438) are threaded through to the persisted
    marker's ``idempotency_key`` derivation so a fan-out cardinality position
    and a content-version payload are folded into the key — the SAME arguments
    the executor's suppression read passes, so both sides compute the identical
    key. The sandbox node body call sites pass ``None`` (a single node has no
    separate fan-out item / content-edit payload here; the node_id already
    encodes ``parent+index`` for fan-out children).
    """
    text = _normalize_marker_text(source)
    marker: dict[str, Any] = {
        "_modulo_marker": True,
        "status": status,
        "summary": summary,
        "raw_output": _redact_raw_output(text)[:_MAX_ARTIFACT_LOG],
        "parse_error": parse_error,
        "pr_url": _extract_pr_url(text),
        "exit_code": exit_code,
        "stdout_length": stdout_length,
        "stderr_length": stderr_length,
        "attempt_key": attempt_key,
        "node_id": node_id,
    }
    if _source_contains_delivery_sentinel(text, delivery_sentinel):
        marker["delivery_done"] = True
    await _persist_raw_output_marker(
        session_factory,
        run_id=run_id,
        org_id_raw=org_id_raw,
        node_id=node_id,
        attempt_key=attempt_key,
        marker=marker,
        index=index,
        payload=payload,
    )


async def _persist_raw_output_marker(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    attempt_key: str | None,
    marker: dict[str, Any],
    index: int | str | None = None,
    payload: str | bytes | None = None,
    promote_newest_key: bool = False,
    preserve_delivery_done: bool = True,
) -> bool:
    """Best-effort persist of a raw-output retention marker onto ``runs.raw_output_markers``.

    FAR-188 (QA round 1): the marker lives in a DEDICATED column keyed by
    ``attempt_key`` — NEVER in ``outputs_json`` / ``node_telemetry_json``. This
    keeps the Agent Return Contract columns clean: the node-output endpoint can
    never serve raw stdout, ``recover_node``'s already-completed guard never
    sees a fake completed node, and finalize's split-output machinery never
    touches the marker.

    Keyed by ``attempt_key`` (not ``node_id``) so a retry that re-executes the
    same node does not clobber the previous attempt's evidence: if an existing
    marker for the SAME attempt_key already carries a non-empty ``pr_url`` it is
    preserved (a retry's empty pr_url never wipes attempt-1's evidence).

    The whole session body is bounded by ``asyncio.wait_for(..., timeout=
    _RAW_OUTPUT_MARKER_PERSIST_TIMEOUT)``: a hung DB fails open with a log
    BEFORE the node decorator grace budget is consumed, so the retryable
    ``SandboxNodeFailedError`` survives instead of being replaced by a terminal
    ``node_timeout``.

    NEVER raises (except cancellation): a persistence failure must not block the
    node's retryable raise — run terminalization depends on it.

    DURABILITY GAP (known, documented, FAR-438): because the persist is
    best-effort and swallows failures (a timeout or an exception is logged via
    ``sandbox_agent.raw_output_marker_persist_timeout_or_error`` and the run
    proceeds), the read-before-write dedupe's evidence (the ``delivery_done``
    sentinel + the stamped ``idempotency_key``) can be LOST on a DB hiccup. If a
    delivery genuinely happened but its marker was not persisted, the next
    transient retry will NOT see ``delivery_done`` and will re-fire — a
    potential double-write. This is a deliberate trade (failing open to preserve
    the retryable raise beats blocking terminalization), but it means the
    idempotency gate is best-effort, not exactly-once, under DB failure.
    """
    if session_factory is None:
        _log.warning(
            "sandbox_agent.raw_output_marker_skip_no_session",
            extra={"run_id": run_id, "node_id": node_id},
        )
        return False
    if not run_id:
        _log.warning("sandbox_agent.raw_output_marker_skip_no_run_id", extra={"node_id": node_id})
        return False
    org_uuid: uuid.UUID | None = None
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        _log.warning(
            "sandbox_agent.raw_output_marker_skip_unparseable_org",
            extra={"run_id": run_id, "node_id": node_id},
        )
        return False

    try:
        await asyncio.wait_for(
            _write_raw_output_marker(
                session_factory,
                org_uuid=org_uuid,
                run_id=run_id,
                node_id=node_id,
                attempt_key=attempt_key,
                marker=marker,
                index=index,
                payload=payload,
                promote_newest_key=promote_newest_key,
                preserve_delivery_done=preserve_delivery_done,
            ),
            timeout=_RAW_OUTPUT_MARKER_PERSIST_TIMEOUT,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "sandbox_agent.raw_output_marker_persist_timeout_or_error",
            extra={"run_id": run_id, "node_id": node_id, "attempt_key": attempt_key},
        )
        return False
    return True


async def _write_raw_output_marker(
    session_factory: Callable[..., Any],
    *,
    org_uuid: uuid.UUID | None,
    run_id: str,
    node_id: str,
    attempt_key: str | None,
    marker: dict[str, Any],
    index: int | str | None = None,
    payload: str | bytes | None = None,
    promote_newest_key: bool = False,
    preserve_delivery_done: bool = True,
) -> None:
    """Bounded persist of a single raw-output retention marker row.

    Imports live INSIDE the try so a first-call import failure is
    swallowed+logged like any other persist failure — it must never
    propagate and convert the retryable raise into a generic failed node.
    """
    from sqlalchemy import select as _sql_select

    from modulo.db.models.run import Run as _RunModel

    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await set_rls_execution_context(session)
            run = (
                await session.execute(_sql_select(_RunModel).where(_RunModel.id == run_id).with_for_update())
            ).scalar_one_or_none()
            if run is None:
                _log.warning(
                    "sandbox_agent.raw_output_marker_skip_row_not_found",
                    extra={
                        "run_id": run_id,
                        "node_id": node_id,
                        "hint": "row missing or RLS-hidden by another org",
                    },
                )
                return
            markers = dict(run.raw_output_markers) if isinstance(run.raw_output_markers, dict) else {}
            key = attempt_key or f"run:{run_id}:node:{node_id}:fallback"
            # FAR-438 read-before-write: stamp the derived per-node idempotency key
            # (from the run's PERSISTED run-level key) so a re-run that reuses the
            # same key can suppress a duplicate write. Fail-open — a missing or
            # malformed persisted key simply stamps nothing. Monotone: setdefault
            # never downgrades an already-applied marker's key. ``index`` and
            # ``payload`` are threaded into the derivation so fan-out cardinality
            # and content-version keys are computed consistently with the
            # suppression read (executor ``_idempotency_gate_ok``) — the same
            # node_id at a different `index`, or the same node_id with an edited
            # `payload`, must derive a DIFFERENT key.
            run_ref = run.idempotency_key if hasattr(run, "idempotency_key") else None
            if run_ref:
                with suppress(TypeError, ValueError):
                    derived = node_idempotency_key(run_ref, node_id, index=index, payload=payload)
                    if promote_newest_key:
                        # CONNECTOR path (FAR-458): a content-edit re-run that
                        # delivers a NEWER payload must promote that newest
                        # derived key, so the latest delivery is independently
                        # suppressible and a superseded content-version is not.
                        marker["idempotency_key"] = derived
                    else:
                        marker.setdefault("idempotency_key", derived)
            persisted_marker = _merge_existing_raw_output_marker(
                marker,
                markers.get(key),
                promote_newest_key=promote_newest_key,
                preserve_delivery_done=preserve_delivery_done,
            )
            markers[key] = persisted_marker
            run.raw_output_markers = markers
            await session.flush()
            _log.info(
                "sandbox_agent.raw_output_marker_persisted",
                extra={
                    "run_id": run_id,
                    "node_id": node_id,
                    "attempt_key": key,
                    "retained_bytes": len(persisted_marker.get("raw_output") or ""),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "sandbox_agent.raw_output_marker_persist_failed",
            extra={"run_id": run_id, "node_id": node_id, "attempt_key": attempt_key},
        )


def _merge_existing_raw_output_marker(
    marker: dict[str, Any], existing: Any, *, promote_newest_key: bool = False, preserve_delivery_done: bool = True
) -> dict[str, Any]:
    """Monotone preservation: a prior attempt's evidence is never wiped by a retry.

    A prior marker's non-empty ``pr_url`` and an OR'd ``delivery_done`` are
    retained; all other marker fields come from the new ``marker`` unchanged
    (pr_url is preserved as-is so a retry's empty pr_url never wipes
    attempt-1's evidence).

    ``promote_newest_key`` (FAR-458 connector path) inverts the
    ``idempotency_key`` handling: instead of pinning the marker to the first /
    existing derived key, the NEWEST delivered content-version's key wins. This
    is what lets a content-edit re-run promote a fresh key (so the edited
    delivery is independently suppressible) rather than being pinned to a
    superseded content-version's key (which would re-fire the edited payload as
    an un-deduped double-submit).

    ``preserve_delivery_done`` (FAR-531) — ``False`` for the connector INTENT
    and definite-NO-DELIVERY writes: those markers describe a write that has
    NOT (yet) been confirmed delivered, so inherited delivery evidence from a
    SUPERSEDED key's marker must not bleed in. Without this, an intent marker
    written for a NEW content-version (different derived key, same slot) would
    merge with the previous key's ``delivery_done: True`` row and claim the NEW
    key was already delivered BEFORE the write fired — a fail_closed-relevant
    silent miss (the new write's crash would look like a confirmed delivery and
    suppress its legitimate re-fire). The delivery stamp itself keeps the OR
    (it only fires after a genuine delivery, where the new marker already
    carries ``delivery_done: True`` itself).

    QA Fix 2 (FAR-531): the drop is keyed on IDENTITY — delivery evidence is
    dropped ONLY when the incoming marker carries a DIFFERENT derived key than
    the existing marker (the superseded content-version case above). When the
    keys MATCH, ``delivery_done`` is still OR'd even under
    ``preserve_delivery_done=False``: a same-key intent / no-delivery persist
    arriving AFTER a confirmed delivery stamp (concurrent-attempt window, or a
    brownout re-run whose gate read timed out but whose persist succeeded) must
    not WIPE the delivered evidence — wiping it let a later attempt re-fire a
    write that had already delivered (a duplicate). Delivered evidence for the
    same key is monotone: the gate suppresses on ``delivery_done`` + matching
    key regardless of which kind of marker shares the slot.
    """
    if not isinstance(existing, dict):
        return marker
    preserved: dict[str, Any] = {}
    if existing.get("pr_url"):
        preserved["pr_url"] = existing["pr_url"]
    marker_key = marker.get("idempotency_key")
    same_derived_key = marker_key is not None and existing.get("idempotency_key") == marker_key
    if (preserve_delivery_done or same_derived_key) and (existing.get("delivery_done") or marker.get("delivery_done")):
        preserved["delivery_done"] = True
    if promote_newest_key:
        if marker.get("idempotency_key"):
            preserved["idempotency_key"] = marker["idempotency_key"]
    elif existing.get("idempotency_key"):
        preserved["idempotency_key"] = existing["idempotency_key"]
    merged = dict(marker)
    merged.update(preserved)
    return merged


def _idempotency_gate_skipped_envelope(
    node_id: str, *, gate_tag: str = "email_sent", delivered: bool = True
) -> dict[str, Any]:
    """FAR-228: the single artifact envelope produced by BOTH guards.

    The ``output_json`` sub-key is REQUIRED so ``_split_sandbox_agent`` returns
    a proper dict into ``outputs_json[node_id]`` (not None); the skip marker
    makes ``_node_output_has_valid_artifact`` count it, so ``work_intact``
    computes True for the single-node gated run; and ``idempotency_gate`` is
    what suppresses agent_signal re-firing (NEVER ``status == "skipped"`` —
    template-error skips fire today).

    ``gate_tag`` (FAR-458) is the driver-readable reason recorded under
    ``idempotency_gate`` — the sandbox path defaults to ``"email_sent"`` (the
    FAR-228 delivery sentinel); a connector node that suppressed a duplicate
    write passes ``"connector_write_suppressed"`` so observability shows the
    real cause rather than a misleading email tag. Any non-empty tag works:
    ``_node_output_has_idempotency_gate`` only checks truthiness.

    ``delivered`` (FAR-531 AC4 — envelope honesty): ``True`` only when the
    suppression reason is a CONFIRMED prior delivery (the sandbox sentinel and
    the ``connector_write_suppressed`` dedup tag). The fail-closed AMBIGUOUS
    suppression (``"connector_write_fail_closed"``) suppressed a write whose
    delivery is UNKNOWN — claiming ``delivery_done: True`` there would
    misreport a suppressed-never-fired write as delivered, so it passes
    ``delivered=False``. Suppressed ≠ delivered.
    """
    return {
        "artifacts": [
            {
                "node_id": node_id,
                "status": "skipped",
                "output": {
                    "output_json": {
                        "status": "skipped",
                        "delivery_done": delivered,
                        "idempotency_gate": gate_tag,
                    }
                },
            }
        ]
    }


async def _read_run_raw_output_markers_for_gate(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    claim_lease: str | None,
    node_id: str,
) -> dict[str, Any] | None:
    """FAR-228 guard A: SINGLE fenced read of ``runs.raw_output_markers``.

    Bounded by ``_IDEMPOTENCY_GATE_READ_TIMEOUT`` (3s); fail-open to ``None``
    (provision normally) on any failure — the gate must never block dispatch.
    Fenced on the claim token + ``status='running'`` exactly like the dispatch
    marker (``_acquire_dispatch_marker``) so a superseded executor never reads
    a successor's markers as its own. This is a SEPARATE read from the atomic
    dispatch marker (A4) — do NOT fuse them.
    """
    if session_factory is None or not claim_lease:
        return None
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return None
    from sqlalchemy import text as _sql_text

    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async def _read() -> dict[str, Any] | None:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await set_rls_execution_context(session)
            row = (
                await session.execute(
                    _sql_text(
                        "SELECT raw_output_markers FROM runs WHERE id=:rid AND organisation_id=:oid "
                        "AND claim_token=:tok AND status='running'"
                    ),
                    {"rid": run_id, "oid": str(org_uuid), "tok": claim_lease},
                )
            ).fetchone()
            if row is None:
                return None
            value = row[0]
            return value if isinstance(value, dict) else None

    try:
        return await asyncio.wait_for(_read(), timeout=_IDEMPOTENCY_GATE_READ_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "sandbox_agent.idempotency_gate_read_failed",
            extra={"node_id": node_id, "run_id": run_id},
        )
        return None


# FAR-458 connector-write idempotency: the connector node's write boundary is the
# connector-specific UNKNOWN-recovery decision point. These helpers mirror the
# sandbox marker machinery (`_retain_raw_output_marker` / `_persist_raw_output_marker`)
# but read BOTH the run's persisted idempotency key AND its markers, and stamp a
# `delivery_done` marker when a connector write genuinely succeeds — the evidence
# the read-before-write suppression (`read_before_write_suppression`) requires.


async def _read_connector_idempotency_gate_state(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read a rewrite-write run's ``(raw_output_markers, idempotency_key)``.

    Bounded by ``_IDEMPOTENCY_GATE_READ_TIMEOUT`` (3s); fail-open to
    ``(None, None)`` (the write proceeds, no suppression) on any failure — the
    gate must never block a connector write. Reads the run row directly (no
    claim-token fencing — a connector node has no dispatch lease), so only the
    run id + org id are required. Returns the parsed markers dict (or ``None``)
    and the persisted ``idempotency_key`` (or ``None`` when the run is missing
    or carries no persisted key).

    FENCING (FAR-458 MAJOR 3): the marker read is taken under
    ``SELECT ... FOR UPDATE`` so concurrent re-runs of the same UNKNOWN write
    serialise on the run row rather than both observing "no delivery_done" and
    both firing the write. The deletion sentinel / delivery evidence is only
    ever committed under the same row lock (``_write_raw_output_marker`` also
    takes ``with_for_update``), so a gate decision cannot read past an
    in-progress concurrent terminalization/stamp.

    REMAINING WINDOW (honest, documented): this fences + serialises the gate
    READS against the row, and the marker WRITE side takes the same lock, but
    the actual upstream connector write executes between the gate returning and
    the later ``delivery_done`` stamp — so two concurrently-started re-runs can
    both pass the (now serialised) gate and both send the write before either
    stamps. Fully closing that residual double-write requires a
    ``write_started`` lease stamped under FOR UPDATE and held across the write;
    that is intentionally NOT added here because a stale lease from a dead
    pre-UNKNOWN execution would fail-CLOSED and block legitimate recovery
    (the write never deferred). The marker write lock is what the sandbox path
    relies on; this connector read now matches it.
    """
    if session_factory is None or not run_id:
        return None, None
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return None, None
    from sqlalchemy import text as _sql_text

    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async def _read() -> tuple[dict[str, Any] | None, str | None]:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await set_rls_execution_context(session)
            row = (
                await session.execute(
                    _sql_text(
                        "SELECT raw_output_markers, idempotency_key FROM runs "
                        "WHERE id=:rid AND organisation_id=:oid FOR UPDATE"
                    ),
                    {"rid": run_id, "oid": str(org_uuid)},
                )
            ).fetchone()
            if row is None:
                return None, None
            markers = row[0]
            markers_dict = markers if isinstance(markers, dict) else None
            persisted_key = row[1]
            return markers_dict, (str(persisted_key) if persisted_key else None)

    try:
        return await asyncio.wait_for(_read(), timeout=_IDEMPOTENCY_GATE_READ_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "connector.idempotency_gate_read_failed",
            extra={"node_id": node_id, "run_id": run_id},
        )
        return None, None


def _connector_write_payload_hash(resource: str, filters: dict[str, Any] | None, data: dict[str, Any]) -> str:
    """Stable full-write-identity hash for a connector write's key derivation.

    Folds the WHOLE write identity into the key — not just ``data`` — so a
    re-run that changes the write TARGET (``resource``, or a write-relevant
    ``provider_ref``) with byte-identical ``data`` derives a DIFFERENT key and
    is not wrongly suppressed. ``resource`` is the ``ConnectorPayload.resource``
    (the write's destination/verb); ``provider_ref`` (the shell connector's
    execution target) may live in either ``filters`` or ``data``, so both are
    consulted. ``data`` is the rendered write body (``ConnectorPayload.data``).

    It is serialised deterministically (sorted keys) so an unchanged re-run
    produces the identical payload component — and thus the identical
    idempotency key — while a genuinely-edited content-version OR target
    produces a different one. ``str`` coercion covers non-JSON values (dates,
    Paths) without raising.
    """
    import json as _json

    provider_ref = filters.get("provider_ref") if isinstance(filters, dict) else None
    if provider_ref is None and isinstance(data, dict):
        provider_ref = data.get("provider_ref")
    identity: dict[str, Any] = {"resource": resource, "provider_ref": provider_ref, "data": data}
    # ``json.dumps(..., default=str)`` would stringify set/frozenset members via
    # ``str(set)``, whose ordering is PYTHONHASHSEED-dependent — so the gate and
    # stamp sides could derive DIFFERENT keys across worker processes and the
    # connector-write dedup would be silently defeated. Pre-canonicalise only the
    # non-JSON-native set containers to sorted lists (everything else is passed
    # through unchanged, so the primary ``default=str`` path keeps handling
    # dates/Paths as before and existing non-set keys are unaffected). The
    # ``default`` hook is :func:`_canonical_scalar` (NOT bare ``str``): an object
    # with the default ``__str__``/``__repr__`` renders its MEMORY ADDRESS, which
    # differs in every worker process — the same nondeterminism in another form.
    identity = _canonicalize_sets(identity)
    try:
        return _json.dumps(identity, sort_keys=True, default=_canonical_scalar)
    except (TypeError, ValueError):
        # ``_canonical_scalar`` handles the common non-JSON scalars (dates, Paths),
        # so this branch only triggers on a genuinely unserialisable structure.
        # Fall back to a DETERMINISTIC coercion (every value stringified, keys
        # sorted) rather than ``repr`` — ``repr`` is NOT canonical across
        # processes, so two different invocations could derive DIFFERENT keys and
        # silently defeat the dedup (gate vs stamp side disagree). See
        # ``canonical_payload_hash`` in trigger_engine/pre_guardrail.py.
        return _json.dumps(_canonical_coerce(identity), sort_keys=True)


# The object-default ``__str__``/``__repr__`` method objects (captured once) —
# their runtime identity marks a type whose stringification embeds the instance
# memory address (see :func:`_canonical_scalar`).
_OBJECT_DEFAULT_STR = object.__str__
_OBJECT_DEFAULT_REPR = object.__repr__


def _canonical_scalar(obj: Any) -> str:
    """Deterministic string rendering for a non-JSON-native leaf value.

    ``str()`` of an object that defines NEITHER ``__str__`` NOR ``__repr__``
    falls back to ``object.__repr__``, which embeds the instance's memory
    address (``<pkg.X object at 0x7f...>``) — a value that differs in every
    worker process. A payload containing such an object would hash DIFFERENTLY
    on the gate and stamp sides and silently defeat the connector-write dedup.
    For those objects render a stable type-identity string instead; every type
    with a custom ``__str__``/``__repr__`` (datetime, Path, UUID, Decimal, ...)
    keeps its meaningful, process-independent ``str()``. Used as the
    ``json.dumps`` ``default`` hook in :func:`_connector_write_payload_hash` and
    as the leaf coercion in :func:`_canonical_coerce`.
    """
    obj_type = type(obj)
    # Runtime identity check against the object defaults (getattr keeps the
    # method objects as plain attributes, so the comparison is well-defined).
    if (
        getattr(obj_type, "__str__", None) is _OBJECT_DEFAULT_STR
        and getattr(obj_type, "__repr__", None) is _OBJECT_DEFAULT_REPR
    ):
        return f"<{obj_type.__module__}.{obj_type.__qualname__}>"
    return str(obj)


def _canonicalize_sets(obj: Any) -> Any:
    """Recursively convert set/frozenset containers to sorted lists.

    ``json.dumps`` has no native encoding for sets and would otherwise fall back
    to ``str(set)`` (via the default hook), whose member order depends on
    ``PYTHONHASHSEED`` — producing different serialisations across worker
    processes and silently defeating the connector-write dedup. Converting sets
    to sorted lists (sorted by the canonical scalar rendering of the coerced
    member, matching :func:`_canonical_coerce`) makes the output byte-identical
    across processes. All other containers and scalars are passed through
    unchanged so the primary ``json.dumps(..., default=_canonical_scalar)`` path
    keeps handling dates/Paths as before.
    """
    if isinstance(obj, dict):
        return {k: _canonicalize_sets(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize_sets(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_canonicalize_sets(v) for v in obj), key=_canonical_scalar)
    return obj


def _canonical_coerce(obj: Any) -> Any:
    """Deterministically coerce *obj* into a JSON-serialisable structure.

    Every scalar becomes ``str`` and every mapping/sequence is rebuilt with
    sorted/stable ordering so the result is byte-identical across processes —
    used as the safe fallback in :func:`_connector_write_payload_hash` when the
    primary ``json.dumps(...)`` still raises. Leaves go through
    :func:`_canonical_scalar` so an object with the default address-embedded
    ``__str__``/``__repr__`` renders its type identity (never a memory address).
    """
    if isinstance(obj, dict):
        return {str(k): _canonical_coerce(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_canonical_coerce(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        # Set iteration order is nondeterministic across processes
        # (PYTHONHASHSEED); sort the coerced members so the serialisation stays
        # byte-identical between the gate and stamp sides — otherwise two
        # invocations could derive DIFFERENT keys and silently defeat the dedup.
        return sorted((_canonical_coerce(v) for v in obj), key=_canonical_scalar)
    return _canonical_scalar(obj)


def _connector_marker_attempt_key(run_id: str, node_id: str) -> str:
    """Stable marker key for a connector node's delivery record.

    Unlike the sandbox path (which keys by ``claim_count`` per attempt), the
    connector node makes ONE logical write per invocation, so a stable
    ``run:{run_id}:node:{node_id}`` key lets a retry's marker merge with (and
    preserve) the prior attempt's ``delivery_done`` via
    ``_merge_existing_raw_output_marker``.
    """
    return f"run:{run_id}:node:{node_id}:connector"


def _connector_on_unknown(connector: Any, resource: str) -> str:
    """Read the effective ``on_unknown`` mode for a connector write to *resource*.

    FAR-458: decisions of fail-open vs fail-closed on the ambiguous
    (couldn't-confirm-delivery) path belong to the ACTION's semantics, so each
    connector-op declares its own mode (``ConnectorBase.on_unknown_for``; REST
    reads a per-op config value defaulting to ``fail_open``). The lookup is
    defensive: a connector that does not expose ``on_unknown_for`` (or a reader
    that raises) falls back to the fail-open default, so the gate never blocks a
    write on a missing/illegible policy. Any value outside the three valid modes
    is also coerced to ``fail_open`` (an invalid value is a config error the
    connector surfaces loudly at parse time; the gate stays fail-open).
    """
    reader = getattr(connector, "on_unknown_for", None)
    if not callable(reader):
        return DEFAULT_ON_UNKNOWN
    try:
        mode = reader(resource)
    except Exception:
        _log.warning(
            "connector.idempotency_gate.on_unknown_read_failed",
            extra={"resource": resource},
        )
        return DEFAULT_ON_UNKNOWN
    return mode if mode in ON_UNKNOWN_MODES else DEFAULT_ON_UNKNOWN


async def _resolve_connector_write_outcome(
    session_factory: Callable[..., Any] | None,
    *,
    connector: Any,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    resource: str,
    filters: dict[str, Any] | None,
    data: dict[str, Any],
    result: Any = None,
    intent_active: bool,
    exception: BaseException | None = None,
) -> None:
    """Post-write resolution for a connector write (FAR-458 + FAR-531 AC6).

    The SINGLE authority deciding what the marker slot records after the
    upstream write — every terminal transition flows through here:

    - A RAISED error (``exception`` is not None, QA Fix 1) is classified
      AMBIGUOUS and the in-flight intent marker is left AS-IS — no
      ``no_delivery_confirmed`` is persisted. A raised error cannot tell the
      engine WHETHER the write reached upstream: a read-timeout /
      connection-reset AFTER dispatch may have landed it, so persisting
      definite no-delivery evidence would be a lie (and fail_closed's
      documented "possible silent miss" suppression would never engage). Under
      ``fail_closed`` the in-flight intent suppresses the re-fire; under
      ``fail_open`` it re-fires (unchanged). Note a deterministic PRE-dispatch
      failure (e.g. payload validation raising) therefore also stays ambiguous
      under fail_closed — that IS the contract: only the connector's OWN
      reported-failure shape is trusted as definite.
    - A reported failure (the connector's ``write_reported_failure`` hook,
      default False — connectors whose results carry a failure shape OPT IN) is
      a DEFINITE no-delivery: ``delivery_done`` is NEVER stamped (the
      pre-FAR-458 silent-miss guard), and an in-flight intent marker resolves
      to ``no_delivery_confirmed`` so a later attempt re-fires under BOTH
      modes. Without intent markers this is exactly the pre-FAR-531 behaviour
      (no stamp at all).
    - Otherwise the write genuinely delivered: stamp ``delivery_done``
      (promoting the intent marker in place).
    """
    if exception is not None:
        # QA Fix 1: ambiguous — leave the in-flight intent AS-IS (if any).
        if intent_active:
            _log.warning(
                "connector.idempotency_intent_left_in_flight_on_error",
                extra={
                    "run_id": run_id,
                    "node_id": node_id,
                    "error_type": type(exception).__name__,
                    "hint": "raised_connector_error_is_ambiguous_fail_closed_suppresses",
                },
            )
        return
    if _connector_write_reported_failure(connector, result):
        if intent_active:
            await _mark_connector_write_no_delivery(
                session_factory,
                run_id=run_id,
                org_id_raw=org_id_raw,
                node_id=node_id,
                resource=resource,
                filters=filters,
                data=data,
                reason="connector_reported_failure",
            )
        return
    await _stamp_connector_write_delivered(
        session_factory,
        run_id=run_id,
        org_id_raw=org_id_raw,
        node_id=node_id,
        resource=resource,
        filters=filters,
        data=data,
        result=result,
    )


async def _stamp_connector_write_delivered(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    resource: str,
    filters: dict[str, Any] | None,
    data: dict[str, Any],
    result: Any = None,
) -> None:
    """Best-effort persist of a ``delivery_done`` marker for a successful connector write.

    FAR-458 MAJOR 1: the stamp fires only when the write GENUINELY succeeded —
    the CALLER owns that decision (FAR-531 AC6: it consults the connector's
    ``write_reported_failure`` hook, so connectors that report a failed write in
    their return value without raising — e.g. ``ShellConnector.write`` for the
    ``command`` resource returning ``{"exit_code": <non-zero>}`` — are routed to
    :func:`_mark_connector_write_no_delivery` instead of here). Stamping
    ``delivery_done`` on a failed result would suppress the operator's
    recover-by-re-run of the SAME run (the exact "silent miss" the code warns
    about).

    Mirrors the sandbox marker persist (bounded, never raises) — the evidence
    a connector write genuinely reached upstream. Fail-open: a persistence
    failure is logged and ignored, so a DB hiccup cannot convert a successful
    write into a failed node. When no run id / session factory is available the
    marker is skipped (the write still succeeds).

    ``resource`` / ``filters`` / ``data`` are the FULL write identity folded
    into the marker's derived ``idempotency_key`` (via
    :func:`_connector_write_payload_hash`) on BOTH the stamp and gate sides, so
    a re-run that edits the content OR the target derives the matching key.

    MAJOR 1 (FAR-458): the connector marker slot is keyed ONCE per
    ``(run, node)``; a content-edit re-run must PROMOTE the newest delivered
    key (``promote_newest_key=True``) rather than pin the slot to a superseded
    key — otherwise a later re-run of the edited payload misfires (double
    submit) while a re-run of the superseded original is wrongly suppressed.
    FAR-531: the stamp UPDATES-IN-PLACE the slot an in-flight intent marker
    (:func:`_persist_connector_write_intent`) already occupies — the merge
    promotes ``delivery_done: True`` over the intent, never a duplicate row.

    DURABILITY (FAR-458 MAJOR 4; mode-dependent per FAR-531 QA Fix 6):
    persistence is best-effort, so a lost stamp's consequence depends on what
    evidence survives. With an in-flight intent marker still in the slot
    (killswitch on), a lost DELIVERY stamp leaves the write AMBIGUOUS:
    ``fail_open`` re-fires the delivered write (potential double-submit) while
    ``fail_closed`` SUPPRESSES the re-fire (possible silent miss — the operator
    reconciles; NOT a double-submit). With no marker at all (killswitch off, or
    the intent persist was lost too) the re-run re-fires in every mode
    (potential double-submit). Emits the structured
    ``connector.idempotency_marker_lost`` counter on any non-persisted marker
    so the loss is observable in analytics/metrics; the underlying failure is
    already logged by ``_persist_raw_output_marker``.

    NEVER RAISES (QA Fix 5, FAR-531): the payload-hash computation and the
    persist are bounded by this body — a hostile payload (``str(obj)`` raising
    escapes the hash's ``except (TypeError, ValueError)``) or any persist
    failure degrades to "no marker" with a log, never a node failure.
    """
    if session_factory is None or not run_id:
        return
    try:
        payload = _connector_write_payload_hash(resource=resource, filters=filters, data=data)
        attempt_key = _connector_marker_attempt_key(run_id, node_id)
        marker: dict[str, Any] = {
            "_modulo_marker": True,
            "status": "completed",
            "summary": "connector write delivered (delivery_done)",
            "node_id": node_id,
            "attempt_key": attempt_key,
            "delivery_done": True,
        }
        persisted = await _persist_raw_output_marker(
            session_factory,
            run_id=run_id,
            org_id_raw=org_id_raw,
            node_id=node_id,
            attempt_key=attempt_key,
            marker=marker,
            index=None,
            payload=payload,
            promote_newest_key=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "connector.idempotency_stamp_failed",
            extra={"run_id": run_id, "node_id": node_id},
        )
        return
    if not persisted:
        _log.warning(
            "connector.idempotency_marker_lost",
            extra={
                "run_id": run_id,
                "node_id": node_id,
                "attempt_key": attempt_key,
                "hint": "delivery_marker_not_persisted_fail_open_double_submit_or_fail_closed_suppressed",
            },
        )


# ── FAR-531 intent markers (write-before / stamp-after) ──────────────────────
# The FAR-458 gate's ``on_unknown: fail_closed`` could never engage in
# production: ``read_before_write_ambiguous`` requires a marker carrying the
# write's derived key WITHOUT ``delivery_done``, but the only production writer
# (``_stamp_connector_write_delivered``) always stamps ``delivery_done: True``
# — and a crash/timeout between write dispatch and stamp left NO marker at all.
# The intent marker closes that gap: it is persisted AFTER the gate proceeds
# and BEFORE the upstream write fires, in the SAME marker slot the delivery
# stamp updates (update-in-place, no duplicate rows). State machine per
# ``(run, node)`` slot + derived key:
#
#   (absent) --gate proceed--> INTENT (in-flight, ambiguous)
#   INTENT --write succeeds--> DELIVERED (delivery_done: True; suppressed in
#                              every mode except ``off``)
#   INTENT --reported failure-> NO_DELIVERY (no_delivery_confirmed: True;
#                              NOT ambiguous — re-fires under BOTH modes; the
#                              connector's OWN result shape reported the
#                              failure via ``write_reported_failure`` so the
#                              no-delivery is DEFINITE)
#   INTENT --write raises-----> stays INTENT (AMBIGUOUS — QA Fix 1: a raised
#                              error cannot tell whether the write landed (a
#                              read-timeout AFTER dispatch vs a pre-dispatch
#                              validation failure look identical), so it is
#                              NOT trusted as a definite no-delivery:
#                              fail_closed suppresses ("possible silent
#                              miss"), fail_open re-fires. A deterministic
#                              pre-dispatch failure therefore also stays
#                              ambiguous under fail_closed — that IS the
#                              contract.)
#   INTENT --crash/timeout----> stays INTENT (ambiguous: fail_closed
#                              suppresses — the headline FAR-531 fix; fail_open
#                              re-fires, unchanged)
#
# RESIDUAL (killswitch flip-OFF orphan, QA Fix 7): intent written →
# crash/timeout → operator flips the killswitch OFF → a re-run bypasses the
# gate ENTIRELY (the write re-fires un-deduped) and the slot still holds the
# in-flight intent → if that re-run's write fails, no resolution is persisted
# (the intent path is disabled with the killswitch) → the ORPHANED in-flight
# intent survives. Re-enabling the killswitch with ``fail_closed`` then
# suppresses a re-run of a write that may have DEFINITELY failed (the slot
# looks ambiguous) until an operator reconciles/clears the orphaned slot.
# Accepted within fail_closed's documented "possible silent miss" contract.

_CONNECTOR_INTENT_MARKER_KIND = "connector_write_intent"
_CONNECTOR_NO_DELIVERY_MARKER_KIND = "connector_write_no_delivery"


def _connector_gate_enabled(on_unknown: str, *, node_id: str | None = None, run_id: str | None = None) -> bool:
    """Whether the connector-write gate may act on this op's outcome (QA Fix 3).

    The SINGLE authority for gate eligibility: the per-op ``on_unknown`` mode is
    not ``off`` (the op bypasses the gate entirely) AND the opt-in killswitch
    ``modulo_connector_write_gate_enabled`` is enabled. The killswitch read is
    fail-open — an exception reads as disabled (the gate proceeds on the same
    failure).

    Consumed by BOTH ``_connector_write_gate`` (may I suppress?) and
    ``_connector_intent_marker_enabled`` (should I persist an intent marker?),
    so the feature's correctness invariant — an intent marker is written
    if-and-only-if the gate could suppress on ambiguity — is enforced in ONE
    place instead of parallel copies that can drift.
    """
    if on_unknown == "off":
        return False
    try:
        from modulo.settings import get_settings

        return bool(getattr(get_settings(), "modulo_connector_write_gate_enabled", False))
    except Exception:
        # Killswitch read failure must not block a write — proceed (fail-open).
        _log.warning(
            "connector.idempotency_gate_killswitch_check_failed",
            extra={"node_id": node_id, "run_id": run_id},
        )
        return False


def _connector_intent_marker_enabled(on_unknown: str) -> bool:
    """Whether an intent marker should be written for a connector write.

    An intent marker only exists to be consumed by the read-before-write gate,
    so it is written ONLY when the gate could actually suppress on ambiguity.
    Delegates to :func:`_connector_gate_enabled` (QA Fix 3) — the same
    killswitch + ``off``-bypass policy the gate itself evaluates, so the marker
    write and the gate can never disagree. Deliberate scope note: the marker is
    written for ``fail_open`` too, not only ``fail_closed`` — one uniform
    marker state machine, and the evidence survives an operator later flipping
    the mode to fail_closed (fail_open gate semantics are unchanged: the
    ambiguous branch still never suppresses under fail_open).
    """
    return _connector_gate_enabled(on_unknown)


def _connector_write_reported_failure(connector: Any, result: Any) -> bool:
    """Defensively read the connector's ``write_reported_failure`` hook (AC6).

    True ONLY when the connector exposes the hook, calling it does not raise,
    and the hook reports a failure for this non-raising write result. Any
    missing hook, read error, or non-bool answer is treated as "not a reported
    failure" (the pre-FAR-531 default for connectors that raise on failure).
    """
    reader = getattr(connector, "write_reported_failure", None)
    if not callable(reader):
        return False
    try:
        return bool(reader(result))
    except Exception:
        _log.warning(
            "connector.idempotency_gate.write_reported_failure_read_failed",
            extra={"connector_type": str(getattr(connector, "connector_type", ""))},
        )
        return False


async def _persist_connector_write_intent(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    resource: str,
    filters: dict[str, Any] | None,
    data: dict[str, Any],
) -> None:
    """Persist the IN-FLIGHT intent marker BEFORE the upstream write fires.

    FAR-531: called exactly between the gate returning "proceed" and the
    connector write, carrying the SAME derived key the gate reads (identical
    ``resource`` / ``filters`` / ``data`` identity and the run's persisted
    idempotency key via ``_write_raw_output_marker``). A crash, timeout, or
    worker kill between this persist and the delivery stamp leaves the marker
    in-flight — the ambiguous state ``read_before_write_ambiguous`` reports, so
    a later attempt's gate SUPPRESSES the re-fire under ``fail_closed`` (the
    headline fix) and re-fires under ``fail_open`` (unchanged).

    The marker occupies the SAME slot (``_connector_marker_attempt_key``) the
    delivery stamp updates, so a successful write promotes it in place to
    ``delivery_done: True`` (no duplicate rows). Best-effort and bounded
    exactly like the delivery stamp — an intent-write failure must never fail
    the node (fail-open, logged).

    NEVER RAISES (QA Fix 5, FAR-531): the payload-hash computation and the
    persist are bounded by this body — a hostile payload (``str(obj)`` raising
    escapes the hash's ``except (TypeError, ValueError)``) or any persist
    failure degrades to "no marker" with a log. The node therefore always
    proceeds to the write; the pre-write intent persist can never fail it.
    """
    if session_factory is None or not run_id:
        return
    try:
        payload = _connector_write_payload_hash(resource=resource, filters=filters, data=data)
        attempt_key = _connector_marker_attempt_key(run_id, node_id)
        marker: dict[str, Any] = {
            "_modulo_marker": True,
            "status": "running",
            "summary": "connector write intent (in-flight, delivery unconfirmed)",
            "node_id": node_id,
            "attempt_key": attempt_key,
            "marker_kind": _CONNECTOR_INTENT_MARKER_KIND,
        }
        await _persist_raw_output_marker(
            session_factory,
            run_id=run_id,
            org_id_raw=org_id_raw,
            node_id=node_id,
            attempt_key=attempt_key,
            marker=marker,
            index=None,
            payload=payload,
            promote_newest_key=True,
            # The intent describes a write that has NOT happened yet — it must
            # never inherit ``delivery_done: True`` from a SUPERSEDED key's
            # delivered marker sharing the slot (that would claim the new key
            # was already delivered before the write fired). A SAME-KEY
            # delivered marker's evidence IS inherited (QA Fix 2): a
            # concurrent-attempt intent persist must not wipe delivered
            # evidence for the identical write.
            preserve_delivery_done=False,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "connector.idempotency_intent_persist_failed",
            extra={"run_id": run_id, "node_id": node_id},
        )


async def _mark_connector_write_no_delivery(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    resource: str,
    filters: dict[str, Any] | None,
    data: dict[str, Any],
    reason: str,
) -> None:
    """Resolve the in-flight intent marker to a DEFINITE no-delivery state.

    FAR-531: fired when the write DEFINITELY did not reach upstream — ONLY the
    non-raising path where the connector's OWN result shape reported the
    failure (``reason="connector_reported_failure"`` via the
    ``write_reported_failure`` hook, AC6). A RAISED connector error is NOT
    routed here (QA Fix 1): a raise cannot tell whether the write reached
    upstream, so it is classified AMBIGUOUS and the in-flight intent is left
    as-is (fail_closed suppresses, fail_open re-fires). The marker flips to
    ``no_delivery_confirmed: True`` in the SAME slot, so a later attempt's gate
    treats the key as NOT ambiguous (``read_before_write_ambiguous`` excludes
    it) and RE-FIRES the write under BOTH modes — honouring FAR-458's "never
    suppress a definite failure".

    RESIDUAL (documented, within fail_closed's contract): the resolve is
    best-effort. A crash between the failure being detected and this persist
    leaves the in-flight intent marker → ambiguous → fail_closed suppresses a
    definitely-failed write ONCE ("possible silent miss"; the operator
    reconciles). Best-effort + bounded exactly like the delivery stamp.

    NEVER RAISES (QA Fix 5, FAR-531): the payload-hash computation and the
    persist are bounded by this body — a hostile payload (``str(obj)`` raising
    escapes the hash's ``except (TypeError, ValueError)``) or any persist
    failure degrades to "no marker" with a log; the connector's original error
    or result is never masked by a persist failure.
    """
    if session_factory is None or not run_id:
        return
    try:
        payload = _connector_write_payload_hash(resource=resource, filters=filters, data=data)
        attempt_key = _connector_marker_attempt_key(run_id, node_id)
        marker: dict[str, Any] = {
            "_modulo_marker": True,
            "status": "failed",
            "summary": f"connector write did not reach upstream ({reason})",
            "node_id": node_id,
            "attempt_key": attempt_key,
            "marker_kind": _CONNECTOR_NO_DELIVERY_MARKER_KIND,
            "no_delivery_confirmed": True,
        }
        await _persist_raw_output_marker(
            session_factory,
            run_id=run_id,
            org_id_raw=org_id_raw,
            node_id=node_id,
            attempt_key=attempt_key,
            marker=marker,
            index=None,
            payload=payload,
            promote_newest_key=True,
            # Same reasoning as the intent marker: a definite no-delivery for
            # the newest key must not inherit a SUPERSEDED key's delivery
            # evidence — but a SAME-KEY delivered marker's evidence IS
            # inherited (QA Fix 2): the delivery genuinely happened for this
            # key, and wiping it would re-fire a delivered write.
            preserve_delivery_done=False,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "connector.idempotency_no_delivery_persist_failed",
            extra={"run_id": run_id, "node_id": node_id},
        )


async def _connector_write_gate(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    resource: str,
    filters: dict[str, Any] | None,
    data: dict[str, Any],
    on_unknown: str = "fail_open",
) -> dict[str, Any] | None:
    """FAR-458 read-before-write gate for a connector write.

    Suppresses (returns a skipped envelope) in EXACTLY two situations:

    1. **CONFIRMED delivery** (the dedup's whole point, mode-INDEPENDENT): the
       run's persisted idempotency key derives a per-node key that a recorded
       marker carries WITH ``delivery_done is True`` — a genuine prior upstream
       delivery. Always suppressed, regardless of ``on_unknown``.
    2. **AMBIGUOUS delivery** (governed by ``on_unknown``): a recorded marker
       carries the SAME derived key but WITHOUT ``delivery_done is True`` AND
       WITHOUT ``no_delivery_confirmed is True`` — a prior attempt touched this
       exact write but its delivery could not be confirmed. With the FAR-531
       intent markers this is the IN-FLIGHT intent marker left by a
       crash/timeout between the intent persist and the delivery stamp.
       ``on_unknown="fail_closed"`` suppresses (possible silent miss; the
       operator reconciles); ``on_unknown="fail_open"`` (default) does NOT
       suppress (the write fires, possible duplicate — usually recoverable).

    A first-time write (no marker), a changed-payload/target re-run (a
    DIFFERENT derived key), and a DEFINITE-failure re-run (the intent marker
    was resolved to ``no_delivery_confirmed: True`` — the connector's result
    reported failure; a connector that RAISED is ambiguous instead and its
    intent marker stays in-flight) are NEVER suppressed — the definite failure
    re-fires under BOTH modes (FAR-458: never suppress a definite failure).
    ``on_unknown="off"`` bypasses the gate entirely — the write always fires,
    never deduped.

    The fail-closed suppression envelope records ``delivery_done: False``
    (FAR-531 AC4): the suppressed write was NOT confirmed delivered — claiming
    otherwise would misreport a suppressed-never-fired write as delivered.
    (Suppressed ≠ delivered.)

    ``on_unknown`` (FAR-458) is the per-connector-per-write idempotency mode read
    from the connector's write op config (see ``ConnectorBase.on_unknown_for`` /
    ``_connector_on_unknown``). The default ``fail_open`` preserves the
    pre-existing fail-open gate contract: an ambiguous-but-unconfirmed delivery is
    re-attempted rather than silently dropped. A CONFIRMED-delivered write is
    still suppressed in every mode except ``off``.

    ``resource`` / ``filters`` / ``data`` (MAJOR 2) are the FULL write identity
    folded into the derived key (via :func:`_connector_write_payload_hash`) on
    BOTH this gate side and the marker-stamp side, so a re-run that edits the
    content OR the write target (resource / ``provider_ref``) derives a
    DIFFERENT key and is NOT suppressed (the edit or new target is never
    silently deduped), while an unchanged re-run reuses the same key and IS
    suppressed. Threads the write-content ``payload`` into the key derivation.

    Fail-open in every direction (missing run id / session factory / persisted
    key, killswitch off, DB error, malformed key) returns ``None`` — the write
    proceeds, never blocked. The killswitch
    ``modulo_connector_write_gate_enabled`` is GENUINELY OPT-IN: it defaults to
    ``False`` (set in ``modulo.settings``), so a deploy never silently suppresses
    byte-identical re-executed connector writes — a behavioural change vs. the
    pre-FAR-458 contract where every visit fired. Operators enable it explicitly.
    """
    if session_factory is None or not run_id:
        return None
    # QA Fix 3: the per-op ``off`` bypass + the opt-in killswitch live in ONE
    # shared eligibility helper (also consumed by the intent-marker writer), so
    # "gate can act" and "intent marker written" can never drift apart. The
    # ``off`` check short-circuits before the killswitch and the marker read
    # (the gate is bypassed entirely for that op).
    if not _connector_gate_enabled(on_unknown, node_id=node_id, run_id=run_id):
        return None
    payload = _connector_write_payload_hash(resource=resource, filters=filters, data=data)
    markers, persisted_key = await _read_connector_idempotency_gate_state(
        session_factory,
        run_id=run_id,
        org_id_raw=org_id_raw,
        node_id=node_id,
    )
    if not persisted_key:
        return None
    try:
        suppressed = read_before_write_suppression(
            markers,
            run_ref=persisted_key,
            node_ref=node_id,
            index=None,
            payload=payload,
        )
    except ValueError:
        return None
    if suppressed:
        _log.info(
            "connector.idempotency_gate.suppressed_write",
            extra={"run_id": run_id, "node_id": node_id},
        )
        return _idempotency_gate_skipped_envelope(node_id, gate_tag="connector_write_suppressed")
    if on_unknown == "fail_closed":
        try:
            ambiguous = read_before_write_ambiguous(
                markers,
                run_ref=persisted_key,
                node_ref=node_id,
                index=None,
                payload=payload,
            )
        except ValueError:
            ambiguous = False
        if ambiguous:
            _log.info(
                "connector.idempotency_gate.fail_closed_suppressed",
                extra={"run_id": run_id, "node_id": node_id},
            )
            # FAR-531 AC4: the suppressed write is NOT confirmed delivered — the
            # envelope must not claim ``delivery_done: True`` for it.
            return _idempotency_gate_skipped_envelope(node_id, gate_tag="connector_write_fail_closed", delivered=False)
    return None


def _evaluate_eval_condition(score: float, threshold: float, operator: str) -> bool:
    """Evaluate an eval-reference condition using the given operator.

    Returns True when the condition is satisfied (meaning the gate should fire/interrupt).
    Returns False when the condition is not satisfied (gate should be skipped).
    """
    match operator:
        case "lt":
            return score < threshold
        case "gt":
            return score > threshold
        case "lte":
            return score <= threshold
        case "gte":
            return score >= threshold
        case "eq":
            return score == threshold
        case "neq":
            return score != threshold
        case _:
            _log.warning(
                "hitl_gate.unknown_operator", extra={"operator": operator, "score": score, "threshold": threshold}
            )
            return False


# FAR-215 mid-run conformance re-check context. Set per-run by the executor
# (before graph streaming) so the node builders — created at graph-compile time
# via build_graph_from_json — can perform the capability re-check at NODE RUN
# TIME against the live manifest without threading the session factory / org /
# environment profile through every node builder signature.
#
# The tuple also carries the executor's HOISTED claim discovery: the claimed
# guardrail DTOs for the pipeline (one query per run) and a fail-closed marker
# for a run-start load failure. ``_run_conformance_gate`` forwards these to
# ``check_node_start`` so the per-node path pays zero guardrail-load queries.
_conformance_ctx_cv: ContextVar[tuple[Any, ...] | None] = ContextVar("_conformance_ctx", default=None)


def set_conformance_ctx(
    session_factory: Any,
    org_id: Any,
    environment_profile_id: Any,
    pipeline_id: Any,
    claimed_guardrails: list[Any] | None = None,
    claims_load_failed: bool = False,
) -> None:
    """Set the run-scoped conformance context (executor calls before streaming).

    *claimed_guardrails* is the hoisted per-run list of claimed guardrail DTOs
    (computed once by the executor); ``claims_load_failed`` marks a run-start
    load failure so the node gate fails CLOSED rather than skipping claims.
    """
    _conformance_ctx_cv.set(
        (session_factory, org_id, environment_profile_id, pipeline_id, claimed_guardrails, claims_load_failed)
    )


def get_conformance_ctx() -> tuple[Any, ...] | None:
    return _conformance_ctx_cv.get()


def _parse_uuid_opt(value: Any) -> uuid.UUID | None:
    """Parse a UUID (or str) to a UUID, or None when absent/unparseable."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _run_conformance_gate(
    state: dict[str, Any],
    *,
    node_id: str,
    connector_instance_ids: list[uuid.UUID] | None = None,
    agent_id: uuid.UUID | None = None,
    node_def: dict[str, Any] | None = None,
) -> bool:
    """FAR-215 mid-run capability re-check at node start (block -> HITL).

    On a conformance block this raises a LangGraph ``interrupt()`` (after
    stamping the per-node ``_conformance_blocked_node`` marker and writing the
    audit) — the node body must NOT return a special awaiting_human envelope
    afterwards: the interrupt routes the run to ``awaiting_human`` itself, and
    control never returns to the node. Returns False when the node should
    proceed with normal execution (fast path, resume-approve override, warn/
    observe advisory, or present). ``return True`` is retained only as a
    defensive tail for direct callers/tests where ``interrupt`` is mocked; in
    production the interrupt raises first.

    Reads the run-scoped conformance context (session_factory, org_id,
    environment_profile_id, pipeline_id, hoisted claimed guardrails, run-start
    load-failed marker) set by the executor; the node's bound connector
    instance ids and agent id come from the node definition. *node_def*, when
    provided, is forwarded to the live-manifest reader so a ``sandbox_agent``
    node's mechanically-derived sandbox capability surface (egress certification;
    write/git-credential unknown until PR B — FAR-212 PR A) is included in the
    conformance manifest.

    Behaviour:
      - On resume of THIS node's conformance block (``state`` carries the
        ``_conformance_blocked_node`` marker set before the interrupt) the
        human decision is routed: ``approved`` (or ``deliver_manual``) is the
        documented human override -> the marker is cleared and the node
        continues; ``rejected`` -> the node is DENIED and the run FAILS CLOSED
        (raises ``GuardrailBlockedError`` -> terminal ``eval_failed``), never a
        fail-open continuation. ``_hitl_decision`` alone is NEVER trusted to
        skip the check: it persists in state for the whole run after ANY HITL
        resume, so a foreign decision must not disable this safety gate.
      - No bound guardrail with a conformance claim -> fast path (no DB).
      - ``absent``/``unknown`` on a ``block``-action guardrail -> raise a HITL
        interrupt (the run transitions to ``awaiting_human`` with a
        machine-readable reason), never silent abort, never fail open.
      - ``absent``/``unknown`` on warn/observe -> log + audit warning, continue.
      - ``present`` -> continue.

    An audit event is appended for the block before the interrupt. The audit
    write is best-effort (failure-isolated) and never carries raw payloads.
    """
    ctx = get_conformance_ctx()
    if ctx is None:
        return False
    session_factory, org_id_raw, environment_profile_id, pipeline_id_raw, *rest = ctx
    claimed_guardrails: list[Any] | None = rest[0] if rest else None
    claims_load_failed: bool = bool(rest[1]) if len(rest) > 1 else False
    if session_factory is None or not pipeline_id_raw:
        return False
    if state.get("_conformance_blocked_node") == node_id:
        return _handle_conformance_resume(state, node_id)

    org_uuid = _parse_uuid_opt(org_id_raw)
    if org_uuid is None:
        return False
    pipeline_uuid = _parse_uuid_opt(pipeline_id_raw)
    if pipeline_uuid is None:
        return False
    env_profile_uuid = _parse_uuid_opt(environment_profile_id)

    from modulo.core.guardrails.conformance import check_node_start

    result = await check_node_start(
        session_factory,
        org_id=org_uuid,
        pipeline_id=pipeline_uuid,
        node_id=node_id,
        connector_instance_ids=list(connector_instance_ids or []),
        environment_profile_id=env_profile_uuid,
        agent_id=agent_id,
        node_def=node_def,
        claimed_guardrails=claimed_guardrails,
        claims_load_failed=claims_load_failed,
    )
    if result.blocked:
        return await _handle_conformance_block(session_factory, state, node_id, org_uuid, result)
    if result.warned:
        # Advisory (warn/observe) guardrails never block — log + audit only.
        await _append_conformance_audit(
            session_factory,
            org_id=org_uuid,
            run_id=state.get("_run_id"),
            node_id=node_id,
            detail=result.detail,
            state=result.state,
            event_type="guardrail.conformance_warned_midrun",
        )
    return False


def _handle_conformance_resume(state: dict[str, Any], node_id: str) -> bool:
    """Route a human's decision after a conformance block (True: fail closed).

    On ``rejected`` the run FAILS CLOSED: the capability the block protected is
    still unavailable, so the node must NOT execute. ``GuardrailBlockedError`` is
    mapped by the executor to terminal ``eval_failed``/``eval_blocked`` (never a
    resume). ``approved``/``deliver_manual`` is the documented human override:
    the marker is cleared (so a later foreign resume replay of this node re-runs
    the real check) and normal execution continues.
    """
    decision = state.get("_hitl_decision")
    action = decision.get("action") if isinstance(decision, dict) else None
    if action == "rejected":
        from modulo.core.guardrails import GuardrailBlockedError

        raise GuardrailBlockedError(
            f"conformance_gate_{node_id}",
            "capability conformance gate was rejected by the human reviewer; the run fails closed",
        )
    state["_conformance_blocked_node"] = None
    return False


async def _handle_conformance_block(
    session_factory: Callable[..., Any],
    state: dict[str, Any],
    node_id: str,
    org_uuid: uuid.UUID,
    result: Any,
) -> bool:
    """Audit + stamp the per-node block marker, then raise the HITL interrupt."""
    await _append_conformance_audit(
        session_factory,
        org_id=org_uuid,
        run_id=state.get("_run_id"),
        node_id=node_id,
        detail=result.detail,
        state=result.state,
        event_type="guardrail.conformance_blocked_midrun",
    )
    # Stamp the per-node marker so the resume path can tell THIS node's
    # conformance block apart from any other gate's resume decision
    # (``_hitl_decision`` persists in state for the rest of the run).
    # Mutations before ``interrupt()`` are persisted by the checkpointer
    # (same pattern as ``_hitl_gate`` / ``_manual_node``).
    state["_conformance_blocked_node"] = node_id
    interrupt(
        {
            "gate_id": result.gate_id,
            "reason": result.detail,
            "node_id": node_id,
            "conformance_state": result.state,
            "conformance_blocked": True,
        }
    )
    return True


async def _append_conformance_audit(
    session_factory: Callable[..., Any],
    *,
    org_id: uuid.UUID,
    run_id: Any,
    node_id: str,
    detail: str,
    state: str,
    event_type: str,
) -> None:
    """Best-effort audit event for a mid-run conformance outcome (never raises)."""
    try:
        async with session_factory() as session, session.begin():
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            from modulo.core.audit_logger import append_audit_event

            await append_audit_event(
                session,
                org_id=org_id,
                event_type=event_type,
                resource_type="run",
                resource_id=uuid.UUID(str(run_id)) if run_id else None,
                payload_json={
                    "node_id": node_id,
                    "conformance_state": state,
                    "detail": detail[:5000],
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "guardrail.conformance_audit_failed",
            extra={"org_id": str(org_id), "node_id": node_id},
        )


def _resolve_node_run_overrides(
    run_context: dict[str, Any],
    agent_id: uuid.UUID | None,
    *,
    model_backend_id_str: str | None,
    prompt_template: str,
) -> tuple[str | None, str]:
    """Apply the run's frozen variant overrides on top of the snapshot-embedded node_def.

    FAR-332/342: ``_run_overrides`` (seeded by the executor from the run's
    frozen variant config) may override the model backend id and the per-agent
    prompt version. The override is namespaced under the reserved TOP-LEVEL
    ``_run_overrides`` key — never read from ``run_context["input"]``. Returns
    ``(model_backend_id_str, prompt_template)`` unchanged when no override
    applies.
    """
    _run_overrides = run_context.get("_run_overrides")
    if isinstance(_run_overrides, dict):
        if _run_overrides.get("model_backend_id"):
            model_backend_id_str = str(_run_overrides["model_backend_id"])
        prompt_templates = _run_overrides.get("prompt_templates")
        if isinstance(prompt_templates, dict) and agent_id is not None:
            per_agent_prompt = prompt_templates.get(str(agent_id))
            if isinstance(per_agent_prompt, str) and per_agent_prompt:
                prompt_template = per_agent_prompt
    return model_backend_id_str, prompt_template


def _render_agent_prompt(
    *,
    state: dict[str, Any],
    run_context: dict[str, Any],
    raw_input: Any,
    prompt_template: str,
    node_def: dict[str, Any],
) -> tuple[str, str | None]:
    """Render the node's prompt template (FAR-332/342 overrides applied inbound).

    Injects the state, run_context, input, and any resolved parameters into a
    sandboxed Jinja environment, then appends the LLM-routing prompt when the
    node is an ``llm`` routing node. Returns ``(rendered_prompt, routing_mode)``.
    """
    env = SandboxedEnvironment()
    template = env.from_string(prompt_template)
    # FAR-418 / FAR-436: context_scope — the agent's run_context VIEW (the keys
    # fed to the prompt template) is allowlist-gated to the node's need-to-know
    # set. Internal control keys are always preserved by filter_run_context_scope
    # (_CONTEXT_ALWAYS_KEPT). Absent scope = legacy (full run_context view).
    _node_cap = node_def.get("capability_scope") or {}
    scoped_run_context = filter_run_context_scope(run_context, _node_cap.get("context_scope"))
    template_vars: dict[str, Any] = {
        "state": state,
        "run_context": scoped_run_context,
        "input": raw_input,
    }
    resolved = node_def.get("_resolved_parameters")
    if isinstance(resolved, dict):
        template_vars["parameter"] = resolved
    rendered_prompt = template.render(**template_vars)

    routing_mode: str | None = node_def.get("routing_mode")
    if routing_mode == "llm":
        routing_prompt: str = node_def.get("routing_prompt", "")
        if routing_prompt:
            rendered_prompt = rendered_prompt + "\n\n" + routing_prompt
    return rendered_prompt, routing_mode


async def _invoke_node_model(rendered_prompt: str, model_backend_id_str: str, node_id: str) -> Any:
    """Invoke the configured model backend and return its parsed output.

    Resolves the ModelBackendHub (ContextVar), builds a HumanMessage from the
    rendered prompt, invokes the backend, and best-effort JSON-parses a
    string response. Raises when the hub is unavailable.
    """
    from modulo.core.pipeline_engine.decorator import get_model_backend_hub

    hub = get_model_backend_hub()
    if hub is None:
        raise RuntimeError(f"ModelBackendHub not available for node {node_id!r}")

    backend_id = uuid.UUID(model_backend_id_str)
    backend = await hub.get(backend_id)

    messages = [HumanMessage(content=rendered_prompt)]
    response = await backend.invoke(messages)

    content = response.content if hasattr(response, "content") else str(response)
    output_data: Any = content
    if isinstance(content, str):
        with suppress(json.JSONDecodeError, ValueError):
            output_data = json.loads(content)
    return output_data


def _finalize_node_result(
    node_id: str,
    output_data: Any,
    output_schema_json: dict[str, Any] | None,
    routing_mode: str | None,
) -> dict[str, Any]:
    """Validate schema, build the node artifact result, and surface a routed hop."""
    if isinstance(output_schema_json, dict) and isinstance(output_data, dict):
        _validate_against_schema(output_data, output_schema_json)

    result: dict[str, Any] = {
        "artifacts": [{"node_id": node_id, "status": "completed", "output": output_data}],
        "output": output_data,
    }

    # Extract _next_node from LLM routing output for the router.
    if routing_mode == "llm" and isinstance(output_data, dict):
        next_node = output_data.pop("_next_node", None)
        if next_node is not None:
            result["_llm_next_node"] = next_node

    return result


def make_node_fn(
    node_def: dict[str, Any],
    *,
    role: str | None = None,
    timeout: float | None = None,
    max_input_length: int | None = None,
    token_budget: int | None = None,  # NOSONAR S1172 - API kwarg (graph_cache); budget enforced at executor level
) -> Any:
    """Return a decorated async node function for use in a StateGraph.

    Renders the agent's prompt template against state via SandboxedEnvironment,
    invokes the configured model backend via ModelBackendHub, validates the
    output against the output schema (if defined), and returns the result
    in state["artifacts"] and state["output"].

    Nodes without a ``model_backend_id`` (connector-bindings, etc.) return a
    stub artifact without invoking a model.

    When *max_input_length* is set, input text from ``run_context["input"]`` is
    truncated before being passed to the LLM.

    When *token_budget* is set, per-node token budget is enforced at the
    executor level via ``node_token_budgets`` during ``_stream_graph()``.
    """
    node_id: str = str(node_def["id"])

    @cancellable_node(timeout=timeout, role=role)
    async def _node(state: dict[str, Any]) -> dict[str, Any]:
        # FAR-215: mid-run capability re-check at node start. If a bound
        # block-action guardrail's conformance claim is absent/unknown against
        # the live manifest, the node is blocked and routed to HITL — the gate
        # raises a LangGraph interrupt, so control never reaches the node body.
        agent_id_raw = node_def.get("agent_id")
        agent_id = _parse_uuid_opt(agent_id_raw)
        await _run_conformance_gate(state, node_id=node_id, agent_id=agent_id)
        run_context: dict[str, Any] = state.get("run_context") or {}
        raw_input = run_context.get("input", {})

        # Truncate input if max_input_length is configured for this agent.
        if max_input_length is not None and isinstance(raw_input, str):
            run_context["input"] = truncate_input(raw_input, max_input_length)

        # Get agent data from node_def (embedded at snapshot creation).
        prompt_template = node_def.get("prompt_template") or ""
        model_backend_id_str = node_def.get("model_backend_id")
        output_schema_json = node_def.get("output_schema_json")

        # FAR-332: a model_backend_id run_context override (fired by the variant
        # comparison / A/B test views) takes precedence over the snapshot-embedded
        # node_def backend, so every variant can run on a different model backend.
        # The override is namespaced under the reserved TOP-LEVEL ``_run_overrides``
        # key, seeded by the executor from the run's frozen variant config — never
        # read from ``run_context["input"]``, where a bare top-level
        # ``model_backend_id`` (or a crafted ``_run_overrides``) in user-supplied
        # input is DATA and could silently reroute model routing.
        #
        # FAR-342: a prompt_templates override (resolved from the variant's
        # prompt_version picker at run creation) likewise takes precedence over
        # the snapshot-embedded node_def prompt, so every variant renders with
        # its own prompt version. The override is a PER-AGENT map, so each node
        # reads ONLY the template for its own agent — one agent's template never
        # clobbers another's in a multi-agent snapshot. When the node's agent is
        # absent from the map, fall back to the node_def prompt. Because the
        # executor only ever seeds this from ``variant_config_snapshot``, a NORMAL
        # run that carries ``_run_overrides`` as caller input never reaches this
        # boundary (FAR-342 injection surface closed).
        model_backend_id_str, prompt_template = _resolve_node_run_overrides(
            run_context,
            agent_id,
            model_backend_id_str=model_backend_id_str,
            prompt_template=prompt_template,
        )

        # If no model_backend_id, fall back to stub behavior
        # (connector_binding nodes, manual nodes routed through wrong path, etc.).
        if not model_backend_id_str:
            return {"artifacts": [{"node_id": node_id, "status": "executed"}]}

        # FAR-418: context_scope — the agent's run_context VIEW (the keys fed to
        # the prompt template) is allowlist-gated to the node's need-to-know set.
        # The machinery reads (run_overrides, autonomy) still use the full
        # run_context so internal control keys are never starved.
        _node_cap = node_def.get("capability_scope") or {}
        scoped_run_context = filter_run_context_scope(run_context, _node_cap.get("context_scope"))
        # FAR-418 (MAJOR-3 fix): the need-to-know boundary must also bind the
        # ``state.run_context`` view. ``state`` otherwise carries the full,
        # unscoped run_context, so a template could read gated keys via
        # ``{{ state.run_context.<key> }}`` and defeat the boundary. Pass a
        # shallow copy with the scoped run_context in place — the live state dict
        # is never mutated.
        scoped_state = dict(state)
        scoped_state["run_context"] = scoped_run_context
        rendered_prompt, routing_mode = _render_agent_prompt(
            state=scoped_state,
            run_context=scoped_run_context,
            raw_input=raw_input,
            prompt_template=prompt_template,
            node_def=node_def,
        )

        output_data = await _invoke_node_model(rendered_prompt, model_backend_id_str, node_id)

        return _finalize_node_result(node_id, output_data, output_schema_json, routing_mode)

    _node.__name__ = f"node_{node_id}"
    return _node


def make_router_node_fn(
    router_config: dict[str, Any],
    *,
    node_id: str | None = None,
) -> Callable[[dict[str, Any]], str]:
    """Build a LangGraph *routing* function for a Router node (FAR-402 P1 / F2-A).

    Evaluates ordered ``rules`` ``{guard (JMESPath), target}`` against state,
    first-match-wins. An explicit ``default`` rule maps to its target. LLM
    routing mode (``mode == "classifier"``) matches the ``_llm_next_node`` state
    value against each rule's ``label`` (falling back to the default rule).

    The function reuses the shared JMESPath evaluator
    (:func:`evaluate_jmespath_condition`) — the same engine the existing
    conditional-edge compile path uses — so Router lowers onto that machinery.
    Every other branching primitive shares one truthiness rule.

    When no rule matches and there is no ``default`` rule, raises
    :class:`RouterNoMatchError` so the executor terminalizes the run with the
    ``router_no_match`` status (a terminal, non-failure outcome). Compile-time
    default-rule enforcement (see :func:`_validate_router_config`) is the
    primary guard; this runtime raise is the backstop for a mis-configured
    graph that slipped past validation.
    """
    rules: list[dict[str, Any]] = list(router_config.get("rules", []))
    classifier_mode: bool = router_config.get("mode") == "classifier"

    # Store (guard_expr, target) tuples. The guards are evaluated through the
    # shared JMESPath evaluator so Router and the conditional-edge compile path
    # share ONE truthiness rule.
    rule_targets: list[tuple[str | None, str | None]] = []
    default_target: str | None = None
    for rule in rules:
        target = rule.get("target") or rule.get("target_port")
        if rule.get("default"):
            default_target = target
            continue
        rule_targets.append((rule.get("guard"), target))

    def _router(state: dict[str, Any]) -> str:
        for guard, target in rule_targets:
            if target is not None and evaluate_jmespath_condition(state, guard):
                return target
        if classifier_mode:
            label = state.get("_llm_next_node")
            if label is not None:
                for rule in rules:
                    if rule.get("label") == label:
                        matched: str | None = rule.get("target") or rule.get("target_port")
                        if matched is not None:
                            return matched
        if default_target:
            return default_target
        raise RouterNoMatchError(node_id=node_id)

    _router.__name__ = f"router_{node_id}" if node_id else "router"
    return _router


async def _invoke_backend(
    hub: Any,
    backend_id: uuid.UUID,
    messages: list[Any],
) -> Any:
    """Resolve a backend from the hub and invoke it asynchronously."""
    backend = await hub.get(backend_id)
    return await backend.invoke(messages)


_JUDGE_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _run_coroutine_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine to completion from a sync context.

    ``make_hitl_gate_fn`` runs inside an already-running asyncio event loop,
    so ``asyncio.run`` and ``loop.run_until_complete`` both raise "This event
    loop is already running". The LLMJudgeCallable protocol is synchronous, so
    we bridge sync -> async by executing the coroutine on a dedicated event
    loop in a worker thread and blocking on the result.
    """
    global _JUDGE_EXECUTOR
    if _JUDGE_EXECUTOR is None:
        _JUDGE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="llm-judge",
        )

    def _run() -> Any:
        return asyncio.run(coro)

    future: concurrent.futures.Future[Any] = _JUDGE_EXECUTOR.submit(_run)
    return future.result()


def _build_llm_judge_callable(
    hub: Any,
    backend_id_str: str,
) -> Callable[[dict[str, Any], EvalDefinition], dict[str, Any]]:
    """Build a synchronous LLM judge callable backed by a model backend.

    The ``LLMJudgeCallable`` protocol is synchronous, but ``make_hitl_gate_fn``
    runs inside an already-running asyncio event loop. We bridge sync -> async
    by running the async backend invoke on a dedicated event loop in a worker
    thread via ``_run_coroutine_sync``.
    """
    backend_id = uuid.UUID(backend_id_str)

    def _judge(
        output: dict[str, Any],
        eval_def: EvalDefinition,
    ) -> dict[str, Any]:
        field = eval_def.config.get("field", "")
        content = output.get(field, "")
        prompt = (
            "Evaluate the following output and return JSON with keys passed "
            "(bool), score (float 0-1), detail (str).\n\n"
            f"Output:\n{content}"
        )
        messages = [HumanMessage(content=prompt)]
        response = _run_coroutine_sync(_invoke_backend(hub, backend_id, messages))
        text = response.content if hasattr(response, "content") else str(response)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"passed": False, "score": 0.0, "detail": text}
        detail = parsed.get("detail", "")
        return {
            "passed": bool(parsed.get("passed", False)),
            "score": parsed.get("score"),
            "detail": str(detail) if detail is not None else "",
        }

    return _judge


def _resolve_gate_eval_envelope(
    state: dict[str, Any],
    node_id: str,
) -> dict[str, Any]:
    """Build the gate-eval envelope from the source node's artifact (FAR-311)."""
    artifacts = state.get("artifacts")
    matching = (
        [a for a in artifacts if isinstance(a, dict) and a.get("node_id") == node_id]
        if isinstance(artifacts, list)
        else []
    )
    envelope: dict[str, Any] = {}
    if matching:
        # The source node's most recent artifact (a reject/correction re-run
        # appends another entry under the same node_id). Its ``output`` is the
        # source's OWN output — for an ``agent`` source the contract return is
        # ``envelope["output"]``, so it must come from the matched artifact
        # (``state["output"]`` is last-write-wins across a fan-out and can
        # belong to a sibling).
        matched = matching[-1]
        envelope["artifacts"] = [matched]
        if "output" in matched:
            envelope["output"] = matched["output"]
        elif "output" in state:
            envelope["output"] = state["output"]
    elif "output" in state:
        envelope["output"] = state["output"]
    return envelope


def _resolve_gate_eval_target(
    state: dict[str, Any],
    node_id: str | None,
    node_type_map: dict[str, str] | None,
) -> Any:
    """Resolve the output a HITL gate's eval should validate (FAR-311).

    Gate evals are node-scoped to the edge's SOURCE node, but LangGraph merges
    each node's envelope into the state at its TOP-LEVEL keys — the node id is
    not a state key — so ``state["output"]`` holds the outer ``output``
    envelope (telemetry for a sandbox_agent: status/summary/cost, no pr_url /
    changed_files) and ``state["artifacts"]`` accumulates every completed
    node's artifact. The source node's own artifact is located by its
    ``node_id`` (not position — parallel fan-out concatenates artifacts in
    completion order, so the last artifact may belong to a sibling). That
    artifact's ``output.output_json`` is the sandbox agent's CONTRACT return;
    re-assembling the envelope and running it through ``split_node_output``
    yields the SAME contract output the standalone ``_run_post_node_evals``
    path validates (FAR-311).

    Falls back to the whole *state* when the shape cannot be resolved (no type
    map, unknown node type, or a state with no envelope keys) so legacy
    non-graph states keep their historical behaviour.
    """
    if not isinstance(state, dict) or not node_id or not node_type_map:
        return state
    node_type = node_type_map.get(node_id) or DEFAULT_NODE_TYPE
    if node_type not in SPLITTABLE_NODE_TYPES:
        return state
    artifacts = state.get("artifacts")
    matching = (
        [a for a in artifacts if isinstance(a, dict) and a.get("node_id") == node_id]
        if isinstance(artifacts, list)
        else []
    )
    if not matching and "output" not in state:
        return state
    envelope = _resolve_gate_eval_envelope(state, node_id)
    found, contract_output = resolve_node_contract_output(envelope, node_type)
    if found:
        return contract_output
    _log.debug(
        "hitl_gate.eval.missing_contract_output",
        extra={"node_id": str(node_id), "node_type": node_type},
    )
    return state


def _build_hitl_gate_artifact(gate_id: str, status: str, **extra: Any) -> dict[str, Any]:
    """Build the standard HITL gate artifact envelope."""
    return {"artifacts": [{"node_id": gate_id, "status": status, **extra}]}


async def _dispatch_reject_correction_inner(
    session_factory: Any,
    org_id: Any,
    run_id_for_correction: Any,
    correction_target: Any,
    node_output: dict[str, Any],
    decision: Any,
    gate_id: str,
) -> None:
    """Dispatch a single-node correction for a rejected HITL gate (best-effort)."""
    from modulo.core.feedback_manager import dispatch_reject_correction

    try:
        await dispatch_reject_correction(
            session_factory=session_factory,
            org_id=org_id,
            run_id=run_id_for_correction,
            node_id=str(correction_target),
            node_input=node_output,
            rejection_reason=(
                (decision.get("reason") if isinstance(decision, dict) else None) or "rejected via HITL gate"
            ),
            gate_id=gate_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "hitl_gate.reject_correction_dispatch_failed",
            extra={"gate_id": gate_id, "node_id": str(correction_target)},
        )


async def _dispatch_reject_correction_best_effort(
    state: dict[str, Any],
    decision: Any,
    gate_id: str,
    hitl_gate_config: dict[str, Any],
    session_factory: Any,
    org_id: Any,
) -> None:
    # FAR-210 follow-up: the reject→correction edge. When this gate declares
    # a ``correction_target`` and the human REJECTED it, dispatch the
    # single-node correction for the blocked node (the AUTOMATED path) instead
    # of only kicking back to the plain ``reject_target``. Best-effort and
    # fully failure-isolated — a correction dispatch failure must never crash
    # the reject path.
    action = decision.get("action") if isinstance(decision, dict) else None
    is_rejected = action == "rejected"
    if is_rejected and session_factory is not None and org_id is not None:
        correction_target = hitl_gate_config.get("correction_target")
        run_id_for_correction = state.get("_run_id")
        node_output = state.get("output")
        if correction_target and run_id_for_correction and isinstance(node_output, dict):
            await _dispatch_reject_correction_inner(
                session_factory, org_id, run_id_for_correction, correction_target, node_output, decision, gate_id
            )


def _hitl_gate_deliver_manual_result(
    gate_id: str,
    decision: Any,
) -> tuple[bool, dict[str, Any]]:
    """Build the gate result for a manual-delivery resume decision."""
    manual_output = decision.get("output", {})
    return (
        True,
        {
            "artifacts": [
                {
                    "node_id": gate_id,
                    "status": "interrupted",
                    "result": "delivered_manual",
                    "human_data": decision,
                    "manual_output": manual_output,
                }
            ],
            "output": manual_output,
        },
    )


def _hitl_gate_approve_reject_result(
    gate_id: str,
    decision: Any,
    is_rejected: bool,
) -> dict[str, Any]:
    """Build the gate result for an approve/reject resume decision."""
    result_status = "rejected" if is_rejected else "approved"
    gate_result: dict[str, Any] = {
        "artifacts": [
            {
                "node_id": gate_id,
                "status": "interrupted",
                "result": result_status,
                "human_data": decision,
            }
        ],
    }
    # If the human provided modified output, write it into state so
    # downstream nodes receive the human's version instead of the
    # original agent output.
    if isinstance(decision, dict) and "modified_output" in decision:
        gate_result["output"] = decision["modified_output"]
    return gate_result


async def _hitl_gate_resume_result(
    decision: Any,
    gate_id: str,
    state: dict[str, Any],
    hitl_gate_config: dict[str, Any],
    session_factory: Any,
    org_id: Any,
) -> tuple[bool, dict[str, Any] | None]:
    """Handle a resume where ``state`` carries ``_hitl_decision``.

    Returns ``(True, gate_result)`` when the human's decision resolves the
    gate, or ``(False, None)`` on first invocation so the caller proceeds to
    the condition/eval/autonomy checks.
    """
    if decision is None:
        return (False, None)
    action = decision.get("action") if isinstance(decision, dict) else None
    if action == "deliver_manual":
        return _hitl_gate_deliver_manual_result(gate_id, decision)
    is_rejected = action == "rejected"
    await _dispatch_reject_correction_best_effort(state, decision, gate_id, hitl_gate_config, session_factory, org_id)
    return (True, _hitl_gate_approve_reject_result(gate_id, decision, is_rejected))


def _hitl_gate_condition_skip(gate_id: str, condition_expr: str | None, state: dict[str, Any]) -> dict[str, Any] | None:
    """Evaluate the conditional-gate JMESPath expression; skip artifact when falsy."""
    if condition_expr:
        try:
            compiled = compile_jmespath(condition_expr)
        except ValueError:
            _log.exception("hitl_gate.invalid_condition", extra={"condition": condition_expr})
            raise ValueError(f"Invalid HITL gate condition expression: {condition_expr}") from None
        result = compiled.search(state)
        if not bool(result):
            # Condition falsy — skip the gate entirely. Preserve the raw result
            # in the artifact (mirrors the pre-refactor behaviour).
            return _build_hitl_gate_artifact(
                gate_id, "condition_skipped", condition=condition_expr, condition_result=result
            )
    return None


def _resolve_llm_judge_callable(eval_def: Any) -> Any:
    """Resolve the LLM judge callable for an eval definition, if configured."""
    if eval_def.eval_type == EvalType.LLM_JUDGE and eval_def.config.get("model_backend_id"):
        from modulo.core.pipeline_engine.decorator import get_model_backend_hub

        hub = get_model_backend_hub()
        if hub is not None:
            return _build_llm_judge_callable(hub, str(eval_def.config["model_backend_id"]))
    return None


async def _persist_gate_eval_results(
    state: dict[str, Any],
    eval_definitions: Sequence[EvalDefinition],
    eval_results_by_name: dict[str, EvalResult],
    session_factory: Any,
    org_id: Any,
) -> None:
    """Persist gate eval results to the eval_results table (best-effort)."""
    if session_factory is not None and org_id is not None:
        try:
            _run_id: uuid.UUID | None = state.get("_run_id")
            if _run_id is not None:
                async with session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    await set_rls_execution_context(session)
                    for eval_def in eval_definitions:
                        eval_result = eval_results_by_name[eval_def.name]
                        node_uuid: uuid.UUID | None = uuid.UUID(eval_def.node_id) if eval_def.node_id else None
                        db_result = EvalResultModel(
                            organisation_id=org_id,
                            run_id=_run_id,
                            node_id=node_uuid,
                            eval_id=eval_def.id,
                            eval_definition_version=eval_def.version,
                            passed=eval_result.passed,
                            score=eval_result.score,
                            detail=eval_result.detail,
                        )
                        session.add(db_result)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("hitl_gate.persist_eval_failed")


async def _run_gate_evals(
    state: dict[str, Any],
    eval_definitions: Sequence[EvalDefinition] | None,
    node_type_map: dict[str, str] | None,
    gate_id: str,
    session_factory: Any,
    org_id: Any,
) -> dict[str, EvalResult]:
    """Run the gate's node-scoped evals (eval-before-interrupt) and persist results."""
    eval_results_by_name: dict[str, EvalResult] = {}
    if eval_definitions:
        engine = EvalEngine()
        for eval_def in eval_definitions:
            llm_judge_callable = _resolve_llm_judge_callable(eval_def)
            eval_result = engine.evaluate(
                _resolve_gate_eval_target(state, eval_def.node_id, node_type_map),
                eval_def,
                llm_judge_callable=llm_judge_callable,
            )
            eval_results_by_name[eval_def.name] = eval_result
            _log.info(
                "hitl_gate.eval_result",
                extra={
                    "gate_id": gate_id,
                    "eval_name": eval_def.name,
                    "eval_id": str(eval_def.id),
                    "passed": eval_result.passed,
                    "score": eval_result.score,
                    "detail": eval_result.detail,
                },
            )
        # If any block eval failed, EvalBlockedError was raised above.
        await _persist_gate_eval_results(state, eval_definitions, eval_results_by_name, session_factory, org_id)
    return eval_results_by_name


def _hitl_gate_eval_condition_skip(
    gate_id: str,
    eval_condition_raw: Any,
    eval_results_by_name: dict[str, EvalResult],
) -> dict[str, Any] | None:
    """Evaluate the eval-reference condition against captured eval results."""
    if eval_condition_raw is not None and eval_results_by_name:
        eval_name: str = eval_condition_raw.get("eval_name", "")
        threshold: float = eval_condition_raw.get("threshold", 0.0)
        operator: str = eval_condition_raw.get("operator", "lt")
        matched_result = eval_results_by_name.get(eval_name)
        if matched_result is not None:
            score: float = matched_result.score or 0.0
            condition_true: bool = _evaluate_eval_condition(score, threshold, operator)
            _log.info(
                "hitl_gate.eval_condition",
                extra={
                    "gate_id": gate_id,
                    "eval_name": eval_name,
                    "score": score,
                    "threshold": threshold,
                    "operator": operator,
                    "condition_true": condition_true,
                },
            )
            if not condition_true:
                return _build_hitl_gate_artifact(
                    gate_id,
                    "condition_skipped",
                    condition=eval_condition_raw,
                    condition_result=False,
                )
    return None


def _hitl_gate_autonomy_result(
    gate_id: str, state: dict[str, Any], human_only: bool
) -> tuple[Any, dict[str, Any] | None]:
    """Determine effective autonomy level; return skip/auto-approve artifact, if any."""
    # Determine effective autonomy level from run_context.
    run_context: dict[str, Any] = state.get("run_context") or {}
    pipeline_default: str | None = run_context.get("_pipeline_default_autonomy")
    autonomy = effective_autonomy_level(pipeline_default, run_context)
    human_only_effective: bool = human_only

    # human_only overrides everything — always interrupt.
    if not human_only_effective and should_skip_hitl_gate(autonomy):
        # fully_autonomous: silently skip the gate.
        return (autonomy, _build_hitl_gate_artifact(gate_id, "skipped", autonomy=autonomy.value))
    if not human_only_effective and should_notify_on_complete(autonomy):
        # notify_on_complete: auto-approve, record notification artifact.
        return (
            autonomy,
            _build_hitl_gate_artifact(gate_id, "auto_approved", autonomy=autonomy.value),
        )
    return (autonomy, None)


def make_hitl_gate_fn(
    hitl_gate_config: dict[str, Any],
    *,
    eval_definitions: Sequence[EvalDefinition] | None = None,
    session_factory: Callable[..., Any] | None = None,
    org_id: uuid.UUID | None = None,
    node_type_map: dict[str, str] | None = None,
) -> Any:
    """Return a node function that raises a HITL interrupt.

    The node checks the effective autonomy level from ``run_context`` at
    runtime.  If the gate should be bypassed (autonomous mode) or
    auto-approved (notify mode), no interrupt is raised.

    Conditional gating:
      If ``hitl_gate_config`` contains a ``condition`` JMESPath expression,
      it is evaluated against the current state.  If the result is falsy
      the gate is skipped entirely (no autonomy or decision checks).

    Eval-before-interrupt:
      If ``eval_definitions`` is provided, each definition is evaluated
      against the current state *after* the condition check but *before*
      the interrupt.  Any eval with ``failure_behaviour='block'`` that
      fails raises ``EvalBlockedError``, preventing the interrupt.

      Evals target the SOURCE node's CONTRACT output (the agent's actual
      return) when ``node_type_map`` is provided — the same target the
      standalone ``_run_post_node_evals`` path validates — rather than the
      whole merged state (whose ``output`` key holds the outer envelope /
      telemetry for a sandbox_agent).  Without a type map the whole state is
      used as before.

      If ``session_factory`` is provided, eval results are persisted to
      the ``eval_results`` table so that post-run suite-level threshold
      checks (``_check_eval_suites``) can read them.

    On resume (via ``aupdate_state`` + ``astream_events(None, config)``),
    the node is re-invoked with ``state["_hitl_decision"]`` populated.
    It then returns artifacts reflecting the human's decision.
    """
    gate_id: str = hitl_gate_config.get("gate_id", "gate")
    human_only: bool = hitl_gate_config.get("human_only", False)
    condition_expr: str | None = hitl_gate_config.get("condition")
    eval_condition_raw: dict[str, Any] | None = hitl_gate_config.get("eval_condition")
    required_team_id: str | None = _normalize_required_team_id(gate_id, hitl_gate_config.get("required_team_id"))

    async def _hitl_gate(state: dict[str, Any]) -> dict[str, Any]:
        # --- Resume check — always first so condition/evals aren't re-evaluated. ---
        resumed, resume_result = await _hitl_gate_resume_result(
            state.get("_hitl_decision"),
            gate_id,
            state,
            hitl_gate_config,
            session_factory,
            org_id,
        )
        if resumed:
            return resume_result  # type: ignore[return-value]

        # --- Conditional gate (Section 8.17) — evaluate condition against state. ---
        condition_skip = _hitl_gate_condition_skip(gate_id, condition_expr, state)
        if condition_skip is not None:
            return condition_skip

        # --- Eval-before-interrupt (Section 8.17) — run node-scoped evals. ---
        eval_results_by_name = await _run_gate_evals(
            state,
            eval_definitions,
            node_type_map,
            gate_id,
            session_factory,
            org_id,
        )

        # --- Eval-reference condition check (Section 8.17 v1). ---
        eval_condition_skip = _hitl_gate_eval_condition_skip(gate_id, eval_condition_raw, eval_results_by_name)
        if eval_condition_skip is not None:
            return eval_condition_skip

        # --- Autonomy skip/approve. ---
        autonomy, autonomy_result = _hitl_gate_autonomy_result(gate_id, state, human_only)
        if autonomy_result is not None:
            # skipped (fully_autonomous) or auto_approved (notify_on_complete):
            # record the effective autonomy granted as evidence (fail-open).
            outcome = autonomy_result["artifacts"][0]["status"]
            await emit_autonomy_telemetry(
                session_factory,
                org_id=org_id,
                run_id=state.get("_run_id"),
                gate_id=gate_id,
                autonomy_level=autonomy.value,
                gate_outcome=outcome,
                human_only=human_only,
            )
            return autonomy_result

        # --- First invocation — store config and interrupt. ---
        # Gate fired (human path): record the effective autonomy + fired outcome.
        await emit_autonomy_telemetry(
            session_factory,
            org_id=org_id,
            run_id=state.get("_run_id"),
            gate_id=gate_id,
            autonomy_level=autonomy.value,
            gate_outcome="fired",
            human_only=human_only,
        )
        hitl_gates: list[dict[str, Any]] = list(state.get("_hitl_gates") or [])
        hitl_gates.append(hitl_gate_config)
        state["_hitl_gates"] = hitl_gates

        # State mutations before the interrupt are persisted by the checkpointer.
        decision = interrupt(
            {
                "gate_id": gate_id,
                "autonomy_level": autonomy.value,
                "human_only": human_only,
                "overdue_threshold_minutes": hitl_gate_config.get("overdue_threshold_minutes"),
                "required_team_id": required_team_id,
            }
        )
        return await _hitl_gate({**state, "_hitl_decision": decision})

    _hitl_gate.__name__ = f"hitl_gate_{gate_id}"
    return _hitl_gate


def make_manual_node_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,  # NOSONAR S1172 - API kwarg (graph_cache); manual nodes never time out
) -> Any:
    """Return a node function for a manual-input node.

    The node immediately interrupts and waits for human output. On resume the
    output is validated against output_schema_id (if defined) before continuing.
    """
    node_id: str = str(node_def["id"])
    output_schema_json: dict[str, Any] | None = node_def.get("output_schema_json")
    manual_prompt: str = node_def.get("manual_prompt", "")

    async def _manual_node(state: dict[str, Any]) -> dict[str, Any]:
        # Check if this is a resume with human output.
        decision = state.get("_hitl_decision")
        if decision is not None and isinstance(decision, dict):
            resume_data = decision.get("output")
            manual_output: dict[str, Any] | None = resume_data if isinstance(resume_data, dict) else None
            if output_schema_json and manual_output is not None:
                _validate_against_schema(manual_output, output_schema_json)

            _log.info(
                "manual_node.completed",
                extra={
                    "node_id": node_id,
                    "has_output_schema": output_schema_json is not None,
                },
            )

            return {
                "artifacts": [
                    {
                        "node_id": node_id,
                        "status": "completed",
                        "human_output": manual_output,
                    }
                ],
                "manual_output": manual_output,
            }

        # First invocation —  record pending artifact and interrupt.
        artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])
        artifacts.append({"node_id": node_id, "status": "awaiting_human"})
        state["artifacts"] = artifacts

        _log.info(
            "manual_node.awaiting_human",
            extra={
                "node_id": node_id,
                "prompt": manual_prompt or "",
            },
        )

        decision = interrupt(
            {
                "manual": True,
                "node_id": node_id,
                "prompt": manual_prompt,
                "output_schema_id": node_def.get("output_schema_id"),
            }
        )
        return await _manual_node({**state, "_hitl_decision": decision})

    _manual_node.__name__ = f"manual_{node_id}"
    return _manual_node


def _resolve_binding_connector(
    binding: dict[str, Any],
    node_id: str,
    *,
    allowed_connectors: list[str] | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """Resolve a bound connector instance for *node_id*.

    Returns ``(connector, None)`` on success, or ``(None, error_artifact)``
    when the hub is unavailable, the instance id is missing, the connector
    is outside the node's ``capability_scope.allowed_connectors``, or the
    connector cannot be resolved — the error artifact is already enveloped.

    FAR-418: when *allowed_connectors* is set, a bound connector excluded by
    the scope fails FAST with a typed, logged, metric-emitting
    ``ScopeViolationError`` (never silently). Absent = unrestricted.
    """
    from modulo.core.capability_scope import (
        ScopeViolationError,
        is_connector_allowed,
        record_scope_violation,
    )
    from modulo.core.pipeline_engine.decorator import get_connector_hub

    hub = get_connector_hub()
    if hub is None:
        return (
            None,
            {"artifacts": [{"node_id": node_id, "status": "executed", "output": {"note": "no connector hub"}}]},
        )

    instance_id_str = binding.get("instance_id")
    if not instance_id_str:
        return None, {"artifacts": [{"node_id": node_id, "status": "failed", "error": "no connector instance_id"}]}

    import uuid as _uuid

    instance_uuid = _uuid.UUID(str(instance_id_str))

    # FAR-418: deny-by-default within the node's connector scope.
    connector_type: str = binding.get("type", "")
    if not is_connector_allowed(
        connector_instance_id=instance_uuid,
        connector_type=connector_type,
        allowed_connectors=allowed_connectors,
    ):
        target = connector_type or str(instance_uuid)
        scope_err = ScopeViolationError(node_id=node_id, target=target, kind="connector")
        record_scope_violation(node_id=node_id, target=target, kind="connector")
        _log.error("scope.violation node=%s connector=%s", node_id, target)
        return None, {"artifacts": [{"node_id": node_id, "status": "failed", "error": str(scope_err)}]}

    try:
        connector = hub.get(instance_uuid)
    except Exception as _conn_exc:
        return None, {"artifacts": [{"node_id": node_id, "status": "failed", "error": f"connector error: {_conn_exc}"}]}
    return connector, None


def _connector_inputs(binding: dict[str, Any], state: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Merge run input into the connector filters/data and return (resource, filters, data).

    Run input keys slot into ``filters`` by default and into ``data`` only
    when the binding already carries them; a shell connector without a
    ``provider_ref`` defaults to ``"/"``.
    """
    run_context = state.get("run_context") or {}
    raw_input = run_context.get("input", {})
    resource: str = binding.get("resource", "command")
    filters = dict(binding.get("filters", {}))
    data = dict(binding.get("data", {}))
    if isinstance(raw_input, dict):
        filters.update({k: v for k, v in raw_input.items() if k not in data})
        data.update({k: v for k, v in raw_input.items() if k not in filters})

    # Ensure provider_ref for shell connectors
    if "provider_ref" not in filters and "provider_ref" not in data:
        filters["provider_ref"] = "/"
    return resource, filters, data


def _enforce_connector_scope(
    binding: dict[str, Any],
    node_id: str,
    connector_type: str,
    allowed_connectors: list[str] | None,
) -> dict[str, Any] | None:
    """FAR-418 deny-by-default scope gate for a connector node.

    Fires BEFORE the connector is resolved from the hub, so a node that targets
    a connector excluded by its ``capability_scope`` never decrypts or touches
    the connection (fail-fast with a typed, logged, metric-emitting
    ``ScopeViolationError``). The hub is only consulted for in-scope connectors.
    Returns a failed-artifact dict on violation, else ``None``.
    """
    from modulo.core.capability_scope import (
        ScopeViolationError,
        is_connector_allowed,
        record_scope_violation,
    )

    instance_id_str = binding.get("instance_id")
    if not instance_id_str:
        return None
    import uuid as _uuid

    instance_uuid = _uuid.UUID(str(instance_id_str))
    if is_connector_allowed(
        connector_instance_id=instance_uuid,
        connector_type=connector_type,
        allowed_connectors=allowed_connectors,
    ):
        return None
    target = connector_type or str(instance_uuid)
    scope_err = ScopeViolationError(node_id=node_id, target=target, kind="connector")
    record_scope_violation(node_id=node_id, target=target, kind="connector")
    _log.error("scope.violation node=%s connector=%s", node_id, target)
    return {"artifacts": [{"node_id": node_id, "status": "failed", "error": str(scope_err)}]}


async def _run_connector_action(
    connector: Any,
    op: str,
    resource: str,
    filters: dict[str, Any],
    data: dict[str, Any],
) -> Any:
    """Execute a connector ``write``/``query`` action and return its result."""
    from modulo.connectors.base import ConnectorPayload, ConnectorQuery

    if op == "write":
        payload = ConnectorPayload(resource=resource, data=data)
        return await connector.write(payload)
    query = ConnectorQuery(resource=resource, filters=filters)
    return await connector.query(query)


def _guard_connector_secret_output(result: Any, node_id: str) -> dict[str, Any] | None:
    """FAR-418 secret hygiene: connector/secret OBJECTS are never valid port
    payload types — only opaque connector IDs may enter state.

    Guards the output before it is written into the run's state/ports. Returns
    a failed-artifact dict on violation, else ``None``.
    """
    from modulo.core.capability_scope import (
        ScopeViolationError,
        assert_no_secret_objects,
        record_scope_violation,
    )

    try:
        assert_no_secret_objects(result, node_id=node_id)
    except ScopeViolationError as scope_err:
        record_scope_violation(node_id=node_id, target=scope_err.target, kind="secret")
        _log.error("scope.violation node=%s secret=%s", node_id, scope_err.target)
        return {"artifacts": [{"node_id": node_id, "status": "failed", "error": str(scope_err)}]}
    return None


def make_connector_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
    session_factory: Callable[..., Any] | None = None,
) -> Any:
    """Return a decorated async node function that resolves a connector
    from the ConnectorHub and executes a connector action (query/write).

    The node_def must have a 'connector_binding' dict with:
      - instance_id: uuid of the ConnectorInstance
      - type: connector type (e.g. 'shell')
      - operation: 'query' or 'write' (optional, default 'query')
      - input: dict of input parameters (optional)

    ``session_factory`` (FAR-458) enables the connector-write UNKNOWN-recovery
    read-before-write dedupe: for a ``write`` operation the node loads the run's
    persisted idempotency key + markers and, when ``read_before_write_suppression``
    reports the write was already delivered on the SAME derived key, returns a
    skipped envelope WITHOUT re-sending the duplicate upstream write. On a
    successful write it stamps a ``delivery_done`` marker (the evidence the
    suppression consumes). Both paths are STRICTLY fail-open: a missing run id /
    session factory / persisted key, or a DB error, proceeds exactly as before
    (write sent, no suppression), so the gate can never block or change a
    connector write that has no idempotency context.

    FAR-458 refinement: the AMBIGUOUS (couldn't-confirm-delivery) decision is
    per-connector-per-write via the connector's ``on_unknown`` mode
    (``ConnectorBase.on_unknown_for`` / ``_connector_on_unknown``). The default
    ``fail_open`` keeps the gate fail-open on ambiguity (possible duplicate);
    ``fail_closed`` suppresses an ambiguous write (possible silent miss);
    ``off`` bypasses the gate entirely. The CONFIRMED-delivered suppression is
    mode-independent.

    FAR-531 intent markers: after the gate proceeds and BEFORE the upstream
    write fires, an IN-FLIGHT intent marker (same derived key, same marker
    slot) is persisted when the shared gate-eligibility policy says the gate
    could suppress (killswitch enabled, mode not ``off``). Success promotes it
    in place to ``delivery_done: True``; a reported failure (the connector's
    result shape reports failure via the ``write_reported_failure`` hook)
    resolves it to ``no_delivery_confirmed: True`` so a later attempt re-fires
    under BOTH modes. A RAISED connector error is AMBIGUOUS (QA Fix 1) — the
    intent stays in-flight: fail_closed suppresses, fail_open re-fires. A
    crash/timeout between the intent persist and the resolution leaves it
    in-flight — the ambiguous state fail_closed suppresses (the previously
    unreachable protection) and fail_open re-fires (unchanged).
    """
    node_id: str = str(node_def["id"])
    binding = node_def.get("connector_binding") or {}
    op: str = binding.get("operation", "query")
    # FAR-418: node-level capability_scope. ``allowed_connectors`` narrows (never
    # widens) which connectors this node may resolve — deny-by-default within the
    # scope. Absent/empty (the UNRESTRICTED default) preserves the pre-scope
    # behaviour: the node may use anything the hub fetched.
    scope = node_def.get("capability_scope") or {}
    allowed_connectors: list[str] | None = scope.get("allowed_connectors")
    connector_type: str = binding.get("type", "")

    @cancellable_node(timeout=timeout)
    async def _connector_node(state: dict[str, Any]) -> dict[str, Any]:
        # FAR-215: mid-run capability re-check at node start (block -> HITL).
        # The gate raises a LangGraph interrupt on block; control only reaches
        # the node body when the node may proceed.
        instance_id = _parse_uuid_opt(binding.get("instance_id"))
        await _run_conformance_gate(
            state,
            node_id=node_id,
            connector_instance_ids=[instance_id] if instance_id is not None else [],
        )

        scope_block = _enforce_connector_scope(binding, node_id, connector_type, allowed_connectors)
        if scope_block is not None:
            return scope_block

        connector, error_artifact = _resolve_binding_connector(
            binding,
            node_id,
            allowed_connectors=allowed_connectors,
        )
        if error_artifact is not None:
            return error_artifact

        resource, filters, data = _connector_inputs(binding, state)

        # FAR-458 connector-write UNKNOWN-recovery: the read-before-write dedupe
        # decision point. Only a WRITE is side-effecting; a query never double-
        # submits. The gate reads the run's persisted idempotency key + markers,
        # and suppresses a CONFIRMED-delivered duplicate (matching key +
        # ``delivery_done``) in EVERY mode, and additionally suppresses an
        # AMBIGUOUS (matching key, no ``delivery_done``, no
        # ``no_delivery_confirmed``) write when the connector's ``on_unknown``
        # policy is ``fail_closed``. Fail-open in every direction — no run id /
        # session factory / persisted key, the killswitch, a DB error, or
        # ``on_unknown="off"`` all proceed to send the write normally; default
        # ``fail_open`` lets an unconfirmed write fire.
        intent_active = False
        if op == "write":
            run_id = str(state.get("_run_id", "") or "")
            on_unknown_mode = _connector_on_unknown(connector, resource)
            gate_result = await _connector_write_gate(
                session_factory,
                run_id=run_id,
                org_id_raw=state.get("_org_id"),
                node_id=node_id,
                resource=resource,
                filters=filters,
                data=data,
                on_unknown=on_unknown_mode,
            )
            if gate_result is not None:
                return gate_result
            # FAR-531 intent marker (write-before / stamp-after): persisted
            # AFTER the gate proceeds and BEFORE the upstream write fires, in
            # the SAME slot the delivery stamp updates. A crash/timeout between
            # here and the stamp leaves the marker in-flight — the ambiguous
            # state fail_closed suppresses on a later attempt (the headline
            # fix; fail_open re-fires, unchanged). Guarded by the killswitch +
            # ``on_unknown != off`` — pointless when the gate can never
            # suppress. Best-effort: an intent-write failure never fails the
            # node.
            intent_active = _connector_intent_marker_enabled(on_unknown_mode)
            if intent_active:
                # QA Fix 5: the intent persist (incl. its payload-hash
                # computation) must never fail the node BEFORE the write — any
                # failure degrades to "no marker" and the write still fires.
                try:
                    await _persist_connector_write_intent(
                        session_factory,
                        run_id=run_id,
                        org_id_raw=state.get("_org_id"),
                        node_id=node_id,
                        resource=resource,
                        filters=filters,
                        data=data,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception(
                        "connector.connector_write_intent_persist_failed",
                        extra={"run_id": run_id, "node_id": node_id},
                    )

        try:
            result = await _run_connector_action(connector, op, resource, filters, data)
        except Exception as exc:
            # QA Fix 1: the raised connector error is classified by the SINGLE
            # authority (``_resolve_connector_write_outcome``) as AMBIGUOUS — a
            # raise cannot tell whether the write landed (read-timeout after
            # dispatch vs pre-dispatch validation failure), so the in-flight
            # intent marker is left AS-IS: fail_closed suppresses the re-fire
            # ("possible silent miss"), fail_open re-fires (unchanged). No
            # no-delivery evidence is persisted for a raise.
            await _resolve_connector_write_outcome(
                session_factory,
                connector=connector,
                run_id=str(state.get("_run_id", "") or ""),
                org_id_raw=state.get("_org_id"),
                node_id=node_id,
                resource=resource,
                filters=filters,
                data=data,
                result=None,
                intent_active=intent_active,
                exception=exc,
            )
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": str(exc)}]}

        # FAR-458: a successful connector WRITE genuinely reached upstream —
        # stamp the delivery marker (bounded, fail-open) so a re-run reusing the
        # SAME persisted key suppresses the duplicate. The full write identity
        # (resource + filters + data) is folded into the derived key on BOTH the
        # gate and the stamp so a target/content edit derives a fresh key.
        # FAR-531 AC6: whether a non-raising result actually delivered is the
        # connector's call (``write_reported_failure`` hook) — a reported
        # failure is a DEFINITE no-delivery (the intent marker resolves to
        # ``no_delivery_confirmed``), never a delivery stamp.
        if op == "write":
            await _resolve_connector_write_outcome(
                session_factory,
                connector=connector,
                run_id=str(state.get("_run_id", "") or ""),
                org_id_raw=state.get("_org_id"),
                node_id=node_id,
                resource=resource,
                filters=filters,
                data=data,
                result=result,
                intent_active=intent_active,
            )

        scope_block = _guard_connector_secret_output(result, node_id)
        if scope_block is not None:
            return scope_block

        return {
            "artifacts": [{"node_id": node_id, "status": "completed", "output": result}],
            "output": result,
        }

    _connector_node.__name__ = f"connector_{node_id}"
    return _connector_node


_secret_ref_re = _re.compile(r"^\{\{\s*secrets\.(\w+)\s*\}\}$")


async def resolve_env_var_refs(
    env_vars: dict[str, Any],
    resolver: Callable[[str], Awaitable[str | None]],
) -> dict[str, str]:
    """Resolve ``{{ secrets.KEY }}`` references in env var values.

    Non-reference values pass through unchanged. ``{{ secrets.KEY }}`` values
    are resolved via *resolver*; an UNRESOLVED reference (resolver returns
    None) is OMITTED from the returned dict with a warning naming the key
    (FAR-480). It is never resolved to an empty string: an empty value would
    still reach the sandbox envs dict and CLOBBER the system-injected default
    (e.g. the host GITHUB_TOKEN), silently breaking the sandbox credential.
    Never raises.
    """
    resolved: dict[str, str] = {}
    for key, value in env_vars.items():
        m = _secret_ref_re.fullmatch(str(value))
        if m:
            secret_key = m.group(1)
            resolved_value = await resolver(secret_key)
            if resolved_value is None:
                _log.warning(
                    "env_var.secret_ref_not_found: env var %s references secret %r "
                    "which could not be resolved — key omitted from sandbox envs",
                    key,
                    secret_key,
                )
                continue
            resolved[key] = resolved_value
        else:
            resolved[key] = value
    return resolved


async def _wait_command_with_idle_watchdog(
    handle: Any,
    *,
    total_timeout: float,
    idle_timeout: float,
    last_activity: Callable[[], float],
    on_tick: Callable[[], Awaitable[None]] | None = None,
    tick_interval: float | None = None,
) -> tuple[Any, str | None]:
    """Wait for a background command, failing fast if the agent goes silent.

    The E2B SDK's commands.run(timeout=...) only enforces a CONNECT timeout;
    the response stream has no read timeout, so a stalled agent blocks the
    node until total_timeout expires. This helper polls handle.wait() in
    idle_timeout slices and returns ``(None, stall_reason)`` as soon as the
    agent has produced no output for idle_timeout seconds (FAR-97 / FAR-98).
    On normal completion it returns ``(cmd_result, None)``; a total-timeout
    still raises TimeoutError. The caller should track last_activity via
    on_stdout/on_stderr callbacks.

    Since the FAR-97 pipe-buffer fix, liveness is tracked by a per-tick drain
    probe (*on_tick*) that reads the sandbox-side output log file: the process's
    stdout is a regular file inside the sandbox, so it can never block on a full
    pipe, and a successful probe proves the sandbox connection is alive even when
    the agent emits nothing for a long LLM turn. The poll slice is reduced to
    *tick_interval* so on_tick runs frequently enough to keep last_activity fresh.

    Each poll slice shields its own ``handle.wait()`` call: the slice await is
    ``asyncio.wait_for(asyncio.shield(handle.wait()), timeout=...)``, so a slice
    timeout cancels only the shield, never the wait. The E2B SDK's
    ``handle.wait()`` merely awaits a long-lived internal events task
    (``self._wait``), so the events task survives every slice timeout and the
    next slice's fresh ``handle.wait()`` still sees it alive. If a slice timeout
    cancelled that events task, the next slice would re-await a dead task and
    immediately raise ``CancelledError`` with ``cancelling()==0`` — which
    LangGraph surfaces as ``NodeCancelledError`` and every sandbox run would
    fail ~one tick in.
    """
    if tick_interval is None:
        tick_interval = _SANDBOX_TAIL_INTERVAL
    deadline = time.monotonic() + total_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"command exceeded total timeout of {total_timeout:.0f}s")
        if on_tick is not None:
            await on_tick()
        try:
            cmd_result = await asyncio.wait_for(
                asyncio.shield(handle.wait()),
                timeout=min(tick_interval, remaining),
            )
            return cmd_result, None
        except TimeoutError:
            if time.monotonic() - last_activity() >= idle_timeout:
                # Kill the command so the still-running agent cannot write a
                # fabricated /home/user/output.json, then fail fast.
                try:
                    await asyncio.wait_for(handle.kill(), timeout=10.0)
                except Exception:
                    _log.exception("sandbox_agent.idle_watchdog_kill_failed")
                return None, f"agent produced no output for {idle_timeout:.0f}s"


class _StallDetector:
    """Per-channel liveness tracking for the sandbox_agent idle watchdog (FAR-306).

    Stall detection is OPT-IN tooling layered on top of the default heartbeat
    (connection liveness). Each *channel* (heartbeat, log-growth, stdout-delta,
    filesystem) tracks its own last-activity timestamp. The watchdog fires only
    when ALL *enabled* channels have been silent for ``stall_timeout_seconds``.

    ``last_activity()`` returns the most recent activity across all enabled
    channels, which is what the idle watchdog compares against the stall window.
    With the default configuration (only the heartbeat enabled) behaviour is
    unchanged from the pre-FAR-306 watchdog: connection responsiveness keeps the
    run alive, never false-killing a busy-but-silent agent.

    Channels are registered explicitly via ``enable(channel)`` so that a channel
    the user has not opted into never counts as a silent channel (which would
    otherwise break the all-channels-silent rule for a default run).
    """

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._now: Callable[[], float] = now or time.monotonic
        self._activity: dict[str, float] = {}
        self._enabled: set[str] = set()

    def enable(self, channel: str) -> None:
        """Mark a channel as active. Enabling seeds its baseline so a brand-new
        channel does not look "already stalled" the instant the run starts."""
        self._enabled.add(channel)
        self._activity.setdefault(channel, self._now())

    def disable(self, channel: str) -> None:
        """Remove a channel from the active set (e.g. heartbeat opt-out)."""
        self._enabled.discard(channel)

    @property
    def enabled(self) -> set[str]:
        return set(self._enabled)

    def touch(self, channel: str) -> None:
        """Record activity on a channel. Ignored for a channel that is not
        enabled (so a stray probe never resurrects a disabled channel)."""
        if channel in self._enabled:
            self._activity[channel] = self._now()

    def last_activity(self) -> float:
        """Most recent activity across all enabled channels.

        With no enabled channels there is nothing to stall on — return ``now``
        so the watchdog never fires (belt-and-braces against a misconfigured
        node with every detector disabled).
        """
        if not self._enabled:
            return self._now()
        return max(self._activity[channel] for channel in self._enabled)


def _delta_ratio(prev: str, new: str) -> float:
    """Fraction of ``new`` that differs from ``prev`` (0.0..1.0).

    Absolute-growth semantics for the stdout-delta detector: a chunk counts as
    meaningful activity when it is substantially different from the previous
    chunk (spinner noise like a repeating cursor or progress bar is near-zero).
    The first chunk always counts as activity (caller handles the None case).
    """
    if prev == new:
        return 0.0
    if not new:
        return 0.0
    if not prev:
        return 1.0
    return 1.0 - difflib.SequenceMatcher(None, prev, new).ratio()


def _path_matches_any_glob(path: str, globs: list[str]) -> bool:
    """True when *path* matches any of the filesystem-detector globs.

    Supports ``*`` (any run of chars) and ``?`` (single char) via fnmatch,
    matched against the basename, the bare path, and a leading-slash form so
    user globs like ``*.log``, ``/home/user/out/*`` and ``output.json`` all
    behave intuitively.
    """
    import fnmatch

    candidates = (path, path.lstrip("/"))
    base = path.rsplit("/", 1)[-1]
    for glob in globs:
        glob_str = str(glob)
        if fnmatch.fnmatch(path, glob_str) or fnmatch.fnmatch(path.lstrip("/"), glob_str):
            return True
        if fnmatch.fnmatch(base, glob_str):
            return True
        for candidate in candidates:
            if glob_str.endswith("/") and fnmatch.fnmatch(candidate, glob_str + "*"):
                return True
    return False


# FAR-227: the E2B sandbox wrapper's fallback echo written to /home/user/output.json
# when the opencode session dies without producing output. It is a PLACEHOLDER,
# NOT an agent verdict — the agent never spoke; the session was interrupted. When
# this echo is detected, the node must NOT be classified as ``agent.failed``
# (a genuine self-declared verdict, non-retryable) — the session death is a
# transient sandbox infra failure, routed to the retryable ``sandbox.no_output_json``.
_SANDBOX_SESSION_LOST_SUMMARY = "No output from agent - session interrupted"


def _is_sandbox_session_lost_echo(output_json: Any) -> bool:
    """True when output.json is the E2B wrapper's fallback echo for a dead session.

    Matched on the wrapper's exact placeholder summary (and the ``error`` field
    as a secondary surface), AND on ``status == "failed"`` — the wrapper always
    writes ``"status":"failed"`` on the echo. Requiring the failed status is the
    anti-false-positive guard: a genuine agent verdict that merely MENTIONS
    session interruption (e.g. "the session was interrupted, here is what I
    completed") carries a ``summary`` without the dead-session signature, and a
    non-failed status can never be the wrapper's dead-session echo. Non-dict /
    non-matching output is never misread.
    """
    if not isinstance(output_json, dict):
        return False
    if output_json.get("status") != "failed":
        return False
    summary = output_json.get("summary")
    error = output_json.get("error")
    haystack = [s for s in (summary, error) if isinstance(s, str)]
    return any(_SANDBOX_SESSION_LOST_SUMMARY in s for s in haystack)


async def _sandbox_resolve_secret_ref(
    secret_key: str,
    *,
    session_factory: Callable[..., Any] | None,
    org_id: str,
) -> str | None:
    """Resolve a ``{{ secrets.KEY }}`` reference to a string value.

    The org vault (per-org encrypted secrets table) is consulted first
    so pipelines resolve against the tenant's stored secrets and honour
    rotation on every run. Returns None if the key is not in the vault
    (does NOT fall back to the process environment to prevent secret
    exfiltration via pipeline references).

    FAR-480: the unresolvable-context paths (no session_factory, missing or
    invalid org_id) log a warning naming the secret key — they used to be
    silent, which made an unresolved ``{{ secrets.X }}`` env ref invisible in
    production until the sandbox failed on the missing credential.
    """
    if session_factory is not None:
        org_uuid: uuid.UUID | None = None
        org_id_raw = org_id
        if org_id_raw:
            try:
                org_uuid = uuid.UUID(str(org_id_raw))
            except (TypeError, ValueError):
                org_uuid = None
        if org_uuid is not None:
            from modulo.core.secrets_backend import create_secrets_backend
            from modulo.db.rls import set_rls_execution_context, set_rls_org
            from modulo.settings import get_settings

            try:
                async with session_factory() as session, session.begin():
                    await set_rls_org(session, org_uuid)
                    await set_rls_execution_context(session)
                    backend = create_secrets_backend(fernet_key=get_settings().fernet_key, session=session)
                    return await backend.get_secret(secret_key)
            except KeyError:
                pass  # not in vault -> return None
            except Exception:
                _log.exception("env_var.secret_resolve_error", extra={"secret_key": secret_key})
        else:
            _log.warning(
                "env_var.secret_ref_no_org_context: secret %r cannot be resolved from the "
                "org vault (run org_id is missing or invalid) — ref will be omitted from sandbox envs",
                secret_key,
            )
    else:
        _log.warning(
            "env_var.secret_ref_no_db_context: secret %r cannot be resolved from the "
            "org vault (no DB session factory on this execution path) — ref will be omitted from sandbox envs",
            secret_key,
        )
    return None


async def _sandbox_acquire_dispatch_marker(
    *,
    session_factory: Callable[..., Any] | None,
    claim_lease: str | None,
    org_id: str,
    run_id: str,
    node_id: str,
) -> str | None:
    """DB-atomic dispatch marker (dist/runtime-core A4): one transaction
    reads ``runs.claim_count`` (fenced on the claim token + status), then
    claims the dispatch slot IMMEDIATELY BEFORE ``AsyncSandbox.create``.

    ``UPDATE runs SET sandbox_dispatch_state=:marker, sandbox_id=:sid
    WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND
    status='running'`` — the marker is a structured JSON carrying the
    attempt key. The UPDATE is atomic, no read-then-create TOCTOU;
    rowcount 0 means the claim is superseded or the run is not running,
    the caller raises :class:`SupersededNodeError` and MUST NOT create a
    sandbox. The SELECT and UPDATE share one transaction, and the UPDATE
    re-checks the same fenced WHERE, so a concurrent claim rotation
    between them makes the UPDATE match zero rows and the attempt key is
    never persisted for a superseded claim.

    Returns the attempt key on success, ``None`` when denied. Fail-open
    (returns a claim-token-derived attempt key WITHOUT writing) when no
    session factory or no claim lease is available.
    """
    if session_factory is None or not claim_lease:
        # Fail-open: no DB fence — derive a per-claim attempt key from the
        # (rotating) claim token so node output still distinguishes attempts.
        return f"run:{run_id}:node:{node_id}:{_claim_token_attempt_suffix(claim_lease)}"
    org_id_raw = org_id
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return f"run:{run_id}:node:{node_id}:{_claim_token_attempt_suffix(claim_lease)}"
    from sqlalchemy import text as _sql_text

    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_uuid)
        await set_rls_execution_context(session)
        row = (
            await session.execute(
                _sql_text(
                    "SELECT claim_count FROM runs WHERE id=:rid AND organisation_id=:oid "
                    "AND claim_token=:tok AND status='running'"
                ),
                {"rid": run_id, "oid": str(org_uuid), "tok": claim_lease},
            )
        ).fetchone()
        if row is None:
            return None
        key = f"run:{run_id}:node:{node_id}:{int(row[0])}"
        result = await session.execute(
            _sql_text(
                "UPDATE runs SET sandbox_dispatch_state=:marker, sandbox_id=:sid "
                "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND status='running' "
                "RETURNING id"
            ),
            {
                "rid": run_id,
                "oid": str(org_uuid),
                "tok": claim_lease,
                "sid": None,
                "marker": _dispatch_marker_json(key),
            },
        )
        if result.fetchone() is None:
            return None
        return key


async def _sandbox_store_dispatch_marker_sandbox(
    sandbox_id_value: str | None,
    *,
    session_factory: Callable[..., Any] | None,
    claim_lease: str | None,
    org_id: str,
    run_id: str,
    attempt_key: str | None,
) -> None:
    """Persist the real sandbox id onto the runs row after a successful create."""
    if session_factory is None or not claim_lease:
        return
    org_id_raw = org_id
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return
    from sqlalchemy import text as _sql_text

    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_uuid)
        await set_rls_execution_context(session)
        await session.execute(
            _sql_text(
                "UPDATE runs SET sandbox_dispatch_state=:marker, sandbox_id=:sid "
                "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND status='running'"
            ),
            {
                "rid": run_id,
                "oid": str(org_uuid),
                "tok": claim_lease,
                "sid": sandbox_id_value,
                "marker": _dispatch_marker_json(attempt_key or ""),
            },
        )


async def _sandbox_store_script_lease(
    *,
    session_factory: Callable[..., Any] | None,
    claim_lease: str | None,
    org_id: str,
    run_id: str,
    attempt_key: str | None,
) -> None:
    """FAR-296 Phase 2 fencing lease: record the script-mode execution claim.

    Reuses the EXISTING ``runs.sandbox_dispatch_state`` machinery — no
    parallel lease store. Persists ``{"state": "script_executing",
    "attempt_key": ...}`` IMMEDIATELY BEFORE ``sandbox.commands.run``, so
    the durable marker proves "the script PROCESS started (execution
    claimed, completion marker pending)". Fenced on the claim token +
    status so a superseded original cannot stamp a lease on a successor's
    row. Fail-open (no session factory / claim lease / org) — the lease
    is a safety backstop, never a correctness dependency.
    """
    if session_factory is None or not claim_lease:
        return
    org_id_raw = org_id
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return
    from sqlalchemy import text as _sql_text

    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_uuid)
        await set_rls_execution_context(session)
        result = await session.execute(
            _sql_text(
                "UPDATE runs SET sandbox_dispatch_state=:marker "
                "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND status='running'"
            ),
            {
                "rid": run_id,
                "oid": str(org_uuid),
                "tok": claim_lease,
                "marker": json.dumps({"state": "script_executing", "attempt_key": attempt_key or ""}),
            },
        )
        if result.rowcount == 0:
            # The UPDATE was fenced on claim_token + status but matched
            # zero rows — the claim was superseded (token rotated by a
            # successor) or the run is no longer running. The lease was
            # NEVER acquired, so a subsequent fault must NOT be treated as
            # post-claim/terminal. Fail as a RETRYABLE SupersededNodeError
            # so the caller never marks the lease claimed.
            raise SupersededNodeError("script lease denied — run superseded or not running")


async def _sandbox_mint_run_api_key_for_sandbox(
    *,
    session_factory: Callable[..., Any] | None,
    org_id: str,
    run_id: str,
    node_id: str,
    sandbox_timeout: int,
) -> str | None:
    """Mint a short-TTL runner-role API key for this script-mode sandbox (FAR-296 Phase 3b).

    Returns the raw key value, or None when minting is unavailable/failed
    (fail-open — the sandbox runs without the key rather than failing the
    dispatch). TTL = max(15 min, node_timeout + 5 min), capped by the
    org-level max (default 1 hour). The raw value is returned to the
    caller only, which injects it into the sandbox envs — it is NEVER
    logged, checkpointed, or placed in LangGraph state.
    """
    if session_factory is None:
        return None
    org_id_raw = org_id
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return None
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        return None
    try:
        from modulo.auth.api_key import mint_run_api_key
        from modulo.db.crud.run import get_run_api_key_ttl_seconds

        ttl_seconds = await get_run_api_key_ttl_seconds(session_factory, org_uuid, sandbox_timeout)
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await set_rls_execution_context(session)
            # account_id: the run's triggering user; fall back to the
            # first active admin in the org when the run row has none.
            from sqlalchemy import text as _sql_text

            row = (
                await session.execute(
                    _sql_text("SELECT account_id FROM runs WHERE id=:rid AND organisation_id=:oid"),
                    {"rid": str(run_uuid), "oid": str(org_uuid)},
                )
            ).fetchone()
            account_id_raw = row[0] if row else None
            if account_id_raw is None:
                admin_row = (
                    await session.execute(
                        _sql_text(
                            "SELECT a.id FROM accounts a JOIN org_memberships om ON om.account_id = a.id "
                            "WHERE om.organisation_id=:oid AND om.role='admin' AND om.deactivated_at IS NULL "
                            "ORDER BY a.created_at LIMIT 1"
                        ),
                        {"oid": str(org_uuid)},
                    )
                ).fetchone()
                account_id_raw = admin_row[0] if admin_row else None
            if account_id_raw is None:
                return None
            minted = await mint_run_api_key(
                session,
                org_id=org_uuid,
                run_id=run_uuid,
                node_id=node_id,
                account_id=uuid.UUID(str(account_id_raw)),
                ttl_seconds=ttl_seconds,
            )
            if minted is None:
                return None
            _key_row, full_key = minted
            return full_key
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "sandbox_agent.run_api_key_mint_failed",
            extra={"run_id": run_id, "node_id": node_id},
        )
        return None


async def _sandbox_clear_dispatch_marker(
    *,
    session_factory: Callable[..., Any] | None,
    claim_lease: str | None,
    org_id: str,
    run_id: str,
) -> None:
    """Fenced marker clear — only when the claim token still matches.

    A superseded original (token rotated by a successor) must not clear
    the successor's dispatch marker / sandbox id.
    """
    if session_factory is None or not claim_lease:
        return
    org_id_raw = org_id
    try:
        org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return
    from sqlalchemy import text as _sql_text

    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_uuid)
        await set_rls_execution_context(session)
        await session.execute(
            _sql_text(
                "UPDATE runs SET sandbox_dispatch_state=NULL, sandbox_id=NULL "
                "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok"
            ),
            {"rid": run_id, "oid": str(org_uuid), "tok": claim_lease},
        )


def _emit_script_span_event(name: str, attrs: dict[str, Any]) -> None:
    """Emit an OTel span event for script-mode milestones (FAR-296 Phase 5a)."""
    try:
        from opentelemetry import trace as _otel_trace

        _span = _otel_trace.get_current_span()
        if _span.is_recording():
            _span.add_event(name, {k: str(v)[:_MAX_OTEL_LOG_ATTR] for k, v in attrs.items()})
    except Exception:  # noqa: S110  # nosec B110 -- never fail on observability
        pass


class _SandboxWatchdog:
    """Per-run sandbox watchdog + live-streaming state (FAR-310 chunk 2b-2)."""

    def __init__(
        self,
        *,
        sandbox: "AsyncSandbox | None",
        stall: _StallDetector,
        node_id: str,
        run_id: str,
        watch_log_path: str | None,
        watch_globs: list[str],
        resource_limits: dict[str, Any] | None,
        sandbox_mode: str,
        stdout_percentage_delta: float | None,
        stream_broker: RunEventBroker | None,
        drained_chunks: list[str],
        wallclock_budget_seconds: int | None,
        start_time: float,
    ) -> None:
        if sandbox is None:
            raise RuntimeError("Sandbox was not created before use")
        self._sandbox = sandbox
        self._stall = stall
        self._node_id = node_id
        self._run_id = run_id
        self._watch_log_path = watch_log_path
        self._watch_globs = watch_globs
        self._resource_limits = resource_limits
        self._sandbox_mode = sandbox_mode
        self._stdout_ratio = stdout_percentage_delta
        self._stream_broker = stream_broker
        self._stream_enabled = isinstance(stream_broker, RunEventBroker)
        self._drained_chunks = drained_chunks
        self._activity: dict[str, Any] = {"last": time.monotonic()}
        self._stdout_prev: str | None = None
        self._drain_offset = 0
        self._drained_len = 0
        self._watch_log_prev_size: int | None = None
        self._fs_state: dict[str, tuple[Any, int]] = {}
        self._fs_last_stat = 0.0
        self._fs_min_stat_interval = 2.0
        self._budget_killed = False
        self._budget_check_ticks = 0
        # FAR-296 Phase 4a: wall-clock spend budget. The watchdog's tick checks
        # the elapsed wall-clock against this budget and kills the sandbox when
        # exceeded (script mode only). ``start_time`` is the monotonic clock at
        # node start so the elapsed measurement survives the provisioning phase.
        self._wallclock_budget_seconds = wallclock_budget_seconds
        self._start_time = start_time

    @property
    def budget_killed(self) -> bool:
        return self._budget_killed

    def stream_chunk(self, chunk: str, stream: str) -> None:
        broker = self._stream_broker
        if not self._stream_enabled or not isinstance(broker, RunEventBroker):
            return
        now = time.monotonic()
        buf_key = f"{stream}_buf"
        buf = self._activity.setdefault(buf_key, [])
        if chunk:
            buf.append(chunk)
        if not buf:
            return
        if now - self._activity.get("last_stream_ts", 0.0) < _STREAM_FLUSH_INTERVAL:
            return
        payload: dict[str, Any] = {
            "node_id": self._node_id,
            "chunk": _redact_raw_output("".join(buf)),
            "ts": int(now * 1000),
        }
        buf.clear()
        self._activity["last_stream_ts"] = now
        try:
            event = broker.publish(
                "node.stdout_chunk" if stream == "stdout" else "node.stderr_chunk",
                payload,
            )
            payload["seq"] = event.seq
        except RuntimeError:
            # Broker already closed (run finalised) — stop streaming.
            return
        except Exception:
            _log.exception(
                "sandbox_agent.stream_publish_failed",
                extra={"node_id": self._node_id, "run_id": self._run_id},
            )

    def touch_stdout(self, chunk: str) -> None:
        if self._stdout_ratio is None:
            return
        prev = self._stdout_prev
        self._stdout_prev = chunk
        if prev is None or _delta_ratio(prev, chunk) > self._stdout_ratio:
            self._stall.touch("stdout")

    async def on_stdout(self, chunk: str) -> None:
        self._stall.touch("heartbeat")
        self.touch_stdout(chunk)
        self.stream_chunk(chunk, "stdout")

    async def on_stderr(self, chunk: str) -> None:
        self._stall.touch("heartbeat")
        self.stream_chunk(chunk, "stderr")

    async def drain_sandbox_log(self) -> None:
        # Probe failed (log file not created yet, sandbox connection
        # unresponsive). Do NOT refresh liveness — the idle watchdog
        # treats a prolonged probe failure as a genuine stall.
        try:
            info = await asyncio.wait_for(
                self._sandbox.files.get_info(_SANDBOX_LOG_PATH),
                timeout=_SANDBOX_TAIL_READ_TIMEOUT,
            )
            # Heartbeat channel: a successful get_info proves the
            # sandbox connection is responsive. When enable_heartbeat
            # is False (strict mode) this touch is a no-op because the
            # channel is not enabled (FAR-306).
            self._stall.touch("heartbeat")
            size = int(getattr(info, "size", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info(
                "sandbox_agent.log_drain_probe_failed",
                extra={"node_id": self._node_id},
            )
            return
        if size <= self._drain_offset:
            return
        try:
            content = await asyncio.wait_for(
                self._sandbox.files.read(_SANDBOX_LOG_PATH, format="text"),
                timeout=_SANDBOX_TAIL_READ_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "sandbox_agent.log_drain_failed",
                extra={"node_id": self._node_id},
            )
            return
        text = content if isinstance(content, str) else bytes(content).decode("utf-8", "replace")
        # Capture the pre-truncation content length BEFORE the window
        # bound below: ``full_len`` is the authoritative absolute end
        # of the file as READ this tick. The probe ``size`` (get_info)
        # is taken before the read and can lag it when the agent
        # appends between probe and read.
        full_len = len(text)
        # D3 trailing-window bound: the E2B files API has no range
        # read, so the full log was transferred again above. Only the
        # last _MAX_DRAIN_WINDOW bytes are retained/processed —
        # bounded per-tick memory and slicing on a multi-MB log.
        # ``full_len`` (what the read actually returned) is the
        # authoritative absolute file length; ``window_start`` is the
        # absolute offset the retained slice begins at, and the
        # new-bytes slice is computed against it, so truncation never
        # loses or double-emits (the emitted chunk is always a
        # suffix). Deriving ``window_start`` from the STALE probe
        # ``size`` instead of ``full_len`` shifts the retained slice
        # left and permanently drops the first (full_len - size)
        # bytes of new in-window content.
        if len(text) > _MAX_DRAIN_WINDOW:
            text = text[-_MAX_DRAIN_WINDOW:]
        window_start = max(full_len - len(text), 0)
        emit_start = max(self._drain_offset, window_start)
        new = text[emit_start - window_start :] if emit_start < full_len else ""
        if new:
            self._drained_chunks.append(new)
            self._drained_len += len(new)
            self.stream_chunk(new, "stdout")
            # Actual agent-log growth is real progress: refresh the
            # output channel (always active, the strict-mode signal),
            # the heartbeat (when enabled), and feed the stdout-delta
            # detector (FAR-306).
            self._stall.touch("output")
            self._stall.touch("heartbeat")
            self.touch_stdout(new)
            self._trim_drained_chunks()
        # Self-correcting drain offset: ``full_len`` (what this tick's
        # read returned) can exceed the STALE probe ``size`` when the
        # agent appends between probe and read. Advancing to the max
        # keeps the no-double-emit invariant — the next tick starts
        # where THIS tick's emitted bytes actually ended, not where
        # the probe saw the file.
        self._drain_offset = max(self._drain_offset, full_len)

    def _trim_drained_chunks(self) -> None:
        """Bound retained memory to the drain window.

        Drops the oldest chunks once the accumulated log exceeds the window,
        then clamps the head chunk to the window so the in-memory total can
        never grow past ``_MAX_DRAIN_WINDOW``.
        """
        while self._drained_len > _MAX_DRAIN_WINDOW and len(self._drained_chunks) > 1:
            dropped = self._drained_chunks.pop(0)
            self._drained_len -= len(dropped)
        if self._drained_len > _MAX_DRAIN_WINDOW and self._drained_chunks:
            self._drained_chunks[0] = self._drained_chunks[0][-_MAX_DRAIN_WINDOW:]
            self._drained_len = len(self._drained_chunks[0])

    async def probe_log_growth(self) -> None:
        try:
            info = await asyncio.wait_for(
                self._sandbox.files.get_info(self._watch_log_path),
                timeout=_SANDBOX_TAIL_READ_TIMEOUT,
            )
            size = int(getattr(info, "size", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info(
                "sandbox_agent.watch_log_probe_failed",
                extra={"node_id": self._node_id, "path": self._watch_log_path},
            )
            return
        if self._watch_log_prev_size is not None and size > self._watch_log_prev_size:
            self._stall.touch("log_growth")
        self._watch_log_prev_size = size

    async def probe_filesystem(self) -> None:
        now = time.monotonic()
        if now - self._fs_last_stat < self._fs_min_stat_interval:
            return
        self._fs_last_stat = now
        try:
            matches = await asyncio.wait_for(
                self._sandbox.files.list(path="/", request_timeout=_SANDBOX_TAIL_READ_TIMEOUT),
                timeout=_SANDBOX_TAIL_READ_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info(
                "sandbox_agent.watch_fs_probe_failed",
                extra={"node_id": self._node_id},
            )
            return
        try:
            entries = matches if isinstance(matches, list) else list(getattr(matches, "files", []) or [])
        except Exception:
            _log.info("sandbox_agent.watch_fs_list_invalid", extra={"node_id": self._node_id})
            return
        changed = False
        seen: set[str] = set()
        for entry in entries:
            if self._track_fs_entry(entry, seen):
                changed = True
        if self._prune_missing_paths(seen):
            changed = True
        if changed:
            self._stall.touch("filesystem")

    def _track_fs_entry(self, entry: Any, seen: set[str]) -> bool:
        """Track one watch-glob fs entry; True when its stat changed or it is new."""
        path = getattr(entry, "path", None) or getattr(entry, "name", None)
        if not isinstance(path, str):
            return False
        if path == _SANDBOX_LOG_PATH or (self._watch_log_path and path == self._watch_log_path):
            return False
        if _path_matches_any_glob(path, self._watch_globs):
            seen.add(path)
            key = (getattr(entry, "mtime", None), int(getattr(entry, "size", 0) or 0))
            if path in self._fs_state and self._fs_state[path] != key:
                return True
            self._fs_state[path] = key
        return False

    def _prune_missing_paths(self, seen: set[str]) -> bool:
        """Drop previously-seen paths that no longer match; True when any were pruned."""
        changed = False
        for prev_path in list(self._fs_state):
            if prev_path not in seen:
                del self._fs_state[prev_path]
                changed = True
        return changed

    async def enforce_resource_limits(self) -> bool:
        """Platform-side resource-cap killer (FAR-296 Phase 3b-3).

        Polls self._sandbox.get_metrics() (bounded wait_for), compares the
        observable caps, and kills the self._sandbox when a cap is exceeded.
        Returns True when the self._sandbox was killed (the caller maps the
        outcome to ``script.budget_killed``).

        Observable caps:
          - ``cpu_usage_pct`` (0-100 PERCENTAGE) vs ``cpu_used_pct``
          - ``memory_mb`` vs ``mem_used`` bytes
          - ``disk_mb`` vs ``disk_used`` bytes

        ``cpu_count`` is a CORE COUNT, NOT a percentage — it is
        informational/metadata-only and is NOT enforced here (same as
        ``max_processes`` / ``max_fds`` / ``max_sockets``, which the
        SDK exposes no observable metric for). Treating a core count
        as a percentage threshold would kill a 2-core self._sandbox at >2%
        CPU usage.
        """
        if not self._resource_limits or self._sandbox_mode != "script" or self._sandbox is None:
            return False
        try:
            # get_metrics is a fresh coroutine per call, so wait_for is
            # safe to cancel; shield for consistency with the SDK-task
            # lesson (never cancel long-lived SDK internal tasks).
            metrics_raw = await asyncio.wait_for(
                asyncio.shield(self._sandbox.get_metrics()),
                timeout=_SANDBOX_METRICS_POLL_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-open: metrics unavailable (SDK too old / self._sandbox
            # unreachable) — never kill on a measurement failure.
            _log.warning(
                "sandbox_agent.resource_metrics_unavailable",
                extra={"node_id": self._node_id, "run_id": self._run_id},
            )
            return False
        # get_metrics returns a LIST of SandboxMetrics samples —
        # compare against the latest (most recent instantaneous sample).
        metrics = metrics_raw[-1] if isinstance(metrics_raw, (list, tuple)) and metrics_raw else metrics_raw
        if metrics is None:
            return False
        try:
            # Observable caps only. cpu_count / max_processes /
            # max_fds / max_sockets are NOT enforceable via the SDK
            # (no matching observable metric) — they stay
            # metadata-only (template-side enforcement).
            killed = self._budget_exceeded(metrics)
        except Exception:
            _log.exception(
                "sandbox_agent.resource_metrics_compare_failed",
                extra={"node_id": self._node_id},
            )
            return False
        if not killed:
            return False
        await self.kill_sandbox_for_budget(self._node_id, reason="resource_limits_exceeded")
        return True

    def _budget_exceeded(self, metrics: Any) -> bool:
        """True when any observable resource cap is exceeded (best-effort).

        Memory/disk thresholds are MB values compared against byte metrics;
        the first exceeded cap short-circuits the rest.
        """
        if not self._resource_limits:
            return False
        cpu_cap = self._resource_limits.get("cpu_usage_pct")
        if cpu_cap is not None:
            cpu_used_pct = getattr(metrics, "cpu_used_pct", None)
            if _is_real_number(cpu_used_pct) and cpu_used_pct > float(cpu_cap):
                return True
        mem_cap = self._resource_limits.get("memory_mb")
        if mem_cap is not None:
            mem_used = getattr(metrics, "mem_used", None)
            if _is_real_number(mem_used) and mem_used > float(mem_cap) * 1024 * 1024:
                return True
        disk_cap = self._resource_limits.get("disk_mb")
        if disk_cap is not None:
            # Disk is only enforced when the SDK reports disk
            # fields (older envd/API versions may omit them).
            disk_used = getattr(metrics, "disk_used", None)
            if _is_real_number(disk_used) and disk_used > float(disk_cap) * 1024 * 1024:
                return True
        return False

    async def kill_sandbox_for_budget(self, node_id: str, reason: str) -> None:
        """Kill the sandbox for a budget overrun (resource caps or wall-clock spend).

        Shared kill semantics for BOTH budget killers (FAR-296 Phase 3b-3
        resource caps + Phase 4a wall-clock spend): set ``self._budget_killed``,
        kill the sandbox (bounded + shielded), and log on failure that the
        sandbox MAY REMAIN ALIVE (the node's finally-block teardown is the
        second kill attempt).
        """
        self._budget_killed = True
        _log.warning(
            "sandbox_agent.budget_killed",
            extra={"node_id": node_id, "run_id": self._run_id, "reason": reason},
        )
        _emit_script_span_event(
            "script.budget_killed",
            {
                "run_id": self._run_id,
                "node_id": node_id,
                "reason": reason,
                "elapsed_seconds": round(time.monotonic() - self._start_time, 1),
            },
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._sandbox.kill(request_timeout=10)),
                timeout=_SANDBOX_KILL_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort kill. The failure is logged but the budget was
            # genuinely exceeded — the sandbox MAY REMAIN ALIVE after this
            # (the kill timed out / the SDK errored), so the budget flag still
            # propagates as a terminal outcome and the sandbox teardown in the
            # node's finally block is the second kill attempt.
            _log.exception(
                "sandbox_agent.resource_kill_failed",
                extra={
                    "node_id": node_id,
                    "run_id": self._run_id,
                    "budget_killed": True,
                    "reason": reason,
                },
            )
            _log.warning(
                "sandbox_agent.budget_killed_sandbox_may_remain_alive",
                extra={"node_id": node_id, "run_id": self._run_id},
            )

    async def tick(self) -> None:
        await self.drain_sandbox_log()
        if self._watch_log_path is not None:
            await self.probe_log_growth()
        if self._watch_globs:
            await self.probe_filesystem()
        self._budget_check_ticks += 1
        if self._budget_check_ticks % _SANDBOX_BUDGET_POLL_INTERVAL_TICKS == 0:
            await self.enforce_resource_limits()
        # FAR-296 Phase 4a: wall-clock spend budget. Pure monotonic comparison —
        # no SDK poll needed. When the elapsed time exceeds the budget and the
        # sandbox has not already been budget-killed, kill it. The guard
        # ``and not self._budget_killed`` prevents a second kill if the
        # resource-cap killer already fired. Gated to script mode only —
        # LLM-mode nodes do not take a wall-clock budget; raising
        # ScriptBudgetKilledError (script-specific, terminal) for an LLM-mode
        # node would be a misclassification.
        if (
            _sandbox_wallclock_budget_exceeded(
                sandbox_mode=self._sandbox_mode,
                wallclock_budget_seconds=self._wallclock_budget_seconds,
                start_time=self._start_time,
            )
            and not self._budget_killed
        ):
            _elapsed = time.monotonic() - self._start_time
            _log.warning(
                "sandbox_agent.wallclock_budget_overrun",
                extra={
                    "run_id": self._run_id,
                    "node_id": self._node_id,
                    "budget_seconds": self._wallclock_budget_seconds,
                    "elapsed_seconds": round(_elapsed, 1),
                },
            )
            await self.kill_sandbox_for_budget(self._node_id, reason="wallclock_budget_exceeded")


@dataclass(frozen=True)
class _SandboxNodeConfig:
    """Immutable per-node configuration for a sandbox-agent dispatch.

    Groups the node_def-derived parameters that previously flowed into
    ``_sandbox_agent_impl`` as ~20 separate keyword arguments. Building this
    once at node-construction time (``make_sandbox_agent_fn``) gives the
    dispatch path a single cohesive config object and removes the excess
    argument surface. ``frozen=True`` keeps the config read-only for the
    duration of a dispatch — a node must never mutate its own configuration.
    """

    node_id: str
    node_def: dict[str, Any]
    sandbox_mode: str
    agent_command: str
    agent_prompt_template: str
    template_id: str
    egress_policy: str | None
    egress_allowlist: list[dict[str, Any]] | None
    resource_limits: dict[str, Any] | None
    read_only: bool
    git_credentials: str | None
    wallclock_budget_seconds: int | None
    output_schema_json: dict[str, Any] | None
    sandbox_timeout: int
    stall_timeout_override: Any
    context_files: dict[str, str]
    enable_heartbeat: bool
    watch_log_path: str | None
    stdout_percentage_delta: float | None
    watch_globs: list[str]
    delivery_sentinel: str | None
    loop_intercept_config: LoopInterceptConfig | None
    session_factory: Callable[..., Any] | None
    single_sandbox_node: bool


def _check_wallclock_budget_pre_run(
    *,
    sandbox_mode: str,
    wallclock_budget_seconds: int | None,
    start_time: float,
    watchdog: _SandboxWatchdog,
    run_id: str,
    node_id: str,
) -> None:
    """Raise ``ScriptBudgetKilledError`` when the wall-clock budget is already spent.

    FAR-296 Phase 4a non-tick path: a slow provisioning / bridge / env-setup
    sequence may already have consumed the wall-clock spend budget before the
    script process starts. Script mode only — LLM-mode nodes take no wall-clock
    budget, and raising the script-specific terminal error for an LLM-mode node
    would be a misclassification. ``watchdog.budget_killed`` may already be set
    by an earlier check, so it guards against a double kill/raise.
    """
    if (
        not _sandbox_wallclock_budget_exceeded(
            sandbox_mode=sandbox_mode,
            wallclock_budget_seconds=wallclock_budget_seconds,
            start_time=start_time,
        )
        or watchdog.budget_killed
    ):
        return
    _log.warning(
        "sandbox_agent.wallclock_budget_overrun_pre_run",
        extra={
            "run_id": run_id,
            "node_id": node_id,
            "budget_seconds": wallclock_budget_seconds,
            "elapsed_seconds": round(time.monotonic() - start_time, 1),
        },
    )
    watchdog._budget_killed = True
    raise ScriptBudgetKilledError(_script_budget_killed_message(node_id)) from None


_UNSET = object()


class _SandboxNodeOutput(NamedTuple):
    """Grouped output fields for a sandbox_agent node envelope.

    The sandbox_agent dispatch builds its ``{artifacts, output}`` envelope in
    three places (success, schema-failed, generic-exception) with subtly
    different key sets. Grouping the fields into this NamedTuple and routing
    them through ``_build_sandbox_node_envelope`` removes the triplicated dict
    construction while keeping each path's exact key presence (sentinel
    ``_UNSET`` = key omitted).
    """

    status: str
    summary: str
    exit_code: int
    wall_clock_time_ms: int
    cost_estimate_usd: float
    cost_source: Any = None
    output_json: Any = _UNSET
    agent_stdout: str = ""
    agent_stderr: str = ""
    stdout_length: int = 0
    stderr_length: int = 0
    attempt_key: str | None = None
    changed_files: Any = _UNSET
    pr_url: Any = _UNSET
    agent_status: Any = _UNSET
    agent_outcome: Any = _UNSET
    stall_reason: Any = _UNSET
    sandbox_session_lost: Any = _UNSET
    error_type: Any = _UNSET
    error_message: Any = _UNSET
    sandbox_id: Any = _UNSET
    sandbox_log_tail: Any = _UNSET
    # FAR-510: True ONLY on the runner's synthetic failure envelopes (the
    # executor's downgrade predicate keys on this marker, never on summary
    # text). Default False so honest envelopes carry no marker key at all.
    modulo_synthetic_failure: bool = False


def _format_sandbox_provider_error(exc: Exception, provider_exc_type: type | None = None) -> str:
    """Compose a run-output-safe error message, enriching provider exceptions.

    FAR-511: a bare ``str(exc)`` for an e2b ``SandboxException`` already carries
    the HTTP status (e.g. ``400: Timeout cannot be greater than 1 hours``), but
    the provider's response body may carry extra detail. When ``provider_exc_type``
    (e.g. ``e2b.exceptions.SandboxException``) is supplied and matches, append the
    response body so ``get_run_output`` reveals the full provisioning failure
    rather than a masked "Sandbox agent execution failed".
    """
    msg = str(exc)[:_MAX_ERROR_MSG]
    if provider_exc_type is not None and isinstance(exc, provider_exc_type):
        response = getattr(exc, "response", None)
        if response is not None:
            body = getattr(response, "text", None)
            if not body and isinstance(response, (dict, list)):
                try:
                    body = json.dumps(response)
                except (TypeError, ValueError):
                    body = None
            if body:
                msg = f"{msg} — {body}"[:_MAX_ERROR_MSG]
    return msg


def _build_sandbox_node_envelope(
    *,
    node_id: str,
    output: _SandboxNodeOutput,
    exclude_from_output: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Assemble a sandbox_agent node envelope from grouped output fields.

    The envelope shape is ``{artifacts: [{node_id, status, output}], output}``.
    The artifact's inner ``output`` carries the FULL field set; the top-level
    ``output`` is the reduced telemetry view that omits ``output_json`` and
    ``exit_code`` plus any caller-supplied artifact-only keys (e.g. the
    success path's ``changed_files``/``pr_url``, the generic-exception path's
    ``error_type``/``error_message``). Optional fields are omitted when left at
    the ``_UNSET`` sentinel so each path emits exactly the key set it did
    before this extraction. The FAR-510 synthetic-failure marker
    (``MODULO_SYNTHETIC_FAILURE_MARKER``) is stamped into BOTH views only when
    ``output.modulo_synthetic_failure`` is True — honest envelopes carry no
    marker key at all, and the marker is deliberately NOT in
    ``exclude_from_output`` so it survives into the persisted telemetry view
    (and, after the P1b split, into ``node_telemetry_json``).
    """
    inner: dict[str, Any] = {
        "status": output.status,
        "summary": output.summary,
        "exit_code": output.exit_code,
        "wall_clock_time_ms": output.wall_clock_time_ms,
        "cost_estimate_usd": output.cost_estimate_usd,
        **_build_model_cost_fields(output.cost_source),
        **_build_token_usage_fields(output.cost_source),
    }
    if output.output_json is not _UNSET:
        inner["output_json"] = output.output_json
    inner["agent_stdout"] = output.agent_stdout
    inner["agent_stderr"] = output.agent_stderr
    inner["stdout_length"] = output.stdout_length
    inner["stderr_length"] = output.stderr_length
    if output.changed_files is not _UNSET:
        inner["changed_files"] = output.changed_files
    if output.pr_url is not _UNSET:
        inner["pr_url"] = output.pr_url
    if output.agent_status is not _UNSET:
        inner["agent_status"] = output.agent_status
    if output.agent_outcome is not _UNSET:
        inner["agent_outcome"] = output.agent_outcome
    if output.stall_reason is not _UNSET and output.stall_reason:
        inner["stall_reason"] = output.stall_reason
    if output.sandbox_session_lost is not _UNSET and output.sandbox_session_lost:
        inner["sandbox_session_lost"] = True
    if output.error_type is not _UNSET:
        inner["error_type"] = output.error_type
    if output.error_message is not _UNSET:
        inner["error_message"] = output.error_message
    if output.sandbox_id is not _UNSET:
        inner["sandbox_id"] = output.sandbox_id
    if output.sandbox_log_tail is not _UNSET:
        inner["sandbox_log_tail"] = output.sandbox_log_tail
    if output.modulo_synthetic_failure:
        inner[MODULO_SYNTHETIC_FAILURE_MARKER] = True
    inner["attempt_key"] = output.attempt_key
    excluded = frozenset(("output_json", "exit_code")) | exclude_from_output
    outer = {key: value for key, value in inner.items() if key not in excluded}
    return {
        "artifacts": [{"node_id": node_id, "status": output.status, "output": inner}],
        "output": outer,
    }


def _build_sandbox_envs(
    *,
    run_id: str,
    pipeline_id: str,
    org_id: str,
    input_json: str,
    sandbox_mode: str,
    env_vars_extra: dict[str, str],
) -> dict[str, str]:
    """Compose the sandbox envs dict: system env vars FIRST, then ``**env_vars_extra`` LAST.

    The ordering is DELIBERATE (AGENTS.md b0c4bde97): pipelines must be able to
    override system defaults for identity separation (e.g. the PR Reviewer
    injects its own modulo-reviewbot PAT via ``env_vars_extra``). Script mode
    does NOT auto-inject the long-lived host credentials (opencode API key /
    GitHub PAT) — a script only gets what the pipeline passes explicitly.
    """
    sandbox_envs: dict[str, str] = {
        "MODULO_RUN_ID": run_id,
        "MODULO_PIPELINE_ID": pipeline_id,
        "MODULO_ORG_ID": org_id,
        "MODULO_INPUT_PAYLOAD": input_json,
    }
    if sandbox_mode != "script":
        sandbox_envs["APP_MODULO_OPENCODE_API_KEY"] = os.environ.get("APP_MODULO_OPENCODE_API_KEY", "")
        sandbox_envs["GITHUB_TOKEN"] = (
            os.environ.get("GITHUB_DOGFOOD_PAT_ALL", "")
            or os.environ.get("GITHUB_DOGFOOD_PAT_WR", "")
            or os.environ.get("GITHUB_TOKEN", "")
        )
    sandbox_envs.update(env_vars_extra)
    return sandbox_envs


def _configure_stall_detector(
    *,
    enable_heartbeat: bool,
    watch_log_path: str | None,
    stdout_percentage_delta: float | None,
    watch_globs: list[str],
) -> _StallDetector:
    """Build the per-run stall detector for the idle watchdog (FAR-306).

    The agent's ACTUAL output is always a liveness signal (``output`` channel);
    the heartbeat (connection liveness) is the default extra channel, dropped
    in strict mode. Opt-in detectors (log-growth, stdout-delta, filesystem) are
    enabled only when their config is present.
    """
    _stall = _StallDetector()
    _stall.enable("output")
    _stall.enable("heartbeat")
    if not enable_heartbeat:
        _stall.disable("heartbeat")
    if watch_log_path is not None:
        _stall.enable("log_growth")
    if stdout_percentage_delta is not None:
        _stall.enable("stdout")
    if watch_globs:
        _stall.enable("filesystem")
    return _stall


def _should_apply_sandbox_policy(
    *,
    read_only: bool,
    git_credentials: str | None,
    egress_policy: str | None,
    egress_allowlist: list[dict[str, Any]] | None,
) -> bool:
    """True when the FAR-212 sandbox policy step must run before the command."""
    return (
        read_only or git_credentials in ("scoped", "none") or (egress_policy == "selected" and bool(egress_allowlist))
    )


def _is_script_post_claim_fault(sandbox_mode: str, script_lease_claimed: bool) -> bool:
    """FAR-296 Phase 2 stage-split: a fault AFTER the script's fencing lease was
    claimed is POST-CLAIM (terminal, never retryable)."""
    return sandbox_mode == "script" and script_lease_claimed


def _sandbox_wallclock_budget_exceeded(
    *,
    sandbox_mode: str,
    wallclock_budget_seconds: int | None,
    start_time: float,
) -> bool:
    """True when a script-mode node's wall-clock spend budget is already exceeded."""
    return (
        sandbox_mode == "script"
        and wallclock_budget_seconds is not None
        and (time.monotonic() - start_time) >= wallclock_budget_seconds
    )


def _script_enforcement_requires_remote(
    *,
    sandbox_mode: str,
    egress_policy: str | None,
    resource_limits: dict[str, Any] | None,
    read_only: bool,
    git_credentials: str | None,
) -> bool:
    """Script mode that REQUIRES enforcement needs a REMOTE E2B provider."""
    return bool(
        sandbox_mode == "script"
        and (
            egress_policy in ("deny_all", "selected")
            or resource_limits
            or read_only
            or git_credentials in ("scoped", "none")
        )
    )


def _run_identity_strs(state: dict[str, Any]) -> tuple[str, str, str]:
    """Derive the run/pipeline/org identity strings from internal state keys.

    An explicit ``None`` state value renders as ``""`` — the same result a
    missing key produces — never the literal string ``"None"`` (gh-1802).
    """
    _run_id = state.get("_run_id")
    _pipeline_id = state.get("_pipeline_id")
    _org_id = state.get("_org_id")
    return (
        str(_run_id) if _run_id is not None else "",
        str(_pipeline_id) if _pipeline_id is not None else "",
        str(_org_id) if _org_id is not None else "",
    )


async def _sandbox_agent_impl(  # NOSONAR S3776 - sandbox root dispatch; delegates to extracted helpers (FAR-310)
    state: dict[str, Any],
    *,
    config: _SandboxNodeConfig,
) -> dict[str, Any]:
    # Destructure the immutable config back to the local names the dispatch
    # body uses, so the body is unchanged from the pre-dataclass form. Only the
    # SIGNATURE narrows to a single config object — the running body behaves
    # identically.
    node_id = config.node_id
    node_def = config.node_def
    sandbox_mode = config.sandbox_mode
    agent_command = config.agent_command
    agent_prompt_template = config.agent_prompt_template
    template_id = config.template_id
    egress_policy = config.egress_policy
    egress_allowlist = config.egress_allowlist
    resource_limits = config.resource_limits
    read_only = config.read_only
    git_credentials = config.git_credentials
    wallclock_budget_seconds = config.wallclock_budget_seconds
    output_schema_json = config.output_schema_json
    sandbox_timeout = config.sandbox_timeout
    stall_timeout_override = config.stall_timeout_override
    context_files = config.context_files
    enable_heartbeat = config.enable_heartbeat
    watch_log_path = config.watch_log_path
    stdout_percentage_delta = config.stdout_percentage_delta
    watch_globs = config.watch_globs
    delivery_sentinel = config.delivery_sentinel
    loop_intercept_config = config.loop_intercept_config
    session_factory = config.session_factory
    single_sandbox_node = config.single_sandbox_node

    from e2b import AsyncSandbox
    from e2b.exceptions import RateLimitException, SandboxException
    from opentelemetry import trace as _otel_trace

    from modulo.core.guardrails.loop_intercept import (
        LoopInterceptCallbackServer,
        bridge_client_source,
        load_loop_intercept_guardrails,
        persist_loop_interception_audit,
    )

    # FAR-215: mid-run capability re-check at node start (block -> HITL).
    # The gate raises a LangGraph interrupt on block; control only reaches
    # the sandbox body when the node may proceed. node_def is forwarded so
    # the sandbox capability surface (egress certification; write/git-credential
    # unknown until PR B — FAR-212 PR A) is mechanically derived into the
    # conformance manifest.
    agent_id = _parse_uuid_opt(node_def.get("agent_id"))
    await _run_conformance_gate(state, node_id=node_id, agent_id=agent_id, node_def=node_def)

    run_context: dict[str, Any] = state.get("run_context") or {}
    raw_input: Any = run_context.get("input", {})

    # FAR-418 (MAJOR-3 fix, sandbox_agent path): the sandbox agent's render view
    # must be bound by context_scope too — otherwise a gated run_context key leaks
    # into the rendered prompt (written to /home/user/prompt.md) and agent_command
    # via ``{{ run_context.<key> }}`` / ``{{ state.run_context.<key> }}``. Mirror
    # the make_node_fn scoped_state/scoped_run_context boundary. ``raw_input`` stays
    # sourced from the full run_context so input routing is unaffected.
    _node_cap = node_def.get("capability_scope") or {}
    scoped_run_context = filter_run_context_scope(run_context, _node_cap.get("context_scope"))
    scoped_state = dict(state)
    scoped_state["run_context"] = scoped_run_context

    run_id, pipeline_id, org_id = _run_identity_strs(state)

    # FAR-296 mode split: llm mode renders the prompt + agent_command through
    # the SandboxedEnvironment; script mode runs script_command VERBATIM —
    # no Jinja render of the command, no prompt, no template_vars.
    if sandbox_mode == "script":
        rendered_prompt: str = ""
        rendered_agent_command = agent_command
    else:
        env = SandboxedEnvironment()
        template = env.from_string(agent_prompt_template)
        # FAR-436: context_scope — the sandbox agent's run_context VIEW (the keys
        # fed to the prompt + agent_command templates) is allowlist-gated to the
        # node's need-to-know set. Internal control keys are always preserved by
        # filter_run_context_scope (_CONTEXT_ALWAYS_KEPT). Absent scope = legacy.
        _node_cap = node_def.get("capability_scope") or {}
        scoped_run_context = filter_run_context_scope(run_context, _node_cap.get("context_scope"))
        template_vars: dict[str, Any] = {
            "state": scoped_state,
            "run_context": scoped_run_context,
            "input": raw_input,
        }
        resolved = node_def.get("_resolved_parameters")
        if isinstance(resolved, dict):
            template_vars["parameter"] = resolved

        try:
            rendered_prompt = template.render(**template_vars)
        except (jinja2.UndefinedError, TypeError) as e:
            _log.warning("Prompt template UndefinedError for run %s: %s", run_id, e)
            return {
                "status": "skipped",
                "summary": f"Skipped: prompt template references missing input fields ({e})",
                "agent_stdout": "",
                "agent_stderr": "",
                "exit_code": 0,
            }

        # Render agent_command through the same SandboxedEnvironment +
        # template vars as the prompt, so the LLM model (or any other
        # command flag) can be a per-run / per-parameter value (e.g.
        # ``--model {{ input.model }}`` for A/B testing). A command with NO
        # ``{{ }}`` templates renders to itself unchanged — backward
        # compatible with every existing pipeline.
        try:
            rendered_agent_command = env.from_string(agent_command).render(**template_vars)
        except (jinja2.UndefinedError, TypeError) as e:
            _log.warning(
                "agent_command template UndefinedError for run %s node %s: %s",
                run_id,
                node_id,
                e,
            )
            return {
                "status": "skipped",
                "summary": f"Skipped: agent_command template references missing input fields ({e})",
                "agent_stdout": "",
                "agent_stderr": "",
                "exit_code": 0,
            }
        except jinja2.TemplateSyntaxError as e:
            # Legacy commands that contain Jinja-like syntax without being
            # valid templates (e.g. ``${{ }}`` shell fragments or an unclosed
            # ``{{``) predate #1291 and must keep running verbatim.
            # Rendering them as templates would crash the run with an
            # uncaught TemplateSyntaxError.
            _log.warning(
                "agent_command is not a valid template for run %s node %s; using verbatim: %s",
                run_id,
                node_id,
                e,
            )
            rendered_agent_command = agent_command
        if not rendered_agent_command.strip():
            raise ValueError(
                f"sandbox_agent node '{node_id}' rendered agent_command is empty after template resolution"
                " — a sandbox agent cannot run an empty command"
            )

    start_time = time.monotonic()
    sandbox: AsyncSandbox | None = None
    # The executor's CAPTURED claim token (seeded into LangGraph state as
    # ``_claim_lease`` at execute/resume start). Used to fence the DB-atomic
    # dispatch marker so a superseded original cannot set a marker / create a
    # sandbox for a run a successor owns.
    claim_lease: str | None = state.get("_claim_lease")
    dispatch_marker_set = False
    # FAR-296 Phase 2 fencing lease: True once the script-mode fencing lease
    # is persisted IMMEDIATELY BEFORE ``sandbox.commands.run`` — i.e. the
    # script PROCESS has started (execution claimed). Gates the stage-split:
    # a fault with ``_script_lease_claimed`` True is POST-CLAIM (terminal,
    # never retryable); before it the fault is PRE-CLAIM (retryable).
    _script_lease_claimed = False
    # FAR-296 Phase 3b-3: platform-side resource-cap killer state lives on
    # the per-run _SandboxWatchdog instance (never LangGraph state):
    # ``_budget_check_ticks`` counts watchdog.tick invocations to pace the
    # get_metrics() poll; ``_budget_killed`` flips True the instant the killer
    # kills the sandbox so the post-watchdog classification raises
    # ScriptBudgetKilledError (never a generic side-effect-unknown / failed
    # misclassification).
    # Per-node, per-claim-attempt idempotency key (dist/cleanup-idempotency D5):
    # ``run:{run_id}:node:{node_id}:{claim_count}``. Derived from the run row's
    # claim_count inside the fenced acquire, carried by the dispatch marker, and
    # surfaced on the node output/telemetry so a re-run of the same node under a
    # DIFFERENT claim is distinguishable (a successor resumes from the previous
    # claim's checkpoint and re-executes the wedged node with a new attempt key).
    attempt_key: str | None = None

    _stdout_len = 0
    _stderr_len = 0
    _sandbox_id: str | None = None
    _sandbox_log_tail: str = ""
    agent_stdout: str = ""
    agent_stderr: str = ""
    output_json: Any = None
    # FAR-228: the cancellation-retention handler below runs whenever a
    # CancelledError escapes — including during provisioning, BEFORE the
    # inner drain closure is ever defined. Pre-bind at function scope so
    # the handler's guard (`_drain_fn is not None`) short-circuits safely
    # instead of raising UnboundLocalError that replaces the cancellation.
    _drain_fn: Any = None
    _drained_chunks: list[str] = []
    # FAR-211: the local loop-interception callback server, started when the
    # node's loop_intercept config is enabled AND the pipeline has bound
    # guardrails. Pre-bound at function scope so the finally-block teardown
    # short-circuits safely (None) when the bridge was never started.
    _bridge_server: LoopInterceptCallbackServer | None = None

    # FAR-228 guard A (early skipped-return / fallback): when the run has
    # ALREADY delivered (a prior attempt's marker carries delivery_done=True)
    # and this is the opt-in single-node case, return the SKIPPED ENVELOPE
    # without provisioning a sandbox. Non-opt-in nodes pay ZERO here (no
    # settings read, no DB read). Fail-open in every direction: a settings
    # read error, a DB read error, or a non-matching marker all proceed to
    # provision normally. The run then completes COMPLETE via the normal
    # path (_stream_graph publishes run_completed); the phantom
    # node_attempt_count increment is accepted by design.
    if delivery_sentinel and single_sandbox_node:
        _gate_enabled = True
        try:
            from modulo.settings import get_settings

            _gate_enabled = getattr(get_settings(), "modulo_idempotency_gate_enabled", True)
        except Exception:
            _log.warning(
                "sandbox_agent.idempotency_gate_killswitch_check_failed",
                extra={"node_id": node_id, "run_id": run_id},
            )
        if _gate_enabled:
            _markers = await _read_run_raw_output_markers_for_gate(
                session_factory,
                run_id=run_id,
                org_id_raw=org_id,
                claim_lease=claim_lease,
                node_id=node_id,
            )
            if _marker_delivery_done_for_node(_markers, run_id, node_id):
                _log.info(
                    "sandbox_agent.idempotency_gate.skipped",
                    extra={"node_id": node_id, "run_id": run_id},
                )
                return _idempotency_gate_skipped_envelope(node_id)

    async def _acquire_dispatch_marker() -> str | None:
        """DB-atomic dispatch marker (dist/runtime-core A4): one transaction
        reads ``runs.claim_count`` (fenced on the claim token + status), then
        claims the dispatch slot IMMEDIATELY BEFORE ``AsyncSandbox.create``.

        ``UPDATE runs SET sandbox_dispatch_state=:marker, sandbox_id=:sid
        WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND
        status='running'`` — the marker is a structured JSON carrying the
        attempt key. The UPDATE is atomic, no read-then-create TOCTOU;
        rowcount 0 means the claim is superseded or the run is not running,
        the caller raises :class:`SupersededNodeError` and MUST NOT create a
        sandbox. The SELECT and UPDATE share one transaction, and the UPDATE
        re-checks the same fenced WHERE, so a concurrent claim rotation
        between them makes the UPDATE match zero rows and the attempt key is
        never persisted for a superseded claim.

        Returns the attempt key on success, ``None`` when denied. Fail-open
        (returns a claim-token-derived attempt key WITHOUT writing) when no
        session factory or no claim lease is available.
        """
        return await _sandbox_acquire_dispatch_marker(
            session_factory=session_factory,
            claim_lease=claim_lease,
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
        )

    async def _store_dispatch_marker_sandbox(sandbox_id_value: str | None) -> None:
        """Persist the real sandbox id onto the runs row after a successful create."""
        await _sandbox_store_dispatch_marker_sandbox(
            sandbox_id_value,
            session_factory=session_factory,
            claim_lease=claim_lease,
            org_id=org_id,
            run_id=run_id,
            attempt_key=attempt_key,
        )

    async def _store_script_lease() -> None:
        """FAR-296 Phase 2 fencing lease: record the script-mode execution claim.

        Reuses the EXISTING ``runs.sandbox_dispatch_state`` machinery — no
        parallel lease store. Persists ``{"state": "script_executing",
        "attempt_key": ...}`` IMMEDIATELY BEFORE ``sandbox.commands.run``, so
        the durable marker proves "the script PROCESS started (execution
        claimed, completion marker pending)". Fenced on the claim token +
        status so a superseded original cannot stamp a lease on a successor's
        row. Fail-open (no session factory / claim lease / org) — the lease
        is a safety backstop, never a correctness dependency.
        """
        await _sandbox_store_script_lease(
            session_factory=session_factory,
            claim_lease=claim_lease,
            org_id=org_id,
            run_id=run_id,
            attempt_key=attempt_key,
        )

    async def _mint_run_api_key_for_sandbox() -> str | None:
        """Mint a short-TTL runner-role API key for this script-mode sandbox (FAR-296 Phase 3b).

        Returns the raw key value, or None when minting is unavailable/failed
        (fail-open — the sandbox runs without the key rather than failing the
        dispatch). TTL = max(15 min, node_timeout + 5 min), capped by the
        org-level max (default 1 hour). The raw value is returned to the
        caller only, which injects it into the sandbox envs — it is NEVER
        logged, checkpointed, or placed in LangGraph state.
        """
        return await _sandbox_mint_run_api_key_for_sandbox(
            session_factory=session_factory,
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            sandbox_timeout=sandbox_timeout,
        )

    async def _clear_dispatch_marker() -> None:
        """Fenced marker clear — only when the claim token still matches.

        A superseded original (token rotated by a successor) must not clear
        the successor's dispatch marker / sandbox id.
        """
        await _sandbox_clear_dispatch_marker(
            session_factory=session_factory,
            claim_lease=claim_lease,
            org_id=org_id,
            run_id=run_id,
        )

    try:
        # FAR-296 Phase 4b: dispatch-time sandbox capacity gate. Fail-fast
        # before wasting E2B provisioning time — if the org is at sandbox
        # capacity, raise a retryable capacity.org error immediately instead
        # of letting the sandbox provision and then get demoted at claim time.
        if sandbox_mode == "script" and session_factory is not None:
            _org_id_raw = state.get("_org_id")
            try:
                _org_uuid = uuid.UUID(str(_org_id_raw)) if _org_id_raw else None
            except (TypeError, ValueError):
                _org_uuid = None
            if _org_uuid is not None:
                try:
                    from modulo.db.crud.run import (
                        count_active_sandbox_leases_for_org,
                        get_sandbox_concurrency_limit,
                    )
                    from modulo.db.rls import set_rls_execution_context, set_rls_org

                    async with session_factory() as _cap_session, _cap_session.begin():
                        await set_rls_org(_cap_session, _org_uuid)
                        await set_rls_execution_context(_cap_session)
                        _cap = await get_sandbox_concurrency_limit(_cap_session, _org_uuid)
                        if _cap is not None:
                            _active = await count_active_sandbox_leases_for_org(
                                _cap_session, _org_uuid, exclude_run_id=uuid.UUID(str(run_id))
                            )
                            if _active >= _cap:
                                _log.info(
                                    "sandbox_agent.dispatch_capacity_denied",
                                    extra={
                                        "run_id": run_id,
                                        "org_id": str(_org_uuid),
                                        "active": _active,
                                        "cap": _cap,
                                    },
                                )
                                raise SandboxCapacityExceededError(
                                    f"Sandbox dispatch denied: org {_org_uuid} at capacity "
                                    f"({_active}/{_cap} active sandbox leases)"
                                )
                except SandboxCapacityExceededError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning(
                        "sandbox_agent.dispatch_capacity_check_failed",
                        extra={"run_id": run_id, "org_id": str(_org_uuid)},
                    )
        # DB-atomic dispatch marker (dist/runtime-core A4) — replaces the
        # retired Redis SETNX E2B fence. Exactly ONE executor wins the
        # dispatch slot; a superseded claim / non-running run is refused
        # BEFORE any sandbox is created. Fail-open when no session factory
        # or no claim lease is available (the heartbeat claim fence remains
        # the primary guard).
        if (attempt_key := await _acquire_dispatch_marker()) is None:
            _log.warning(
                "sandbox_agent.dispatch_marker_denied",
                extra={"node_id": node_id, "run_id": run_id},
            )
            raise SupersededNodeError("E2B dispatch marker denied — run superseded or not running; sandbox not created")
        dispatch_marker_set = True

        # FAR-296 Phase 3/3b-3: egress control + resource limits. deny_all
        # and selected map to allow_internet_access=False; resource_limits
        # and the selected-mode host:port allowlist are carried as sandbox
        # metadata (e2b tags) so a server-side template/config can enforce
        # them (the e2b SDK has no native allowlist / cap enforcement).
        _metadata: dict[str, str] = {}
        if resource_limits:
            _metadata["resource_limits"] = json.dumps(resource_limits)
        if egress_policy == "selected" and egress_allowlist:
            _metadata["egress_allowlist"] = json.dumps(egress_allowlist)
        # FAR-296 Phase 4a: E2B concurrent-sandbox rate limits (429 / resource
        # exhausted) are TRANSIENT. Retry ``AsyncSandbox.create`` with
        # exponential backoff, bounded by the create timeout window and the
        # node timeout. A run that exhausts the retries fails RETRYABLY
        # (sandbox.rate_limited) — it must NOT permanently fail as
        # harness.unknown.
        _rate_limit_attempt = 0
        while True:
            try:
                sandbox = await asyncio.wait_for(
                    AsyncSandbox.create(
                        template=template_id,
                        # FAR-487: lifetime STRICTLY greater than the command
                        # timeout (+ _SANDBOX_LIFETIME_GRACE_S) so the platform
                        # endAt kill can never preempt the runner's own timeout
                        # path — a mid-command sandbox death fabricated a
                        # zero-exit completion and misreported the failure as
                        # "no parseable output.json (exit code 0)".
                        # FAR-489: int() — the e2b SDK's attrs model does NOT
                        # coerce a float, and E2B's Go server rejects
                        # "1320.0" with 400 (int32 unmarshal), instantly
                        # failing every sandbox create.
                        timeout=int(sandbox_timeout + _SANDBOX_LIFETIME_GRACE_S),
                        allow_internet_access=(egress_policy not in ("deny_all", "selected")),
                        # deny_all/selected -> no internet; default/None ->
                        # internet allowed (e2b default). IMPORTANT
                        # (FAR-296 Phase 3b-3): ``selected`` DENIES ALL egress
                        # at this boolean level — the host:port egress_allowlist
                        # is carried only as metadata and is NOT yet honored by
                        # any enforcement point (no template-side mechanism
                        # exists; the e2b SDK has no native allowlist control).
                        # ``selected`` is functionally equivalent to ``deny_all``
                        # until that point lands.
                        metadata=_metadata or None,
                    ),
                    timeout=min(sandbox_timeout, 120),
                )
                break
            except RateLimitException as _rle:
                _rate_limit_attempt += 1
                if _rate_limit_attempt > _SANDBOX_RATE_LIMIT_MAX_RETRIES:
                    raise SandboxQueueTimeoutError(
                        f"E2B rate-limited after {_rate_limit_attempt} attempts creating sandbox for node '{node_id}'"
                    ) from None
                _backoff = _SANDBOX_RATE_LIMIT_BASE_BACKOFF_S * (2 ** (_rate_limit_attempt - 1))
                _log.warning(
                    "sandbox_agent.e2b_rate_limited_retrying",
                    extra={
                        "run_id": run_id,
                        "node_id": node_id,
                        "attempt": _rate_limit_attempt,
                        "backoff_seconds": _backoff,
                    },
                )
                _emit_script_span_event(
                    "script.rate_limited_retry",
                    {
                        "attempt": _rate_limit_attempt,
                        "backoff_seconds": _backoff,
                    },
                )
                await asyncio.wait_for(
                    asyncio.sleep(_backoff),
                    timeout=min(sandbox_timeout, 120),
                )
        if sandbox is None:
            raise RuntimeError("Sandbox was not created before use")
        _sandbox_id = getattr(sandbox, "sandbox_id", None) or None
        _emit_script_span_event(
            "script.provisioned",
            {
                "sandbox_id": _sandbox_id,
                "template": template_id,
                "mode": sandbox_mode,
            },
        )
        # Persist the real sandbox id so the heartbeat-lost path
        # (run_executor_with_watchdog) can kill the sandbox by id.
        await _store_dispatch_marker_sandbox(_sandbox_id)
        for raw_path, raw_content in context_files.items():
            write_path = raw_path.removesuffix(".b64") if raw_path.endswith(".b64") else raw_path
            write_content = base64.b64decode(raw_content).decode() if raw_path.endswith(".b64") else raw_content
            await asyncio.wait_for(sandbox.files.write(write_path, write_content), timeout=_SANDBOX_IO_TIMEOUT)

        # FAR-296 mode split: llm mode writes the rendered prompt to
        # prompt.md; script mode writes the FULL run input (no 10KB
        # truncation) to /home/user/input.json and never writes a prompt.
        _input_json = json.dumps(raw_input)
        if sandbox_mode == "script":
            await asyncio.wait_for(
                sandbox.files.write("/home/user/input.json", _input_json),
                timeout=_SANDBOX_IO_TIMEOUT,
            )
        else:
            if len(_input_json) > 10240:
                _input_json = json.dumps(
                    {"_truncated": True, "_key_count": len(raw_input) if isinstance(raw_input, dict) else 0}
                )
            await asyncio.wait_for(
                sandbox.files.write("/home/user/prompt.md", rendered_prompt),
                timeout=_SANDBOX_IO_TIMEOUT,
            )

        env_vars_extra: dict[str, str] = await resolve_env_var_refs(
            node_def.get("env_vars") or {},
            lambda k: _sandbox_resolve_secret_ref(
                k,
                session_factory=session_factory,
                org_id=org_id,
            ),
        )

        # FAR-418: expose the node's capability_scope.allowed_tools to the sandbox
        # agent runtime (FAR-402 P4 / FAR-418) so the agent's MCP client can
        # forward it as the ``X-Modulo-Allowed-Tools`` header. The MCP server's
        # McpAuthMiddleware lifts that header into the request-scoped allow-list
        # consumed by check_tool_scope, enforcing node-level tool scoping in the
        # production run path. Absent/empty (the UNRESTRICTED default) sets nothing,
        # preserving pre-scope behaviour.
        _node_scope = node_def.get("capability_scope") or {}
        _allowed_tools = _node_scope.get("allowed_tools")
        if _allowed_tools:
            env_vars_extra["MODULO_ALLOWED_TOOLS"] = ",".join(str(t) for t in _allowed_tools)

        # FAR-212 PR B: apply the enforced sandbox policy AFTER the Modulo-owned
        # context files / prompt / input are written but BEFORE the agent/script
        # command executes. Any policy field set on the node invokes the policy
        # step (read-only workspace chmod, git-credential scope helper, or
        # selected-mode egress allowlist). Selected-mode allowlist hostnames are
        # pre-resolved to concrete IPs so the iptables rules bind real addresses
        # (an unresolvable host stays denied — fail-closed). The policy is
        # best-effort at the command level (each step is itself fail-closed) and
        # never wedges the dispatch.
        if _should_apply_sandbox_policy(
            read_only=read_only,
            git_credentials=git_credentials,
            egress_policy=egress_policy,
            egress_allowlist=egress_allowlist,
        ):
            from modulo.core.pipeline_engine.sandbox_policy import apply_sandbox_policy

            await apply_sandbox_policy(
                sandbox,
                read_only=read_only,
                git_credentials=git_credentials,
                egress_policy=egress_policy,
                egress_allowlist=await _resolve_egress_allowlist(egress_allowlist),
            )

        try:
            # FAR-306: per-channel stall detector. The heartbeat channel
            # (connection liveness) is the default; the opt-in detectors
            # (log-growth, stdout-delta, filesystem) add extra channels that
            # must ALL be silent before the watchdog fires. ``_activity``
            # remains the stream-buffer/throttle dict (not a liveness source).
            _stall = _configure_stall_detector(
                enable_heartbeat=enable_heartbeat,
                watch_log_path=watch_log_path,
                stdout_percentage_delta=stdout_percentage_delta,
                watch_globs=watch_globs,
            )
            # Track the last time the agent emitted output so the idle
            # watchdog can fail fast on stalls (FAR-97). The callbacks run
            # from the SDK's event task and may be async or sync.
            stall_reason: str | None = None

            # FAR-98: stall_timeout_seconds (node config) overrides the idle
            # watchdog's silence window. Resolve the DEFAULT at runtime so
            # the constant stays patchable (the FAR-97 tests patch it).
            try:
                stall_timeout: float = (
                    float(stall_timeout_override) if stall_timeout_override is not None else _SANDBOX_IDLE_TIMEOUT
                )
            except (TypeError, ValueError):
                _log.warning(
                    "sandbox_agent.stall_timeout_invalid_fallback",
                    extra={"node_id": node_id, "stall_timeout_raw": stall_timeout_override},
                )
                stall_timeout = _SANDBOX_IDLE_TIMEOUT

            # Live-output streaming (FAR-98): look the run event broker up in
            # the process-local registry by run id (the broker is never carried
            # inside LangGraph state — it is not msgpack-serializable, and
            # carrying it in state broke checkpoint writes for every run).
            # Buffer stdout/stderr chunks and publish a throttled
            # node.stdout_chunk / node.stderr_chunk event at most once per
            # _STREAM_FLUSH_INTERVAL so Run detail can show live output while
            # the sandbox process runs. No broker registered -> skip silently
            # (streaming is best-effort, never fatal).
            _stream_broker = None
            if run_id:
                try:
                    _stream_broker = get_registry().get(uuid.UUID(run_id))
                except (TypeError, ValueError):
                    _stream_broker = None

            watchdog = _SandboxWatchdog(
                sandbox=sandbox,
                stall=_stall,
                node_id=node_id,
                run_id=run_id,
                watch_log_path=watch_log_path,
                watch_globs=watch_globs,
                resource_limits=resource_limits,
                sandbox_mode=sandbox_mode,
                stdout_percentage_delta=stdout_percentage_delta,
                stream_broker=_stream_broker,
                drained_chunks=_drained_chunks,
                wallclock_budget_seconds=wallclock_budget_seconds,
                start_time=start_time,
            )

            _drain_fn = watchdog.drain_sandbox_log

            # Redirect the agent's stdout/stderr into a sandbox log file so
            # the process writes to a regular file — never a pipe that can
            # fill and block a long session (FAR-97). The subshell preserves
            # the command's exit code for the SDK's wait().
            # System env vars first — provide defaults from the host. DO NOT
            # move pipeline env before these: pipelines need to override
            # GITHUB_TOKEN for identity separation (e.g. PR Reviewer uses
            # modulo-reviewbot PAT, not the system default farnalabs bot).
            # The reserved-prefix validator already prevents overriding
            # MODULO_* vars, so update() below is the sanctioned override.
            sandbox_envs: dict[str, str] = _build_sandbox_envs(
                run_id=run_id,
                pipeline_id=pipeline_id,
                org_id=org_id,
                input_json=_input_json,
                sandbox_mode=sandbox_mode,
                env_vars_extra=env_vars_extra,
            )
            # FAR-296 Phase 4a: wall-clock spend budget — non-tick path. A very
            # slow provisioning sequence may already have consumed the budget
            # before the script process starts. Even though no side effects
            # occurred, the budget was exhausted — re-dispatching would waste
            # another provisioning cycle on a run that already consumed its
            # budget allocation. Script mode only: LLM-mode nodes do not take a
            # wall-clock budget; raising ScriptBudgetKilledError for an LLM-mode
            # node would be a misclassification.
            _check_wallclock_budget_pre_run(
                sandbox_mode=sandbox_mode,
                wallclock_budget_seconds=wallclock_budget_seconds,
                start_time=start_time,
                watchdog=watchdog,
                run_id=run_id,
                node_id=node_id,
            )
            # FAR-296 Phase 2 fencing lease (script mode only): persist the
            # execution claim IMMEDIATELY BEFORE the script process starts so
            # a durable marker proves the script RAN. Once claimed, any fault
            # is POST-CLAIM (terminal — never retryable, exactly-once). LLM
            # mode does NOT take a lease (at-least-once, no fencing).
            if sandbox_mode == "script":
                await _store_script_lease()
                _script_lease_claimed = True
                _emit_script_span_event(
                    "script.lease_claimed",
                    {
                        "run_id": run_id,
                        "node_id": node_id,
                    },
                )
                # FAR-296 Phase 3b: mint a short-TTL runner-role API key so
                # the script can authenticate to the Modulo API with a
                # restricted identity. The key is per-run, revoked at run
                # finalization, and never the long-lived host credentials.
                # Fail-open: a mint failure leaves the key absent (the
                # sandbox still runs).
                _run_api_key = await _mint_run_api_key_for_sandbox()
                if _run_api_key:
                    sandbox_envs["MODULO_API_KEY"] = _run_api_key
            _bridge_wrapped_command = rendered_agent_command
            # FAR-211: start the loop-interception callback server + write
            # the bridge client into the sandbox. Best-effort and
            # fail-open in EVERY direction: the bridge is only started when
            # the node's loop_intercept config is enabled AND the pipeline
            # has bound guardrails (zero guardrails -> the bridge is
            # inert); a setup failure disables the bridge for this node but
            # NEVER blocks the dispatch or wedges the agent loop. The
            # endpoint is the Modulo sandbox-agent process's localhost —
            # the ADR 003 amendment documents how it is exposed into the
            # sandbox (in the test-driven slice it is the same host).
            if loop_intercept_config is not None and loop_intercept_config.enabled:
                try:
                    bridge_defs = await load_loop_intercept_guardrails(
                        session_factory,
                        org_id=state.get("_org_id"),
                        pipeline_id=state.get("_pipeline_id"),
                    )
                    if bridge_defs:
                        _bridge_server = LoopInterceptCallbackServer(
                            engine=EvalEngine(),
                            definitions=bridge_defs,
                            config=loop_intercept_config,
                            audit_sink=partial(
                                persist_loop_interception_audit,
                                session_factory=session_factory,
                                org_id=org_id,
                                run_id=run_id,
                                node_id=node_id,
                            ),
                        )
                        _bridge_port = await _bridge_server.start()
                        await asyncio.wait_for(
                            sandbox.files.write("/home/user/modulo_bridge.py", bridge_client_source()),
                            timeout=_SANDBOX_IO_TIMEOUT,
                        )
                        await asyncio.wait_for(
                            sandbox.files.write(
                                "/home/user/modulo_bridge_config.json",
                                json.dumps(loop_intercept_config.model_dump(mode="json")),
                            ),
                            timeout=_SANDBOX_IO_TIMEOUT,
                        )
                        sandbox_envs["MODULO_BRIDGE_ENDPOINT"] = f"http://127.0.0.1:{_bridge_port}"
                        sandbox_envs["MODULO_BRIDGE_CONFIG"] = "/home/user/modulo_bridge_config.json"
                        _bridge_wrapped_command = (
                            f"python3 /home/user/modulo_bridge.py --wrap -- {rendered_agent_command}"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception(
                        "sandbox_agent.loop_intercept_setup_failed",
                        extra={"node_id": node_id, "run_id": run_id},
                    )
                    _bridge_server = None
            # FAR-296 Phase 4a: wall-clock spend budget — non-tick path (final
            # check immediately before the command starts). A very slow bridge /
            # env setup may have consumed the budget; do not start the command
            # past the budget. ``_budget_killed`` may already be set by the
            # pre-lease check above (script mode) — guard against a double
            # kill/raise. Script mode only: same rationale as the pre-lease
            # check.
            _check_wallclock_budget_pre_run(
                sandbox_mode=sandbox_mode,
                wallclock_budget_seconds=wallclock_budget_seconds,
                start_time=start_time,
                watchdog=watchdog,
                run_id=run_id,
                node_id=node_id,
            )
            wrapped_command = f"( {_bridge_wrapped_command} ) > {_SANDBOX_LOG_PATH} 2>&1"
            cmd_handle = await asyncio.wait_for(
                sandbox.commands.run(
                    wrapped_command,
                    background=True,
                    on_stdout=watchdog.on_stdout,
                    on_stderr=watchdog.on_stderr,
                    timeout=sandbox_timeout,
                    envs=sandbox_envs,
                ),
                timeout=min(sandbox_timeout, 120),
            )
            _emit_script_span_event(
                "script.command_started",
                {
                    "run_id": run_id,
                    "node_id": node_id,
                    "command": rendered_agent_command[:200] if sandbox_mode == "script" else "llm",
                },
            )
            cmd_result, stall_reason = await _wait_command_with_idle_watchdog(
                cmd_handle,
                total_timeout=sandbox_timeout,
                idle_timeout=stall_timeout,
                # FAR-306: last_activity is the max across all ENABLED
                # channels (heartbeat + any opt-in detectors).
                last_activity=_stall.last_activity,
                on_tick=watchdog.tick,
                tick_interval=_SANDBOX_TAIL_INTERVAL,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _log.warning(
                "sandbox_agent.command_timed_out",
                extra={
                    "node_id": node_id,
                    "timeout": sandbox_timeout,
                },
            )
            cmd_result = None
        except SupersededNodeError:
            # A superseded script lease (rowcount 0 in _store_script_lease)
            # must propagate as a RETRYABLE SupersededNodeError — never be
            # swallowed into a post-claim script failure. Raising here lets
            # the outer handler re-raise it to the executor's retry path.
            raise
        except Exception as _cee:
            _log.exception(
                "sandbox_agent.command_failed",
                extra={
                    "node_id": node_id,
                    "exc_type": type(_cee).__name__,
                    "exc_msg": str(_cee)[:_MAX_ERROR_MSG],
                },
            )
            cmd_result = getattr(_cee, "result", None) or _cee

        # One final drain so the last growth (between the last tick and the
        # process exit) is captured before we read output.json. The probe is
        # fully guarded — on a dead sandbox it returns immediately.
        await watchdog.drain_sandbox_log()

        elapsed = time.monotonic() - start_time
        exit_code: int = getattr(cmd_result, "exit_code", -1)
        # The redirected log file is the process's real stdout — prefer the
        # drained content (which also survives a timeout where cmd_result is
        # None and would otherwise surface EMPTY output), falling back to the
        # SDK's captured stream for non-redirected (legacy) paths.
        agent_stdout_raw: str = "".join(_drained_chunks) or (getattr(cmd_result, "stdout", "") or "")
        agent_stderr_raw: str = getattr(cmd_result, "stderr", "") or ""
        _stdout_len = len(agent_stdout_raw)
        _stderr_len = len(agent_stderr_raw)
        agent_stdout = _redact_raw_output(agent_stdout_raw[:_MAX_ARTIFACT_LOG])
        agent_stderr = _redact_raw_output(agent_stderr_raw[:_MAX_ARTIFACT_LOG])

        # A timed-out command leaves ``cmd_result`` as None: the run timed
        # out (1800s node timeout) with COMPLETELY EMPTY stdout/stderr and
        # exit_code -1. Surface a clear explanation instead of silently
        # returning an empty-summary failure. A STALLED command (idle
        # watchdog fired, FAR-98) carries a distinct stall_reason so the
        # two failure modes are distinguishable.
        command_error: str = ""
        if cmd_result is None:
            if stall_reason:
                command_error = stall_reason
            else:
                command_error = (
                    f"Sandbox agent command produced no output within {sandbox_timeout}s. "
                    "No stdout/stderr was captured — the agent likely hung before "
                    "writing any result."
                )
            # The E2B kill reason only lives in the sandbox logs, and the logs
            # endpoint only serves live sandboxes — fetch the tail BEFORE the
            # kill below (FAR-97 observability).
            _sandbox_log_tail = await _fetch_sandbox_log_tail(_sandbox_id)
            # The command stalled or timed out. Kill the sandbox BEFORE
            # reading output.json: the interrupted-but-alive process could
            # otherwise write a fabricated completion in the grace window
            # (FAR-97 — Improve Tests reported "improvement applied" with
            # changed_files: [] exactly this way).
            try:
                await asyncio.wait_for(
                    sandbox.kill(request_timeout=_OUTPUT_READ_TIMEOUT),
                    timeout=_OUTPUT_READ_TIMEOUT,
                )
            except Exception:
                _log.exception(
                    "sandbox_agent.kill_before_output_read_failed",
                    extra={"node_id": node_id},
                )
            # A6: a stall or total timeout is a retryable sandbox-infra
            # failure — RAISE (never a silent completed/wrong-success node).
            # FAR-188: output.json was never read, so retain the captured
            # stdout as the raw evidence — a pr_url created before the stall
            # must not be lost either. The drain window keeps only the last
            # _MAX_DRAIN_WINDOW (512KB) tail, so the marker's raw_output is
            # a bounded tail (documented in _retain_raw_output_marker).
            _stall_source: str = agent_stdout_raw
            if cmd_result is not None:
                _sdk_stdout = str(getattr(cmd_result, "stdout", "") or "")
                if len(_sdk_stdout) > len(_stall_source):
                    _stall_source = _sdk_stdout
            await _retain_raw_output_marker(
                session_factory,
                run_id=run_id,
                org_id_raw=org_id,
                node_id=node_id,
                attempt_key=attempt_key,
                summary=(
                    "Sandbox agent command stalled/timed out — raw stdout retained "
                    "(drain window keeps the last 512KB tail)"
                ),
                source=_stall_source,
                parse_error=command_error,
                exit_code=exit_code,
                stdout_length=_stdout_len,
                stderr_length=_stderr_len,
                delivery_sentinel=delivery_sentinel,
            )
            if watchdog.budget_killed:
                # FAR-296 Phase 3b-3: the platform-side resource-cap killer
                # fired — the sandbox was killed for exceeding its limits.
                # TERMINAL (never retryable, exactly-once) and distinct from
                # an unknown side-effect state: the kill REASON is known.
                raise ScriptBudgetKilledError(_script_budget_killed_message(node_id)) from None
            if _is_script_post_claim_fault(sandbox_mode, _script_lease_claimed):
                # FAR-296 Phase 2 stage-split (3): the script PROCESS started
                # (fencing lease claimed) and was terminated mid-execution
                # (timeout / budget / watchdog) with exit undetermined — the
                # side effect may or may not have happened. Never retryable:
                # escalate to needs-human via ``script.side_effect_unknown``.
                raise ScriptSideEffectUnknownError(
                    "Script-mode sandbox terminated mid-execution (side effect unknown): "
                    + (command_error or f"no output within {sandbox_timeout}s")
                )
            raise SandboxNodeFailedError(
                command_error or f"Sandbox agent command failed (no output within {sandbox_timeout}s)",
                node_id=node_id,
            )

        raw_output: str = ""
        output_read_error: str = ""
        if cmd_result is not None:
            try:
                _remaining_after_cmd = max(_OUTPUT_READ_TIMEOUT, sandbox_timeout - (time.monotonic() - start_time))
                raw_output = await asyncio.wait_for(
                    sandbox.files.read(
                        "/home/user/output.json",
                        request_timeout=_remaining_after_cmd,
                    ),
                    timeout=_remaining_after_cmd,
                )
                output_json = json.loads(raw_output)
            except asyncio.CancelledError:
                raise
            except Exception as _read_exc:
                output_read_error = f"{type(_read_exc).__name__}: {str(_read_exc)[:_MAX_ERROR_MSG]}"
                _log.info(
                    "sandbox_agent.no_output_json",
                    extra={
                        "node_id": node_id,
                        "exit_code": exit_code,
                        "command_error": command_error,
                        "output_read_error": output_read_error,
                        "stdout_length": _stdout_len,
                        "stderr_length": _stderr_len,
                        "sandbox_id": _sandbox_id,
                    },
                )

        # FAR-188: an agent that produced no usable output.json content — a
        # failed/unparseable read (output_json None) or a PARSEABLE but
        # non-dict value ([], "str", 123, null). In BOTH cases the raw
        # evidence is retained (pr_url extraction, marker persist). Only the
        # TRULY-no-output case (output_json is None — read failure or
        # json.loads("null")) raises SandboxNodeFailedError: a node with
        # zero usable work must never complete the run silently (A6).
        # A parseable non-dict value retains the marker and CONTINUES
        # through the existing shaping path — agent_status stays None
        # exactly as before (corrected FIX 4; test_node_runner_agent_status
        # asserts this proceed-with-None behaviour and stays green).
        if output_json is None or not isinstance(output_json, dict):
            # Evidence is the UNION of raw sources — the file content AND
            # the captured stdout (a pr_url echoed to stdout when output.json
            # is present-but-malformed must be found). pr_url is extracted
            # from the FULL pre-truncation source, then raw_output is
            # truncated for storage (inside the builder).
            _raw_parts = [p for p in (_normalize_marker_text(raw_output), agent_stdout_raw) if p]
            _raw_combined = "\n".join(_raw_parts)
            if output_json is None:
                _parse_error = output_read_error or "output.json is empty or JSON null"
                await _retain_raw_output_marker(
                    session_factory,
                    run_id=run_id,
                    org_id_raw=org_id,
                    node_id=node_id,
                    attempt_key=attempt_key,
                    summary="Sandbox agent produced no parseable output.json — raw output retained",
                    source=_raw_combined,
                    parse_error=_parse_error,
                    exit_code=exit_code,
                    stdout_length=_stdout_len,
                    stderr_length=_stderr_len,
                    delivery_sentinel=delivery_sentinel,
                )
                # FAR-197: surface WHY the agent failed. The captured
                # stdout/stderr tails plus the E2B log tail (the only place
                # the kill reason lives) are fetched BEFORE the finally-block
                # kill, while the sandbox is still alive — the logs endpoint
                # only serves live sandboxes.
                _no_output_log_tail = await _fetch_sandbox_log_tail(_sandbox_id)
                if watchdog.budget_killed:
                    # FAR-296 Phase 3b-3: the platform-side resource-cap killer
                    # fired. On the REAL kill path the command handle raises an
                    # e2b SandboxException (not TimeoutError), which lands here
                    # with cmd_result non-None and output.json unreadable — the
                    # kill REASON is known, so never misclassify as
                    # script.invalid_output / script.side_effect_unknown.
                    raise ScriptBudgetKilledError(_script_budget_killed_message(node_id)) from None
                if _is_script_post_claim_fault(sandbox_mode, _script_lease_claimed):
                    # FAR-296 Phase 2 stage-split (2): the script PROCESS
                    # started (lease claimed) and produced no parseable
                    # output.json — post-claim, TERMINAL (never retryable).
                    raise ScriptInvalidOutputError(
                        _build_no_output_message(
                            exit_code=exit_code,
                            stdout_raw=agent_stdout_raw,
                            stderr_raw=agent_stderr_raw,
                            sandbox_id=_sandbox_id,
                            read_raw=raw_output,
                            read_error=output_read_error,
                            log_tail=_no_output_log_tail,
                        )
                    )
                raise SandboxNodeFailedError(
                    _build_no_output_message(
                        exit_code=exit_code,
                        stdout_raw=agent_stdout_raw,
                        stderr_raw=agent_stderr_raw,
                        sandbox_id=_sandbox_id,
                        read_raw=raw_output,
                        read_error=output_read_error,
                        log_tail=_no_output_log_tail,
                    ),
                    node_id=node_id,
                )
            # Parseable non-dict output: retain the marker, do NOT raise —
            # fall through to the shared shaping path below.
            await _retain_raw_output_marker(
                session_factory,
                run_id=run_id,
                org_id_raw=org_id,
                node_id=node_id,
                attempt_key=attempt_key,
                summary="Sandbox agent output.json parsed to a non-dict value — raw output retained",
                source=_raw_combined,
                parse_error=f"output.json parsed to non-dict type {type(output_json).__name__}",
                exit_code=exit_code,
                stdout_length=_stdout_len,
                stderr_length=_stderr_len,
                delivery_sentinel=delivery_sentinel,
            )

        if sandbox_mode == "script":
            _emit_script_span_event(
                "script.finalized",
                {
                    "run_id": run_id,
                    "node_id": node_id,
                    "elapsed_seconds": round(time.monotonic() - start_time, 1),
                    "budget_killed": watchdog.budget_killed,
                    "exit_code": exit_code if cmd_result is not None else None,
                },
            )

        _span = _otel_trace.get_current_span()
        if _span.is_recording():
            _span.add_event(
                "sandbox.agent.output",
                {
                    "stdout": agent_stdout[:_MAX_OTEL_LOG_ATTR],
                    "stderr": agent_stderr[:_MAX_OTEL_LOG_ATTR],
                    "stdout_length": _stdout_len,
                    "stderr_length": _stderr_len,
                    "attempt_key": attempt_key or "",
                },
            )

        if isinstance(output_schema_json, dict) and isinstance(output_json, dict):
            try:
                _validate_against_schema(output_json, output_schema_json)
            except ValueError as _schema_exc:
                _log.exception(
                    "sandbox_agent.schema_validation_failed",
                    extra={"node_id": node_id},
                )
                if _is_script_post_claim_fault(sandbox_mode, _script_lease_claimed):
                    # FAR-296 Phase 2 stage-split (2): the script PROCESS
                    # started (lease claimed) and its output failed schema
                    # validation — post-claim, TERMINAL (never retryable).
                    # Raise a ScriptModeError subclass (not the shared
                    # OutputSchemaValidationError) so the fault surfaces
                    # under the never-retryable ``script.invalid_output``
                    # code and is excluded from ``failure`` retries. The
                    # generic ``schema_validation_failure`` code is SHARED
                    # with LLM-mode/manual paths and must stay retryable.
                    raise ScriptInvalidOutputError(
                        f"Script-mode output failed schema validation for node '{node_id}': {_schema_exc}"
                    ) from None
                elapsed = time.monotonic() - start_time
                _cost_estimate_usd = _compute_sandbox_cost(elapsed, output_json)
                return _build_sandbox_node_envelope(
                    node_id=node_id,
                    output=_SandboxNodeOutput(
                        status="failed",
                        # FAR-487: name the rejected field so an operator can
                        # align the agent's output shape (e.g. a synthesized
                        # failed-output) with what the schema accepts —
                        # without loosening the schema itself.
                        summary=f"Output failed schema validation: {_schema_exc}",
                        exit_code=exit_code,
                        wall_clock_time_ms=int(elapsed * 1000),
                        cost_estimate_usd=_cost_estimate_usd,
                        cost_source=output_json,
                        output_json=output_json,
                        agent_stdout=agent_stdout,
                        agent_stderr=agent_stderr,
                        stdout_length=_stdout_len,
                        stderr_length=_stderr_len,
                        attempt_key=attempt_key,
                        modulo_synthetic_failure=True,
                    ),
                )

        status: str = "completed" if exit_code == 0 else "failed"
        result_summary: str = ""
        changed_files: list[str] = []
        pr_url: str = ""
        # A1 elevation input (agent-failure UX, phase 1): surface the
        # agent's RAW verdict from output.json VERBATIM — never derived from
        # exit_code. Missing / non-string values degrade to None so a
        # missing status can never look like a false "complete".
        agent_status: str | None = None
        agent_outcome: str | None = None
        # FAR-227: the E2B wrapper's fallback echo for a dead opencode
        # session is NOT an agent verdict — the agent never wrote it. Detect
        # the placeholder, suppress the fabricated ``agent_status="failed"``
        # (which A1-elevates to non-retryable ``agent.failed``), and stamp a
        # distinct marker the executor routes to retryable
        # ``sandbox.no_output_json`` instead. The summary is preserved so the
        # failure detail stays visible.
        sandbox_session_lost: bool = False

        if sandbox_mode == "script":
            # FAR-296 Phase 2 stage-split (2): a non-zero exit AFTER the
            # script process started (the fencing lease is claimed) is a
            # POST-CLAIM fault — TERMINAL, never retryable (exactly-once).
            # Re-dispatching could double-execute the side-effecting script.
            if exit_code != 0:
                if watchdog.budget_killed:
                    # The command died BECAUSE the platform-side resource-cap
                    # killer killed the sandbox — never misclassify the kill
                    # as a generic script.failed.
                    raise ScriptBudgetKilledError(_script_budget_killed_message(node_id)) from None
                raise ScriptFailedError(f"Script-mode sandbox exited with code {exit_code} (post-claim, terminal)")
            # FAR-296 script mode: the raw parsed output IS the node output.
            # No LLM envelope extraction (summary/changed_files/pr_url/
            # status/outcome elevation) — output_json carries the script's
            # output verbatim and the summary is a short auto-generated line.
            result_summary = f"script mode: exit_code={exit_code}"
        elif isinstance(output_json, dict):
            result_summary = output_json.get("summary", "")
            changed_files = output_json.get("changed_files", [])
            pr_url = output_json.get("pr_url", "")
            sandbox_session_lost = _is_sandbox_session_lost_echo(output_json)
            _raw_status = output_json.get("status")
            _raw_outcome = output_json.get("outcome")
            if isinstance(_raw_status, str) and not sandbox_session_lost:
                agent_status = _raw_status
            if isinstance(_raw_outcome, str) and not sandbox_session_lost:
                agent_outcome = _raw_outcome
            if sandbox_session_lost:
                # A dead session is never a completed node, regardless of the
                # command's exit code (the wrapper may exit 0 after echoing).
                status = "failed"

        if status == "failed" and not result_summary:
            # Never report a silent empty-summary failure — explain WHY the
            # command produced no usable output.
            result_summary = command_error or "Sandbox agent command failed"

        _cost_estimate_usd = _compute_sandbox_cost(elapsed, output_json)

        # FAR-228 success-path marker: when opt-in AND the sentinel is a
        # FULL-LINE match in the FULL pre-truncation stdout (``agent_stdout_raw``,
        # NOT the head-truncated ``agent_stdout``), persist a delivery_done
        # marker onto ``raw_output_markers`` — the ONLY column guard A /
        # guard B / classification / gate_fired read. The envelope stamp
        # alone was unobservable (nothing reads delivery_done from
        # outputs_json); the marker is the durable record that closes the
        # completed-node-then-process-death gap. Best-effort and fail-open:
        # a persist failure must never break a successful run.
        if delivery_sentinel and _source_contains_delivery_sentinel(agent_stdout_raw, delivery_sentinel):
            try:
                await _retain_raw_output_marker(
                    session_factory,
                    run_id=run_id,
                    org_id_raw=org_id,
                    node_id=node_id,
                    attempt_key=attempt_key,
                    summary="Sandbox agent completed with delivery sentinel observed (idempotency gate)",
                    source=agent_stdout_raw,
                    parse_error="",
                    exit_code=exit_code,
                    stdout_length=_stdout_len,
                    stderr_length=_stderr_len,
                    delivery_sentinel=delivery_sentinel,
                    status=status,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "sandbox_agent.success_delivery_marker_persist_failed",
                    extra={"node_id": node_id, "run_id": run_id},
                )

        return _build_sandbox_node_envelope(
            node_id=node_id,
            output=_SandboxNodeOutput(
                status=status,
                summary=result_summary,
                changed_files=changed_files,
                pr_url=pr_url,
                exit_code=exit_code,
                wall_clock_time_ms=int(elapsed * 1000),
                cost_estimate_usd=_cost_estimate_usd,
                cost_source=output_json,
                output_json=output_json,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
                stdout_length=_stdout_len,
                stderr_length=_stderr_len,
                attempt_key=attempt_key,
                agent_status=agent_status,
                agent_outcome=agent_outcome,
                stall_reason=stall_reason,
                sandbox_session_lost=sandbox_session_lost,
            ),
            exclude_from_output=frozenset({"changed_files", "pr_url"}),
        )

    except asyncio.CancelledError:
        # FAR-228 (THE INCIDENT FIX): before re-raising, retain delivery
        # evidence. Run 9559's attempt 1 was cancelled AFTER the email was
        # sent but no marker was retained, so attempt 2 re-sent it. The
        # cancellation may land between the last 5s drain tick and the
        # process exit, so do ONE guarded final drain, then best-effort
        # persist a delivery_done marker when the sentinel is a full-line
        # match in the drained tail.
        if _drain_fn is not None and sandbox is not None:
            try:
                await asyncio.wait_for(asyncio.shield(_drain_fn()), timeout=_IDEMPOTENCY_GATE_CANCEL_PERSIST_TIMEOUT)
            # Nested cancellation here is deliberate — idempotency-gate cleanup
            # must not re-raise early or the delivery-done marker is skipped
            # (FAR-228 fix); the original cancellation re-raises at line 4566.
            except asyncio.CancelledError:  # NOSONAR
                _uncancel_current_task()
            except Exception:
                _log.exception(
                    "sandbox_agent.cancel_retention_drain_failed",
                    extra={"node_id": node_id, "run_id": run_id},
                )
            _drained_tail = "".join(_drained_chunks)
            if delivery_sentinel and _source_contains_delivery_sentinel(_drained_tail, delivery_sentinel):
                _cancel_marker: dict[str, Any] = {
                    "_modulo_marker": True,
                    "status": "failed",
                    "summary": (
                        "Sandbox agent cancelled after delivery sentinel observed — "
                        "delivery_done retained (idempotency gate)"
                    ),
                    "raw_output": _redact_raw_output(_drained_tail)[:_MAX_ARTIFACT_LOG],
                    "parse_error": "",
                    "pr_url": _extract_pr_url(_drained_tail),
                    "exit_code": -1,
                    "stdout_length": len(_drained_tail),
                    "stderr_length": 0,
                    "attempt_key": attempt_key,
                    "node_id": node_id,
                    "delivery_done": True,
                }
                try:
                    await _sandbox_cancel_retention_persist(
                        session_factory=session_factory,
                        run_id=run_id,
                        org_id=org_id,
                        node_id=node_id,
                        attempt_key=attempt_key,
                        marker=_cancel_marker,
                    )
                # Nested cancellation here is deliberate — idempotency-gate cleanup
                # must not re-raise early or the delivery-done marker is skipped
                # (FAR-228 fix); the original cancellation re-raises at line 4566.
                except asyncio.CancelledError:  # NOSONAR
                    _uncancel_current_task()
                except Exception:
                    _log.exception(
                        "sandbox_agent.cancel_retention_persist_failed",
                        extra={"node_id": node_id, "run_id": run_id},
                    )
        raise
    except (SupersededNodeError, SandboxNodeFailedError, ScriptModeError):
        # A6: the retryable/superseded node-failure classes propagate to the
        # executor — they must NOT be swallowed into a failed-node output
        # dict (a superseded dispatch must never look like a completed/failed
        # node, and a retryable infra failure must reach the retry path).
        # FAR-296 Phase 2: the TERMINAL ScriptModeError subclasses (post-claim
        # script-mode faults) also propagate so the executor maps them to the
        # never-retryable ``script.*`` codes — never to a silent failed-node
        # output dict that would let the run land 'complete'.
        raise
    except Exception as _exc:
        elapsed = time.monotonic() - start_time
        _exc_type = type(_exc).__name__
        # FAR-511: surface the provider error in the run output. The generic
        # exception envelope previously excluded error_type/error_message, so a
        # sandbox-provisioning failure (e.g. e2b ``400: Timeout cannot be greater
        # than 1 hours``) was only visible in Fly logs as a bare
        # "Sandbox agent execution failed". For an e2b SandboxException also pull
        # the provider response body so the 400 detail is visible via get_run_output.
        _exc_msg = _format_sandbox_provider_error(_exc, SandboxException)
        _log.exception(
            "sandbox_agent.execution_failed",
            extra={
                "node_id": node_id,
                "elapsed_ms": int(elapsed * 1000),
                "exc_type": _exc_type,
                "exc_msg": _exc_msg,
            },
        )
        _span = _otel_trace.get_current_span()
        if _span.is_recording():
            _span.add_event(
                "sandbox.agent.output",
                {
                    "stdout": agent_stdout[:_MAX_OTEL_LOG_ATTR],
                    "stderr": agent_stderr[:_MAX_OTEL_LOG_ATTR],
                    "stdout_length": _stdout_len,
                    "stderr_length": _stderr_len,
                    "attempt_key": attempt_key or "",
                },
            )
        _exc_stdout = agent_stdout
        _exc_stderr = agent_stderr
        _exc_output_json = output_json
        # Best-effort sandbox trace on the generic-exception path too — the
        # sandbox may already be dead, in which case the helper returns "".
        _exc_log_tail = await _fetch_sandbox_log_tail(_sandbox_id)
        _cost_estimate_usd = _compute_sandbox_cost(elapsed, _exc_output_json)
        return _build_sandbox_node_envelope(
            node_id=node_id,
            output=_SandboxNodeOutput(
                status="failed",
                summary=SANDBOX_AGENT_FAILED_SUMMARY,
                exit_code=-1,
                wall_clock_time_ms=int(elapsed * 1000),
                cost_estimate_usd=_cost_estimate_usd,
                cost_source=_exc_output_json,
                agent_stdout=_exc_stdout,
                agent_stderr=_exc_stderr,
                stdout_length=_stdout_len,
                stderr_length=_stderr_len,
                attempt_key=attempt_key,
                error_type=_exc_type,
                error_message=_exc_msg,
                sandbox_id=_sandbox_id,
                sandbox_log_tail=_exc_log_tail,
                modulo_synthetic_failure=True,
            ),
            # FAR-511: stop hiding error_type/error_message. The node-failure
            # envelope now carries the provider error (e.g. the e2b 400 detail)
            # instead of masking it as a bare "Sandbox agent execution failed".
            exclude_from_output=frozenset(),
        )
    finally:
        # FAR-211: stop the loop-interception callback server. Best-effort
        # (shielded + bounded) — a teardown failure must not mask the
        # sandbox kill or the marker clear below.
        if _bridge_server is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(_bridge_server.close()),
                    timeout=_OUTPUT_READ_TIMEOUT,
                )
            except Exception:
                _log.exception(
                    "sandbox_agent.loop_intercept_teardown_failed",
                    extra={"node_id": node_id, "run_id": run_id},
                )
        if sandbox is not None:
            try:
                # Shield the kill so a second CancelledError cannot abort the
                # sandbox teardown (dist/runtime-core A3).
                await asyncio.wait_for(
                    asyncio.shield(sandbox.kill(request_timeout=_OUTPUT_READ_TIMEOUT)),
                    timeout=_OUTPUT_READ_TIMEOUT,
                )
            except Exception:
                _log.exception(
                    "sandbox_agent.kill_failed",
                    extra={"node_id": node_id},
                )
        # Fenced dispatch-marker clear (3.11): runs in a finally REGARDLESS
        # of whether ``sandbox`` was assigned (covers the cancel-during-create
        # leak). Only clears when the claim token still matches — a
        # superseded original must not clear the successor's marker.
        if dispatch_marker_set:
            try:
                await _clear_dispatch_marker()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "sandbox_agent.dispatch_marker_clear_failed",
                    extra={"node_id": node_id, "run_id": run_id},
                )


async def _sandbox_cancel_retention_persist(
    *,
    session_factory: Callable[..., Any] | None,
    run_id: str,
    org_id: str,
    node_id: str,
    attempt_key: str | None,
    marker: dict[str, Any],
) -> None:
    """Best-effort persist of a cancellation-retention marker (FAR-228).

    Runs inside a caught ``asyncio.CancelledError``. Shield + uncancel: awaiting
    a DB write inside a caught CancelledError must not be re-cancelled or
    mis-attributed to the persist's own timeout bookkeeping. Bounded and
    fail-open — the caller's re-raise must not be delayed.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    # nosemgrep: create-task-without-guard — this helper is an `async def`, so a
    # running event loop always exists; the rule's own `async def` exclusion
    # applies. CI's --baseline-commit mode flags it only because this helper is
    # new on this branch.
    _persist_task = asyncio.create_task(  # nosemgrep: create-task-without-guard
        _persist_raw_output_marker(
            session_factory,
            run_id=run_id,
            org_id_raw=org_id,
            node_id=node_id,
            attempt_key=attempt_key,
            marker=marker,
        )
    )
    await asyncio.wait_for(
        asyncio.shield(_persist_task),
        timeout=_IDEMPOTENCY_GATE_CANCEL_PERSIST_TIMEOUT,
    )
    _uncancel_current_task()


def _coerce_stdout_percentage_delta(raw: Any) -> float | None:
    """Coerce a node's ``stdout_percentage_delta`` to a (0, 1] fraction or None."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.0 < value <= 1.0 else None


def _filter_watch_globs(raw: Any) -> list[str]:
    """Filter a node's ``watch_globs`` to non-empty string entries."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [g for g in raw if isinstance(g, str) and g]


def _build_sandbox_node_config(
    node_def: dict[str, Any],
    *,
    session_factory: Callable[..., Any] | None,
    single_sandbox_node: bool,
) -> _SandboxNodeConfig:
    """Read + validate a sandbox_agent node's config from ``node_def``.

    The single source for the ``node_def`` -> ``_SandboxNodeConfig`` mapping
    used by ``make_sandbox_agent_fn``. All the defensive reads (validated
    scopes, float coercion, glob-list filtering) live here so the factory stays
    a thin wrapper around the immutable config.
    """
    node_id: str = str(node_def["id"])
    sandbox_mode, agent_command, _sandbox_mode_config = _validate_sandbox_mode_config(node_def)
    agent_prompt_template: str = str(_sandbox_mode_config.get("agent_prompt") or "")
    template_id: str = node_def.get("template_id", "opencode")
    # FAR-296 Phase 3: egress control + resource-limit config surface. The
    # shared validators (sandbox_mode) have already rejected any unknown
    # egress_policy / resource_limits keys at save-time AND here at run-time.
    egress_policy: str | None = node_def.get("egress_policy")
    egress_allowlist: list[dict[str, Any]] | None = node_def.get("egress_allowlist")
    resource_limits: dict[str, Any] | None = node_def.get("resource_limits")
    # FAR-212 PR B: read-only-workspace + git-credential scope surface. The
    # shared validators (sandbox_mode) have already rejected any invalid
    # read_only / git_credentials keys at save-time (Pydantic, GraphValidator,
    # MCP) AND here at run-time; the values are read defensively so a smuggled
    # raw-import value can never trigger a policy step that does not match what
    # the capability derivation certified (read_only is only enforced when it is
    # the genuine boolean True; git_credentials only when it is a recognised
    # scope).
    read_only: bool = node_def.get("read_only") is True
    git_credentials: str | None = node_def.get("git_credentials")
    git_credentials = git_credentials if git_credentials in ("scoped", "unscoped", "none") else None
    # FAR-296 Phase 4a: wall-clock spend budget (seconds). When set, the sandbox
    # is killed once the wall-clock elapsed time exceeds this budget — a tighter
    # spend bound than the node timeout. Validated (positive int) at save-time
    # and here; a None value disables the wall-clock killer.
    wallclock_budget_seconds: int | None = node_def.get("wallclock_budget_seconds")
    # FAR-296 Phase 3: script mode that REQUIRES enforcement (egress denial,
    # resource limits, read-only workspace, or git-credential scope) requires a
    # REMOTE E2B provider (the E2B API key) because local providers have no
    # egress/resource/enforcement point — deny_all / selected (allowlist) /
    # resource_limits / read_only / git-credential scoping would silently no-op.
    # Fail closed ONLY when enforcement is actually requested; a plain script-mode
    # node with no enforcement config runs fine on any provider (there is nothing
    # to enforce). This keeps the refusal scoped to exactly the security concern
    # it guards.
    if _script_enforcement_requires_remote(
        sandbox_mode=sandbox_mode,
        egress_policy=egress_policy,
        resource_limits=resource_limits,
        read_only=read_only,
        git_credentials=git_credentials,
    ) and not (os.environ.get("MODULO_E2B_API_KEY") or os.environ.get("E2B_API_KEY")):
        raise ValueError(
            f"sandbox_agent node '{node_id}' mode='script' requests egress/resource/"
            "sandbox-policy enforcement (egress_policy='deny_all'/'selected', resource_limits, "
            "read_only, or git_credentials scoped/none) which requires a "
            "remote E2B provider (set MODULO_E2B_API_KEY or E2B_API_KEY) — local "
            "providers have no egress, resource-limit, or sandbox-policy enforcement point for script mode"
        )
    output_schema_json: dict[str, Any] | None = node_def.get("output_schema_json")
    sandbox_timeout: int = node_def.get("timeout_seconds", 1200)
    stall_timeout_override: Any = node_def.get("stall_timeout_seconds")
    context_files: dict[str, str] = node_def.get("context_files") or {}

    # FAR-306: opt-in stall detectors layered on the default heartbeat. Each
    # detector is a separate liveness channel; the idle watchdog fires only when
    # ALL *enabled* channels are silent for stall_timeout_seconds. Defaults keep
    # the heartbeat ON and every opt-in detector OFF — behaviour is unchanged
    # unless a user explicitly enables one.
    enable_heartbeat: bool = node_def.get("enable_heartbeat", True) is not False
    watch_log_path: str | None = node_def.get("watch_log_path")
    watch_log_path = watch_log_path if isinstance(watch_log_path, str) and watch_log_path else None
    stdout_percentage_delta: float | None = _coerce_stdout_percentage_delta(node_def.get("stdout_percentage_delta"))
    watch_globs: list[str] = _filter_watch_globs(node_def.get("watch_globs"))
    # FAR-228: the opt-in delivery sentinel (full-line marker in sandbox output
    # that proves the side effect — e.g. an email — was sent) and the
    # single-node guard (the gate is inert on multi-node graphs).
    delivery_sentinel: str | None = node_def.get("delivery_sentinel")
    delivery_sentinel = delivery_sentinel if isinstance(delivery_sentinel, str) and delivery_sentinel else None

    # FAR-211: agent-loop interior tool-call interception (ADR 003 amendment).
    # When the node carries a ``loop_intercept`` config, a Modulo-hosted bridge
    # runs INSIDE the sandbox alongside the agent: tool calls are reported to
    # the Modulo side BEFORE execution and tool results BEFORE they re-enter
    # the model context, evaluated against the SAME bound guardrails as the T1
    # ingestion edge. A malformed config is a programming error (graph
    # validation already rejects it at save-time) — fail the node construction,
    # never silently disable a declared control.
    from modulo.core.guardrails.loop_intercept import (
        LoopInterceptConfigError,
        parse_loop_intercept_config,
    )

    loop_intercept_config: LoopInterceptConfig | None = None
    loop_intercept_raw = node_def.get("loop_intercept")
    if loop_intercept_raw:
        try:
            loop_intercept_config = parse_loop_intercept_config(loop_intercept_raw)
        except LoopInterceptConfigError as exc:
            raise ValueError(f"sandbox_agent node '{node_id}' has malformed loop_intercept config: {exc}") from exc

    return _SandboxNodeConfig(
        node_id=node_id,
        node_def=node_def,
        sandbox_mode=sandbox_mode,
        agent_command=agent_command,
        agent_prompt_template=agent_prompt_template,
        template_id=template_id,
        egress_policy=egress_policy,
        egress_allowlist=egress_allowlist,
        resource_limits=resource_limits,
        read_only=read_only,
        git_credentials=git_credentials,
        wallclock_budget_seconds=wallclock_budget_seconds,
        output_schema_json=output_schema_json,
        sandbox_timeout=sandbox_timeout,
        stall_timeout_override=stall_timeout_override,
        context_files=context_files,
        enable_heartbeat=enable_heartbeat,
        watch_log_path=watch_log_path,
        stdout_percentage_delta=stdout_percentage_delta,
        watch_globs=watch_globs,
        delivery_sentinel=delivery_sentinel,
        loop_intercept_config=loop_intercept_config,
        session_factory=session_factory,
        single_sandbox_node=single_sandbox_node,
    )


def make_sandbox_agent_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
    session_factory: Callable[..., Any] | None = None,
    single_sandbox_node: bool = False,
) -> Any:
    """Return a decorated async node function that dispatches work to an external
    agent runtime in an E2B sandbox.

    The node_def must have:
      - mode: "llm" | "script" —  (default "llm"). llm mode dispatches an LLM
        agent; script mode runs a verbatim script.
      - agent_prompt: str —  REQUIRED in llm mode: Jinja2 template rendered
        against state (never required in script mode).
      - template_id: str —  E2B sandbox template ID (default "opencode")
      - agent_command: str —  REQUIRED in llm mode: command to run inside the
        sandbox (no default — a sandbox agent cannot run without an explicit
        command). Belongs to llm mode; mutually exclusive with script_command.
      - script_command: str —  REQUIRED in script mode: command to run VERBATIM
        inside the sandbox (no Jinja render). Mutually exclusive with
        agent_command / agent_commands.
      - output_schema_json: dict | None —  optional output schema validation
      - timeout_seconds: int —  max wall-clock time (default 1200)
      - stall_timeout_seconds: float —  max seconds of agent silence before the
        idle watchdog treats the command as stalled (default 300)
      - context_files: dict[str, str] —  optional files to write into the sandbox
        keyed by path
      - loop_intercept: dict | None —  optional agent-loop interior tool-call
        interception config (FAR-211 / ADR 003 amendment). When enabled AND the
        pipeline has bound guardrails, a Modulo-hosted bridge runs inside the
        sandbox: tool calls are evaluated against the SAME bound guardrails as
        the T1 ingestion edge before execution and before results re-enter the
        model context, under a per-call latency budget (fail-open with audit,
        never wedges the loop). Best-effort: a bridge setup failure disables it
        for that node but never blocks the dispatch.

    The node creates an E2B sandbox. In llm mode it writes the rendered prompt +
    context files and runs the external agent; in script mode it writes the full
    run input to /home/user/input.json (no 10KB truncation) and runs the script
    verbatim. Structured output is read from /home/user/output.json and the
    sandbox is torn down. Wall-clock time and exit code are captured natively —
    even on failure. Script mode does NOT run the LLM envelope field extraction
    (summary/changed_files/pr_url/status/outcome) — the raw parsed output is the
    node output.

    env_vars values may reference secrets with ``{{ secrets.KEY }}``. These are
    resolved at run time from the org vault (when a ``session_factory`` is
    provided), so secret rotation takes effect on the next run and secrets
    never enter the compiled graph. There is NO process-environment fallback
    (anti-exfiltration). An unresolved reference is OMITTED from the sandbox
    envs with a warning (FAR-480) — never resolved to an empty string, which
    would clobber the system-injected default (e.g. the host GITHUB_TOKEN) and
    silently break the sandbox credential.
    """
    config = _build_sandbox_node_config(
        node_def,
        session_factory=session_factory,
        single_sandbox_node=single_sandbox_node,
    )
    node_id = config.node_id
    sandbox_timeout = config.sandbox_timeout

    @cancellable_node(
        timeout=(timeout or sandbox_timeout) + _OUTPUT_READ_TIMEOUT + _DECORATOR_GRACE,
        role="sandbox_agent",
    )
    async def _sandbox_agent(state: dict[str, Any]) -> dict[str, Any]:
        return await _sandbox_agent_impl(state, config=config)

    _sandbox_agent.__name__ = f"sandbox_agent_{node_id}"
    return _sandbox_agent


def _validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Lightweight field-presence validation against a JSON schema.

    Raises :class:`OutputSchemaValidationError` on first missing required field
    (a ValueError subclass the executor maps to the domain-specific
    ``schema_validation_failure`` error code). Full JSON Schema validation (via
    a library like `jsonschema`) is deferred to v1.
    """
    required: list[str] = schema.get("required", [])
    for field in required:
        if field not in data:
            raise OutputSchemaValidationError(f"Manual output missing required field {field!r} (required: {required})")
