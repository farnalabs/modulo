"""Typed runtime-source coverage contract for loop interception."""

from __future__ import annotations

import re
import shlex
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LoopCoverageState(StrEnum):
    ENFORCED = "enforced"
    DEGRADED = "degraded"
    NOT_APPLICABLE = "not_applicable"


class LoopCoverageProvenance(StrEnum):
    OBSERVED = "observed"
    DERIVED = "derived"


class LoopCoverage(BaseModel):
    """Persisted and API-visible loop interception coverage."""

    model_config = ConfigDict(extra="forbid")

    state: LoopCoverageState
    reason: str = Field(min_length=1)
    provenance: LoopCoverageProvenance


class RuntimeToolEvent(BaseModel):
    """The authenticated shape of a sandbox bridge tool event."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    args: dict[str, Any]
    direction: Literal["before", "after"]
    result_summary: str = ""


def parse_loop_coverage(raw: Any) -> LoopCoverage | None:
    """Validate stored coverage, returning ``None`` for a legacy NULL value."""
    if raw is None:
        return None
    try:
        return LoopCoverage.model_validate(raw)
    except ValidationError:
        return LoopCoverage(
            state=LoopCoverageState.DEGRADED,
            reason="persisted_loop_coverage_invalid",
            provenance=LoopCoverageProvenance.DERIVED,
        )


def loop_intercept_is_enabled(raw: Any) -> bool:
    """Return whether a frozen node config actually enables interception."""
    return isinstance(raw, dict) and raw.get("enabled", True) is True


def initial_loop_coverage(*, configured: bool) -> LoopCoverage:
    """Return the honest pre-event state for a configured runtime source."""
    if configured:
        return LoopCoverage(
            state=LoopCoverageState.DEGRADED,
            reason="runtime_tool_event_source_unobserved",
            provenance=LoopCoverageProvenance.DERIVED,
        )
    return LoopCoverage(
        state=LoopCoverageState.NOT_APPLICABLE,
        reason="no_runtime_loop_guardrail_bound",
        provenance=LoopCoverageProvenance.DERIVED,
    )


_OPENCODE_EXECUTABLE = re.compile(r"(?i)(?:^|[\\/])opencode(?:\.exe)?$")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SHELL_SEPARATORS = ("&&", "||", ";", "\n")


def _shell_segments(command: str) -> list[str]:
    """Split a shell command line on top-level ``&&`` / ``||`` / ``;`` / newlines."""
    segments: list[str] = []
    start = 0
    index = 0
    quote = ""
    while index < len(command):
        char = command[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        separator = next((item for item in _SHELL_SEPARATORS if command.startswith(item, index)), None)
        if separator is not None:
            segments.append(command[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    segments.append(command[start:])
    return [segment for segment in segments if segment.strip()]


def _segment_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return []
    return [token.strip("\\\"'") for token in tokens]


def _is_opencode_executable(token: str) -> bool:
    normalized = token.replace("\\\\", "/")
    return bool(_OPENCODE_EXECUTABLE.search(normalized)) or normalized.casefold() in {"opencode", "opencode.exe"}


def _executed_command(tokens: list[str]) -> list[str]:
    """Strip the supported wrappers so the first remaining token is the command."""
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index].casefold() == "timeout":
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
        index += 1  # the timeout duration
    return tokens[index:]


def runtime_supports_tool_events(command: str) -> bool:
    """Return whether a shell command in the dispatch directly invokes OpenCode.

    Production dispatch joins ``agent_commands`` with ``&&`` and renders
    environment assignments before the runtime binary, so each shell command is
    inspected on its own. A token appearing in another program's arguments is
    never treated as the runtime: only the executed command counts.
    """
    for segment in _shell_segments(command):
        tokens = _executed_command(_segment_tokens(segment))
        if len(tokens) >= 2 and _is_opencode_executable(tokens[0]) and tokens[1].casefold() == "run":
            return True
    return False


def bridge_plugin_source() -> str:
    """Return the stdlib-independent opencode plugin installed in a sandbox.

    Opencode invokes the default export as a plugin factory. The hooks call the
    host callback directly, so production tool events do not depend on an agent
    printing a forged ``MODULO_BRIDGE_EVENT`` line.
    """
    return """const endpoint = () => process.env.MODULO_BRIDGE_ENDPOINT || "";

function persistenceFailure(value) {
  return value && typeof value === "object" && value.reason === "coverage_persistence_failed";
}

async function decide(event) {
  const base = endpoint();
  if (!base) return { action: "pass", blocked: false, reason: "bridge_endpoint_missing" };
  try {
    const response = await fetch(`${base}/intercept`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(event),
    });
    const value = await response.json();
    if (persistenceFailure(value)) {
      return { action: "block", blocked: true, reason: "coverage_persistence_failed" };
    }
    return value && typeof value === "object"
      ? value
      : { action: "pass", blocked: false, reason: "bridge_invalid_response" };
  } catch (error) {
    return {
      action: "pass",
      blocked: false,
      reason: `bridge_error:${error?.constructor?.name || "Error"}`,
    };
  }
}

function toolName(input) {
  return String(input?.tool || input?.name || input?.toolName || "");
}

function toolArgs(input) {
  return input?.args && typeof input.args === "object" ? input.args : {};
}

function resultSummary(output) {
  if (typeof output === "string") return output.slice(0, 1000);
  try { return JSON.stringify(output).slice(0, 1000); } catch { return ""; }
}

export default function moduloBridgePlugin() {
  return {
    "tool.execute.before": async (input) => {
      const decision = await decide({
        tool_name: toolName(input),
        args: toolArgs(input),
        direction: "before",
        result_summary: "",
      });
      if (decision.action === "redact" && decision.masked_args && typeof decision.masked_args === "object") {
        return { args: decision.masked_args };
      }
      if ((decision.action === "block" && decision.blocked) || decision.action === "escalate") {
        const reason = decision.reason || "unspecified";
        throw new Error(`Modulo loop guardrail ${decision.action} refused tool ${toolName(input)}: ${reason}`);
      }
      return;
    },
    "tool.execute.after": async (input, output) => {
      await decide({
        tool_name: toolName(input),
        args: toolArgs(input),
        direction: "after",
        result_summary: resultSummary(output),
      });
    },
  };
}
"""


__all__ = [
    "LoopCoverage",
    "LoopCoverageProvenance",
    "LoopCoverageState",
    "RuntimeToolEvent",
    "bridge_plugin_source",
    "initial_loop_coverage",
    "loop_intercept_is_enabled",
    "parse_loop_coverage",
    "runtime_supports_tool_events",
]
