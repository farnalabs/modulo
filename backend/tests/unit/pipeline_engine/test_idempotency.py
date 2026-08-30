"""Unit tests for the FAR-410 stable idempotency-key derivation."""

import pytest

from modulo.core.pipeline_engine.idempotency import stable_idempotency_key


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
