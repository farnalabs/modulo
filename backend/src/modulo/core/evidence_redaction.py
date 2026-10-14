"""Server-side evidence field-level redaction boundary (FAR-944, chunk 9b §2.2/§2.3).

The boundary is enforced EXCLUSIVELY here (and by the routes that call it) —
the frontend receives only the already-filtered payload and never redacts
anything itself (§2.2 enforcement point).

Two redaction CLASSES group the evidence ``value`` JSON keys that must be
hidden per role. A rule row (``evidence_redaction_rules``) declares, for a
``(role, key_pattern)`` pair, which classes are redacted; the actual key set is
derived from :data:`REDACTION_CLASS_FIELDS` here so the two can never drift.

Fields NOT listed in either class (decision outcome, pass/fail status,
timestamps, run identifiers, ``status``, ...) are NEVER redacted.
"""

from __future__ import annotations

import fnmatch
import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.evidence_redaction_rule import EvidenceRedactionRule

#: Sentinel replacing the value of a redacted key (never the whole row).
REDACTED_SENTINEL = "[REDACTED]"

#: The redaction CLASSES (§2.3) and the evidence ``value`` JSON keys each covers.
#:
#: The names are the vocabulary stored in ``evidence_redaction_rules.redacted_fields``
#: (and in the §2.4 catch-all seed) — the mapping to JSON keys lives HERE, in one
#: place, so a rule never needs to enumerate raw field names.
REDACTION_CLASS_FIELDS: dict[str, frozenset[str]] = {
    # Raw LLM judge output, model response text, raw regex match groups.
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

#: Built-in fallback when an organisation has NO matching rule at all. The §2.4
#: catch-all seed normally supplies the viewer entry; this default keeps the
#: same safety property for orgs whose catch-all row is absent (e.g. an org
#: created after the seed migration) or for a rules-less backend.
_DEFAULT_CLASSES_BY_ROLE: dict[str, frozenset[str]] = {
    "viewer": frozenset({"sensitive_output", "internal_metadata"}),
}

#: The lowest-priority, match-everything pattern (§2.4 catch-all).
_CATCH_ALL_PATTERN = "*"


def _redacted_keys(classes: frozenset[str]) -> frozenset[str]:
    """Expand a set of redaction CLASS names into the JSON keys they cover."""
    keys: set[str] = set()
    for class_name in classes:
        keys |= REDACTION_CLASS_FIELDS.get(class_name, frozenset())
    return frozenset(keys)


def apply_redaction(value: Any, role: str, classes: frozenset[str]) -> dict[str, Any] | None:
    """Return a copy of *value* with every class-covered key sentinel-replaced.

    A shallow copy is returned so the stored row is never mutated. Whole rows
    are never dropped, and keys outside the resolved classes are passed through
    untouched (no blanket redaction). A ``None`` or non-dict *value* (evidence
    values may be booleans or null) is returned unchanged.
    """
    if not isinstance(value, dict):
        # evidence values may be booleans or null (see Evidence.value) — only a
        # JSON object can carry redactable fields, so anything else is a passthrough.
        return cast("dict[str, Any] | None", value)
    redacted_keys = _redacted_keys(classes)
    return {key: (REDACTED_SENTINEL if key in redacted_keys else item) for key, item in value.items()}


async def resolve_redaction_classes(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    role: str,
    key: str,
) -> frozenset[str]:
    """Resolve the redaction CLASSES that apply to ``(role, key)`` for one org.

    Most-specific-wins: the longest non-``*`` ``key_pattern`` that matches *key*
    (``fnmatch.fnmatchcase`` semantics), else the ``*`` catch-all rule for that
    role, else the built-in default (both classes for ``viewer``; none for other
    roles — operators/admins see everything until a rule targets them).

    Soft-deleted rules are excluded by the global soft-delete filter.
    """
    result = await session.execute(
        select(EvidenceRedactionRule).where(
            EvidenceRedactionRule.organisation_id == organisation_id,
            EvidenceRedactionRule.role == role,
        )
    )
    rules = list(result.scalars().all())

    best: EvidenceRedactionRule | None = None
    best_len = -1
    catch_all: EvidenceRedactionRule | None = None
    for rule in rules:
        if rule.key_pattern == _CATCH_ALL_PATTERN:
            catch_all = rule
            continue
        if fnmatch.fnmatchcase(key, rule.key_pattern) and len(rule.key_pattern) > best_len:
            best = rule
            best_len = len(rule.key_pattern)

    matched = best if best is not None else catch_all
    if matched is None:
        return _DEFAULT_CLASSES_BY_ROLE.get(role, frozenset())
    return frozenset(matched.redacted_fields or [])
