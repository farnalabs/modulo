"""Sanitisation of untrusted sample/example data before it reaches an LLM prompt.

PRD §8.16 requires that sampled records be treated as untrusted input: the
inference prompt uses structural separators and never interpolates raw field
values into instructions. Connector-sampled records can contain secrets
(access tokens, api keys), control characters, or prompt-injection payloads
embedded in user-controlled fields (issue descriptions, PR bodies). This
module defensively scrubs records before serialisation:

- Sensitive-keyed string values are masked (never forwarded to the model).
- Control characters are stripped from string values.
- String values are capped in length and arrays in cardinality so a single
  pathological record cannot blow up the prompt.
- Nesting depth is bounded; deep structures are truncated.

The returned records are a deep copy — the caller's data is never mutated.
"""

import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_VALUE_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022"

_SENSITIVE_SEGMENTS = frozenset({"token", "secret", "password", "passwd", "credential"})

_SENSITIVE_SUFFIXES = (
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "client_secret",
    "authorization",
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_MAX_STRING_LENGTH = 2000
_MAX_LIST_LENGTH = 100
_MAX_DEPTH = 8

_SAMPLE_BLOCK_START = "<<<SAMPLE_DATA>>>"
_SAMPLE_BLOCK_END = "<<<END_SAMPLE_DATA>>>"


def is_sensitive_key(key: str) -> bool:
    """Return True when a field name looks like it carries a credential.

    Matching is segment/suffix-based (never bare substring), so legitimate
    inference signal is preserved: ``monkey``, ``author``, and ``key_name``
    are not flagged, while ``access_token``, ``api_key``, and
    ``client_secret`` are.
    """
    normalized = key.lower().replace("-", "_").replace(" ", "_").strip("_")
    segments = [s for s in normalized.split("_") if s]
    if any(segment in _SENSITIVE_SEGMENTS for segment in segments):
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _sanitise_value(value: Any, key: str, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, str):
        cleaned = _CONTROL_RE.sub("", value)[:_MAX_STRING_LENGTH]
        if is_sensitive_key(key):
            return SENSITIVE_VALUE_MASK
        return cleaned
    if isinstance(value, Mapping):
        return {k: _sanitise_value(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitise_value(v, key, depth + 1) for v in value[:_MAX_LIST_LENGTH]]
    if isinstance(value, tuple):
        return tuple(_sanitise_value(v, key, depth + 1) for v in value[:_MAX_LIST_LENGTH])
    return value


def sanitise_sample_records(records: Any) -> Any:
    """Return a deep, defensive copy of records with secrets masked.

    The input records are never mutated. If the input is not a list it is
    passed through unchanged so the caller's validation still governs shape.
    """
    if not isinstance(records, list):
        return records
    return [_sanitise_value(record, "", 0) for record in records]
