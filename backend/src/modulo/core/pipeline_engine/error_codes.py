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

import re
from dataclasses import dataclass
from typing import Any

from modulo.core.secret_patterns import AWS_ACCESS_KEY_PATTERN, GITHUB_PAT_PATTERN


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
_CODE_CAPACITY_ORG = "capacity.org"


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
    "scope.violation": ErrorCodeSpec(
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
    "pipeline_capacity": "capacity.pipeline",
    "org_capacity_limited": _CODE_CAPACITY_ORG,
    "capacity_timeout": "capacity.timeout",
    # FAR-418: scope violation (legacy snake_case spelling canonicalized to
    # scope.violation).
    "scope_violation": "scope.violation",
    "ScopeViolationError": "scope.violation",
}


def map_legacy_code(code: str | None) -> str:
    """Map a (legacy or already-dotted) error code to its canonical dotted code.

    Legacy codes are resolved through :data:`LEGACY_ALIASES`; already-dotted
    registry codes pass through unchanged. Unmapped codes fall back to
    ``harness.unknown`` (§3.2) so presentation always has a resolvable code.
    """
    if not code:
        return _CODE_HARNESS_UNKNOWN
    resolved = LEGACY_ALIASES.get(code)
    if resolved is not None:
        return resolved
    if code in ERROR_CODE_REGISTRY:
        return code
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
# pattern engine). ``runs.error_detail`` is String(5000), so this also mirrors
# the column bound.
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
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk_live_[A-Za-z0-9]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[ousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{8,}"),
    AWS_ACCESS_KEY_PATTERN,
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"xox[bap]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"://[^:\s@]+:[^@\s@]+@"),
    re.compile(r"secret_[A-Za-z0-9]{16,}"),
    re.compile(r"npm_[A-Za-z0-9]{20,}"),
    GITHUB_PAT_PATTERN,
)

# Hard control characters (NUL, bell, vertical tab, form feed, C0 except
# \n \t \r, DEL). Printable text, newlines, tabs and carriage returns pass
# through untouched — the sanitizer is a NO-OP for clean strings.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_error_text(text: Any) -> str:
    """Control-char strip + secret-pattern redaction for error detail.

    Idempotent and a NO-OP for clean strings (the redacted replacement never
    matches a secret pattern). Input is capped at :data:`_ERROR_DETAIL_HARD_LIMIT`
    code points BEFORE any regex runs (ReDoS defense). Non-str input is coerced
    via ``str()`` — never raises.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    capped = text[:_ERROR_DETAIL_HARD_LIMIT]
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
