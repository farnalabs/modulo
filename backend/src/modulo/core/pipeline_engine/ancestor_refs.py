"""DAG-ancestor work-item ref injection (FAR-795, pre-wiring slice).

A pure collector that computes the work-item refs to INJECT into a node's
input at node start: the run's create-time refs plus the refs emitted by the
node's DAG ancestors (from their completed-and-committed attempts). The
node-runner wiring — the caller that resolves ``dag_ancestor_ids`` from the
compiled graph, folds per-attempt emissions into ``completed_node_outputs``,
and applies the unified cap setting — lands in a follow-up slice, so this
module is deliberately free of engine imports and holds ONE pure,
unit-testable function.
"""

from __future__ import annotations

from typing import Any

from modulo.db.lifecycle_refs import _SOURCE_RANK

# Ref entries are expected to be ``validate_ref_entry``-canonicalised by the
# intake boundaries upstream (create-time stamping and the emission merge), so
# the dedupe/cap identity is used as-is. A non-dict entry, or one without a
# non-empty string ``kind``/``ref``, is malformed and skipped — fail-open,
# mirroring every other refs read path (``_node_input_work_item_refs``).


def _ref_identity(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Canonical (org-free) ``(kind, ref)`` identity, or None when malformed.

    "Org-free" because the journey's ``canonical_work_item_id`` folds the org
    in; within a single run the org is fixed, so ``(kind, ref)`` alone is the
    dedupe key.
    """
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    ref = entry.get("ref")
    if not isinstance(kind, str) or not kind:
        return None
    if not isinstance(ref, str) or not ref:
        return None
    return (kind, ref)


def collect_injected_refs(
    *,
    run_create_time_refs: list[dict[str, Any]],
    dag_ancestor_ids: set[str],
    completed_node_outputs: dict[str, list[dict[str, Any]]],
    cap: int,
) -> list[dict[str, Any]]:
    """Compute the work-item refs to inject into a node at start (FAR-795).

    Result = ``run_create_time_refs`` + the refs emitted by nodes whose id is
    in ``dag_ancestor_ids`` (from ``completed_node_outputs``, the caller-folded
    union of every completed attempt). Pure: no I/O, no clock, no logging
    side effects; the inputs are never mutated and the output is deterministic
    given the inputs.

    Rules:

    * Nodes NOT in ``dag_ancestor_ids`` (parallel/unrelated branches) contribute
      nothing. The target node's own id is not an ancestor of itself, so its
      own emissions contribute nothing — that exclusion is the caller's
      ``dag_ancestor_ids`` set, which must not contain the target.
    * Only nodes present in ``completed_node_outputs`` contribute: a
      skipped/not-executed node simply is not a key, so it contributes nothing.
    * FAILED ancestors ARE included: outcome filtering is the CALLER's job.
      Whatever emissions the caller passes for a failed node are injected
      verbatim — this function never filters by run/node outcome.
    * Dedupe is by the canonical (org-free) ``(kind, ref)`` identity; on
      duplicates the highest-ranked source survives (``caller`` > ``derived``
      > ``agent``; ties keep the earliest input position).
    * The cap applies AFTER dedupe, over the deterministic total order
      ``(source rank ascending, input position ascending, (kind, ref))`` —
      i.e. over-cap drops ``agent`` entries first, then ``derived``, then
      ``caller``, mirroring the unified FAR-794 cap drop map. ``cap <= 0``
      yields an empty list.
    * The returned list keeps input order: create-time refs first (their array
      order), then each contributing ancestor's emissions in list order.
    """
    if cap <= 0:
        return []

    # Concatenate the supplies with a single running input position so the
    # total order's "array position" tie-break is well defined. Dict iteration
    # order over ``completed_node_outputs`` is the caller's folding order.
    sequence: list[tuple[int, dict[str, Any]]] = list(enumerate(run_create_time_refs))
    next_position = len(sequence)
    for node_id, emissions in completed_node_outputs.items():
        if node_id not in dag_ancestor_ids:
            continue
        for entry in emissions:
            sequence.append((next_position, entry))
            next_position += 1

    # Dedupe by (kind, ref), keeping the highest-ranked source (ties: earliest
    # position). ``rank`` defaults to 0 for an unknown/missing source so an
    # unranked entry never outranks a ranked one.
    best: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}
    for position, entry in sequence:
        identity = _ref_identity(entry)
        if identity is None:
            continue
        rank = _SOURCE_RANK.get(str(entry.get("source")), 0)
        current = best.get(identity)
        if current is None or rank > current[0] or (rank == current[0] and position < current[1]):
            best[identity] = (rank, position, entry)

    survivors = [(rank, position, identity, entry) for identity, (rank, position, entry) in best.items()]
    # Cap drop order: rank ascending (agent first), then position, then the
    # canonical identity — the front of this order is dropped first.
    drop_order = sorted(survivors, key=lambda candidate: (candidate[0], candidate[1], candidate[2]))
    kept = drop_order[max(len(drop_order) - cap, 0) :]
    # Output keeps input order (positions are unique, so this is total).
    return [entry for _, position, _, entry in sorted(kept, key=lambda candidate: candidate[1])]
