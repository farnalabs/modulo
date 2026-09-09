"""FAR-734: classify model-backend failures from retained sandbox stdout.

Best-effort pattern matching on the terminal JSONL event stream captured in
retained agent stdout.  Scans for ``"type":"error"`` lines carrying known
provider-error signatures (UnknownError/timeout, APIError/Model-is-disabled,
connection, rate-limit) and returns a first-class error code.

Trust boundary: this is a BEST-EFFORT classifier — when no signature matches,
callers fail open to the existing generic classification.  Redaction rules
apply: this module NEVER reads or persists credential-bearing stdout content;
it receives already-sanitised strings (the exception message or the retained
raw_output marker content).

The canonical codes live in :mod:`error_codes` (ERROR_CODE_REGISTRY + LEGACY_ALIASES).
This module owns ONLY the signature→code mapping; it does not depend on DB,
settings, or any heavy framework code so unit tests stay fast.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Signature patterns → canonical error codes
# ---------------------------------------------------------------------------
# Each entry is (compiled_regex, error_code).  Patterns are applied in order;
# the first match wins.  All patterns target the JSONL ``"type":"error"``
# event stream that the agent runtime emits to stdout.

_PROVIDER_TIMEOUT_PATTERN = re.compile(
    r'"type"\s*:\s*"error".*?"name"\s*:\s*"UnknownError".*?"message"\s*:\s*'
    r'"The operation timed out\."',
    re.DOTALL,
)

_MODEL_DISABLED_PATTERN = re.compile(
    r'"type"\s*:\s*"error".*?"name"\s*:\s*"APIError".*?'
    r'"message"\s*:\s*"Model is disabled".*?"statusCode"\s*:\s*401',
    re.DOTALL,
)

_CONNECTION_ERROR_PATTERN = re.compile(
    r'"type"\s*:\s*"error".*?"name"\s*:\s*"'
    r'(?:APIConnectionError|ConnectionError|NetworkError|ETIMEDOUT|ECONNREFUSED|ECONNRESET)"',
    re.DOTALL,
)

_RATE_LIMIT_PATTERN = re.compile(
    r'"type"\s*:\s*"error".*?(?:'
    r'"name"\s*:\s*"(?:RateLimitError|RateLimitExceededError|TooManyRequests)"'
    r'|"statusCode"\s*:\s*429)',
    re.DOTALL,
)

# Ordered list: first match wins.
_SIGNATURE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_PROVIDER_TIMEOUT_PATTERN, "model.provider_timeout"),
    (_MODEL_DISABLED_PATTERN, "model_disabled"),
    (_CONNECTION_ERROR_PATTERN, "model.connection"),
    (_RATE_LIMIT_PATTERN, "model.rate_limited"),
]


def classify_provider_error(text: str) -> str | None:
    """Scan *text* for a terminal provider-error signature in the JSONL event stream.

    Returns the canonical error code (e.g. ``"model.provider_timeout"``) when a
    signature matches, or ``None`` when no signature is found — callers fail open
    to the existing generic classification.

    The input is expected to be an already-sanitised string (the exception
    message, the retained raw_output content, or any other source that may
    contain the terminal JSONL ``"type":"error"`` lines).  The function is
    intentionally stateless and side-effect-free.
    """
    if not text:
        return None
    for pattern, code in _SIGNATURE_PATTERNS:
        if pattern.search(text):
            return code
    return None
