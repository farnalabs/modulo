"""Sanitisation of untrusted sample/example data before it reaches an LLM prompt.

PRD §8.16 requires that sampled records be treated as untrusted input: the
inference prompt uses structural separators and never interpolates raw field
values into instructions. Connector-sampled records can contain secrets
(access tokens, api keys), control characters, or prompt-injection payloads
embedded in user-controlled fields (issue descriptions, PR bodies). This
module defensively scrubs records before serialisation:

- Sensitive-keyed values are masked (never forwarded to the model): string
  values and non-string scalars under a sensitive key are replaced with a
  mask, and the contents of any list/dict under a sensitive key are masked
  regardless of the nested key names.
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


def _singular(word: str) -> str:
    """Return the singular form of a simple English plural, best-effort."""
    if len(word) > 1 and word.endswith("s"):
        return word[:-1]
    return word


def is_sensitive_key(key: str) -> bool:
    """Return True when a field name looks like it carries a credential.

    Matching is segment/suffix-based (never bare substring), and both segment
    and suffix comparisons use the singular form so plural/collection names
    (``tokens``, ``api_keys``, ``passwords``, ``secrets``) are flagged too.
    Legitimate inference signal is preserved: ``monkey``, ``author``, and
    ``key_name`` are not flagged, while ``access_token``, ``api_key``, and
    ``client_secret`` are.
    """
    normalized = key.lower().replace("-", "_").replace(" ", "_").strip("_")
    segments = [s for s in normalized.split("_") if s]
    if any(_singular(segment) in _SENSITIVE_SEGMENTS for segment in segments):
        return True
    return any(_singular(normalized).endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _sanitise_value(value: Any, key: str, depth: int) -> Any:
    if depth >= _MAX_DEPTH:
        return None
    if is_sensitive_key(key):
        # Any value under a credential-like key is masked. Containers have
        # their contents masked regardless of the nested key name so a secret
        # cannot leak via a sibling key ({"token": {"value": "..."}}) or a
        # plural/collection field ({"tokens": ["tok1", "tok2"]}).
        if isinstance(value, str):
            return SENSITIVE_VALUE_MASK
        if isinstance(value, Mapping):
            return dict.fromkeys(value, SENSITIVE_VALUE_MASK)
        if isinstance(value, tuple):
            return tuple(SENSITIVE_VALUE_MASK for _ in value[:_MAX_LIST_LENGTH])
        if isinstance(value, list):
            return [SENSITIVE_VALUE_MASK for _ in value[:_MAX_LIST_LENGTH]]
        return SENSITIVE_VALUE_MASK
    if isinstance(value, str):
        return _CONTROL_RE.sub("", value)[:_MAX_STRING_LENGTH]
    if isinstance(value, Mapping):
        return {k: _sanitise_value(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitise_value(v, key, depth + 1) for v in value[:_MAX_LIST_LENGTH]]
    if isinstance(value, tuple):
        return tuple(_sanitise_value(v, key, depth + 1) for v in value[:_MAX_LIST_LENGTH])
    return value


def _escape_block_markers(text: str) -> str:
    """Escape structural-separator markers embedded in sample data.

    Untrusted sample values can legitimately contain the delimiter strings.
    Backslash-escaping them before the block is wrapped prevents an injected
    marker from terminating the sample-data block early (prompt-injection
    hardening, defence in depth on top of the system-prompt instruction).
    """
    return text.replace(_SAMPLE_BLOCK_END, "\\" + _SAMPLE_BLOCK_END).replace(
        _SAMPLE_BLOCK_START, "\\" + _SAMPLE_BLOCK_START
    )


def sanitise_sample_records(records: Any) -> Any:
    """Return a deep, defensive copy of records with secrets masked.

    The input records are never mutated. If the input is not a list it is
    passed through unchanged so the caller's validation still governs shape.
    """
    if not isinstance(records, list):
        return records
    return [_sanitise_value(record, "", 0) for record in records]
