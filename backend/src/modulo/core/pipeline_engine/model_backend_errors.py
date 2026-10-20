"""FAR-734: classify model-backend failures from retained sandbox stdout.

Best-effort pattern matching on the terminal JSONL event stream captured in
retained agent stdout.  Scans for ``"type":"error"`` lines carrying known
provider-error signatures (insufficient funds / HTTP 402, UnknownError/timeout,
APIError/Model-is-disabled, provider auth 401, provider billing 402, connection,
rate-limit) and returns a first-class error code.  The 401/402 signatures are
also matched in a plain stderr/stdout line (the gateway echo shape measured in
FAR-2000) because the runtime does not always wrap the refusal in a JSONL error
event.

Vocabulary: a JSONL ``"type":"error"`` event carrying ``"statusCode":402`` is
``model.insufficient_funds`` (the more specific code, matched FIRST — see the
ordering note below); the provider-class ``provider.billing`` /
``provider.authentication`` codes cover the plain-line gateway echo and the
message-only spellings that carry no 402/401 status code.

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

from modulo.core.pipeline_engine.error_codes import map_legacy_code

# ---------------------------------------------------------------------------
# Signature patterns → canonical error codes
# ---------------------------------------------------------------------------
# Each entry is (compiled_regex, error_code).  Patterns are applied in order;
# the first match wins.  All patterns target the JSONL ``"type":"error"``
# event stream that the agent runtime emits to stdout.
#
# ORDERING: ``_INSUFFICIENT_FUNDS_PATTERN`` is deliberately FIRST.  A billing
# fault is a hard blocker — every retry re-hits the same wall — so when a
# stdout stream carries BOTH a transient signature (timeout / connection) and a
# funds refusal, the funds refusal is the outcome that must be surfaced.
# ``_MODEL_DISABLED_PATTERN`` follows the timeout pattern so a 401 carrying
# "Model is disabled" keeps its own existing code rather than being read as an
# auth refusal; the FAR-2006 ``provider.billing`` / ``provider.authentication``
# patterns sit before the connection / rate-limit ones for the same
# hard-blocker-first reason (neither can succeed on a retry).

# The OpenCode Go gateway answers HTTP 402 "Insufficient account funds" when the
# provider account balance is exhausted.  Captured verbatim from production
# retained stdout::
#
#   {"type":"error",...,"error":{"name":"APIError","data":{
#     "message":"Upstream request failed: Insufficient account funds",
#     "statusCode":402,"isRetryable":false,
#     "metadata":{"url":"https://opencode.ai/inference/go/openai/v1/chat/completions"}}}}
#
# Tolerates EITHER the numeric status code OR the message text (case-insensitive)
# so a variant payload that reports only one of the two still classifies.
#
# SINGLE-LINE BOUND: the scan is over a JSONL stream, and one event is exactly
# one line.  The gap uses ``[^\n]*?`` (not ``.*?``) so a ``"type":"error"``
# event can never be joined to a DIFFERENT line's ``"statusCode":402`` — an
# unrelated later event must not be read as this event's billing refusal.  The
# ``(?!\d)`` after 402 stops a longer code (e.g. ``4021``) from matching.
_INSUFFICIENT_FUNDS_PATTERN = re.compile(
    r'"type"\s*:\s*"error"[^\n]*?(?:'
    r'"statusCode"\s*:\s*402(?!\d)'
    r'|"message"\s*:\s*"[^\n"]*?insufficient\s+account\s+funds)',
    re.IGNORECASE,
)

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

# FAR-2006: provider credential / funding refusals. These are NOT transient —
# a 401 (expired/invalid key) and a 402 (unfunded account / exhausted quota)
# can never succeed on a retry, so they must be classified as their own
# terminal codes instead of the retryable ``sandbox.no_output_json`` the
# sandbox path fell back to. The plain stderr/stdout line the gateway echo
# produced in the FAR-2000 measurement (``HTTP 401 "This API key has expired"``
# / ``HTTP 402 "Insufficient account funds"``) and the message-only spellings
# are matched here; a JSONL ``"type":"error"`` event carrying a 402 status code
# is owned by the earlier, more specific ``_INSUFFICIENT_FUNDS_PATTERN``. The
# 402 clause here is deliberately NOT ``"type":"error".*?"statusCode":402``:
# that shape is ``model.insufficient_funds``, and a DOTALL join would also
# violate the single-line bound that pattern guards.
# ``_MODEL_DISABLED_PATTERN`` stays ahead: a 401 carrying "Model is disabled"
# is its own existing code, not an auth failure.
_PROVIDER_BILLING_PATTERN = re.compile(
    r"HTTP\s+402\b[^\n]{0,160}?(?:funds|quota|billing|credit|payment|balance)"
    r'|"(?:message|error)"\s*:\s*"[^"]*'
    r"(?:Insufficient\s+(?:account\s+funds|funds|quota|credits?)"
    r"|insufficient_quota"
    r"|credit\s+balance\s+is\s+too\s+low"
    r"|payment\s+required)",
    re.IGNORECASE | re.DOTALL,
)

_PROVIDER_AUTH_PATTERN = re.compile(
    r'"type"\s*:\s*"error".*?"statusCode"\s*:\s*401'
    r"|HTTP\s+401\b[^\n]{0,160}?(?:API\s*key|Unauthorized|authentication|invalid)"
    r'|"(?:message|error)"\s*:\s*"[^"]*'
    r"(?:API\s*key\s+(?:has\s+expired|is\s+expired|is\s+invalid|is\s+revoked)"
    r"|Incorrect\s+API\s*key"
    r"|invalid[_ -]?api[_ -]?key)",
    re.IGNORECASE | re.DOTALL,
)

# Ordered list: first match wins.
_SIGNATURE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_INSUFFICIENT_FUNDS_PATTERN, "model.insufficient_funds"),
    (_PROVIDER_TIMEOUT_PATTERN, "model.provider_timeout"),
    (_MODEL_DISABLED_PATTERN, "model_disabled"),
    (_PROVIDER_BILLING_PATTERN, "provider.billing"),
    (_PROVIDER_AUTH_PATTERN, "provider.authentication"),
    (_CONNECTION_ERROR_PATTERN, "model.connection"),
    (_RATE_LIMIT_PATTERN, "model.rate_limited"),
]

#: Provider-REFUSAL codes that can never succeed on a retry. Used by BOTH retry
#: paths (the executor's run-level transient retry and the node-level runtime
#: retry) and by the executor's ``failure`` retry-policy matcher — one
#: definition, no drift between the three consumers.
#:
#: Includes ``model.insufficient_funds``: a JSONL ``"statusCode":402`` event is
#: classified as that (more specific) code rather than ``provider.billing``, and
#: it is exactly as permanent — a 402 refusal must still be excluded from every
#: retry path and still raise the ``provider_failure`` alert.
NON_RETRYABLE_PROVIDER_CODES: frozenset[str] = frozenset(
    {
        "provider.authentication",
        "provider.billing",
        "model.insufficient_funds",
    }
)


def classify_provider_error(text: str | None) -> str | None:
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


def non_retryable_provider_code(text: str | None) -> str | None:
    """Return the canonical provider code when *text* is a NON-retryable refusal.

    A 401/402 provider refusal is permanent — re-dispatching the run or
    re-executing the node can never succeed, it only burns a slot and ~10
    minutes per attempt (FAR-2006). Returns the canonical dotted code
    (``provider.authentication`` / ``provider.billing``) or ``None`` when the
    text carries no such signature (callers keep their existing retry
    behaviour).

    The value is canonicalized through :func:`map_legacy_code` so a legacy
    spelling can never be mistaken for a retryable code.
    """
    code = classify_provider_error(text)
    if code is None:
        return None
    canonical = map_legacy_code(code)
    return canonical if canonical in NON_RETRYABLE_PROVIDER_CODES else None
