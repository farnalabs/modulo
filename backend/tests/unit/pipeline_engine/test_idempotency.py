"""Unit tests for the FAR-410 stable idempotency-key derivation + FAR-438 persistence.

Covers both the derivation primitive (:func:`stable_idempotency_key`) and the
FAR-438 run-record persistence contract (run-level key persist + read-back,
per-node derivation from a persisted key, and the read-before-write dedupe).
Also covers the FAR-458 :func:`read_before_write_ambiguous` primitive — the
couldn't-confirm-delivery detection that the per-connector ``on_unknown`` policy
consumes (distinct from the confirmed-delivered suppression).
"""

import uuid

import pytest

from modulo.core.pipeline_engine.idempotency import (
    node_idempotency_key,
    read_before_write_ambiguous,
    read_before_write_suppression,
    stable_idempotency_key,
)
from modulo.db.crud.run import run_idempotency_key, run_idempotency_ref


def test_stable_idempotency_key_is_deterministic() -> None:
    a = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    b = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert a == b


def test_stable_idempotency_key_differs_across_nodes_and_index() -> None:
    base = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert base != stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=1)
    assert base != stable_idempotency_key(run_ref="pipeline:42", node_ref="node-b", index=0)
    assert base != stable_idempotency_key(run_ref="pipeline:43", node_ref="node-a", index=0)


def test_stable_idempotency_key_ignores_a_fresh_run_id() -> None:
    """A re-run that forks a fresh run_id must NOT mint a new key.

    Two re-runs of the same logical work (same pipeline:run_number, node, index)
    produce the same key — that is what lets an operator re-run reuse the
    persisted key rather than double-applying the write.
    """
    replay_a = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    replay_b = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert replay_a == replay_b


def test_stable_idempotency_key_without_index() -> None:
    key = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a")
    assert key  # a plain 64-char hex digest
    assert len(key) == 64
    assert key == stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a")


def test_stable_idempotency_key_rejects_naive_run_id() -> None:
    """A bare per-replay run_id (no <pipeline_id>:<run_number> shape) must fail
    loudly rather than silently minting a fresh key on every re-run."""
    with pytest.raises(ValueError, match="run_ref must be the stable logical run identity"):
        stable_idempotency_key(run_ref="550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f", node_ref="node-a")
    with pytest.raises(ValueError, match="run_ref must be the stable logical run identity"):
        stable_idempotency_key(run_ref="pipeline:not-a-number", node_ref="node-a")
    with pytest.raises(ValueError, match="run_ref must be the stable logical run identity"):
        stable_idempotency_key(run_ref="", node_ref="node-a")


def test_stable_idempotency_key_accepts_valid_colon_run_ref() -> None:
    """A well-formed <pipeline_id>:<run_number> reference is accepted."""
    key = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert key == stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)


def test_stable_idempotency_key_payload_unchanged_same_key() -> None:
    """An unchanged payload on a re-run reuses the identical key."""
    k1 = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload="hello")
    k2 = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload="hello")
    assert k1 == k2


def test_stable_idempotency_key_payload_changed_different_key() -> None:
    """A genuinely-changed content payload yields a DIFFERENT key (the edited
    write is not silently deduped/dropped)."""
    k1 = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload="hello")
    k2 = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload="world")
    assert k1 != k2


def test_stable_idempotency_key_payload_absent_is_ignored() -> None:
    """Omitting payload is identical to passing None (no content component)."""
    k_none = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert k_none == stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload=None)


def test_stable_idempotency_key_payload_bytes_match_str() -> None:
    """Byte and str forms of the same content normalize to the same key."""
    k_str = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload="hello")
    k_bytes = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0, payload=b"hello")
    assert k_str == k_bytes


# ---------------------------------------------------------------------------
# FAR-438 — run-record idempotency-key persistence
# ---------------------------------------------------------------------------


def test_run_idempotency_ref_is_reusable_stable_identity() -> None:
    """The persisted run key is ``<pipeline_id>:<run_number>``, reusable as a run_ref.

    A re-run that restores the same run recomputes the SAME reference (the
    run_number is allocated once per org), so a per-node key derived from it is
    stable across the re-run.
    """
    pipeline_id = uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f")
    run_ref = run_idempotency_ref(pipeline_id, 42)
    assert run_ref == "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f:42"
    assert run_idempotency_ref(pipeline_id, 42) == run_ref
    # The persisted value is a valid run_ref for the derivation primitive.
    assert stable_idempotency_key(run_ref=run_ref, node_ref="node-a") == stable_idempotency_key(
        run_ref=run_ref, node_ref="node-a"
    )


def test_run_idempotency_key_persist_and_readback() -> None:
    """A run persists its idempotency key and a re-run reads back the same value."""
    run_ref = run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 7)
    persisted = run_idempotency_key(run_ref)
    assert persisted == "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f:7"
    # Read-back: a re-run passes the persisted key straight into the derivation.
    assert run_idempotency_key(persisted) == persisted
    assert stable_idempotency_key(run_ref=persisted, node_ref="node-a", index=0) == stable_idempotency_key(
        run_ref="550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f:7", node_ref="node-a", index=0
    )


def test_run_idempotency_key_excludes_per_replay_run_id() -> None:
    """A naive per-replay run_id is rejected at the persist boundary (FAR-438).

    The value persisted on the run record MUST be ``<pipeline_id>:<run_number>``.
    A bare run_id (a fresh UUID fork per re-run) would mint a NEW key on every
    re-run and silently defeat dedupe, so it must fail loudly here.
    """
    with pytest.raises(ValueError, match="run_ref must be the stable logical run identity"):
        run_idempotency_key("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f")
    with pytest.raises(ValueError, match="run_ref must be the stable logical run identity"):
        run_idempotency_key("pipeline:not-a-number")
    with pytest.raises(ValueError, match="run_ref must be the stable logical run identity"):
        run_idempotency_key("")


def test_node_key_recomputed_from_persisted_run_key_is_stable() -> None:
    """The same persisted run key + node + index recomputes the identical node key.

    This is the read-before-write premise: a re-run that reuses the persisted key
    derives the SAME per-node key as the original, so a duplicate write can be
    detected.
    """
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    original = node_idempotency_key(persisted, "node-a", index=0)
    replay = node_idempotency_key(run_idempotency_key(persisted), "node-a", index=0)
    assert original == replay
    # A different node / cardinality index yields a different node key.
    assert original != node_idempotency_key(persisted, "node-b", index=0)
    assert original != node_idempotency_key(persisted, "node-a", index=1)


def test_read_before_write_suppresses_same_persisted_key() -> None:
    """A re-run reading back the SAME persisted key suppresses a duplicate write."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    applied_key = node_idempotency_key(persisted, "node-a", index=0)
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied_key}}
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is True


def test_read_before_write_no_marker_not_suppressed() -> None:
    """No recorded applied key for the node => the write proceeds (no false dedupe)."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    assert read_before_write_suppression(None, run_ref=persisted, node_ref="node-a", index=0) is False
    assert read_before_write_suppression({}, run_ref=persisted, node_ref="node-a", index=0) is False
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": "other"}}
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is False


def test_read_before_write_different_node_or_index_not_suppressed() -> None:
    """A key recorded for a different node/cardinality must NOT suppress this write."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    other_node_key = node_idempotency_key(persisted, "node-b", index=0)
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": other_node_key}}
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is False


def test_read_before_write_fails_open_on_missing_or_malformed_run_ref() -> None:
    """A missing/malformed persisted run key NEVER suppresses (fail-open)."""
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": "abc"}}
    assert read_before_write_suppression(markers, run_ref=None, node_ref="node-a", index=0) is False
    assert (
        read_before_write_suppression(markers, run_ref="550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f", node_ref="node-a")
        is False
    )
    assert read_before_write_suppression(markers, run_ref="", node_ref="node-a") is False
    # Non-dict markers are ignored.
    assert read_before_write_suppression(["not-a-dict"], run_ref="pipeline:9", node_ref="node-a") is False


def test_read_before_write_first_attempt_failure_not_suppressed() -> None:
    """A first-attempt failure stamp carries the matching key but NO
    ``delivery_done`` — it must NOT suppress (the write never delivered, so the
    run must retry rather than be marked COMPLETE).

    This is the FAR-438 regression: the marker is stamped with ``idempotency_key``
    on EVERY failure (right before the raise), so requiring ONLY the key match
    would let a legitimate first-attempt transient failure suppress itself.
    """
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    failed_key = node_idempotency_key(persisted, "node-a", index=0)
    failed_marker = {"attempt-0": {"_modulo_marker": True, "status": "failed", "idempotency_key": failed_key}}
    # delivery_done absent => the failure must NOT suppress.
    assert read_before_write_suppression(failed_marker, run_ref=persisted, node_ref="node-a", index=0) is False
    # delivery_done explicitly False => still NOT suppressed.
    failed_marker["attempt-0"]["delivery_done"] = False
    assert read_before_write_suppression(failed_marker, run_ref=persisted, node_ref="node-a", index=0) is False


def test_read_before_write_index_payload_item_keys_do_not_collide() -> None:
    """Two fan-out items (index 0 vs 1) for the SAME node derive DIFFERENT keys:
    item B's marker never suppresses item A, so a re-run of item A is not hidden
    by item B's applied key."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    item_a_key = node_idempotency_key(persisted, "node-a", index=0)
    item_b_key = node_idempotency_key(persisted, "node-a", index=1)
    assert item_a_key != item_b_key
    # A marker applied for item B must not suppress the item A probe.
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": item_b_key}}
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is False
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=1) is True


def test_read_before_write_changed_payload_derives_different_key() -> None:
    """A genuinely-edited content-edit payload yields a DIFFERENT key: an edited
    re-run probe is NOT suppressed by the unedited marker (the edit is no longer
    silently deduped/dropped), while the untouched re-run IS suppressed."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    original_key = node_idempotency_key(persisted, "node-a", index=0, payload="v1")
    edited_key = node_idempotency_key(persisted, "node-a", index=0, payload="v2")
    assert original_key != edited_key
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": original_key}}
    # Edited payload probe => different key => not suppressed.
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0, payload="v2") is False
    # Unchanged payload probe => same key + delivery_done => suppressed.
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0, payload="v1") is True


# ── read-before-write ambiguous detection (FAR-458) ─────────────────────────


def test_read_before_write_ambiguous_true_on_unconfirmed_delivery() -> None:
    """A marker carrying the SAME derived key but WITHOUT ``delivery_done`` is the
    ambiguous (couldn't-confirm-delivery) state — the exact case the per-connector
    ``on_unknown`` policy governs. It must NOT count as confirmed (suppression is
    False) but MUST be detectable as ambiguous."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    derived = node_idempotency_key(persisted, "node-a", index=0)
    markers = {"attempt-0": {"_modulo_marker": True, "status": "failed", "idempotency_key": derived}}
    # A failure stamp is NOT a confirmed delivery (never suppress on it)...
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is False
    # ...but it IS ambiguous (a prior attempt touched this exact write, unconfirmed).
    assert read_before_write_ambiguous(markers, run_ref=persisted, node_ref="node-a", index=0) is True


def test_read_before_write_ambiguous_false_on_confirmed_delivery() -> None:
    """A CONFIRMED-delivered marker (``delivery_done is True`` + matching key) is
    NOT ambiguous — it is the dedup's confirmed case, governed by suppression,
    never by ``on_unknown``."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    derived = node_idempotency_key(persisted, "node-a", index=0)
    markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": derived}}
    assert read_before_write_ambiguous(markers, run_ref=persisted, node_ref="node-a", index=0) is False
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is True


def test_read_before_write_ambiguous_false_on_first_time() -> None:
    """A first-time write (no marker) is never ambiguous — there is no prior
    attempt, so nothing to fail-closed on."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    assert read_before_write_ambiguous({}, run_ref=persisted, node_ref="node-a", index=0) is False


def test_read_before_write_ambiguous_false_on_changed_payload() -> None:
    """A changed-payload re-run derives a DIFFERENT key, so it is never ambiguous
    against the unedited marker (and never suppressed under ``fail_closed``)."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    original_key = node_idempotency_key(persisted, "node-a", index=0, payload="v1")
    markers = {"attempt-0": {"_modulo_marker": True, "idempotency_key": original_key}}
    assert read_before_write_ambiguous(markers, run_ref=persisted, node_ref="node-a", index=0, payload="v2") is False
    # The unedited probe IS ambiguous (matching key, no delivery_done).
    assert read_before_write_ambiguous(markers, run_ref=persisted, node_ref="node-a", index=0, payload="v1") is True


def test_read_before_write_ambiguous_fails_open_on_malformed_ref() -> None:
    """A missing / malformed run_ref never counts as ambiguous (fail-open) — a
    misconfigured run record must not drive a fail-closed write drop."""
    marker = {"attempt-0": {"_modulo_marker": True, "delivery_done": False, "idempotency_key": "x"}}
    assert read_before_write_ambiguous(marker, run_ref="", node_ref="node-a") is False
    bare_uuid = "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"
    assert read_before_write_ambiguous(marker, run_ref=bare_uuid, node_ref="node-a") is False
    assert read_before_write_ambiguous(marker, run_ref="pipeline:42", node_ref="") is False


def test_read_before_write_ambiguous_fails_open_on_non_dict_markers() -> None:
    """Non-dict markers never count as ambiguous (fail-open)."""
    assert read_before_write_ambiguous(None, run_ref="pipeline:42", node_ref="node-a") is False
    assert read_before_write_ambiguous(["x"], run_ref="pipeline:42", node_ref="node-a") is False


# ── FAR-531 intent / no-delivery marker states ───────────────────────────────


def test_read_before_write_ambiguous_true_on_intent_marker() -> None:
    """An IN-FLIGHT intent marker (FAR-531: persisted after the gate proceeds,
    before the write fires; matching key, no ``delivery_done``) IS the ambiguous
    state — a later attempt's gate must detect it so fail_closed can suppress."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    derived = node_idempotency_key(persisted, "node-a", index=0)
    markers = {
        "run:r:node:n:connector": {
            "_modulo_marker": True,
            "marker_kind": "connector_write_intent",
            "status": "running",
            "idempotency_key": derived,
        }
    }
    assert read_before_write_ambiguous(markers, run_ref=persisted, node_ref="node-a", index=0) is True
    # And it is NOT a confirmed delivery — a fail_open gate re-fires.
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is False


def test_read_before_write_ambiguous_false_on_no_delivery_confirmed() -> None:
    """A DEFINITE no-delivery marker (``no_delivery_confirmed: True`` — the
    connector's result REPORTED failure via the ``write_reported_failure``
    hook; a raised error is ambiguous per FAR-531 QA Fix 1) is NOT ambiguous:
    the later attempt's gate must re-fire the write under BOTH modes (FAR-458:
    never suppress a definite failure)."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    derived = node_idempotency_key(persisted, "node-a", index=0)
    markers = {
        "run:r:node:n:connector": {
            "_modulo_marker": True,
            "marker_kind": "connector_write_no_delivery",
            "no_delivery_confirmed": True,
            "idempotency_key": derived,
        }
    }
    assert read_before_write_ambiguous(markers, run_ref=persisted, node_ref="node-a", index=0) is False
    assert read_before_write_suppression(markers, run_ref=persisted, node_ref="node-a", index=0) is False


def test_no_delivery_confirmed_resolves_after_in_flight_intent() -> None:
    """The intent → no-delivery transition REPLACES the in-flight state in the
    same slot: once resolved, the same key is no longer ambiguous (the exact
    reported-failure sequence a re-run observes)."""
    persisted = run_idempotency_key(run_idempotency_ref(uuid.UUID("550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"), 9))
    derived = node_idempotency_key(persisted, "node-a", index=0)
    slot = "run:r:node:n:connector"
    intent_markers = {
        slot: {"_modulo_marker": True, "marker_kind": "connector_write_intent", "idempotency_key": derived}
    }
    assert read_before_write_ambiguous(intent_markers, run_ref=persisted, node_ref="node-a", index=0) is True
    resolved_markers = {
        slot: {
            "_modulo_marker": True,
            "marker_kind": "connector_write_no_delivery",
            "no_delivery_confirmed": True,
            "idempotency_key": derived,
        }
    }
    assert read_before_write_ambiguous(resolved_markers, run_ref=persisted, node_ref="node-a", index=0) is False


def test_run_ref_shape_regex_consistent_with_db_layer() -> None:
    """The core (``_RUN_REF_RE``) and DB-layer (``_RUN_IDEMPOTENCY_REF_RE``)
    run-ref shape regexes are mirrored deliberately (import-linter forbids
    ``modulo.db`` importing ``modulo.core``), so they must accept/reject the
    SAME samples — a divergent copy would silently break key read-back."""
    samples = [
        "pipeline:42",  # valid
        "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f:7",  # valid (uuid:number)
        "some_pipeline-name:1",  # valid (slug:number)
        "pipeline:0",  # valid (0 is a number)
        "pipeline:not-a-number",  # invalid (non-numeric)
        "pipeline",  # invalid (no :number)
        "pipeline:",  # invalid (empty number)
        ":42",  # invalid (empty id)
        "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f",  # invalid (bare uuid)
        "",  # invalid (empty)
    ]

    def _accepted_by_core(sample: str) -> bool:
        try:
            stable_idempotency_key(run_ref=sample, node_ref="node-a")
            return True
        except ValueError:
            return False

    def _accepted_by_db(sample: str) -> bool:
        try:
            run_idempotency_key(sample)
            return True
        except ValueError:
            return False

    for sample in samples:
        assert _accepted_by_core(sample) == _accepted_by_db(sample), f"regex divergence for {sample!r}"
