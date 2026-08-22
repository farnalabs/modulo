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
  redact -> ``masked_args`` replace the original args before execution.
  warn/pass -> proceed.

The bridge is BEST-EFFORT and fail-open: any transport error, timeout, or
malformed response means the call proceeds (the interior interception must
never wedge the agent loop). The Modulo side records the failure as a
``guardrail.loop_bridge_timeout`` audit event.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess  # nosec B404 — subprocess is stdlib-only (sandbox file); only used to exec the configured agent command (no shell=True)
import sys
import urllib.request
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

DEFAULT_ENDPOINT = "http://127.0.0.1:8765"


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

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def timeout(self) -> float:
        return self._timeout

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

        A block is refused only when the Modulo decision carries
        ``blocked: True`` (a ``before`` block with ``block_on_guardrail``).
        When the server returns ``action="block"`` with ``blocked: False`` —
        the ``block_on_guardrail: false`` downgrade, or any ``after``-direction
        block routed through here — the block is RECORD-ONLY: the call
        proceeds (ADR 003 amendment: interception is preventive, not
        compensating, and ``block_on_guardrail: false`` downgrades block actions
        to record-only inside the loop).
        """
        decision = self.notify(tool_name, args, "before")
        action = str(decision.get("action") or "pass")
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
    base = os.path.realpath(os.getcwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"config path {path!r} is outside the working directory")
    return resolved


def _load_config(config_path: str | None) -> dict[str, Any]:
    raw = {}
    if config_path:
        try:
            safe_path = _safe_config_path(config_path)
            with open(safe_path, encoding="utf-8") as handle:
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
    side; a ``before`` block decision refuses the call (the child is killed and
    this wrapper exits non-zero after writing a ``MODULO_BRIDGE_BLOCKED:<tool>``
    marker). Best-effort — a bridge failure never blocks the command.
    """
    if not argv:
        _err("error: --wrap requires a command")
        return 2
    shell_cmd = " ".join(argv)
    # NOSONAR: deliberate — configured agent command (pipeline config, not untrusted
    # input) needs shell features; already suppressed for bandit (B603) and ruff (S603).
    try:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 — execs the configured agent command (fixed argv list, shell=False, no user input)  # NOSONAR
            ["/bin/sh", "-c", shell_cmd],
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
            if line.startswith(EVENT_MARKER):
                event_text = line[len(EVENT_MARKER) :].strip()
                try:
                    event = json.loads(event_text)
                except ValueError:
                    event = {}
                if isinstance(event, dict):
                    tool_name = str(event.get("tool_name") or "")
                    raw_args = event.get("args")
                    args = raw_args if isinstance(raw_args, dict) else {}
                    direction = "after" if event.get("direction") == "after" else "before"
                    result_summary = str(event.get("result_summary") or "")
                    if direction == "before":
                        allowed, masked, _ = client.decide_before(tool_name, args)
                        if not allowed:
                            proc.kill()
                            proc.wait()
                            _out(f"{BLOCKED_MARKER}{tool_name}")
                            return 3
                        if masked is not None:
                            _out(f"MODULO_BRIDGE_REDACTED:{json.dumps(masked)}")
                    else:
                        client.notify_after(tool_name, args, result_summary)
                continue
            _out(line)
    finally:
        proc.stdout.close()
    return proc.wait()


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
            # Refuse only on an actual refusal (action=block AND blocked=true);
            # a block_on_guardrail:false downgrade (action=block, blocked=false)
            # is record-only and must NOT exit non-zero.
            blocked = bool(decision.get("blocked"))
            return 0 if decision.get("action") != "block" or not blocked else 3
        _err("error: --notify requires a JSON object")
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
