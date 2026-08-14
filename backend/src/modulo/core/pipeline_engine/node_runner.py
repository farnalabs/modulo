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
import hashlib
import json
import logging
import math
import os
import re as _re
import time
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any

import jinja2
import jmespath
from jinja2.sandbox import SandboxedEnvironment
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from modulo.core.cost_controller.breakdown.constants import (
    MAX_REPORTABLE_BAND_USD,
    MAX_REPORTABLE_USD_MIN,
)
from modulo.core.cost_controller.breakdown.metrics import record_out_of_band
from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult
from modulo.core.pipeline_engine.decorator import cancellable_node
from modulo.core.pipeline_engine.event_broker import RunEventBroker, get_registry
from modulo.core.pipeline_engine.input_truncation import truncate_input
from modulo.core.run_context.autonomy import (
    effective_autonomy_level,
    should_notify_on_complete,
    should_skip_hitl_gate,
)
from modulo.db.models.eval_result import EvalResult as EvalResultModel
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)


class SandboxNodeFailedError(Exception):
    """A sandbox-agent node failed due to sandbox infrastructure (retryable).

    Raised for a stall (idle watchdog), a command timeout, or a non-zero exit
    code with no parseable ``output.json``. The executor maps this to the
    retryable path (fenced reset to ``pending`` + SAQ retry) instead of a
    silent wrong-success completion.
    """


class SupersededNodeError(Exception):
    """The sandbox dispatch marker was denied (claim superseded / not running).

    Raised when the DB-atomic dispatch marker UPDATE matched zero rows — a
    successor rotated the run's claim token or the run is no longer running, so
    a sandbox MUST NOT be created. The executor maps this to a terminal
    ``superseded`` failure (a token-guarded no-op if a successor already owns
    the run) — never a completed run with zero work.
    """


_is_truthy = bool

# Cap for the stored artifact stdout/stderr blobs. 512KB keeps storage bounded
# while capturing realistic sessions (real runs stream 364KB+; the old 100KB
# cap made every long run look cut mid-JSON). Consumers can tell stored
# truncation from a genuine cut via the stdout_length/stderr_length fields.
_MAX_ARTIFACT_LOG = 512000
_MAX_OTEL_LOG_ATTR = 32768
_MAX_ERROR_MSG = 500

# FAR-197: bounds for the no-output.json diagnostic message. The raised
# SandboxNodeFailedError message surfaces through error_codes.sanitize_error_text
# (hard cap 5000 chars), is written to runs.error_detail (String(5000)), and is
# read back by the run-detail endpoints at limit=5000. The section caps therefore
# budget to a total that fits the 5000-char surface WITH room for the prefix,
# sandbox-id line, section headers, and truncation markers — nothing is silently
# cut. Sections are ordered high-value first (stderr tail, then the E2B log tail
# where the kill reason lives), stdout tail LAST so it is the first thing shaved
# if a pathological case ever overruns.
_NO_OUTPUT_STDERR_TAIL = 1500
_NO_OUTPUT_LOG_TAIL = 1500
_NO_OUTPUT_RAW_SNIPPET = 700
_NO_OUTPUT_STDOUT_TAIL = 700

_OUTPUT_READ_TIMEOUT = 30.0  # max seconds to wait for sandbox output after command times out
_DECORATOR_GRACE = 5.0  # scheduling + finally-block margin for decorator safety net
# FAR-188 (QA round 1): the raw-output retention DB write is bounded to fit
# inside the node decorator's grace budget (_DECORATOR_GRACE = 5.0s) so a hung
# DB fails open with a log BEFORE the safety-net timer can convert the
# retryable SandboxNodeFailedError into a terminal node_timeout.
_RAW_OUTPUT_MARKER_PERSIST_TIMEOUT = 5.0
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
# E2B_SANDBOX_USD_PER_HOUR. NOTE: this fallback only applies when settings
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
        preferred: list[str] = []
        rest: list[str] = []
        preferred_levels = {"info", "warn", "warning", "error"}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            msg = entry.get("message")
            if msg is None:
                msg = entry.get("fields")
            if not msg:
                continue
            text = str(msg)
            if isinstance(entry.get("level"), str) and entry["level"].lower() in preferred_levels:
                preferred.append(text)
            else:
                rest.append(text)
        combined = (preferred + rest)[-limit:]
        return "\n".join(combined)[-6000:]
    except Exception:
        return raw[:4000]


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


def _build_no_output_message(
    *,
    exit_code: int,
    stdout_raw: str,
    stderr_raw: str,
    sandbox_id: str | None,
    read_raw: str = "",
    log_tail: str = "",
) -> str:
    """Compose the FAR-197 diagnostic for an unparseable/missing output.json.

    A compact, bounded message: a prefix naming the exit code and sandbox id,
    then bounded tails of the captured agent stderr, the E2B log tail (the only
    place the kill reason lives), any raw bytes read back from output.json (the
    invalid-JSON case), and finally the agent stdout tail. Sections are ordered
    highest-value first — agent errors sit at the END of stderr and the kill
    reason lives at the END of the sandbox log tail, so BOTH lead and survive any
    downstream truncation; stdout tail is last. The section caps budget the
    whole message under the 5000-char sanitizer/column cap so nothing is cut.
    Downstream error-detail sanitization (``sanitize_error_text``) strips control
    chars and redacts secrets; this just keeps the WHY visible within the
    sanitizer's hard cap.
    """
    stdout_raw = str(stdout_raw)
    stderr_raw = str(stderr_raw)
    read_raw = str(read_raw)
    log_tail = str(log_tail)

    parts = [f"Sandbox agent produced no parseable output.json (exit code {exit_code})"]
    if isinstance(sandbox_id, str) and sandbox_id:
        parts.append(f"sandbox id: {sandbox_id}")
    stderr_tail = _bounded_tail(stderr_raw, _NO_OUTPUT_STDERR_TAIL)
    if stderr_tail:
        parts.append("--- stderr tail ---")
        parts.append(stderr_tail)
    log_tail_cap = _bounded_tail(log_tail, _NO_OUTPUT_LOG_TAIL)
    if log_tail_cap:
        parts.append("--- sandbox log tail ---")
        parts.append(log_tail_cap)
    read_snippet = _bounded_tail(read_raw, _NO_OUTPUT_RAW_SNIPPET)
    if read_snippet:
        parts.append(f"--- output.json read ({len(read_raw)} chars) ---")
        parts.append(read_snippet)
    stdout_tail = _bounded_tail(stdout_raw, _NO_OUTPUT_STDOUT_TAIL)
    if stdout_tail:
        parts.append("--- stdout tail ---")
        parts.append(stdout_tail)
    return "\n".join(parts)


_PR_URL_PATTERN = _re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+")

# Credential redaction for retained raw output (FAR-188 QA round 2): sandbox
# commands run with OPENCODE_API_KEY and GITHUB_TOKEN (a PAT) injected, and
# agent ``git push``/``git clone``/``gh`` output routinely embeds tokenized
# URLs like ``https://x-access-token:<PAT>@github.com/...``. The stored
# ``raw_output`` field is scrubbed BEFORE persistence so credentials never
# enter it unmasked. ``_TOKENIZED_GIT_URL_PATTERN`` strips the userinfo from
# any ``http(s)://...@host`` URL; ``_TOKEN_VALUE_PATTERN`` defensively masks
# bare token values that follow known credential labels.
_TOKENIZED_GIT_URL_PATTERN = _re.compile(r"(https?://)[^@\s/]+@")
_TOKEN_VALUE_PATTERN = _re.compile(r"(x-access-token:|gh[pous]_|github_pat_|Bearer\s+|token=)[^\s\"'<>]+")


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
) -> None:
    """Single builder + persist for a raw-output retention marker (FAR-188).

    Both failure branches (no-parseable output.json and stall/timeout) funnel
    through this ONE helper so the marker shape cannot drift between them.

    ``source`` is the FULL pre-truncation evidence: for the no-parseable branch
    it is the union of the file content AND the captured stdout; for the
    stall/timeout branch it is whatever raw source is available (the drained
    tail, which the drain window bounds to the last ``_MAX_DRAIN_WINDOW``
    (512KB) bytes — the retained ``raw_output`` therefore reflects that
    bounded tail, never a multi-MB log). Bytes are decoded exactly once;
    ``pr_url`` is extracted from the FULL UNREDACTED source, then the stored
    ``raw_output`` copy is scrubbed of credentials (``_redact_raw_output``)
    and ONLY THEN truncated to ``_MAX_ARTIFACT_LOG`` for storage.
    """
    text = _normalize_marker_text(source)
    marker: dict[str, Any] = {
        "_modulo_marker": True,
        "status": "failed",
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
    await _persist_raw_output_marker(
        session_factory,
        run_id=run_id,
        org_id_raw=org_id_raw,
        node_id=node_id,
        attempt_key=attempt_key,
        marker=marker,
    )


async def _persist_raw_output_marker(
    session_factory: Callable[..., Any] | None,
    *,
    run_id: str,
    org_id_raw: Any,
    node_id: str,
    attempt_key: str | None,
    marker: dict[str, Any],
) -> None:
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
    """
    if session_factory is None:
        _log.warning(
            "sandbox_agent.raw_output_marker_skip_no_session",
            extra={"run_id": run_id, "node_id": node_id},
        )
        return
    if not run_id:
        _log.warning("sandbox_agent.raw_output_marker_skip_no_run_id", extra={"node_id": node_id})
        return
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
        return

    async def _persist() -> None:
        # Imports live INSIDE the try so a first-call import failure is
        # swallowed+logged like any other persist failure — it must never
        # propagate and convert the retryable raise into a generic failed node.
        from sqlalchemy import select as _sql_select

        from modulo.db.models.run import Run as _RunModel

        try:
            async with session_factory() as session, session.begin():
                await set_rls_org(session, org_uuid)
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
                existing = markers.get(key)
                persisted_marker: dict[str, Any] = marker
                if isinstance(existing, dict) and existing.get("pr_url"):
                    persisted_marker = dict(marker)
                    persisted_marker["pr_url"] = existing["pr_url"]
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

    try:
        await asyncio.wait_for(_persist(), timeout=_RAW_OUTPUT_MARKER_PERSIST_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "sandbox_agent.raw_output_marker_persist_timeout_or_error",
            extra={"run_id": run_id, "node_id": node_id, "attempt_key": attempt_key},
        )


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


def make_node_fn(
    node_def: dict[str, Any],
    *,
    role: str | None = None,
    timeout: float | None = None,
    max_input_length: int | None = None,
    token_budget: int | None = None,
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
        run_context: dict[str, Any] = state.get("run_context") or {}
        raw_input = run_context.get("input", {})

        # Truncate input if max_input_length is configured for this agent.
        if max_input_length is not None and isinstance(raw_input, str):
            run_context["input"] = truncate_input(raw_input, max_input_length)

        # Get agent data from node_def (embedded at snapshot creation).
        prompt_template = node_def.get("prompt_template") or ""
        model_backend_id_str = node_def.get("model_backend_id")
        output_schema_json = node_def.get("output_schema_json")

        # If no model_backend_id, fall back to stub behavior
        # (connector_binding nodes, manual nodes routed through wrong path, etc.).
        if not model_backend_id_str:
            return {"artifacts": [{"node_id": node_id, "status": "executed"}]}

        env = SandboxedEnvironment()
        template = env.from_string(prompt_template)
        template_vars: dict[str, Any] = {
            "state": state,
            "run_context": run_context,
            "input": raw_input,
        }
        # Inject resolved parameters as {{ parameter.<key> }}.
        resolved = node_def.get("_resolved_parameters")
        if isinstance(resolved, dict):
            template_vars["parameter"] = resolved
        rendered_prompt = template.render(**template_vars)

        # Append routing prompt for LLM routing nodes.
        routing_mode: str | None = node_def.get("routing_mode")
        if routing_mode == "llm":
            routing_prompt: str = node_def.get("routing_prompt", "")
            if routing_prompt:
                rendered_prompt = rendered_prompt + "\n\n" + routing_prompt

        # Get ModelBackendHub from ContextVar.
        from modulo.core.pipeline_engine.decorator import get_model_backend_hub

        hub = get_model_backend_hub()
        if hub is None:
            raise RuntimeError(f"ModelBackendHub not available for node {node_id!r}")

        # Resolve backend ID and invoke the model.
        backend_id = uuid.UUID(model_backend_id_str)
        backend = await hub.get(backend_id)

        messages = [HumanMessage(content=rendered_prompt)]
        response = await backend.invoke(messages)

        content = response.content if hasattr(response, "content") else str(response)
        output_data: Any = content
        if isinstance(content, str):
            with suppress(json.JSONDecodeError, ValueError):
                output_data = json.loads(content)

        # Validate against output schema if defined.
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

    _node.__name__ = f"node_{node_id}"
    return _node


def make_hitl_gate_fn(
    hitl_gate_config: dict[str, Any],
    *,
    timeout: float | None = None,
    eval_definitions: Sequence[EvalDefinition] | None = None,
    session_factory: Callable[..., Any] | None = None,
    org_id: uuid.UUID | None = None,
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
    required_team_id: str | None = hitl_gate_config.get("required_team_id")

    async def _hitl_gate(state: dict[str, Any]) -> dict[str, Any]:
        # --- Resume check —  always first so condition/evals aren't re-evaluated. ---
        decision = state.get("_hitl_decision")
        if decision is not None:
            action = decision.get("action") if isinstance(decision, dict) else None
            if action == "deliver_manual":
                manual_output = decision.get("output", {})
                return {
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
                }
            is_rejected = action == "rejected"
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

        # --- Conditional gate ((Section 8.17) —  evaluate condition against state. ---
        if condition_expr:
            try:
                compiled = jmespath.compile(condition_expr)
            except jmespath.exceptions.JMESPathError:
                _log.exception("hitl_gate.invalid_condition", extra={"condition": condition_expr})
                raise ValueError(f"Invalid HITL gate condition expression: {condition_expr}") from None
            result = compiled.search(state)
            if not _is_truthy(result):
                # Condition falsy —  skip the gate entirely.
                return {
                    "artifacts": [
                        {
                            "node_id": gate_id,
                            "status": "condition_skipped",
                            "condition": condition_expr,
                            "condition_result": result,
                        }
                    ],
                }

        # --- Eval-before-interrupt ((Section 8.17) —  run node-scoped evals. ---
        eval_results_by_name: dict[str, EvalResult] = {}
        if eval_definitions:
            engine = EvalEngine()
            for eval_def in eval_definitions:
                eval_result = engine.evaluate(state, eval_def)
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

            # Persist eval results to the eval_results table so post-run
            # suite-level threshold checks can read them.
            if session_factory is not None and org_id is not None:
                try:
                    _run_id: uuid.UUID | None = state.get("_run_id")
                    if _run_id is not None:
                        async with session_factory() as session, session.begin():
                            await set_rls_org(session, org_id)
                            for eval_def in eval_definitions:
                                eval_result = eval_results_by_name[eval_def.name]
                                node_uuid: uuid.UUID | None = uuid.UUID(eval_def.node_id) if eval_def.node_id else None
                                db_result = EvalResultModel(
                                    organisation_id=org_id,
                                    run_id=_run_id,
                                    node_id=node_uuid,
                                    eval_id=eval_def.id,
                                    passed=eval_result.passed,
                                    score=eval_result.score,
                                    detail=eval_result.detail,
                                )
                                session.add(db_result)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("hitl_gate.persist_eval_failed")

        # --- Eval-reference condition check ((Section 8.17 v1) —  evaluate condition
        # against captured eval results. ---
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
                    return {
                        "artifacts": [
                            {
                                "node_id": gate_id,
                                "status": "condition_skipped",
                                "condition": eval_condition_raw,
                                "condition_result": False,
                            }
                        ],
                    }

        # Determine effective autonomy level from run_context.
        run_context: dict[str, Any] = state.get("run_context") or {}
        pipeline_default: str | None = run_context.get("_pipeline_default_autonomy")
        autonomy = effective_autonomy_level(pipeline_default, run_context)
        human_only_effective: bool = human_only

        # human_only overrides everything — always interrupt.
        if not human_only_effective and should_skip_hitl_gate(autonomy):
            # fully_autonomous: silently skip the gate.
            return {
                "artifacts": [
                    {
                        "node_id": gate_id,
                        "status": "skipped",
                        "autonomy": autonomy.value,
                    }
                ],
            }
        if not human_only_effective and should_notify_on_complete(autonomy):
            # notify_on_complete: auto-approve, record notification artifact.
            return {
                "artifacts": [
                    {
                        "node_id": gate_id,
                        "status": "auto_approved",
                        "autonomy": autonomy.value,
                    }
                ],
            }

        # First invocation —  store config and interrupt.
        hitl_gates: list[dict[str, Any]] = list(state.get("_hitl_gates") or [])
        hitl_gates.append(hitl_gate_config)
        state["_hitl_gates"] = hitl_gates

        # State mutations before the interrupt are persisted by the checkpointer.
        decision = interrupt(
            {
                "gate_id": gate_id,
                "autonomy_level": autonomy.value,
                "human_only": human_only_effective,
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
    timeout: float | None = None,
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


def make_connector_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Return a decorated async node function that resolves a connector
    from the ConnectorHub and executes a connector action (query/write).

    The node_def must have a 'connector_binding' dict with:
      - instance_id: uuid of the ConnectorInstance
      - type: connector type (e.g. 'shell')
      - operation: 'query' or 'write' (optional, default 'query')
      - input: dict of input parameters (optional)
    """
    node_id: str = str(node_def["id"])
    binding = node_def.get("connector_binding") or {}
    op: str = binding.get("operation", "query")

    @cancellable_node(timeout=timeout)
    async def _connector_node(state: dict[str, Any]) -> dict[str, Any]:
        from modulo.connectors.base import ConnectorPayload, ConnectorQuery
        from modulo.core.pipeline_engine.decorator import get_connector_hub

        hub = get_connector_hub()
        if hub is None:
            return {"artifacts": [{"node_id": node_id, "status": "executed", "output": {"note": "no connector hub"}}]}

        instance_id_str = binding.get("instance_id")
        if not instance_id_str:
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": "no connector instance_id"}]}

        import uuid as _uuid

        try:
            connector = hub.get(_uuid.UUID(str(instance_id_str)))
        except Exception as _conn_exc:
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": f"connector error: {_conn_exc}"}]}

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

        try:
            if op == "write":
                payload = ConnectorPayload(resource=resource, data=data)
                result = await connector.write(payload)
            else:
                query = ConnectorQuery(resource=resource, filters=filters)
                result = await connector.query(query)
        except Exception as exc:
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": str(exc)}]}

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
    are resolved via *resolver*; a missing secret resolves to ``""`` and logs a
    warning (legacy behaviour), never raising.
    """
    resolved: dict[str, str] = {}
    for key, value in env_vars.items():
        m = _secret_ref_re.fullmatch(str(value))
        if m:
            secret_key = m.group(1)
            resolved_value = await resolver(secret_key)
            if resolved_value is None:
                _log.warning("env_var.secret_ref_not_found", extra={"key": key, "secret_key": secret_key})
                resolved[key] = ""
            else:
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


def make_sandbox_agent_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
    session_factory: Callable[..., Any] | None = None,
) -> Any:
    """Return a decorated async node function that dispatches work to an external
    agent runtime in an E2B sandbox.

    The node_def must have:
      - agent_prompt: str —  Jinja2 template rendered against state
      - template_id: str —  E2B sandbox template ID (default "base")
      - agent_command: str —  REQUIRED command to run inside the sandbox
        (no default — a sandbox agent cannot run without an explicit command)
      - output_schema_json: dict | None —  optional output schema validation
      - timeout_seconds: int —  max wall-clock time (default 600)
      - stall_timeout_seconds: float —  max seconds of agent silence before the
        idle watchdog treats the command as stalled (default 300)
      - context_files: dict[str, str] —  optional files to write into the sandbox
        keyed by path

    The node creates an E2B sandbox, writes the rendered prompt + context files,
    runs the external agent, reads structured output from /home/user/output.json,
    and tears down the sandbox. Wall-clock time and exit code are captured
    natively —  even on failure.

    env_vars values may reference secrets with ``{{ secrets.KEY }}``. These are
    resolved at run time from the org vault (when a ``session_factory`` is
    provided) and fall back to the process environment, so secret rotation
    takes effect on the next run and secrets never enter the compiled graph.
    """
    node_id: str = str(node_def["id"])
    agent_prompt = node_def.get("agent_prompt")
    if not agent_prompt or not str(agent_prompt).strip():
        raise ValueError(
            f"sandbox_agent node '{node_def.get('id')}' is missing required 'agent_prompt' "
            "— an empty prompt would dispatch the agent with no instructions"
        )
    agent_prompt_template: str = str(agent_prompt)
    template_id: str = node_def.get("template_id", "opencode")
    commands_concatenation_string: str = node_def.get("commands_concatenation_string", " && ")
    agent_commands_raw: list[str] | None = node_def.get("agent_commands")
    agent_command_raw: str | None = node_def.get("agent_command")
    if agent_commands_raw:
        agent_command = commands_concatenation_string.join(agent_commands_raw)
    elif agent_command_raw:
        agent_command = agent_command_raw
    else:
        raise ValueError(
            f"sandbox_agent node '{node_def.get('id')}' is missing required 'agent_command' "
            "(or 'agent_commands') — a sandbox agent cannot run without an explicit command"
        )
    output_schema_json: dict[str, Any] | None = node_def.get("output_schema_json")
    sandbox_timeout: int = node_def.get("timeout_seconds", 1200)
    stall_timeout_override: Any = node_def.get("stall_timeout_seconds")
    context_files: dict[str, str] = node_def.get("context_files") or {}

    from e2b import AsyncSandbox  # type: ignore[import-untyped]
    from opentelemetry import trace as _otel_trace

    @cancellable_node(
        timeout=(timeout or sandbox_timeout) + _OUTPUT_READ_TIMEOUT + _DECORATOR_GRACE,
        role="sandbox_agent",
    )
    async def _sandbox_agent(state: dict[str, Any]) -> dict[str, Any]:

        run_context: dict[str, Any] = state.get("run_context") or {}
        raw_input: Any = run_context.get("input", {})

        env = SandboxedEnvironment()
        template = env.from_string(agent_prompt_template)
        template_vars: dict[str, Any] = {
            "state": state,
            "run_context": run_context,
            "input": raw_input,
        }
        resolved = node_def.get("_resolved_parameters")
        if isinstance(resolved, dict):
            template_vars["parameter"] = resolved

        run_id: str = str(state.get("_run_id", ""))
        pipeline_id: str = str(state.get("_pipeline_id", ""))
        org_id: str = str(state.get("_org_id", ""))

        async def _resolve_secret_ref(secret_key: str) -> str | None:
            """Resolve a ``{{ secrets.KEY }}`` reference to a string value.

            The org vault (per-org encrypted secrets table) is consulted first
            so pipelines resolve against the tenant's stored secrets and honour
            rotation on every run. Falls back to the process environment when
            the key is not in the vault.
            """
            if session_factory is not None:
                org_uuid: uuid.UUID | None = None
                org_id_raw = state.get("_org_id")
                if org_id_raw:
                    try:
                        org_uuid = uuid.UUID(str(org_id_raw))
                    except (TypeError, ValueError):
                        org_uuid = None
                if org_uuid is not None:
                    from modulo.core.secrets_backend import create_secrets_backend
                    from modulo.db.rls import set_rls_org
                    from modulo.settings import get_settings

                    try:
                        async with session_factory() as session, session.begin():
                            await set_rls_org(session, org_uuid)
                            backend = create_secrets_backend(fernet_key=get_settings().fernet_key, session=session)
                            return await backend.get_secret(secret_key)
                    except KeyError:
                        pass  # not in vault -> fall back
                    except Exception:
                        _log.exception("env_var.secret_resolve_error", extra={"secret_key": secret_key})
            # Fall back to process environment.
            return os.environ.get(secret_key)

        try:
            rendered_prompt = template.render(**template_vars)
        except jinja2.UndefinedError as e:
            _log.warning("Prompt template UndefinedError for run %s: %s", run_id, e)
            return {
                "status": "skipped",
                "summary": f"Skipped: prompt template references missing input fields ({e})",
                "agent_stdout": "",
                "agent_stderr": "",
                "exit_code": 0,
            }

        # Render agent_command through the same SandboxedEnvironment + template
        # vars as the prompt, so the LLM model (or any other command flag) can
        # be a per-run / per-parameter value (e.g. ``--model {{ input.model }}``
        # for A/B testing). A command with NO ``{{ }}`` templates renders to
        # itself unchanged — backward compatible with every existing pipeline.
        try:
            rendered_agent_command = env.from_string(agent_command).render(**template_vars)
        except jinja2.UndefinedError as e:
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
            if session_factory is None or not claim_lease:
                # Fail-open: no DB fence — derive a per-claim attempt key from the
                # (rotating) claim token so node output still distinguishes attempts.
                return f"run:{run_id}:node:{node_id}:{_claim_token_attempt_suffix(claim_lease)}"
            org_id_raw = state.get("_org_id")
            try:
                org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
            except (TypeError, ValueError):
                org_uuid = None
            if org_uuid is None:
                return f"run:{run_id}:node:{node_id}:{_claim_token_attempt_suffix(claim_lease)}"
            from sqlalchemy import text as _sql_text

            from modulo.db.rls import set_rls_org

            async with session_factory() as session, session.begin():
                await set_rls_org(session, org_uuid)
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

        async def _store_dispatch_marker_sandbox(sandbox_id_value: str | None) -> None:
            """Persist the real sandbox id onto the runs row after a successful create."""
            if session_factory is None or not claim_lease:
                return
            org_id_raw = state.get("_org_id")
            try:
                org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
            except (TypeError, ValueError):
                org_uuid = None
            if org_uuid is None:
                return
            from sqlalchemy import text as _sql_text

            from modulo.db.rls import set_rls_org

            async with session_factory() as session, session.begin():
                await set_rls_org(session, org_uuid)
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

        async def _clear_dispatch_marker() -> None:
            """Fenced marker clear — only when the claim token still matches.

            A superseded original (token rotated by a successor) must not clear
            the successor's dispatch marker / sandbox id.
            """
            if session_factory is None or not claim_lease:
                return
            org_id_raw = state.get("_org_id")
            try:
                org_uuid = uuid.UUID(str(org_id_raw)) if org_id_raw else None
            except (TypeError, ValueError):
                org_uuid = None
            if org_uuid is None:
                return
            from sqlalchemy import text as _sql_text

            from modulo.db.rls import set_rls_org

            async with session_factory() as session, session.begin():
                await set_rls_org(session, org_uuid)
                await session.execute(
                    _sql_text(
                        "UPDATE runs SET sandbox_dispatch_state=NULL, sandbox_id=NULL "
                        "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok"
                    ),
                    {"rid": run_id, "oid": str(org_uuid), "tok": claim_lease},
                )

        try:
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
                raise SupersededNodeError(
                    "E2B dispatch marker denied — run superseded or not running; sandbox not created"
                )
            dispatch_marker_set = True

            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(template=template_id, timeout=sandbox_timeout),
                timeout=min(sandbox_timeout, 120),
            )
            assert sandbox is not None, "Sandbox was not created before use"
            _sandbox_id = getattr(sandbox, "sandbox_id", None) or None
            # Persist the real sandbox id so the heartbeat-lost path
            # (run_executor_with_watchdog) can kill the sandbox by id.
            await _store_dispatch_marker_sandbox(_sandbox_id)
            for path, content in context_files.items():
                if path.endswith(".b64"):
                    content = base64.b64decode(content).decode()
                    path = path[:-4]
                await asyncio.wait_for(sandbox.files.write(path, content), timeout=_SANDBOX_IO_TIMEOUT)

            await asyncio.wait_for(
                sandbox.files.write("/home/user/prompt.md", rendered_prompt),
                timeout=_SANDBOX_IO_TIMEOUT,
            )

            _input_json = json.dumps(raw_input)
            if len(_input_json) > 10240:
                _input_json = json.dumps(
                    {"_truncated": True, "_key_count": len(raw_input) if isinstance(raw_input, dict) else 0}
                )

            env_vars_extra: dict[str, str] = await resolve_env_var_refs(
                node_def.get("env_vars") or {},
                _resolve_secret_ref,
            )

            try:
                # Track the last time the agent emitted output so the idle
                # watchdog can fail fast on stalls (FAR-97). The callbacks run
                # from the SDK's event task and may be async or sync.
                _activity: dict[str, Any] = {"last": time.monotonic()}
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
                _stream_enabled = isinstance(_stream_broker, RunEventBroker)

                def _stream_chunk(chunk: str, stream: str) -> None:
                    broker = _stream_broker
                    if not _stream_enabled or not isinstance(broker, RunEventBroker):
                        return
                    now = time.monotonic()
                    buf_key = f"{stream}_buf"
                    buf = _activity.setdefault(buf_key, [])
                    if chunk:
                        buf.append(chunk)
                    if not buf:
                        return
                    if now - _activity.get("last_stream_ts", 0.0) < _STREAM_FLUSH_INTERVAL:
                        return
                    payload: dict[str, Any] = {
                        "node_id": node_id,
                        "chunk": "".join(buf),
                        "ts": int(now * 1000),
                    }
                    buf.clear()
                    _activity["last_stream_ts"] = now
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
                            extra={"node_id": node_id, "run_id": run_id},
                        )

                async def _on_stdout(chunk: str) -> None:
                    _activity["last"] = time.monotonic()
                    _stream_chunk(chunk, "stdout")

                async def _on_stderr(chunk: str) -> None:
                    _activity["last"] = time.monotonic()
                    _stream_chunk(chunk, "stderr")

                # FAR-97 pipe-buffer fix: the agent command's stdout/stderr are
                # redirected to a log file inside the sandbox, so the process can
                # never block on a full stdout pipe. The drain probe below runs on
                # every watchdog tick: (1) it refreshes the idle watchdog's liveness
                # signal from the sandbox connection (a successful get_info proves
                # the sandbox is responsive even when the agent emits no output for
                # a long LLM turn), (2) it streams newly-appended content to the run
                # event broker (live output), and (3) it accumulates the content for
                # the node artifact.
                _drained_chunks: list[str] = []
                _drain_offset = 0
                _drained_len = 0

                async def _drain_sandbox_log() -> None:
                    nonlocal _drain_offset, _drained_len
                    # Probe failed (log file not created yet, sandbox connection
                    # unresponsive). Do NOT refresh liveness — the idle watchdog
                    # treats a prolonged probe failure as a genuine stall.
                    try:
                        info = await asyncio.wait_for(
                            sandbox.files.get_info(_SANDBOX_LOG_PATH),
                            timeout=_SANDBOX_TAIL_READ_TIMEOUT,
                        )
                        _activity["last"] = time.monotonic()
                        size = int(getattr(info, "size", 0) or 0)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.info(
                            "sandbox_agent.log_drain_probe_failed",
                            extra={"node_id": node_id},
                        )
                        return
                    if size <= _drain_offset:
                        return
                    try:
                        content = await asyncio.wait_for(
                            sandbox.files.read(_SANDBOX_LOG_PATH, format="text"),
                            timeout=_SANDBOX_TAIL_READ_TIMEOUT,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.exception(
                            "sandbox_agent.log_drain_failed",
                            extra={"node_id": node_id},
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
                    emit_start = max(_drain_offset, window_start)
                    new = text[emit_start - window_start :] if emit_start < full_len else ""
                    if new:
                        _drained_chunks.append(new)
                        _drained_len += len(new)
                        _stream_chunk(new, "stdout")
                        _activity["last"] = time.monotonic()
                        # Bound retained memory to the drain window: drop the
                        # oldest chunks once the accumulated log exceeds it.
                        while _drained_len > _MAX_DRAIN_WINDOW and len(_drained_chunks) > 1:
                            dropped = _drained_chunks.pop(0)
                            _drained_len -= len(dropped)
                        if _drained_len > _MAX_DRAIN_WINDOW and _drained_chunks:
                            _drained_chunks[0] = _drained_chunks[0][-_MAX_DRAIN_WINDOW:]
                            _drained_len = len(_drained_chunks[0])
                    # Self-correcting drain offset: ``full_len`` (what this tick's
                    # read returned) can exceed the STALE probe ``size`` when the
                    # agent appends between probe and read. Advancing to the max
                    # keeps the no-double-emit invariant — the next tick starts
                    # where THIS tick's emitted bytes actually ended, not where
                    # the probe saw the file.
                    _drain_offset = max(_drain_offset, full_len)

                # Redirect the agent's stdout/stderr into a sandbox log file so
                # the process writes to a regular file — never a pipe that can
                # fill and block a long session (FAR-97). The subshell preserves
                # the command's exit code for the SDK's wait().
                wrapped_command = f"( {rendered_agent_command} ) > {_SANDBOX_LOG_PATH} 2>&1"
                # System env vars first — provide defaults from the host. DO NOT
                # move pipeline env before these: pipelines need to override
                # GITHUB_TOKEN for identity separation (e.g. PR Reviewer uses
                # modulo-reviewbot PAT, not the system default farnalabs bot).
                # The reserved-prefix validator already prevents overriding
                # MODULO_* vars, so update() below is the sanctioned override.
                sandbox_envs: dict[str, str] = {
                    "MODULO_RUN_ID": run_id,
                    "MODULO_PIPELINE_ID": pipeline_id,
                    "MODULO_ORG_ID": org_id,
                    "MODULO_INPUT_PAYLOAD": _input_json,
                    "APP_MODULO_OPENCODE_API_KEY": os.environ.get("APP_MODULO_OPENCODE_API_KEY", ""),
                    "GITHUB_TOKEN": os.environ.get("GITHUB_DOGFOOD_PAT_ALL", "")
                    or os.environ.get("GITHUB_DOGFOOD_PAT_WR", "")
                    or os.environ.get("GITHUB_TOKEN", ""),
                }
                sandbox_envs.update(env_vars_extra)
                cmd_handle = await asyncio.wait_for(
                    sandbox.commands.run(
                        wrapped_command,
                        background=True,
                        on_stdout=_on_stdout,
                        on_stderr=_on_stderr,
                        timeout=sandbox_timeout,
                        envs=sandbox_envs,
                    ),
                    timeout=min(sandbox_timeout, 120),
                )
                cmd_result, stall_reason = await _wait_command_with_idle_watchdog(
                    cmd_handle,
                    total_timeout=sandbox_timeout,
                    idle_timeout=stall_timeout,
                    last_activity=lambda: _activity["last"],
                    on_tick=_drain_sandbox_log,
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
            await _drain_sandbox_log()

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
            agent_stdout = agent_stdout_raw[:_MAX_ARTIFACT_LOG]
            agent_stderr = agent_stderr_raw[:_MAX_ARTIFACT_LOG]

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
                )
                raise SandboxNodeFailedError(
                    command_error or f"Sandbox agent command failed (no output within {sandbox_timeout}s)"
                )

            raw_output: str = ""
            output_json: Any = None
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
                    )
                    # FAR-197: surface WHY the agent failed. The captured
                    # stdout/stderr tails plus the E2B log tail (the only place
                    # the kill reason lives) are fetched BEFORE the finally-block
                    # kill, while the sandbox is still alive — the logs endpoint
                    # only serves live sandboxes.
                    _no_output_log_tail = await _fetch_sandbox_log_tail(_sandbox_id)
                    raise SandboxNodeFailedError(
                        _build_no_output_message(
                            exit_code=exit_code,
                            stdout_raw=agent_stdout_raw,
                            stderr_raw=agent_stderr_raw,
                            sandbox_id=_sandbox_id,
                            read_raw=raw_output,
                            log_tail=_no_output_log_tail,
                        )
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
                except ValueError:
                    _log.exception(
                        "sandbox_agent.schema_validation_failed",
                        extra={"node_id": node_id},
                    )
                    elapsed = time.monotonic() - start_time
                    _cost_estimate_usd = _compute_sandbox_cost(elapsed, output_json)
                    return {
                        "artifacts": [
                            {
                                "node_id": node_id,
                                "status": "failed",
                                "output": {
                                    "status": "failed",
                                    "summary": "Output failed schema validation",
                                    "exit_code": exit_code,
                                    "wall_clock_time_ms": int(elapsed * 1000),
                                    "cost_estimate_usd": _cost_estimate_usd,
                                    **_build_model_cost_fields(output_json),
                                    "output_json": output_json,
                                    "agent_stdout": agent_stdout,
                                    "agent_stderr": agent_stderr,
                                    "stdout_length": _stdout_len,
                                    "stderr_length": _stderr_len,
                                    "attempt_key": attempt_key,
                                },
                            }
                        ],
                        "output": {
                            "status": "failed",
                            "summary": "Output failed schema validation",
                            "wall_clock_time_ms": int(elapsed * 1000),
                            "cost_estimate_usd": _cost_estimate_usd,
                            **_build_model_cost_fields(output_json),
                            "agent_stdout": agent_stdout,
                            "agent_stderr": agent_stderr,
                            "stdout_length": _stdout_len,
                            "stderr_length": _stderr_len,
                            "attempt_key": attempt_key,
                        },
                    }

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

            if isinstance(output_json, dict):
                result_summary = output_json.get("summary", "")
                changed_files = output_json.get("changed_files", [])
                pr_url = output_json.get("pr_url", "")
                _raw_status = output_json.get("status")
                _raw_outcome = output_json.get("outcome")
                if isinstance(_raw_status, str):
                    agent_status = _raw_status
                if isinstance(_raw_outcome, str):
                    agent_outcome = _raw_outcome

            if status == "failed" and not result_summary:
                # Never report a silent empty-summary failure — explain WHY the
                # command produced no usable output.
                result_summary = command_error or "Sandbox agent command failed"

            _cost_estimate_usd = _compute_sandbox_cost(elapsed, output_json)
            _stall_reason_field: dict[str, str] = {"stall_reason": stall_reason} if stall_reason else {}

            # Only the timeout/stall failure carries the sandbox trace — the
            # success path doesn't need it and stays small.
            _sandbox_failure_fields: dict[str, Any] = {}
            if cmd_result is None:
                _sandbox_failure_fields = {
                    "sandbox_id": _sandbox_id,
                    "sandbox_log_tail": _sandbox_log_tail,
                }

            return {
                "artifacts": [
                    {
                        "node_id": node_id,
                        "status": status,
                        "output": {
                            "status": status,
                            "summary": result_summary,
                            "changed_files": changed_files,
                            "pr_url": pr_url,
                            "exit_code": exit_code,
                            "wall_clock_time_ms": int(elapsed * 1000),
                            "cost_estimate_usd": _cost_estimate_usd,
                            **_build_model_cost_fields(output_json),
                            "output_json": output_json,
                            "agent_stdout": agent_stdout,
                            "agent_stderr": agent_stderr,
                            "stdout_length": _stdout_len,
                            "stderr_length": _stderr_len,
                            "agent_status": agent_status,
                            "agent_outcome": agent_outcome,
                            **_stall_reason_field,
                            **_sandbox_failure_fields,
                            "attempt_key": attempt_key,
                        },
                    }
                ],
                "output": {
                    "status": status,
                    "summary": result_summary,
                    "wall_clock_time_ms": int(elapsed * 1000),
                    "cost_estimate_usd": _cost_estimate_usd,
                    **_build_model_cost_fields(output_json),
                    "agent_stdout": agent_stdout,
                    "agent_stderr": agent_stderr,
                    "stdout_length": _stdout_len,
                    "stderr_length": _stderr_len,
                    "agent_status": agent_status,
                    "agent_outcome": agent_outcome,
                    **_stall_reason_field,
                    **_sandbox_failure_fields,
                    "attempt_key": attempt_key,
                },
            }

        except asyncio.CancelledError:
            raise
        except (SupersededNodeError, SandboxNodeFailedError):
            # A6: the retryable/superseded node-failure classes propagate to the
            # executor — they must NOT be swallowed into a failed-node output
            # dict (a superseded dispatch must never look like a completed/failed
            # node, and a retryable infra failure must reach the retry path).
            raise
        except Exception as _exc:
            elapsed = time.monotonic() - start_time
            _exc_type = type(_exc).__name__
            _exc_msg = str(_exc)[:_MAX_ERROR_MSG]
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
                        "stdout": locals().get("agent_stdout", "")[:_MAX_OTEL_LOG_ATTR],
                        "stderr": locals().get("agent_stderr", "")[:_MAX_OTEL_LOG_ATTR],
                        "stdout_length": _stdout_len,
                        "stderr_length": _stderr_len,
                        "attempt_key": locals().get("attempt_key") or "",
                    },
                )
            _exc_stdout = locals().get("agent_stdout", "")
            _exc_stderr = locals().get("agent_stderr", "")
            _exc_output_json = locals().get("output_json")
            # Best-effort sandbox trace on the generic-exception path too — the
            # sandbox may already be dead, in which case the helper returns "".
            _exc_log_tail = await _fetch_sandbox_log_tail(_sandbox_id)
            _cost_estimate_usd = _compute_sandbox_cost(elapsed, _exc_output_json)
            return {
                "artifacts": [
                    {
                        "node_id": node_id,
                        "status": "failed",
                        "output": {
                            "status": "failed",
                            "summary": "Sandbox agent execution failed",
                            "error_type": _exc_type,
                            "error_message": _exc_msg,
                            "exit_code": -1,
                            "wall_clock_time_ms": int(elapsed * 1000),
                            "cost_estimate_usd": _cost_estimate_usd,
                            **_build_model_cost_fields(_exc_output_json),
                            "agent_stdout": _exc_stdout,
                            "agent_stderr": _exc_stderr,
                            "stdout_length": _stdout_len,
                            "stderr_length": _stderr_len,
                            "sandbox_id": _sandbox_id,
                            "sandbox_log_tail": _exc_log_tail,
                            "attempt_key": locals().get("attempt_key"),
                        },
                    }
                ],
                "output": {
                    "status": "failed",
                    "summary": "Sandbox agent execution failed",
                    "wall_clock_time_ms": int(elapsed * 1000),
                    "cost_estimate_usd": _cost_estimate_usd,
                    **_build_model_cost_fields(_exc_output_json),
                    "agent_stdout": _exc_stdout,
                    "agent_stderr": _exc_stderr,
                    "stdout_length": _stdout_len,
                    "stderr_length": _stderr_len,
                    "sandbox_id": _sandbox_id,
                    "sandbox_log_tail": _exc_log_tail,
                    "attempt_key": locals().get("attempt_key"),
                },
            }
        finally:
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

    _sandbox_agent.__name__ = f"sandbox_agent_{node_id}"
    return _sandbox_agent


def _validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Lightweight field-presence validation against a JSON schema.

    Raises ValueError on first missing required field.  Full JSON Schema
    validation (via a library like `jsonschema`) is deferred to v1.
    """
    required: list[str] = schema.get("required", [])
    for field in required:
        if field not in data:
            raise ValueError(f"Manual output missing required field {field!r} (required: {required})")
