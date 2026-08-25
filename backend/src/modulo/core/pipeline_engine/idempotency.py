"""Stable idempotency-key derivation for connector writes (FAR-410).

An operator re-run of an UNKNOWN-terminated connector node must reuse the SAME
persisted idempotency key so a write that may (or may not) have reached the
upstream is not re-applied as a fresh, distinct operation. A key minted from a
fresh random (or from a fresh per-replay ``run_id``) would break that contract.

The key is derived deterministically from a STABLE LOGICAL identity a re-run can
recompute (typically ``<pipeline_id>:<run_number>`` plus the node id and a
cardinality/fanout index). It is never a fresh random per run, and an in-run
retry reuses the identical key.

NOTE (persistence): the derived key is meant to be stored on the run record so
an operator re-run can READ it back. The runs-column migration is DEFERRED —
the migration chain was repaired (``0136_rename_remy_*`` now re-parents onto
``0131_eval_dataset_corpus`` rather than the absent ``0135_status_check_constraints``,
and migrations 0132-0134 were removed), so the deferred column migration can
now land without compounding breakage. The derivation primitive is the
delivered contract; the column lands once that migration is added.
"""

from __future__ import annotations

import hashlib
import re

_IDEMPOTENCY_NAMESPACE = "modulo"

# The stable logical run identity passed as ``run_ref`` MUST be the
# ``<pipeline_id>:<run_number>`` pair (recomputed on a re-run from the pipeline
# identity), NOT the per-replay ``run_id``. A per-replay ``run_id`` is a fresh
# UUID fork of the pipeline and would mint a NEW key on every re-run — silently
# defeating the idempotency contract. Validate the ``<id>:<number>`` shape here
# so a naive ``run_id`` fails loudly instead of silently breaking dedupe.
_RUN_REF_RE = re.compile(r"^[A-Za-z0-9_-]+:\d+$")


def stable_idempotency_key(
    *,
    run_ref: str,
    node_ref: str,
    index: int | str | None = None,
    payload: str | bytes | None = None,
) -> str:
    """Derive a deterministic idempotency key for a single node execution.

    ``run_ref`` is the stable logical run identity a re-run can recompute
    (e.g. ``"<pipeline_id>:<run_number>"``), NOT the per-replay ``run_id`` —
    re-running the pipeline forks a fresh ``run_id``, so keying on it would mint
    a new key for the same logical work. A malformed ``run_ref`` (a bare UUID,
    any string without the ``<id>:<number>`` shape, or a non-positive
    run_number) raises ``ValueError`` rather than silently minting a fresh key
    every re-run. ``node_ref`` is the node id/name; ``index`` is the item /
    fanout-cardinality position (``None`` for a single-execution node).

    ``payload`` is the normalized request payload / content version. When
    provided, a content hash is folded into the raw input so a genuinely-changed
    content-edit between an UNKNOWN re-run and the original produces a DIFFERENT
    key (the edit is no longer silently deduped), while an unchanged retry
    produces the SAME key. Omit it (``None``) when the write has no payload the
    re-run could edit. The same inputs always produce the same key, which is
    what makes an operator re-run (and an in-run retry) reuse the identical
    persisted key.
    """
    if not isinstance(run_ref, str) or not _RUN_REF_RE.match(run_ref):
        raise ValueError(
            "run_ref must be the stable logical run identity '<pipeline_id>:<run_number>' "
            "(recomputed on a re-run), NOT the per-replay run_id; got "
            f"{run_ref!r}"
        )
    raw = f"{_IDEMPOTENCY_NAMESPACE}:{run_ref}:{node_ref}"
    if index is not None:
        raw = f"{raw}:{index}"
    if payload is not None:
        payload_bytes: bytes = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        raw = f"{raw}:{hashlib.sha256(payload_bytes).hexdigest()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
