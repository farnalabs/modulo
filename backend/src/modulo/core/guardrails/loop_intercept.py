"""Agent-loop interior tool-call interception (FAR-211 T3, first slice).

The sandbox agent loop is the highest-risk unguarded surface: for
``sandbox_agent`` nodes, the internal LLM loop (tool results re-injected into
the model mid-loop) sits between the ingestion-edge guardrail (T1,
run-creation) and any output gate. The sandbox is Modulo-hosted, so it is NOT
"unmediated external runtime" — it is a covered boundary with no guardrail.
This module adds a Modulo-hosted tool_call-dispatch interception bridge INSIDE
the sandbox agent loop:

  * each tool invocation is reported to the Modulo side BEFORE execution
    (the sandbox-side bridge client POSTs ``{tool_name, args, direction}`` to
    a local callback server the Modulo sandbox-agent process hosts);
  * each tool result is reported BEFORE it re-enters the model context
    (``direction="after"`` with ``result_summary``).

The bridge REUSES the T1 guardrail rows + engine — detection is never
reimplemented. Action semantics:

  block  -> the tool call is NOT executed (``before`` direction; the bridge
            client refuses it). For an ALREADY-executed tool result (``after``
            direction) a block cannot un-execute the call — it is recorded.
            Interception is preventive, not compensating (compensation is
            FAR-213).
  warn   -> the call proceeds; the violation is recorded (audit only).
  redact -> the payload is masked BEFORE the tool executes (``before``) or the
            result summary is masked before it re-enters the model context
            (``after``).
  pass   -> no bound guardrail fired, or the tool is not in the intercepted
            pattern set (read-only/local-only calls pass through).

Per-call latency budget: the evaluation runs under ``asyncio.wait_for`` with
``latency_budget_ms``. On timeout or bridge error the call is ALLOWED
(best-effort fail-open WITH a log + audit) — the interior interception must
never wedge the agent loop.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult
from modulo.core.guardrails import (
    GuardrailAction,
    GuardrailBlockedError,
    _detect_one,
    _interpret_violation,
    _resolve_action,
    _resolve_detection,
    apply_redaction_masks,
    resolve_guardrail_timeout,
)

_log = logging.getLogger(__name__)

# Default per-call latency budget for the interception round-trip (milliseconds).
# A slow bridge never blocks the agent — on timeout the call is ALLOWED with a
# log + audit (best-effort fail-open).
DEFAULT_LOOP_INTERCEPT_LATENCY_BUDGET_MS = 250

# Default tool-name glob patterns the bridge intercepts. Connector-mediated
# writes (git push, gh PR creation, gh API writes), sandbox filesystem writes,
# and network egress are intercepted. Read-only/local-only calls (file reads
# within the workspace, ``git status``) are low-risk and pass through.
DEFAULT_LOOP_INTERCEPT_PATTERNS: tuple[str, ...] = (
    "git push*",
    "gh pr create*",
    "gh issue create*",
    "gh repo create*",
    "gh api*",
    "curl*",
    "wget*",
    "fly deploy*",
    "flyctl deploy*",
    "docker push*",
    "npm publish*",
    "pip install*",
)

# Cap on the serialised tool-args payload handed to the guardrail engine. The
# cap is a DoS-amplification guard (regex/json_schema detection over bounded
# input); over-cap args are truncated with a marker. The tool call itself is
# NEVER refused because of size.
MAX_LOOP_INTERCEPT_ARGS_BYTES = 100_000

# The evaluation is a server-side round-trip; give the handler a little grace
# beyond the latency budget for audit persistence before it returns a pass
# decision to the sandbox bridge.
_CALLBACK_HANDLER_GRACE_SECONDS = 10.0


class LoopInterceptConfigError(ValueError):
    """Raised when a ``loop_intercept`` node config is malformed."""


class LoopInterceptConfig(BaseModel):
    """The ``loop_intercept`` config on a ``sandbox_agent`` node (config_json).

    ``intercepted_tool_patterns`` is a list of glob patterns matched (with
    ``fnmatch``) against the tool-call name. ``intercept_tool_results`` enables
    the ``after`` (tool-result) interception. ``block_on_guardrail`` makes a
    ``block``-action guardrail REFUSE the tool call on the ``before``
    direction; when False a block is recorded as a warn (never refuse).
    """

    enabled: bool = True
    latency_budget_ms: int = Field(default=DEFAULT_LOOP_INTERCEPT_LATENCY_BUDGET_MS, ge=1, le=5000)
    intercepted_tool_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_LOOP_INTERCEPT_PATTERNS))
    intercept_tool_results: bool = True
    block_on_guardrail: bool = True


def parse_loop_intercept_config(raw: Any) -> LoopInterceptConfig:
    """Parse + validate a ``loop_intercept`` node config.

    Raises :class:`LoopInterceptConfigError` on a malformed config. ``None`` /
    absent config (the default) is NOT an error — the caller checks for
    presence before calling this.
    """
    try:
        return LoopInterceptConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise LoopInterceptConfigError(f"malformed loop_intercept config: {details}") from exc


def validate_loop_intercept_config_errors(raw: Any) -> list[str]:
    """Return a list of config-shape error strings (empty when valid).

    Used by the graph validator to surface loop_intercept problems at
    save-time without raising.
    """
    try:
        LoopInterceptConfig.model_validate(raw)
    except ValidationError as exc:
        return [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return []


def tool_matches_patterns(tool_name: str, patterns: Sequence[str]) -> bool:
    """True when *tool_name* matches any glob *patterns* (fnmatch)."""
    return any(fnmatch.fnmatch(tool_name, pattern) for pattern in patterns)


def serialize_tool_event(
    tool_name: str,
    args: Any,
    direction: str,
    result_summary: str = "",
) -> dict[str, Any]:
    """Build the JSON-safe guardrail payload for a tool-call event.

    Args are round-tripped through JSON so only JSON-serialisable values reach
    the deterministic regex/json_schema detection (a non-serialisable arg can
    never crash the engine), and capped at :data:`MAX_LOOP_INTERCEPT_ARGS_BYTES`
    so a huge tool argument never amplifies detection cost. The event payload
    is NEVER written to audit — audit records are summary-only.
    """
    args_out: dict[str, Any] = {}
    if isinstance(args, dict):
        try:
            blob = json.dumps(args, separators=(",", ":"), default=str)
            if len(blob) > MAX_LOOP_INTERCEPT_ARGS_BYTES:
                args_out = {"_truncated": True, "_arg_count": len(args)}
            else:
                args_out = json.loads(blob)
        except (TypeError, ValueError):
            args_out = {"_unserializable": True}
    return {
        "tool": tool_name,
        "args": args_out,
        "direction": direction,
        "result_summary": result_summary,
    }


@dataclass(frozen=True)
class LoopInterceptOutcome:
    """Decision for one intercepted tool-call/result event.

    ``action`` is ``pass`` | ``block`` | ``warn`` | ``redact``. ``blocked`` is
    True only when the bridge client must REFUSE the call (a ``before`` block
    with ``block_on_guardrail``) — an ``after`` block is recorded but cannot
    refuse an already-executed call. ``masked_args`` carries the post-redaction
    args when ``action == "redact"``. ``bridge_failed`` is True when the
    evaluation itself failed (timeout / mechanism error) and the call was
    ALLOWED (fail-open with audit).
    """

    tool_name: str
    direction: str
    action: Literal["pass", "block", "warn", "redact"]
    blocked: bool
    guardrail_name: str = ""
    masked_args: dict[str, Any] | None = None
    result_summary: str = ""
    latency_ms: int = 0
    bridge_failed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class LoopInterceptAuditRecord:
    """A summary-only audit record produced by a loop-interception decision.

    ``payload`` NEVER contains raw args/payloads — only the tool name, guardrail
    name, direction, and a non-payload reason.
    """

    event_type: str
    payload: dict[str, Any]


def _consume_future(future: asyncio.Future[Any]) -> None:
    """Retrieve a run_in_executor outcome so a thread-raised exception is not
    reported as "exception was never retrieved" when the outer budget cancels
    the await mid-flight (the executor thread itself cannot be cancelled)."""
    if future.cancelled():
        return
    future.exception()


async def _detect_one_bounded(
    engine: EvalEngine,
    payload: dict[str, Any],
    eval_def: EvalDefinition,
    timeout_seconds: float,
) -> EvalResult:
    """Run one guardrail's detection in a worker thread under a hard timeout.

    Detection reuses T1's ``_detect_one`` (validate + mirror + pure
    regex/json_schema evaluation). The result future is consumed on
    cancellation so a thread-side exception is never an un-retrieved warning.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, _detect_one, engine, payload, eval_def)
    future.add_done_callback(_consume_future)
    return await asyncio.wait_for(future, timeout=timeout_seconds)


async def _evaluate_event(
    engine: EvalEngine,
    definitions: Sequence[EvalDefinition],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[list[tuple[EvalDefinition, EvalResult]], str | None]:
    """Evaluate ALL bound guardrails against the tool-call payload.

    Returns ``(fired, mechanism_error)``. ``fired`` lists ``(def, result)``
    pairs where detection flagged a violation. ``mechanism_error`` is a
    summary reason string when ANY guardrail's detection failed (timeout,
    malformed config, misrouting) — the caller then FAILS OPEN (allows the
    call) because the interior interception must never wedge the agent loop.
    """
    fired: list[tuple[EvalDefinition, EvalResult]] = []
    for eval_def in definitions:
        try:
            result = await _detect_one_bounded(engine, payload, eval_def, timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = (
                f"detection exceeded {timeout_seconds:g}s budget"
                if isinstance(exc, TimeoutError)
                else f"detection error on guardrail {eval_def.name!r}: {type(exc).__name__}"
            )
            _log.warning(
                "guardrail.loop_detection_mechanism_error",
                extra={"guardrail": eval_def.name, "reason": reason},
            )
            return fired, reason
        detection_type, _ = _resolve_detection(eval_def)
        if _interpret_violation(detection_type, result):
            fired.append((eval_def, result))
    return fired, None


def _redaction_policies(definitions: Sequence[EvalDefinition]) -> tuple[list[Any], str]:
    """Collect static redaction policies from redact-action guardrails.

    Returns ``(policies, names)`` — ``policies`` is the list of
    ``FieldRedactionPolicy`` DTOs, ``names`` a comma-joined summary of the
    contributing guardrail names (never payload values).
    """
    from modulo.core.guardrails import FieldRedactionPolicy

    policies: list[Any] = []
    names: list[str] = []
    for eval_def in definitions:
        if _resolve_action(eval_def) != GuardrailAction.REDACT:
            continue
        names.append(eval_def.name)
        for raw in eval_def.config.get("redaction", []) or []:
            try:
                policies.append(FieldRedactionPolicy.model_validate(raw))
            except ValidationError:
                _log.warning(
                    "guardrail.loop_redaction_policy_invalid",
                    extra={"guardrail": eval_def.name},
                )
    return policies, ",".join(sorted(set(names)))


async def run_loop_interception(
    engine: EvalEngine,
    definitions: Sequence[EvalDefinition],
    event: dict[str, Any],
    *,
    config: LoopInterceptConfig,
) -> tuple[LoopInterceptOutcome, list[LoopInterceptAuditRecord]]:
    """Evaluate one tool-call/result event against the bound guardrails.

    *event* is the bridge's POST body: ``{tool_name, args, direction,
    result_summary}``. Returns ``(outcome, audit_records)``. The outcome's
    ``blocked`` flag tells the bridge client whether to refuse the call.

    Guarantees:
      * The per-call latency budget is enforced with ``asyncio.wait_for``; a
        timeout or detection mechanism error ALLOWS the call (fail-open) and
        emits a ``guardrail.loop_bridge_timeout`` audit record — the
        interception never wedges the agent loop.
      * Low-risk tools outside ``intercepted_tool_patterns`` pass through
        without any evaluation cost.
      * Audit records are summary-only (tool, guardrail, direction, reason) —
        NEVER raw args/payloads.
    """
    tool_name = str(event.get("tool_name") or "")
    direction: str = "after" if event.get("direction") == "after" else "before"
    raw_args = event.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    result_summary = str(event.get("result_summary") or "")

    start = time.monotonic()

    def _pass(reason: str = "", bridge_failed: bool = False) -> LoopInterceptOutcome:
        return LoopInterceptOutcome(
            tool_name=tool_name,
            direction=direction,
            action="pass",
            blocked=False,
            latency_ms=int((time.monotonic() - start) * 1000),
            bridge_failed=bridge_failed,
            reason=reason,
        )

    if not config.enabled or not definitions:
        return _pass(), []
    if not tool_matches_patterns(tool_name, config.intercepted_tool_patterns):
        # Read-only/local-only calls pass through (ADR 003 amendment).
        return _pass(), []
    if direction == "after" and not config.intercept_tool_results:
        return _pass(), []

    payload = serialize_tool_event(tool_name, args, direction, result_summary)
    budget_seconds = config.latency_budget_ms / 1000.0
    guardrail_timeout = resolve_guardrail_timeout(definitions)

    try:
        fired, mechanism_error = await asyncio.wait_for(
            _evaluate_event(engine, definitions, payload, guardrail_timeout),
            timeout=budget_seconds,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        fired = []
        mechanism_error = "latency budget exceeded"
    except Exception as exc:
        fired = []
        mechanism_error = f"bridge error: {type(exc).__name__}"

    latency_ms = int((time.monotonic() - start) * 1000)

    if mechanism_error:
        return (
            LoopInterceptOutcome(
                tool_name=tool_name,
                direction=direction,
                action="pass",
                blocked=False,
                latency_ms=latency_ms,
                bridge_failed=True,
                reason=mechanism_error,
            ),
            [
                LoopInterceptAuditRecord(
                    "guardrail.loop_bridge_timeout",
                    {"tool": tool_name, "direction": direction, "reason": mechanism_error},
                )
            ],
        )

    blocking = [eval_def for eval_def, _ in fired if _resolve_action(eval_def) == GuardrailAction.BLOCK]
    if blocking:
        guardrail_name = blocking[0].name
        refuse = direction == "before" and config.block_on_guardrail
        return (
            LoopInterceptOutcome(
                tool_name=tool_name,
                direction=direction,
                action="block",
                blocked=refuse,
                guardrail_name=guardrail_name,
                latency_ms=latency_ms,
            ),
            [
                LoopInterceptAuditRecord(
                    "guardrail.loop_blocked",
                    {"tool": tool_name, "direction": direction, "guardrail": guardrail_name},
                )
            ],
        )

    redact_defs = [eval_def for eval_def in definitions if _resolve_action(eval_def) == GuardrailAction.REDACT]
    if redact_defs:
        policies, redact_names = _redaction_policies(redact_defs)
        if policies:
            try:
                redacted_payload, redaction_entries = apply_redaction_masks(
                    payload,
                    policies,
                    raise_on_block=True,
                    guardrail_name=redact_names or "<guardrail>",
                )
            except GuardrailBlockedError as exc:
                # A block-mode redaction policy fired — evidence of the guarded
                # condition in the args. Refuse the call on the before direction.
                refuse = direction == "before" and config.block_on_guardrail
                return (
                    LoopInterceptOutcome(
                        tool_name=tool_name,
                        direction=direction,
                        action="block",
                        blocked=refuse,
                        guardrail_name=redact_names,
                        latency_ms=latency_ms,
                        reason=str(exc),
                    ),
                    [
                        LoopInterceptAuditRecord(
                            "guardrail.loop_blocked",
                            {"tool": tool_name, "direction": direction, "guardrail": redact_names},
                        )
                    ],
                )
            if any(entry.applied for entry in redaction_entries):
                masked = redacted_payload.get("args")
                masked_args = masked if isinstance(masked, dict) else dict(args)
                return (
                    LoopInterceptOutcome(
                        tool_name=tool_name,
                        direction=direction,
                        action="redact",
                        blocked=False,
                        guardrail_name=redact_names,
                        masked_args=masked_args,
                        latency_ms=latency_ms,
                    ),
                    [
                        LoopInterceptAuditRecord(
                            "guardrail.loop_redacted",
                            {"tool": tool_name, "direction": direction, "guardrail": redact_names},
                        )
                    ],
                )

    warned = sorted({eval_def.name for eval_def, _ in fired if _resolve_action(eval_def) == GuardrailAction.WARN})
    if warned:
        warn_names = ",".join(warned)
        return (
            LoopInterceptOutcome(
                tool_name=tool_name,
                direction=direction,
                action="warn",
                blocked=False,
                guardrail_name=warn_names,
                latency_ms=latency_ms,
            ),
            [
                LoopInterceptAuditRecord(
                    "guardrail.loop_warned",
                    {"tool": tool_name, "direction": direction, "guardrail": warn_names},
                )
            ],
        )

    return _pass(), []


# ---------------------------------------------------------------------------
# Callback server (hosted by the Modulo sandbox-agent process)
# ---------------------------------------------------------------------------


class _BridgeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server carrying a typed reference to the owner."""

    owner: LoopInterceptCallbackServer


class _BridgeHTTPHandler(BaseHTTPRequestHandler):
    """POST /intercept — evaluate a tool-call event and return the decision."""

    protocol_version = "HTTP/1.0"
    server_version = "ModuloLoopIntercept/1.0"

    def do_POST(self) -> None:
        server = self.server
        if not isinstance(server, _BridgeHTTPServer):
            raise TypeError("bridge server must be a _BridgeHTTPServer")
        owner = server.owner
        loop = owner.loop
        if self.path.rstrip("/") != "/intercept" or loop is None:
            self._respond(
                {"action": "pass", "blocked": False, "masked_args": None, "reason": "bridge_unavailable"},
                404,
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            event = json.loads(body.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            event = {}
        if not isinstance(event, dict):
            event = {}
        future = asyncio.run_coroutine_threadsafe(owner.evaluate(event), loop)
        max_wait = owner.max_wait_seconds
        try:
            decision = future.result(timeout=max_wait)
        except Exception:
            _log.exception(
                "guardrail.loop_callback_eval_failed",
                extra={"tool": str(event.get("tool_name") or "")},
            )
            decision = {"action": "pass", "blocked": False, "masked_args": None, "reason": "server_eval_error"}
        self._respond(decision, 200)

    def _respond(self, decision: dict[str, Any], status: int) -> None:
        payload = json.dumps(decision).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        # Silenced: the per-request default logging is noise; structured logs
        # already carry the audit trail.
        del format, args


# ``http.server`` dispatches to the handler methods by name (BaseHTTPRequestHandler
# calls ``self.do_POST()`` / ``self.log_message(...)`` at request time), so vulture
# cannot see a call site. Reference them explicitly so the dead-code gate counts
# them as used — they are the load-bearing http.server request contract.
_BRIDGE_HANDLER_CONTRACT = (_BridgeHTTPHandler.do_POST, _BridgeHTTPHandler.log_message)


class LoopInterceptCallbackServer:
    """Local HTTP callback server hosting the Modulo-side loop interception.

    Binds a loopback (or configured) host/port in a daemon thread. The
    sandbox-side bridge client POSTs ``{tool_name, args, direction,
    result_summary}`` to ``/intercept``; each event is evaluated against the
    bound guardrails and a decision ``{action, blocked, masked_args, guardrail,
    reason}`` is returned. Audit records are persisted best-effort via
    *audit_sink* (a callable ``(records) -> Awaitable[None]``), fail-open.

    The endpoint the sandbox bridge reaches must be reachable FROM the sandbox
    (in production this is the Modulo sandbox-agent process's loopback, exposed
    into the sandbox; in tests it binds 127.0.0.1 and the stub agent runs on
    the same host). See the ADR 003 amendment for the deployment model.
    """

    def __init__(
        self,
        engine: EvalEngine,
        definitions: Sequence[EvalDefinition],
        config: LoopInterceptConfig,
        *,
        audit_sink: Callable[[Sequence[LoopInterceptAuditRecord]], Awaitable[None]] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._engine = engine
        self._definitions = list(definitions)
        self._config = config
        self._audit_sink = audit_sink
        self._host = host
        self._port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.max_wait_seconds = config.latency_budget_ms / 1000.0 + _CALLBACK_HANDLER_GRACE_SECONDS
        self._server: _BridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def port(self) -> int | None:
        if self._server is None:
            return None
        return int(self._server.server_address[1])

    async def start(self) -> int:
        """Bind + start serving; returns the bound port."""
        self.loop = asyncio.get_running_loop()
        server = _BridgeHTTPServer((self._host, self._port), _BridgeHTTPHandler)
        server.owner = self
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="modulo-loop-intercept",
            daemon=True,
        )
        self._thread.start()
        return int(server.server_address[1])

    async def close(self) -> None:
        """Stop serving and release the port. Idempotent, best-effort."""
        server = self._server
        self._server = None
        self.loop = None
        if server is None:
            return
        thread = self._thread
        self._thread = None
        try:
            await asyncio.to_thread(server.shutdown)
        except asyncio.CancelledError:
            server.server_close()
            raise
        except Exception:
            _log.exception("guardrail.loop_bridge_shutdown_failed")
        server.server_close()
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 5)

    async def evaluate(self, event: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a tool-call event; persists audit records best-effort."""
        outcome, records = await run_loop_interception(
            self._engine,
            self._definitions,
            event,
            config=self._config,
        )
        if records and self._audit_sink is not None:
            self._spawn_audit(records)
        return {
            "action": outcome.action,
            "blocked": outcome.blocked,
            "masked_args": outcome.masked_args,
            "guardrail": outcome.guardrail_name,
            "reason": outcome.reason,
        }

    def _spawn_audit(self, records: Sequence[LoopInterceptAuditRecord]) -> None:
        loop = self.loop
        if loop is None:
            return
        task = loop.create_task(self._run_audit(records))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_audit(self, records: Sequence[LoopInterceptAuditRecord]) -> None:
        try:
            if self._audit_sink is None:
                raise RuntimeError("loop audit sink is not configured")
            await self._audit_sink(records)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("guardrail.loop_audit_sink_failed")


# ---------------------------------------------------------------------------
# Guardrail row loading + audit persistence (org-scoped)
# ---------------------------------------------------------------------------


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def load_loop_intercept_guardrails(
    session_factory: Any,
    *,
    org_id: Any,
    pipeline_id: Any,
) -> list[EvalDefinition]:
    """Load the pipeline's bound guardrails as engine DTOs (org-scoped).

    Reuses the T1 row-loading (``load_pipeline_guardrail_rows``) so the loop
    interception evaluates the SAME guardrail rows as the ingestion-edge seam.
    Zero guardrails bound -> empty list (the bridge is inert). Returns empty
    when no session factory is available (no DB context — fail-open).
    """
    if session_factory is None:
        return []
    org_uuid = _coerce_uuid(org_id)
    pipe_uuid = _coerce_uuid(pipeline_id)
    if org_uuid is None or pipe_uuid is None:
        return []

    from modulo.core.guardrails import to_engine_definition
    from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows
    from modulo.db.rls import set_rls_org

    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            rows = await load_pipeline_guardrail_rows(
                session,
                pipeline_id=pipe_uuid,
                organisation_id=org_uuid,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrail.loop_load_failed", extra={"org_id": str(org_uuid)})
        return []
    return [to_engine_definition(row) for row in rows]


async def persist_loop_interception_audit(
    session_factory: Any,
    *,
    org_id: Any,
    run_id: Any,
    node_id: str,
    records: Sequence[LoopInterceptAuditRecord],
) -> None:
    """Persist loop-interception audit records (org-scoped, best-effort).

    Fail-open WITH a log: the interception decision already happened; the audit
    is observability and must never break the run. Payloads are summary-only.
    """
    if session_factory is None or not records:
        return
    org_uuid = _coerce_uuid(org_id)
    run_uuid = _coerce_uuid(run_id)
    if org_uuid is None or run_uuid is None:
        return
    try:
        from modulo.core.audit_logger import append_audit_event
        from modulo.db.rls import set_rls_org

        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            for record in records:
                payload = dict(record.payload)
                if node_id:
                    payload["node_id"] = node_id
                await append_audit_event(
                    session,
                    org_id=org_uuid,
                    event_type=record.event_type,
                    resource_type="run",
                    resource_id=run_uuid,
                    payload_json=payload,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrail.loop_audit_persist_failed", extra={"org_id": str(org_uuid)})


def bridge_client_source() -> str:
    """Return the sandbox-side bridge client source (written into the sandbox).

    The bridge client is deliberately STDLIB-ONLY — it runs inside the E2B
    sandbox where Modulo is not installed. Reading the file (rather than a
    string constant) keeps one source of truth.
    """
    from pathlib import Path

    return Path(__file__).with_name("sandbox_bridge.py").read_text(encoding="utf-8")


__all__ = [
    "DEFAULT_LOOP_INTERCEPT_LATENCY_BUDGET_MS",
    "DEFAULT_LOOP_INTERCEPT_PATTERNS",
    "MAX_LOOP_INTERCEPT_ARGS_BYTES",
    "LoopInterceptAuditRecord",
    "LoopInterceptCallbackServer",
    "LoopInterceptConfig",
    "LoopInterceptConfigError",
    "LoopInterceptOutcome",
    "bridge_client_source",
    "load_loop_intercept_guardrails",
    "parse_loop_intercept_config",
    "persist_loop_interception_audit",
    "run_loop_interception",
    "serialize_tool_event",
    "tool_matches_patterns",
    "validate_loop_intercept_config_errors",
]
