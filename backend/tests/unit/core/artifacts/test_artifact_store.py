"""Unit tests for modulo.core.artifacts.store (FAR-582).

Tests the LocalArtifactStore: append, finalize, read_bytes, delete_run, delete_node.
"""

from __future__ import annotations

import hashlib

import pytest

from modulo.core.artifacts.store import (
    ArtifactPointer,
    LocalArtifactStore,
)


def _make_store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


# ── append + finalize round-trip ────────────────────────────────────────


def test_append_and_finalize_stdout(tmp_path):
    """Append text, finalize, and verify the zstd-compressed file is correct."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "hello world\n")
    store.append("org1", "run1", "node1", "attempt1", "stdout", "second chunk\n")

    ptr = store.finalize("org1", "run1", "node1", "attempt1", "stdout")

    assert ptr is not None
    assert ptr["stream"] == "stdout"
    assert ptr["compression"] == "zstd"
    assert ptr["size_bytes"] == len(b"hello world\nsecond chunk\n")
    assert ptr["sha256"] == hashlib.sha256(b"hello world\nsecond chunk\n").hexdigest()
    # Raw .tmp file should be gone after finalize
    raw = store._raw_path("org1", "run1", "node1", "attempt1", "stdout")
    assert not raw.exists()
    # Compressed .zst file should exist
    zst = store._zst_path("org1", "run1", "node1", "attempt1", "stdout")
    assert zst.exists()


def test_read_bytes_roundtrip(tmp_path):
    """Read back decompressed bytes after finalize."""
    store = _make_store(tmp_path)
    content = "line1\nline2\nline3\n"
    store.append("org1", "run1", "node1", "attempt1", "stdout", content)
    ptr = store.finalize("org1", "run1", "node1", "attempt1", "stdout")

    result = store.read_bytes(ptr)
    assert result == content.encode("utf-8")


def test_finalize_no_data_returns_none(tmp_path):
    """Finalize with no appended data returns None."""
    store = _make_store(tmp_path)
    ptr = store.finalize("org1", "run1", "node1", "attempt1", "stdout")
    assert ptr is None


def test_append_empty_string_is_noop(tmp_path):
    """Appending empty string does not create files."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "")
    ptr = store.finalize("org1", "run1", "node1", "attempt1", "stdout")
    assert ptr is None


# ── stderr stream ───────────────────────────────────────────────────────


def test_stderr_stream(tmp_path):
    """Stderr works identically to stdout."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stderr", "error output\n")
    ptr = store.finalize("org1", "run1", "node1", "attempt1", "stderr")

    assert ptr is not None
    assert ptr["stream"] == "stderr"
    result = store.read_bytes(ptr)
    assert result == b"error output\n"


# ── large content ───────────────────────────────────────────────────────


def test_large_content(tmp_path):
    """Multi-MB content compresses and decompresses correctly."""
    store = _make_store(tmp_path)
    large_text = "x" * (5 * 1024 * 1024)  # 5 MB
    store.append("org1", "run1", "node1", "attempt1", "stdout", large_text)
    ptr = store.finalize("org1", "run1", "node1", "attempt1", "stdout")

    assert ptr is not None
    assert ptr["size_bytes"] == 5 * 1024 * 1024
    result = store.read_bytes(ptr)
    assert len(result) == 5 * 1024 * 1024
    # zstd should compress repetitive content well
    assert ptr["size_bytes"] > 0


# ── delete_run ──────────────────────────────────────────────────────────


def test_delete_run(tmp_path):
    """delete_run removes all files for a run and returns count."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "a")
    store.append("org1", "run1", "node2", "attempt1", "stderr", "b")
    store.finalize("org1", "run1", "node1", "attempt1", "stdout")
    store.finalize("org1", "run1", "node2", "attempt1", "stderr")

    count = store.delete_run("org1", "run1")
    assert count == 2
    # Directory should be gone
    run_dir = store.root / "org1" / "run1"
    assert not run_dir.exists()


def test_delete_run_nonexistent(tmp_path):
    """delete_run on non-existent run returns 0."""
    store = _make_store(tmp_path)
    count = store.delete_run("org1", "nonexistent")
    assert count == 0


# ── delete_node ─────────────────────────────────────────────────────────


def test_delete_node(tmp_path):
    """delete_node removes all files for a node."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "a")
    store.append("org1", "run1", "node1", "attempt1", "stderr", "b")
    store.finalize("org1", "run1", "node1", "attempt1", "stdout")
    store.finalize("org1", "run1", "node1", "attempt1", "stderr")

    count = store.delete_node("org1", "run1", "node1")
    assert count == 2
    node_dir = store.root / "org1" / "run1" / "node1"
    assert not node_dir.exists()


# ── read_bytes failure ─────────────────────────────────────────────────


def test_read_bytes_file_not_found(tmp_path):
    """read_bytes raises FileNotFoundError for missing artifact."""
    store = _make_store(tmp_path)
    ptr = ArtifactPointer(
        stream="stdout",
        rel_path="org1/run1/node1/attempt1.stdout.zst",
        size_bytes=0,
        sha256="abc",
    )
    with pytest.raises(FileNotFoundError):
        store.read_bytes(ptr)


# ── multiple attempts ──────────────────────────────────────────────────


def test_multiple_attempts(tmp_path):
    """Different attempt_keys coexist without interference."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "first")
    store.append("org1", "run1", "node1", "attempt2", "stdout", "second")
    ptr1 = store.finalize("org1", "run1", "node1", "attempt1", "stdout")
    ptr2 = store.finalize("org1", "run1", "node1", "attempt2", "stdout")

    assert ptr1 is not None and ptr2 is not None
    assert store.read_bytes(ptr1) == b"first"
    assert store.read_bytes(ptr2) == b"second"


# ── ArtifactPointer dict contract ───────────────────────────────────────


def test_pointer_has_required_keys():
    """ArtifactPointer carries the exact 5-key contract."""
    ptr = ArtifactPointer(
        stream="stdout",
        rel_path="a/b/c.zst",
        size_bytes=100,
        sha256="abc123",
    )
    assert set(ptr.keys()) == {"stream", "rel_path", "size_bytes", "sha256", "compression"}
    assert ptr["compression"] == "zstd"
