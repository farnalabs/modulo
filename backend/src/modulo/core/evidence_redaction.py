"""Server-side evidence field-level redaction boundary (FAR-944, chunk 9b §2.2/§2.3).

The boundary is enforced EXCLUSIVELY here (and by the routes that call it) —
the frontend receives only the already-filtered payload and never redacts
anything itself (§2.2 enforcement point).

Two redaction CLASSES group the evidence ``value`` JSON keys that must be
hidden per role. A rule row (``evidence_redaction_rules``) declares, for a
``(role, key_pattern)`` pair, which classes are redacted; the actual key set is
derived from :data:`REDACTION_CLASS_FIELDS` here so the two can never drift.

How it actually works (name-based, not default-deny): a class is a *set of JSON
key names*, and resolution replaces a key's value ONLY when that key's name is
in the resolved class. It is not a schema or a whitelist — a future evidence key
whose name is not listed in a class is NOT redacted, whatever the class set
says. "Fail closed" here means *the classes are redacted by default for an
untrusted role*, never "every field is redacted unless allowlisted".

Fields NOT listed in either class (decision outcome, pass/fail status,
timestamps, run identifiers, ``status``, ...) are NEVER redacted.

Fail-closed default (deliberate deviation from acceptance criterion 3):
criterion 3 says "no matching rule -> no redaction". That holds ONLY for
TRUSTED roles (``admin``/``operator``). For every other role — including the
DEFAULT org role ``runner`` and any future role — a role with no matching rule
is redacted with BOTH classes. Before this change ``runner`` (a valid
``evidence.read`` principal) resolved to an EMPTY class set and saw every
sensitive field; the fallback was a fail-open hole. Any role not explicitly
trusted is now redacted by default, so a newly introduced role leaks nothing
until it is deliberately trusted.
"""

from __future__ import annotations

import fnmatch
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.evidence_redaction_rule import (
    EVIDENCE_REDACTION_CATCH_ALL_PATTERN,
    EvidenceRedactionRule,
)

logger = logging.getLogger(__name__)

#: Sentinel replacing the value of a redacted key (never the whole row).
REDACTED_SENTINEL = "[REDACTED]"

#: The redaction CLASSES (§2.3) and the evidence ``value`` JSON keys each covers.
#:
#: The names are the vocabulary stored in ``evidence_redaction_rules.redacted_fields``
#: (and in the §2.4 catch-all seed) — the mapping to JSON keys lives HERE, in one
#: place, so a rule never needs to enumerate raw field names.
REDACTION_CLASS_FIELDS: dict[str, frozenset[str]] = {
    # Raw LLM judge output, model response text, raw regex match groups, and the
    # prompt/tool payloads a run fed the model (all model-facing content).
    "sensitive_output": frozenset(
        {
            "raw_output",
            "raw",
            "model_response",
            "response_text",
            "reasoning",
            "judge_output",
            "match_groups",
            "matched_text",
            "output_text",
            "chain_of_thought",
            "prompt",
            "tool_args",
        }
    ),
    # Eval configuration details, pipeline-internal node identifiers, snapshot internals.
    "internal_metadata": frozenset(
        {
            "config",
            "eval_config",
            "node_id",
            "snapshot_id",
            "snapshot",
            "pipeline_internals",
        }
    ),
}

#: Roles trusted to see unredacted evidence when no rule targets them. Every
#: other role — ``runner``, ``viewer``, and any future role — fails CLOSED (both
#: classes redacted). See the module docstring.
_TRUSTED_ROLES: frozenset[str] = frozenset({"admin", "operator"})

#: The lowest-priority, match-everything pattern (§2.4 catch-all). Shared with
#: the org-creation seed so the pattern can never drift between the two.
_CATCH_ALL_PATTERN = EVIDENCE_REDACTION_CATCH_ALL_PATTERN


def _default_classes(role: str) -> frozenset[str]:
    """Classes applied when NO rule matches ``role``.

    Fail closed: a trusted role sees everything (empty set); every other role —
    including ``runner`` and any role not yet invented — redacts both classes.
    """
    if role in _TRUSTED_ROLES:
        return frozenset()
    return frozenset(REDACTION_CLASS_FIELDS)


def _redacted_keys(classes: frozenset[str]) -> frozenset[str]:
    """Expand a set of redaction CLASS names into the JSON keys they cover.

    A stored class name that is not in :data:`REDACTION_CLASS_FIELDS` is a
    configuration error (it would silently redact nothing): it is logged and
    skipped rather than failing the whole read.
    """
    keys: set[str] = set()
    for class_name in classes:
        fields = REDACTION_CLASS_FIELDS.get(class_name)
        if fields is None:
            logger.warning("evidence_redaction.unknown_class class=%s", class_name)
            continue
        keys |= fields
    return frozenset(keys)


def apply_redaction(value: Any, classes: frozenset[str]) -> Any:
    """Return a copy of *value* with every class-covered key sentinel-replaced.

    The redaction applies at ANY DEPTH: dict values are recursed into, and lists
    are recursed element-wise, so a class-covered key nested inside another
    object (e.g. ``{"config": {"raw_output": "..."}}``) is replaced too. A
    shallow walk would let a nested key leak.

    A non-dict, non-list value (bool, number, string, ``None``) is returned
    unchanged: a scalar carries no FIELD NAME of its own, so there is no key to
    match against the redaction set — only a mapping key can be classified.

    The input is never mutated — new mappings/lists are built. Whole rows are
    never dropped, and keys outside the resolved classes pass through untouched.
    """
    if not classes:
        # Nothing to redact: return the value as-is (still never mutated).
        return value
    return _redact_node(value, _redacted_keys(classes))


def _redact_node(node: Any, redacted_keys: frozenset[str]) -> Any:
    """Recursively replace class-covered mapping keys, preserving list shape."""
    if isinstance(node, dict):
        return {
            key: (REDACTED_SENTINEL if key in redacted_keys else _redact_node(item, redacted_keys))
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [_redact_node(item, redacted_keys) for item in node]
    return node


async def load_redaction_rules(
    session: AsyncSession,
    organisation_id: uuid.UUID,
) -> list[EvidenceRedactionRule]:
    """Load ALL of an org's live redaction rules in ONE query.

    Callers that resolve redaction for many rows (the evidence list route) load
    once per request and resolve each row from this in-memory list via
    :func:`resolve_redaction_classes_from`, instead of issuing a query per row.
    Soft-deleted rules are excluded by the global soft-delete filter.
    """
    result = await session.execute(
        select(EvidenceRedactionRule).where(
            EvidenceRedactionRule.organisation_id == organisation_id,
        )
    )
    return list(result.scalars().all())


def resolve_redaction_classes_from(
    rules: list[EvidenceRedactionRule],
    role: str,
    key: str,
) -> frozenset[str]:
    """Resolve the redaction CLASSES for ``(role, key)`` from pre-loaded rules.

    Pure — no session. ``rules`` may cover every role; this filters to *role*
    itself. Most-specific-wins: the longest non-``*`` ``key_pattern`` that
    matches *key* (``fnmatch.fnmatchcase`` semantics), else the ``*`` catch-all
    rule for that role, else :func:`_default_classes` (trusted roles: none;
    everyone else: both classes, fail closed).

    Ties between equal-length matching patterns are broken lexicographically —
    rules are ordered by ``(-len(pattern), pattern)`` and the first is taken —
    so an ambiguous rule set resolves deterministically rather than by row order.
    """
    matching: list[EvidenceRedactionRule] = []
    catch_all: EvidenceRedactionRule | None = None
    for rule in rules:
        if rule.role != role:
            continue
        if rule.key_pattern == _CATCH_ALL_PATTERN:
            catch_all = rule
            continue
        if fnmatch.fnmatchcase(key, rule.key_pattern):
            matching.append(rule)

    if matching:
        best = min(matching, key=lambda rule: (-len(rule.key_pattern), rule.key_pattern))
        return frozenset(best.redacted_fields or [])
    if catch_all is not None:
        return frozenset(catch_all.redacted_fields or [])
    return _default_classes(role)
