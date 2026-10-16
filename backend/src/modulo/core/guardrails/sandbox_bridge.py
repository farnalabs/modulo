"""Modulo agent-loop interception bridge client (FAR-211).

THIS FILE IS WRITTEN INTO THE E2B SANDBOX BY THE ``sandbox_agent`` NODE RUNNER
(see ``modulo.core.guardrails.loop_intercept.bridge_client_source``) and
executed by the agent command wrapper. It is intentionally STDLIB-ONLY — it
runs inside the sandbox where Modulo is not installed.

It talks to the Modulo-hosted callback server (``LoopInterceptCallbackServer``
in ``modulo.core.guardrails.loop_intercept``). Each tool-call event is POSTed
BEFORE the tool executes (``direction="before"``) and each tool result BEFORE
it re-enters the model context (``direction="after"``, with a result summary).
The server's decision is enforced:

  block  -> the tool call is refused (``decide_before`` returns ``(False, ...)``;
            the ``--wrap`` CLI kills the child and exits non-zero after
            printing a ``MODULO_BRIDGE_BLOCKED`` marker).
  escalate -> the tool call is refused like a block, but the loop is being
            STOPPED for a human decision (FAR-1694): the ``--wrap`` CLI kills
            the child, prints a typed ``MODULO_BRIDGE_ESCALATED:<json>`` marker
            and exits with the dedicated ``ESCALATED_EXIT_CODE`` so the host
            can tell an escalation from a block without parsing prose.
  redact -> ``masked_args`` replace the original args before execution.
  warn/pass -> proceed.

UNTRUSTED CHANNEL (FAR-1694 S2 security note): the escalation marker and the
dedicated exit code travel on the wrapped agent's OWN stdout — this wrapper is
a transparent proxy for that stream (every non-``MODULO_BRIDGE_EVENT`` child
line is echoed verbatim) and returns the child's exit code when no refusal
occurs, so a compromised or prompt-injected agent can forge BOTH the marker and
exit code 4. The marker is therefore evidence only: it MUST NOT gate a control
decision. The S3 suspension slice (which turns the marker into ``interrupt()``
/ ``awaiting_human``) MUST authenticate the channel first — e.g. deliver the
escalation over the host callback, a child-inaccessible pipe/fd, or a
host-recorded decision — before acting on it. S2 consumes the marker solely to
explain a failure the node has already failed closed on.

The bridge is BEST-EFFORT and fail-open: any transport error, timeout, or
malformed response means the call proceeds (the interior interception must
never wedge the agent loop). The Modulo side records the failure as a
``guardrail.loop_bridge_timeout`` audit event.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess  # nosec B404 — subprocess is stdlib-only (sandbox file); only used to exec the configured agent command (no shell=True)
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PATTERNS = (
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

EVENT_MARKER = "MODULO_BRIDGE_EVENT:"
BLOCKED_MARKER = "MODULO_BRIDGE_BLOCKED:"
# FAR-1694 S2: emitted when an in-loop ``escalate`` verdict stops the loop. The
# payload is a single line of JSON (see :func:`_escalation_payload`).
# UNTRUSTED: the marker rides the agent's own stdout (which this wrapper proxies
# verbatim), so an agent can forge it — see the module docstring. It must never
# gate a control decision until S3 authenticates the channel.
ESCALATED_MARKER = "MODULO_BRIDGE_ESCALATED:"

# Dedicated process exit codes for a bridge-enforced loop stop. Distinct so the
# host can tell an escalation from a block without parsing prose — 3 = blocked,
# 4 = escalated. The escalation code is the protocol's typed stop signal; the
# mid-loop suspension that acts on it ships in a later FAR-1694 slice.
BLOCKED_EXIT_CODE = 3
ESCALATED_EXIT_CODE = 4

DEFAULT_ENDPOINT = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class _Refusal:
    """A bridge decision that stops a ``before`` tool call.

    ``action`` is ``"block"`` or ``"escalate"``. ``payload`` carries the
    escalation context for the ``MODULO_BRIDGE_ESCALATED`` marker (empty for a
    block, which marks only the tool name).
    """

    action: str
    tool_name: str
    payload: dict[str, Any]


def _args_sha256(args: dict[str, Any]) -> str:
    """Return a one-way SHA-256 of the serialised args (evidence, never args).

    Recorded in the escalation marker so a human reviewing the escalation has
    an args fingerprint without the raw (possibly credential-bearing) args
    crossing the boundary or entering persistence.
    """
    try:
        blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        # Unserialisable args (e.g. a cycle) all fall back to hashing the empty
        # string, so distinct unserialisable payloads share this one fingerprint.
        # Best-effort evidence only — the hash never gates behaviour.
        blob = ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _escalation_payload(
    tool_name: str, args: dict[str, Any], decision: dict[str, Any], direction: str
) -> dict[str, Any]:
    """Build the ``MODULO_BRIDGE_ESCALATED`` marker payload for one event.

    Summary-only by design: the tool name, direction, the firing guardrail, a
    non-payload reason, and an args hash. Raw args are NEVER included.
    ``direction`` is the event's ACTUAL direction (``before`` or ``after``) and
    is passed through — never hardcoded — so an after-direction escalation is
    not mislabelled as a before-direction one.
    """
    return {
        "tool": tool_name,
        "direction": direction,
        "guardrail": str(decision.get("guardrail") or ""),
        "reason": str(decision.get("reason") or ""),
        "args_sha256": _args_sha256(args),
    }


def _out(line: str = "") -> None:
    """Write a protocol line to stdout, flushed (the wrapper's stdout is piped)."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _err(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class BridgeClient:
    """Best-effort, fail-open client for the Modulo-side interception callback.

    Args are never logged and never persisted client-side; only the decision
    dict from the Modulo side is acted on.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout: float = 1.0,
        patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._patterns = tuple(patterns)
        self._last_decision: dict[str, Any] | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def last_decision(self) -> dict[str, Any]:
        """The most recent server decision (``{}`` before any event).

        The wrapper handles tool-call events strictly serially, so the last
        decision is always the one the current event produced. Recorded so the
        wrapper can build the typed escalation marker payload (guardrail /
        reason) from the server's decision without a second POST.
        """
        return self._last_decision or {}

    def should_intercept(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatch(tool_name, pattern) for pattern in self._patterns)

    def notify(
        self,
        tool_name: str,
        args: dict[str, Any],
        direction: str,
        result_summary: str = "",
    ) -> dict[str, Any]:
        """POST a tool-call/result event; returns the decision dict.

        Never raises for a bridge failure — a transport error, timeout, or
        malformed response returns ``{"action": "pass"}`` (fail-open). The
        Modulo side is responsible for auditing the failure.
        """
        if not self.should_intercept(tool_name):
            return {"action": "pass", "blocked": False, "masked_args": None, "reason": "not-intercepted"}
        payload = json.dumps(
            {
                "tool_name": tool_name,
                "args": args,
                "direction": direction,
                "result_summary": result_summary,
            }
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - trusted Modulo-side endpoint (loopback/config)
            self._endpoint + "/intercept",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310  # nosec B310 - the endpoint is the Modulo-side callback (loopback/config), not an arbitrary URL
                body = response.read().decode("utf-8", "replace")
                decision = json.loads(body)
        except Exception as exc:
            reason = f"bridge_error:{type(exc).__name__}"
            return {"action": "pass", "blocked": False, "masked_args": None, "reason": reason}
        if not isinstance(decision, dict):
            return {"action": "pass", "blocked": False, "masked_args": None, "reason": "bridge_error:non-dict"}
        return decision

    def decide_before(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str]:
        """Evaluate a tool call BEFORE it executes.

        Returns ``(allowed, masked_args, action)``. ``allowed`` is False when
        the Modulo side REFUSES the call (the caller must not execute it).
        ``masked_args`` carries the post-redaction args when ``action`` is
        ``redact``. Fail-open: any bridge failure returns ``(True, None, ...)``.

        An ``escalate`` decision is ALWAYS a refusal: it means the loop is being
        stopped for a human decision (FAR-1694), and — unlike block — there is
        no record-only downgrade, because the server emits it only when a human
        must decide.

        A block is refused only when the Modulo decision carries
        ``blocked: True`` (a ``before`` block with ``block_on_guardrail``).
        When the server returns ``action="block"`` with ``blocked: False`` —
        the ``block_on_guardrail: false`` downgrade, or any ``after``-direction
        block routed through here — the block is RECORD-ONLY: the call
        proceeds (ADR 044 amendment: interception is preventive, not
        compensating, and ``block_on_guardrail: false`` downgrades block actions
        to record-only inside the loop).
        """
        decision = self.notify(tool_name, args, "before")
        # Record the decision so the wrapper can build the typed escalation
        # marker payload (guardrail / reason) without a second POST.
        self._last_decision = decision
        action = str(decision.get("action") or "pass")
        if action == "escalate":
            return False, None, "escalate"
        if action == "block":
            if decision.get("blocked"):
                return False, None, "block"
            # blocked=False -> server downgraded the block; proceed, do not refuse.
            return True, None, "block"
        if action == "redact":
            masked = decision.get("masked_args")
            if isinstance(masked, dict):
                return True, masked, "redact"
        return True, None, action

    def notify_after(self, tool_name: str, args: dict[str, Any], result_summary: str) -> dict[str, Any]:
        """Report a tool result BEFORE it re-enters the model context."""
        return self.notify(tool_name, args, "after", result_summary)


def _safe_config_path(path: str) -> str:
    """Resolve *path* and require it to stay within the working directory."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(Path.cwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"config path {path!r} is outside the working directory")
    return resolved


def _load_config(config_path: str | None) -> dict[str, Any]:
    raw = {}
    if config_path:
        try:
            safe_path = _safe_config_path(config_path)
            with Path(safe_path).open(encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            pass
    patterns = raw.get("intercepted_tool_patterns")
    resolved = tuple(str(p) for p in patterns if isinstance(p, str)) if isinstance(patterns, list) else DEFAULT_PATTERNS
    return {"patterns": resolved}


def _build_client() -> tuple[BridgeClient, dict[str, Any]]:
    endpoint = os.environ.get("MODULO_BRIDGE_ENDPOINT") or DEFAULT_ENDPOINT
    config_path = os.environ.get("MODULO_BRIDGE_CONFIG")
    config = _load_config(config_path)
    return BridgeClient(endpoint, patterns=config.get("patterns", DEFAULT_PATTERNS)), config


def _wrap_command(argv: list[str], client: BridgeClient) -> int:
    """Run the wrapped agent command, forwarding tool-call events.

    The wrapped command emits tool-call events as stdout lines prefixed with
    ``MODULO_BRIDGE_EVENT: <json>``. Each event is forwarded to the Modulo
    side; a ``before`` refusal kills the child and this wrapper exits non-zero
    after writing the matching marker — ``MODULO_BRIDGE_BLOCKED:<tool>`` with
    exit code :data:`BLOCKED_EXIT_CODE` for a block, or
    ``MODULO_BRIDGE_ESCALATED:<json>`` with exit code
    :data:`ESCALATED_EXIT_CODE` for an escalate (FAR-1694 S2). Best-effort — a
    bridge failure never blocks the command.
    """
    if not argv:
        _err("error: --wrap requires a command")
        return 2
    shell_cmd = " ".join(argv)
    return _spawn_and_collect(["/bin/sh", "-c", shell_cmd], client)


def _spawn_and_collect(process_args: list[str], client: BridgeClient) -> int:
    """Launch *process_args* and stream its stdout, forwarding bridge events.

    Launch and collection failures are fail-closed: an ``OSError`` from the
    launch reports the error and returns 1, and a child started without a
    stdout stream raises instead of proceeding without event interception.
    The child's exit code is returned once its stdout is exhausted; a refused
    ``before`` call kills the child and returns :data:`BLOCKED_EXIT_CODE`
    (block) or :data:`ESCALATED_EXIT_CODE` (escalate).

    The child's stdout is agent-controlled and echoed verbatim (see the module
    docstring's UNTRUSTED CHANNEL note): a marker or exit code seen here is
    evidence, not an authenticated control signal.
    """
    # Suppression on the Popen call below is deliberate — configured agent command (pipeline config, not untrusted
    # input) needs shell features; already suppressed for bandit (B603) and ruff (S603).
    try:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 — execs the configured agent command (fixed argv list, shell=False, no user input)  # NOSONAR
            process_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
        )
    except OSError as exc:
        _err(f"error: failed to start wrapped command: {exc}")
        return 1
    if proc.stdout is None:
        raise RuntimeError("subprocess started without a stdout stream")
    try:
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith(EVENT_MARKER):
                _out(line)
                continue
            refusal = _forward_event_line(line, client)
            if refusal is not None:
                proc.kill()
                proc.wait()
                if refusal.action == "escalate":
                    _out(f"{ESCALATED_MARKER}{json.dumps(refusal.payload)}")
                    return ESCALATED_EXIT_CODE
                _out(f"{BLOCKED_MARKER}{refusal.tool_name}")
                return BLOCKED_EXIT_CODE
    finally:
        proc.stdout.close()
    return proc.wait()


def _forward_event_line(line: str, client: BridgeClient) -> _Refusal | None:
    """Forward one ``MODULO_BRIDGE_EVENT`` stdout line to the Modulo side.

    Returns a :class:`_Refusal` when the Modulo side REFUSES the call (the
    caller must kill the child, emit the matching marker and exit non-zero), or
    ``None`` when the child may continue. A malformed event payload is treated
    as an empty event and forwarded with an empty tool name — fail-open, never
    an echo (event lines are never written back to stdout). A redact decision
    emits the ``MODULO_BRIDGE_REDACTED`` marker; an ``after`` event is reported
    via ``notify_after``. Best-effort — a bridge failure never refuses
    (``BridgeClient.decide_before`` fails open).
    """
    event_text = line[len(EVENT_MARKER) :].strip()
    try:
        event = json.loads(event_text)
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        return None
    tool_name = str(event.get("tool_name") or "")
    raw_args = event.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    direction = "after" if event.get("direction") == "after" else "before"
    result_summary = str(event.get("result_summary") or "")
    if direction == "before":
        allowed, masked, action = client.decide_before(tool_name, args)
        if not allowed:
            if action == "escalate":
                payload = _escalation_payload(tool_name, args, client.last_decision, direction)
                return _Refusal("escalate", tool_name, payload)
            return _Refusal("block", tool_name, {})
        if masked is not None:
            _out(f"MODULO_BRIDGE_REDACTED:{json.dumps(masked)}")
        return None
    client.notify_after(tool_name, args, result_summary)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="modulo_bridge", description="Modulo agent-loop interception bridge client")
    parser.add_argument("--notify", help="evaluate a single event: --notify '<json>'")
    parser.add_argument("--wrap", action="store_true", help="wrap a command, forwarding tool-call events")
    parser.add_argument("--endpoint", help="Modulo-side interception endpoint (default: $MODULO_BRIDGE_ENDPOINT)")
    parser.add_argument("--config", help="bridge config JSON path (default: $MODULO_BRIDGE_CONFIG)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the wrapped command (after --)")
    args = parser.parse_args(argv)

    if args.config:
        config = _load_config(args.config)
        endpoint = args.endpoint or os.environ.get("MODULO_BRIDGE_ENDPOINT") or DEFAULT_ENDPOINT
        client = BridgeClient(endpoint, patterns=config.get("patterns", DEFAULT_PATTERNS))
    elif args.endpoint:
        client = BridgeClient(args.endpoint)
    else:
        client, _ = _build_client()
    if args.wrap:
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        return _wrap_command(command, client)
    if args.notify:
        try:
            event = json.loads(args.notify)
        except ValueError:
            event = {}
        if isinstance(event, dict):
            tool_name = str(event.get("tool_name") or "")
            raw_args = event.get("args")
            args_dict = raw_args if isinstance(raw_args, dict) else {}
            direction = "after" if event.get("direction") == "after" else "before"
            result_summary = str(event.get("result_summary") or "")
            decision = client.notify(tool_name, args_dict, direction, result_summary)
            _out(json.dumps(decision))
            # An escalate is always a stop; emit the typed marker + dedicated
            # code so the host can distinguish it from a block (FAR-1694 S2).
            if decision.get("action") == "escalate":
                _out(f"{ESCALATED_MARKER}{json.dumps(_escalation_payload(tool_name, args_dict, decision, direction))}")
                return ESCALATED_EXIT_CODE
            # Refuse only on an actual refusal (action=block AND blocked=true);
            # a block_on_guardrail:false downgrade (action=block, blocked=false)
            # is record-only and must NOT exit non-zero.
            blocked = bool(decision.get("blocked"))
            return 0 if decision.get("action") != "block" or not blocked else BLOCKED_EXIT_CODE
        _err("error: --notify requires a JSON object")
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
