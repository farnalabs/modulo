"""Error-code registry for run/agent failure classification (agent-failure UX, phase 1).

Single source of truth for the dotted error-code taxonomy
(``<namespace>.<reason>``) described in the agent-failure-ux-proposal (§1, §3,
§15.16). This module implements the write-time legacy→dotted mapping plus the
registry lookups shared by ``_retry_after_policy``, the alert-rule matcher, and
the notifier ``event_mapper`` — one table, three consumers, no drift (§3.2
hard rules).

The module is intentionally dependency-free (no DB, no settings import) so unit
tests are fast and the registry is importable from any consumer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCodeSpec:
    """One registry entry: classification tag, retry default, alert severity, guidance."""

    error_class: str
    retryable: bool
    alert_severity: str | None
    guidance: str


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
        guidance="Agent went silent.",
    ),
    # --- contract (output) codes ----------------------------------------
    "contract.schema": ErrorCodeSpec(
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
    # --- harness (machinery) codes ---------------------------------------
    # ``harness.unknown`` is the fallback for unmapped legacy codes — any code
    # that has no alias and no registry entry resolves here so presentation
    # always has a resolvable code (§3.2). Non-retryable by default: an
    # unclassified failure is never auto-retried (fail-safe default).
    "harness.unknown": ErrorCodeSpec(
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
    "harness.executor_failed": ErrorCodeSpec(
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
    "harness.dispatch_failed": ErrorCodeSpec(
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
    # --- node guard codes ------------------------------------------------
    "node.timeout": ErrorCodeSpec(
        error_class="node",
        retryable=True,
        alert_severity="warning",
        guidance="Hit the timeout guard.",
    ),
    "node.runaway": ErrorCodeSpec(
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
    # --- capacity codes --------------------------------------------------
    "capacity.org": ErrorCodeSpec(
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
    "eval.blocked": ErrorCodeSpec(
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
    "node_timeout": "node.timeout",
    "TimeoutError": "node.timeout",
    "runaway": "node.runaway",
    "runaway.tokens_exceeded": "node.runaway",
    "node_cancelled": "node.cancelled",
    # Run-level.
    "executor_superseded": "run.superseded",
    # Contract.
    "output_rejected": "contract.schema",
    # Harness machinery (§3.2). ``TypeError``/``OperationalError`` are the
    # raw exception class names that executor's generic catch publishes.
    "OperationalError": "harness.db.connection_lost",
    "TypeError": "harness.state_serialization",
    "NodeCancelledError": "harness.sdk_task_cancelled",
    "SandboxNodeFailedError": "sandbox.no_output_json",
    "executor_setup_failed": "harness.executor_failed",
    "executor_failed": "harness.executor_failed",
    "executor_heartbeat_lost": "harness.executor_heartbeat_lost",
    "never_dispatched": "harness.dispatch_failed",
    "dispatch_failed": "harness.dispatch_failed",
    "worker_lost": "harness.dispatch_failed",
    "task_failure": "harness.worker_failed",
    "gate_creation_failed": "harness.gate_creation_failed",
    # Eval.
    "eval_blocked": "eval.blocked",
    "eval_suite_blocked": "eval.blocked",
    # Config.
    "configuration_error": "config.error",
    # Capacity.
    "claim_cap_exhausted": "capacity.claim",
    "pipeline_capacity": "capacity.pipeline",
    "org_capacity_limited": "capacity.org",
    "capacity_timeout": "capacity.timeout",
}


def map_legacy_code(code: str | None) -> str:
    """Map a (legacy or already-dotted) error code to its canonical dotted code.

    Legacy codes are resolved through :data:`LEGACY_ALIASES`; already-dotted
    registry codes pass through unchanged. Unmapped codes fall back to
    ``harness.unknown`` (§3.2) so presentation always has a resolvable code.
    """
    if not code:
        return "harness.unknown"
    resolved = LEGACY_ALIASES.get(code)
    if resolved is not None:
        return resolved
    if code in ERROR_CODE_REGISTRY:
        return code
    return "harness.unknown"


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
