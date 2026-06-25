"""Unit tests for run CRUD helpers (no DB — only _input_hash)."""

from modulo.db.crud.run import _input_hash


def test_input_hash_is_deterministic():
    payload = {"key": "value", "num": 42}
    assert _input_hash(payload) == _input_hash(payload)


def test_input_hash_is_order_independent():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert _input_hash(a) == _input_hash(b)


def test_input_hash_differs_for_different_payloads():
    assert _input_hash({"x": 1}) != _input_hash({"x": 2})


def test_input_hash_empty_payload():
    h = _input_hash({})
    assert len(h) == 64  # SHA-256 hex digest
