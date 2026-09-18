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

import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

# Keys that a user-supplied ``input_payload`` may NEVER set. System-injected
# data (work-item stamping, feedback-correction context, the FAR-604 queue
# coalesce key) flows through explicit ``create_run`` kwargs — never through
# ``input_payload`` — so a webhook payload or a manual POST /runs body can
# never forge a ``work_item_id`` or a planted ``_coalesce_key`` (which would
# let a later webhook delivery fold into the attacker's run, replacing its
# payload and carrying its stale work-item refs).
#
# ``_work_item_refs`` (FAR-794 slice 2a) is the system-managed carrier for the
# run's canonical work-item refs inside ``input_payload``. It is reserved (a
# trigger ``payload_mapping`` can never target it, mirroring the other keys)
# but EXEMPT from the pre-hash strip in ``create_run``: refs must survive the
# coalesce identity, and provenance is ENGINE-ASSIGNED (the wire ``source``
# value is never trusted — ``create_run`` re-stamps every entry by channel),
# so a forged key cannot escalate provenance.
# Mirrors ``_RESERVED_RUN_CONTEXT_KEYS`` in
# ``modulo.core.pipeline_engine.decorator``.
_RESERVED_INPUT_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "_work_item_id",
        "_modulo.work_item",
        "_feedback_correction",
        "_coalesce_key",
        "_work_item_refs",
    }
)

# The system-managed payload key carrying the run's canonical work-item refs.
WORK_ITEM_REFS_KEY = "_work_item_refs"
# Inbound wire alias: an unprefixed ``work_item_refs`` array in a run-creation
# payload is accepted as a refs supply channel (provenance still engine-assigned).
WIRE_REFS_ALIAS_KEY = "work_item_refs"

# Fixed v5 namespace for canonical work-item ids. Deliberately NOT
# ``uuid.NAMESPACE_DNS`` so these ids never collide with other uuid5 uses in
# the codebase.
_WORK_ITEM_NAMESPACE = uuid.UUID("b2f3d4a6-0b1c-4f00-9c1e-8c9f2a1b4d6e")

# Provenance vocabulary (FAR-794 slice 2a). ``caller`` = an authenticated
# API/MCP param; ``derived`` = engine extraction from a webhook/cron payload;
# ``agent`` = a node's own emission (or a workflow's advisory self-report
# claim, normalised at the parser). The legacy ``reported`` value is NO LONGER
# accepted: every read/write path validates against ``_VALID_SOURCES``, and
# stored rows were rewritten to ``agent`` by migration 0222. A snapshot or
# replay payload still carrying ``source: reported`` is rejected/dropped by
# the validator.
_VALID_SOURCES: frozenset[str] = frozenset({"caller", "derived", "agent"})
_VALID_STATUSES: frozenset[str] = frozenset({"done", "attempted"})

# Provenance rank: ``agent < derived < caller``. Journey provenance only ever
# moves RIGHTWARD (never downgraded); ``first_seen_source`` is immutable.
_SOURCE_RANK: dict[str, int] = {"agent": 0, "derived": 1, "caller": 2}

# Trigger types whose run creation carries an authenticated caller account —
# those runs get ``caller``-provenance refs; every other channel (webhook,
# cron, polling, agent_signal, replay) gets ``derived``.
CALLER_TRIGGER_TYPES: frozenset[str] = frozenset({"manual", "rerun"})

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


def validate_ref_entry(
    entry: Any,
    *,
    allowed_sources: frozenset[str] = _VALID_SOURCES,
    force_source: str | None = None,
) -> dict[str, Any]:
    """Validate + canonicalise a ref entry dict (shape-only, FAR-794 slice 2a).

    Returns the canonicalised entry ``{kind, ref, source, status?}`` — the
    ONLY keys a ref entry may assert; any other keys in the input are stripped
    (the engine owns ``source``/``source_node_id`` and assigns them itself).
    Raises ``ValueError`` for a non-dict entry, a missing/blank kind or ref,
    or a ``source`` outside *allowed_sources* / an invalid ``status``.

    Policy parameters:

    * *allowed_sources* — the source vocabulary this call accepts. Defaults to
      the full persisted vocabulary (``caller``/``derived``/``agent``); the
      legacy ``reported`` value is invalid on every path.
    * *force_source* — ENGINE-ASSIGNED provenance: when set, the wire
      ``source`` value is ignored entirely (never trusted) and every returned
      entry carries *force_source*. No vocabulary error is raised for the wire
      value — callers count unknown submissions separately.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"work-item ref entry must be a dict, got {type(entry).__name__}")
    kind = canonicalise_kind(entry.get("kind"))
    ref = entry.get("ref")
    if ref is None or not str(ref).strip():
        raise ValueError("work-item ref entry 'ref' is required")
    source = entry.get("source", "derived")
    if force_source is not None:
        if force_source not in _VALID_SOURCES:
            raise ValueError(f"force_source must be one of {sorted(_VALID_SOURCES)}, got {force_source!r}")
        source = force_source
    elif source not in allowed_sources:
        raise ValueError(f"work-item ref 'source' must be one of {sorted(allowed_sources)}, got {source!r}")
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


def assigned_source_for_trigger(trigger_type: str | None) -> str:
    """Engine-assigned provenance for a run-creation channel (FAR-794 slice 2a).

    Authenticated caller channels (REST/MCP manual triggers, operator reruns)
    get ``caller``; every extraction-driven channel (webhook, cron, polling,
    agent_signal, replay) gets ``derived``. The wire ``source`` is NEVER
    accepted.
    """
    if trigger_type in CALLER_TRIGGER_TYPES:
        return "caller"
    return "derived"


def sort_canonical_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ordering for a canonical refs list (hash-fold stability).

    Sorts by ``(kind, ref, source)`` so two ref sets with the same members
    always hash identically regardless of arrival order.
    """
    return sorted(refs, key=lambda e: (e["kind"], e["ref"], e["source"]))


# --- Shadow-mode / intake observability hook (FAR-794 slice 2a) -------------
#
# The db layer must NOT import ``modulo.core.*`` (import-linter), but the
# work-item-refs counters' metrics home lives beside the journey counters in
# ``modulo.core.lifecycle_map.reconcile``. The bridge is a single hook:
# ``modulo.db.crud.run`` and ``modulo.core.pipeline_engine.decorator`` emit
# named counter events through :func:`notify_refs_event`, and reconcile
# registers the sink at import time. With no hook registered (e.g. a bare
# unit-test process, or a process that never imports reconcile) the
# notification degrades to a log line: shadow mode and intake accounting are
# observability, never enforcement, so fail-open is correct.

RefsCounterHook = Callable[[str, dict[str, Any]], None]
_refs_counter_hook: RefsCounterHook | None = None

# Emitted event names (kept as a vocabulary so sinks can dispatch explicitly).
REFS_EVENT_SHADOW_STRIP_HIT = "shadow_strip_hit"
REFS_EVENT_ASSIGNED_SOURCE = "refs_by_source"
REFS_EVENT_UNKNOWN_SOURCE = "unknown_source_submission"
REFS_EVENT_MALFORMED = "malformed_entry"
REFS_EVENT_DISMISSAL_SUPPRESSED = "refs_dismissal_suppressed"


def set_refs_counter_hook(hook: RefsCounterHook | None) -> None:
    """Register the counter sink for work-item-refs events (called by reconcile)."""
    global _refs_counter_hook
    _refs_counter_hook = hook


def notify_refs_event(event: str, /, **attrs: Any) -> None:
    """Emit a work-item-refs observability event (log + registered hook).

    Never raises: a failing sink is logged at debug and swallowed —
    observability must not alter run-creation behaviour.
    """
    _log.info("work_item_refs.event", extra={"event": event, **attrs})
    hook = _refs_counter_hook
    if hook is not None:
        try:
            hook(event, attrs)
        except Exception:
            _log.debug("work_item_refs.counter_hook_failed", exc_info=True)


def notify_refs_shadow_strip_hit(surface: str) -> None:
    """Record a ``_work_item_refs`` collision seen at a strip boundary.

    *surface* is ``"input_payload"`` (create-run / coalesce chokepoint) or
    ``"run_context"`` (decorator context-setter guard). Log + counter only —
    shadow mode never alters behaviour.
    """
    notify_refs_event(REFS_EVENT_SHADOW_STRIP_HIT, surface=surface)


def canonical_work_item_id(org_id: uuid.UUID, kind: Any, ref: Any) -> uuid.UUID:
    """Deterministic canonical journey id — ``uuid5(NAMESPACE, f"{org}:{kind}:{ref}")``.

    The same (org, kind, ref) ALWAYS produces the same id, so the journey
    row's ``canonical_work_item_id`` is derivable at create time and again at
    finalise/query time without mint races or overwrites.
    """
    k = canonicalise_kind(kind)
    r = canonicalise_ref(k, ref)
    return uuid.uuid5(_WORK_ITEM_NAMESPACE, f"{org_id}:{k}:{r}")
