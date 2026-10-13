"""Destroy-redaction mode — HMAC pepper + candidate generation (FAR-209 Track B).

The ``destroy`` redaction mode (``FieldRedactionMode.DESTROY``) replaces a
guardrail's violating content with a deterministic, key-derived HMAC token
instead of the fixed masks-only token. Unlike the masks-only modes, a destroy
redaction produces no unkeyed token — the ONLY way a column of destroy tokens
can be interpreted is with the deployment's own pepper, so the token carries
no information about the destroyed content (full SHA-256 hex under a secret
key).

Candidate generation (D1 in the Track B design):
* A string leaf's candidates are the matches of the guardrail DETECTION
  pattern inside the leaf — per-match replacement, so the untouched
  surroundings of each match stay readable (what "destroy candidates" means);
* No pattern match resolution (e.g. no detection pattern) → the WHOLE leaf
  value is one candidate, so a firing destroy policy always destroys
  something (never a silent no-act);
* A non-string leaf (list / dict / number) is JSON-serialized into one
  candidate — never naively ``str()``-cast, which would produce
  reconstruction-fragile candidate text for composite values.

Fail-closed (redaction security): applying a ``destroy`` policy without a
configured pepper raises :class:`GuardrailRedactionMechanismError`, which the
calling seam treats like any other guardrail mechanism error — fail CLOSED
for block/redact guardrails, never a silent masks-only fallback and never an
unredacted persist.

The pepper live-wires from the organisation settings
(``modulo.settings.get_settings().guardrail_destroy_pepper``) at apply time —
rotating the pepper breaks cross-run linkage of previously destroyed values
(documented in ``docs/product-map/configure/guardrails.md``); callers that
need a fixed pepper for tests pass it explicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from modulo.settings import get_settings

DESTROY_TOKEN_PREFIX = "hmac-sha256:"


class GuardrailRedactionMechanismError(Exception):
    """A destroy-mode redaction could not be applied — callers fail closed.

    Raised when a ``destroy`` policy fires and the deployment pepper is
    missing/unset, or the policy's candidate pattern is not a usable regex.
    Deliberately NOT a :class:`~modulo.core.guardrails.GuardrailBlockedError`
    subclass: the calling seam's mechanism-error handler (which merges any
    redaction/destroy failure into the fail-closed ``blocked`` decision) is
    the intended destination — never the in-phase block merge.
    """


def resolve_guardrail_destroy_pepper(explicit: str | None = None) -> str:
    """Return the configured destroy pepper.

    An explicit non-empty argument wins (tests / explicit callers); otherwise
    the lazily-loaded org settings value. Never raises for an EMPTY pepper —
    the applier decides fail-closed (a pass with no destroy-mode policy must
    not require the pepper at all).
    """
    if explicit:
        return explicit
    try:
        return get_settings().guardrail_destroy_pepper or ""
    except Exception:  # pragma: no cover - settings load failure is environmental
        return ""


def destroy_token(candidate: str, pepper: str) -> str:
    """Compute one deterministic HMAC-SHA256 destroy token for *candidate*.

    Deterministic over (candidate, pepper): the same destroyed value always
    produces the same token, so an investigation can replay-tokenize a claim
    and confirm a match WITHOUT the pepper being recoverable from the token.
    Full 64-hex digest — no truncation (prefix truncation reduces the token's
    distinguishing power but the dictionary surface is identical; the full
    digest keeps recomputation exact).
    """
    digest = hmac.new(pepper.encode("utf-8"), candidate.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{DESTROY_TOKEN_PREFIX}{digest}"


def destroy_candidates(value: Any, pattern: str | None) -> list[str]:
    """Generate the destroy candidates for a leaf value.

    Returns the candidate STRINGS whose tokens will replace the destroying
    span in the value:

    * ``pattern`` given → every regex match inside a string leaf is one
      candidate (per-match destroy; the untouched surroundings keep
      readability). A pattern that matches nothing yields NO candidates from
      this branch;
    * No pattern (or no match inside the leaf yet the policy still fires) →
      the whole value is ONE candidate: string leaves use the string
      verbatim; anything else uses its compact JSON serialization;
    * A non-string leaf with a pattern also falls through to the whole-value
      candidate — a regex never matches inside a non-string leaf.
    """
    if isinstance(value, str) and pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise GuardrailRedactionMechanismError(
                f"destroy candidate pattern {pattern!r} is not a usable regex: {exc}"
            ) from None
        candidates = [match.group(0) for match in compiled.finditer(value)]
        if candidates:
            return candidates
    # Whole-value candidate — string verbatim, others compact JSON.
    if isinstance(value, str):
        return [value]
    return [json.dumps(value, sort_keys=True, default=str)]


__all__ = [
    "DESTROY_TOKEN_PREFIX",
    "GuardrailRedactionMechanismError",
    "destroy_candidates",
    "destroy_token",
    "resolve_guardrail_destroy_pepper",
]
