"""Canonical work-item refs + reserved input-payload keys (FAR-142).

The journey/work-item data model anchors runs to deterministic canonical
work-item ids. This module owns the canonicalisation rules (kind + ref), the
reserved-key set that user input may never forge, and the deterministic
canonical id (uuid5) derivation.

It lives in the db layer so the create-time stamping path
(``modulo.db.crud.run``) and future consumers (FAR-143 self-report parsing,
query lookups) share ONE set of rules — and it must NOT import
``modulo.core.*`` (the ``db-does-not-import-core`` import-linter contract).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

# Keys that a user-supplied ``input_payload`` may NEVER set. System-injected
# data (work-item stamping, feedback-correction context, the FAR-604 queue
# coalesce key) flows through explicit ``create_run`` kwargs — never through
# ``input_payload`` — so a webhook payload or a manual POST /runs body can
# never forge a ``work_item_id`` or a planted ``_coalesce_key`` (which would
# let a later webhook delivery fold into the attacker's run, replacing its
# payload and carrying its stale work-item refs).
# Mirrors ``_RESERVED_RUN_CONTEXT_KEYS`` in
# ``modulo.core.pipeline_engine.decorator``.
_RESERVED_INPUT_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "_work_item_id",
        "_modulo.work_item",
        "_feedback_correction",
        "_coalesce_key",
    }
)

# Fixed v5 namespace for canonical work-item ids. Deliberately NOT
# ``uuid.NAMESPACE_DNS`` so these ids never collide with other uuid5 uses in
# the codebase.
_WORK_ITEM_NAMESPACE = uuid.UUID("b2f3d4a6-0b1c-4f00-9c1e-8c9f2a1b4d6e")

_VALID_SOURCES: frozenset[str] = frozenset({"derived", "reported"})
_VALID_STATUSES: frozenset[str] = frozenset({"done", "attempted"})

# Kinds whose refs may carry a leading '#' and/or GitHub URL prefixes.
_GITHUB_KINDS: frozenset[str] = frozenset({"github", "github_issue", "github_pr"})
# Kinds whose refs are tracker ids (uppercased, spaces/dashes collapsed).
_TRACKER_KINDS: frozenset[str] = frozenset({"linear", "jira"})

_URL_PREFIX_RE = re.compile(r"^https?://(?:www\.)?", re.IGNORECASE)
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/(?:pull|issues|commit)/(\d+)",
    re.IGNORECASE,
)
_GITHUB_OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)$")


def canonicalise_kind(kind: Any) -> str:
    """Normalise a work-item kind to its canonical form.

    Strips surrounding whitespace, lowercases, and collapses inner whitespace
    to a single underscore. A blank kind raises ``ValueError`` (a ref entry
    without a kind is ambiguous).
    """
    if kind is None:
        raise ValueError("work-item kind must not be None")
    k = str(kind).strip().lower()
    if not k:
        raise ValueError("work-item kind must not be empty")
    return re.sub(r"\s+", "_", k)


def _canonicalise_github_ref(raw: str) -> str:
    """Canonical form for a github-family ref.

    * ``https://github.com/owner/repo/pull/123`` → ``owner/repo#123``
    * ``owner/repo#123`` → ``owner/repo#123``
    * ``#123`` / ``123`` → ``123``
    """
    m = _GITHUB_URL_RE.match(raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
    m = _GITHUB_OWNER_REPO_RE.match(raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
    return raw.lstrip("#").strip()


def _canonicalise_tracker_ref(raw: str) -> str:
    """Canonical form for a tracker (linear/jira) ref.

    Strips URL prefixes and leading '#', then uppercases the id with its
    project key. ``far 123`` / ``far-123`` / ``FAR:123`` → ``FAR-123``; a URL
    like ``https://linear.app/acme/issue/FAR-123/xyz`` → ``FAR-123``.
    """
    bare = _URL_PREFIX_RE.sub("", raw).strip()
    bare = bare.lstrip("#").strip()
    m = re.search(r"\b([A-Za-z]{1,12})[\s\-:](\d+)\b", bare)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
    return bare.upper()


def canonicalise_ref(kind: Any, ref: Any) -> str:
    """Normalise a work-item ref to its canonical form for *kind*.

    Per-kind handling:

    * github kinds — strip ``https://github.com/.../{pull|issues|commit}/N``
      URL prefixes, collapse ``#123`` / ``123`` to ``123``, and preserve the
      qualified ``owner/repo#123`` form.
    * linear / jira kinds — strip URL prefixes, uppercase the tracker id.
    * generic kinds — strip a leading ``#`` and surrounding whitespace.
    """
    if ref is None:
        raise ValueError("work-item ref must not be None")
    raw = str(ref).strip()
    if not raw:
        raise ValueError("work-item ref must not be empty")
    k = canonicalise_kind(kind)
    if k in _GITHUB_KINDS:
        return _canonicalise_github_ref(raw)
    if k in _TRACKER_KINDS:
        return _canonicalise_tracker_ref(raw)
    return raw.lstrip("#").strip()


def validate_ref_entry(entry: Any) -> dict[str, Any]:
    """Validate + canonicalise a ref entry dict.

    Returns the canonicalised entry ``{kind, ref, source, status?}``. Raises
    ``ValueError`` for a non-dict entry, a missing/blank kind or ref, or an
    invalid ``source`` / ``status``.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"work-item ref entry must be a dict, got {type(entry).__name__}")
    kind = canonicalise_kind(entry.get("kind"))
    ref = entry.get("ref")
    if ref is None or not str(ref).strip():
        raise ValueError("work-item ref entry 'ref' is required")
    source = entry.get("source", "derived")
    if source not in _VALID_SOURCES:
        raise ValueError(f"work-item ref 'source' must be one of {sorted(_VALID_SOURCES)}, got {source!r}")
    status = entry.get("status")
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"work-item ref 'status' must be one of {sorted(_VALID_STATUSES)}, got {status!r}")
    canonical: dict[str, Any] = {
        "kind": kind,
        "ref": canonicalise_ref(kind, ref),
        "source": source,
    }
    if status is not None:
        canonical["status"] = status
    return canonical


def canonical_work_item_id(org_id: uuid.UUID, kind: Any, ref: Any) -> uuid.UUID:
    """Deterministic canonical journey id — ``uuid5(NAMESPACE, f"{org}:{kind}:{ref}")``.

    The same (org, kind, ref) ALWAYS produces the same id, so the journey
    row's ``canonical_work_item_id`` is derivable at create time and again at
    finalise/query time without mint races or overwrites.
    """
    k = canonicalise_kind(kind)
    r = canonicalise_ref(k, ref)
    return uuid.uuid5(_WORK_ITEM_NAMESPACE, f"{org_id}:{k}:{r}")
