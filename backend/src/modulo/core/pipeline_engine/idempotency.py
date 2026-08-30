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
an operator re-run can READ it back. The run-record column
(``runs.idempotency_key``, migration 0156, FAR-438) now lands it: the run stores
its stable logical identity ``<pipeline_id>:<run_number>`` (built + validated in
``modulo.db.crud.run`` — the DB layer owns the run-record storage contract, per
the import-linter layering that forbids ``modulo.db`` importing ``modulo.core``),
and a re-run that restores the SAME run reads it back and recomputes the
identical per-node keys via :func:`node_idempotency_key`. The derivation
primitive (:func:`stable_idempotency_key`) is the delivered contract;
:func:`read_before_write_suppression` is the read-before-write dedupe that
consumes it.

SCOPE LIMITATION (connector-write dedupe, FAR-438): the read-before-write
dedupe (:func:`read_before_write_suppression`) is currently wired ONLY to the
sandbox single-node transient-recovery path (the executor's
``_idempotency_gate_ok`` reads ``runs.raw_output_markers`` and applies
suppression for a ``single_sandbox_node`` graph on the sandbox transient
retry). It is NOT yet wired to the connector-write UNKNOWN-recovery surface
(the actual FAR-410 scenario this module was framed around): a connector node
whose write reached upstream but whose outcome was reported UNKNOWN is not
currently deduped. The derivation primitive + the suppression decision function
are the portable contract — wiring the connector-write surface to consume them
is deferred (see the TODO below) and should land with the connector
UNKNOWN-recovery path + its tests.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_IDEMPOTENCY_NAMESPACE = "modulo"

# The stable logical run identity passed as ``run_ref`` MUST be the
# ``<pipeline_id>:<run_number>`` pair (recomputed on a re-run from the pipeline
# identity), NOT the per-replay ``run_id``. A per-replay ``run_id`` is a fresh
# UUID fork of the pipeline and would mint a NEW key on every re-run — silently
# defeating the idempotency contract. Validate the ``<id>:<number>`` shape here
# so a naive ``run_id`` fails loudly instead of silently breaking dedupe.
# NOTE: this regex is mirrored by ``_RUN_IDEMPOTENCY_REF_RE`` in
# ``modulo.db.crud.run`` (the DB layer cannot import this module, so the two
# copy the shape deliberately). ``test_idempotency`` asserts both accept/reject
# the same samples so the mirror cannot drift from this definition.
_RUN_REF_RE = re.compile(r"^[A-Za-z0-9_-]+:\d+$")

# TODO(FAR-438): wire :func:`read_before_write_suppression` into the
# connector-write UNKNOWN-recovery path. Today the dedupe runs only on the
# sandbox single-node transient-recovery surface (see the SCOPE LIMITATION note
# in the module docstring); the connector write that actually reaches upstream
# but reports UNKNOWN is NOT deduped. Add tests alongside the wiring.


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
    any string without the ``<id>:<number>`` shape, or a non-numeric
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


# FAR-438: the run-record persistence boundary. ``stable_idempotency_key`` is the
# derivation primitive; the run-storage helpers (run-ref building + validation)
# live in the DB layer (``modulo.db.crud.run``) because import-linter forbids
# ``modulo.db`` importing ``modulo.core`` and the run record is a DB concern. The
# helpers here (``node_idempotency_key`` + ``read_before_write_suppression``) are
# the CORE side: they derive a per-node key FROM the persisted run value and
# decide whether a re-run reusing the same key must suppress a duplicate write.


def node_idempotency_key(
    run_ref: str,
    node_ref: str,
    index: int | str | None = None,
    payload: str | bytes | None = None,
) -> str:
    """Derive the per-node idempotency key from a run's PERSISTED key.

    Thin wrapper over :func:`stable_idempotency_key` with an explicit
    ``run_ref`` named as the run-record value a re-run reads back. A re-run that
    restores the same run passes the SAME persisted ``run_ref``, node and
    cardinality index, so it recomputes the IDENTICAL per-node key -- which is
    what lets the read-before-write check detect a duplicate write.
    """
    return stable_idempotency_key(run_ref=run_ref, node_ref=node_ref, index=index, payload=payload)


def read_before_write_suppression(
    markers: Any,
    *,
    run_ref: str,
    node_ref: str,
    index: int | str | None = None,
    payload: str | bytes | None = None,
) -> bool:
    """READ-BEFORE-WRITE dedupe (FAR-438): should this write be suppressed?

    True ONLY when a re-run reused the run's PERSISTED idempotency key AND the
    recorded markers already carry BOTH:
      - ``marker["idempotency_key"] == node_idempotency_key(run_ref, node_ref, ...)``
        (the same derived per-node key, computed with the SAME ``index`` /
        ``payload`` as the marker write), AND
      - ``marker["delivery_done"] is True`` — the FAR-228 sentinel that the
        side-effecting delivery actually happened.

    ``delivery_done is True`` is REQUIRED for suppression. A marker that carries
    the matching ``idempotency_key`` but NOT ``delivery_done`` is a mere
    FAILURE stamp (written right before the raise; nothing was delivered) —
    suppressing on it would mark an unexecuted run COMPLETE and never retry it.
    Requiring the delivery sentinel is what makes "re-run with the same key"
    suppress a duplicate write (no double-submit) ONLY when a delivery genuinely
    occurred, while a first-attempt failure (no delivery) is never suppressed
    and retries.

    ``index`` / ``payload`` are threaded into the derivation exactly as on the
    marker-write side, so per-item (fan-out cardinality) and content-version
    keys are computed consistently on BOTH sides — a marker for fan-out item B
    never suppresses item A, and an edited content payload derives a fresh key
    that is not already applied.

    Fail-open: a missing/None ``run_ref``, a malformed ``run_ref``, or a
    non-dict ``markers`` never suppresses (the write proceeds), so a
    misconfigured run record can never silently drop a real write.
    """
    if not run_ref or not node_ref:
        return False
    if not isinstance(markers, dict):
        return False
    try:
        derived = node_idempotency_key(run_ref, node_ref, index=index, payload=payload)
    except ValueError:
        # A malformed persisted run key must fail open (never silently suppress).
        return False
    return any(
        isinstance(marker, dict) and marker.get("delivery_done") is True and marker.get("idempotency_key") == derived
        for marker in markers.values()
    )
