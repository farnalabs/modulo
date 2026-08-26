"""Self-report work-item ref parser for merged pipeline output (FAR-143).

Output-field contract
---------------------
Pipeline authors emit work-item refs as ``work_item_refs`` entries in a node's
structured output:

.. code-block:: json

    {
      "work_item_refs": [
        {"kind": "github_pr", "ref": "#123", "status": "attempted"}
      ]
    }

Entries are ``{kind, ref, status?}``. The ``status`` is optional and must be
one of ``"done"`` / ``"attempted"``. The parser treats every reported entry as
an ADVISORY OPERATOR CLAIM: provenance is forced to ``"reported"``, so a
reported claim can only confirm or match an existing journey row keyed by the
same canonical ``(org, kind, ref)`` — it can NEVER mint one. Minting is owned
by the create-time ``INSERT ... ON CONFLICT DO NOTHING`` path in
``modulo.db.crud.run`` (FAR-142).

Both functions here are PURE — no DB access, no side effects, deterministic —
so they can be unit tested in isolation and reused by the finalise path and any
consumer that reads merged run output.
"""

from __future__ import annotations

from typing import Any

from modulo.db.lifecycle_refs import canonicalise_kind, canonicalise_ref

_REF_KEYS: frozenset[str] = frozenset({"work_item_refs", "modulo.work_item_refs", "touched_work_items"})

_REPORTED_SOURCE = "reported"
_VALID_STATUSES: frozenset[str] = frozenset({"done", "attempted"})


def parse_self_report_refs(merged_outputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract raw work-item ref entries from merged pipeline output.

    Recursively walks the whole output tree collecting every value found under
    a self-report key (``work_item_refs``, ``modulo.work_item_refs``,
    ``touched_work_items``). The single traversal covers BOTH the top-level key
    placement and the FAR-125 node-keyed placement
    (``{node_id: {..., "output": {...}}}``), plus arbitrary nesting.

    A key whose value is a list contributes its items; any other value
    (dict, scalar) contributes that value as a single raw entry — so malformed
    emissions surface downstream instead of vanishing silently. Raw entries are
    returned unmodified, including non-dict / incomplete ones, so
    :func:`validate_and_normalise_reported_refs` can classify and count them.

    The input is never mutated. Returns ``[]`` when the output is not a dict or
    holds no matching keys.
    """
    if not isinstance(merged_outputs, dict):
        return []

    collected: list[Any] = []
    seen_ids: set[int] = set()
    _walk_node(merged_outputs, collected, seen_ids)
    return collected


def _collect_value(value: Any, collected: list[Any]) -> None:
    """Append a reported value to ``collected`` (spreading lists)."""
    if isinstance(value, list):
        collected.extend(value)
    else:
        collected.append(value)


def _walk_dict(node: dict[str, Any], collected: list[Any], seen_ids: set[int]) -> None:
    """Walk a dict node, collecting values under self-report keys."""
    if id(node) in seen_ids:
        return
    seen_ids.add(id(node))
    for key, value in node.items():
        if key in _REF_KEYS:
            _collect_value(value, collected)
        _walk_node(value, collected, seen_ids)


def _walk_list(node: list[Any], collected: list[Any], seen_ids: set[int]) -> None:
    """Walk a list node, walking each element."""
    if id(node) in seen_ids:
        return
    seen_ids.add(id(node))
    for item in node:
        _walk_node(item, collected, seen_ids)


def _walk_node(node: Any, collected: list[Any], seen_ids: set[int]) -> None:
    """Dispatch a node to the dict or list walker (recursive)."""
    if isinstance(node, dict):
        _walk_dict(node, collected, seen_ids)
    elif isinstance(node, list):
        _walk_list(node, collected, seen_ids)


def validate_and_normalise_reported_refs(
    entries: list[dict[str, Any]], max_refs: int = 100
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate + normalise raw reported refs into canonical reported entries.

    For each raw entry:

    * a non-dict, a blank/missing ``kind`` or ``ref``, or a canonicalisation
      failure is counted as ``malformed`` and skipped;
    * ``source`` is forced to ``"reported"`` (advisory operator claim);
    * an optional ``status`` is kept only when it is one of ``{"done",
      "attempted"}``, otherwise dropped;
    * duplicates of an already-seen ``(kind, ref, source)`` triple are
      collapsed (first occurrence wins);
    * the result is capped at ``max_refs`` unique entries — overflow entries
      are counted as ``capped``. Dedup runs BEFORE the cap, so a duplicate of
      an already-seen entry never consumes the cap, and a capped entry's
      duplicates are not double-counted.

    Returns ``(valid_entries, counters)`` where ``counters`` carries the
    ``malformed`` / ``capped`` / ``valid`` counts. Deterministic: the same
    input always yields the same output.
    """
    counters = {"malformed": 0, "capped": 0, "valid": 0}
    valid: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in entries:
        if not isinstance(raw, dict):
            counters["malformed"] += 1
            continue
        kind_raw = raw.get("kind")
        ref_raw = raw.get("ref")
        if kind_raw is None or ref_raw is None:
            counters["malformed"] += 1
            continue
        try:
            kind = canonicalise_kind(kind_raw)
            ref = canonicalise_ref(kind, ref_raw)
        except ValueError:
            counters["malformed"] += 1
            continue

        key = (kind, ref, _REPORTED_SOURCE)
        if key in seen:
            continue
        seen.add(key)

        if len(valid) >= max_refs:
            counters["capped"] += 1
            continue

        normalised: dict[str, Any] = {"kind": kind, "ref": ref, "source": _REPORTED_SOURCE}
        status = raw.get("status")
        if isinstance(status, str) and status in _VALID_STATUSES:
            normalised["status"] = status
        valid.append(normalised)
        counters["valid"] += 1

    return valid, counters
