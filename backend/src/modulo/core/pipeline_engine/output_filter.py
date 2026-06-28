"""Output filter — re-filter LLM output for injection before connector writes.

Defence-in-depth: the output should already be safe from earlier eval stages,
but we verify once more before external write operations.
"""

import re
from typing import Any

from modulo.connectors.base import ConnectorPayload

# Patterns indicating prompt injection attempts in LLM output.
_SYSTEM_PROMPT_OVERRIDE = re.compile(
    r"(?i)\b("
    r"ignore\s+(all\s+)?previous\s+(instructions|prompts|directions)"
    r"|forget\s+(all\s+)?(your\s+)?(instructions|prompts|directions)"
    r"|ignore\s+(all\s+)?above"
    r"|you\s+are\s+now\b"
    r"|new\s+instructions?"
    r"|override\s+(system\s+)?(prompt|instructions)"
    r"|disregard\s+(all\s+)?previous"
    r")\b"
)

_SUSPICIOUS_EXECUTION = re.compile(
    r"(?i)\b("
    r"eval\s*\("
    r"|exec\s*\("
    r"|os\.system\s*\("
    r"|subprocess\.(?:call|run|Popen)\s*\("
    r"|__import__\s*\("
    r"|import\s+(?:os|subprocess|shutil|socket)\b"
    r"|compile\s*\("
    r")"
)

_SECRETS_ACCESS = re.compile(
    r"(?i)\b("
    r"os\.environ\b"
    r"|os\.getenv\s*\("
    r"|process\.env\b"
    r"|process\.argv\b"
    r"|environ\["
    r"|getenv\s*\("
    r"|environ\.get\s*\("
    r")"
)


class OutputRejectedError(RuntimeError):
    """Raised when LLM output is rejected before a connector write."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Output rejected before connector write: {reason}")
        self.reason = reason


class OutputFilterResult:
    """Result of an output filter check."""

    def __init__(self, passed: bool, reason: str | None = None) -> None:
        self.passed = passed
        self.reason = reason


def filter_output_for_injection(output: str) -> OutputFilterResult:
    """Check *output* for prompt injection indicators.

    Returns OutputFilterResult(passed=True) if the output is clean.
    Returns OutputFilterResult(passed=False, reason=...) if injection is suspected.
    """
    if _SYSTEM_PROMPT_OVERRIDE.search(output):
        return OutputFilterResult(
            passed=False,
            reason="System prompt override attempt detected",
        )

    if _SUSPICIOUS_EXECUTION.search(output):
        return OutputFilterResult(
            passed=False,
            reason="Suspicious code execution pattern detected",
        )

    if _SECRETS_ACCESS.search(output):
        return OutputFilterResult(
            passed=False,
            reason="Environment variable or secrets access detected",
        )

    return OutputFilterResult(passed=True)


def _string_values(payload: ConnectorPayload) -> list[str]:
    """Extract all string values recursively from a payload's data dict."""
    values: list[str] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, str):
            values.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(payload.data)
    return values


def filter_payload_for_injection(payload: ConnectorPayload) -> None:
    """Check *payload* for injection patterns. Raises OutputRejectedError if blocked."""
    for value in _string_values(payload):
        result = filter_output_for_injection(value)
        if not result.passed:
            raise OutputRejectedError(
                f"{result.reason} (resource: {payload.resource!r})"
            )
