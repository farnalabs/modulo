"""Unit tests for modulo.core.artifacts.store (FAR-582).

Tests the LocalArtifactStore: append, finalize, read_bytes, delete_run, delete_node.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from modulo.core.artifacts.store import (
    ArtifactPointer,
    LocalArtifactStore,
    _encode_segment,
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

    assert ptr1 is not None
    assert ptr2 is not None
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


# ── _encode_segment ────────────────────────────────────────────────────


def test_encode_segment_colon():
    """Colons are encoded to %3A."""
    assert _encode_segment("run:abc:node:n1:0") == "run%3Aabc%3Anode%3An1%3A0"


def test_encode_segment_slash():
    """Slashes are encoded to %2F."""
    assert _encode_segment("a/b") == "a%2Fb"


def test_encode_segment_traversal_neutralised():
    """Traversal names get a _ prefix after encoding."""
    # After encoding, "" stays "", "." stays ".", ".." stays ".."
    # Each gets prefixed with "_"
    assert _encode_segment("") == "_"
    assert _encode_segment(".") == "_."
    assert _encode_segment("..") == "_.."


def test_encode_segment_safe_chars_pass_through():
    """Alphanumeric, dot, underscore, hyphen pass through unchanged."""
    assert _encode_segment("node_1") == "node_1"
    assert _encode_segment("run-id.test") == "run-id.test"


# ── colon-containing attempt_key round-trip ─────────────────────────────


def test_colon_attempt_key_roundtrip(tmp_path):
    """Colon-bearing attempt_key (real format) round-trips through the store."""
    store = _make_store(tmp_path)
    run_id = str(uuid.uuid4())
    node_id = "sandbox_1"
    attempt_key = f"run:{run_id}:node:{node_id}:0"

    store.append("org1", run_id, node_id, attempt_key, "stdout", "hello colon world\n")
    ptr = store.finalize("org1", run_id, node_id, attempt_key, "stdout")

    assert ptr is not None
    # The rel_path must NOT contain raw colons — they are encoded
    assert ":" not in ptr["rel_path"]
    # The filename on disk must also not contain colons
    zst = store._zst_path("org1", run_id, node_id, attempt_key, "stdout")
    assert zst.exists()
    assert ":" not in zst.name

    # Round-trip: read back the content
    result = store.read_bytes(ptr)
    assert result == b"hello colon world\n"


def test_colon_attempt_key_multiple_streams(tmp_path):
    """Colon-bearing attempt_key works for both stdout and stderr."""
    store = _make_store(tmp_path)
    run_id = str(uuid.uuid4())
    node_id = "node_0"
    attempt_key = f"run:{run_id}:node:{node_id}:0"

    store.append("org1", run_id, node_id, attempt_key, "stdout", "out\n")
    store.append("org1", run_id, node_id, attempt_key, "stderr", "err\n")
    ptr_out = store.finalize("org1", run_id, node_id, attempt_key, "stdout")
    ptr_err = store.finalize("org1", run_id, node_id, attempt_key, "stderr")

    assert ptr_out is not None
    assert ptr_err is not None
    assert store.read_bytes(ptr_out) == b"out\n"
    assert store.read_bytes(ptr_err) == b"err\n"


# ── path traversal validation ─────────────────────────────────────────


def test_validate_path_component_rejects_dotslash(tmp_path):
    """Path traversal with '..' is rejected."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="path traversal"):
        store.append("../escape", "run1", "node1", "attempt1", "stdout", "x")


def test_validate_path_component_rejects_forward_slash(tmp_path):
    """Forward slash in a path component is rejected."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="path traversal"):
        store.append("org/1", "run1", "node1", "attempt1", "stdout", "x")


def test_validate_path_component_rejects_backslash(tmp_path):
    """Backslash in a path component is rejected."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="path traversal"):
        store.append("org\\1", "run1", "node1", "attempt1", "stdout", "x")


def test_validate_path_component_rejects_absolute_windows_path(tmp_path):
    """Absolute Windows path (C:) is rejected."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="absolute path"):
        store.append("C:", "run1", "node1", "attempt1", "stdout", "x")


# ── delete_run error paths ───────────────────────────────────────────


def test_delete_run_handles_unlink_error(tmp_path, monkeypatch):
    """delete_run logs warning on OSError during file unlink."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "data")
    store.finalize("org1", "run1", "node1", "attempt1", "stdout")

    def failing_unlink(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    # Should not raise — just logs warning
    count = store.delete_run("org1", "run1")
    assert count == 0


def test_delete_run_handles_rmdir_error(tmp_path, monkeypatch):
    """delete_run handles OSError during directory removal."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "data")
    store.finalize("org1", "run1", "node1", "attempt1", "stdout")

    def failing_rmdir(self, *args, **kwargs):
        raise OSError("directory busy")

    monkeypatch.setattr(Path, "rmdir", failing_rmdir)
    # Should not raise — the rmdir error is suppressed
    count = store.delete_run("org1", "run1")
    # Files should still be deleted even if rmdir fails
    assert count >= 1


# ── delete_node error paths ──────────────────────────────────────────


def test_delete_node_nonexistent(tmp_path):
    """delete_node on non-existent node returns 0."""
    store = _make_store(tmp_path)
    count = store.delete_node("org1", "nonexistent_run", "nonexistent_node")
    assert count == 0


def test_delete_node_handles_unlink_error(tmp_path, monkeypatch):
    """delete_node logs warning on OSError during file unlink."""
    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "data")
    store.finalize("org1", "run1", "node1", "attempt1", "stdout")

    def failing_unlink(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    count = store.delete_node("org1", "run1", "node1")
    assert count == 0


# ── read_bytes uncompressed ──────────────────────────────────────────


def test_read_bytes_uncompressed(tmp_path):
    """read_bytes returns raw bytes when compression is not zstd."""
    store = _make_store(tmp_path)
    # Write a file directly without compression
    node_dir = store._node_dir("org1", "run1", "node1")
    node_dir.mkdir(parents=True, exist_ok=True)
    raw_path = node_dir / "attempt1.stdout.raw"
    raw_path.write_bytes(b"uncompressed data")

    ptr = ArtifactPointer(
        stream="stdout",
        rel_path=store._rel_path(raw_path),
        size_bytes=len(b"uncompressed data"),
        sha256="abc",
        compression="none",
    )
    result = store.read_bytes(ptr)
    assert result == b"uncompressed data"


# ── finalize failure cleanup ─────────────────────────────────────────


def test_finalize_cleanup_on_write_failure(tmp_path):
    """finalize cleans up temp file when write fails."""
    from unittest.mock import patch

    store = _make_store(tmp_path)
    store.append("org1", "run1", "node1", "attempt1", "stdout", "data")

    # The finalize flow: read raw -> compress -> write temp -> replace.
    # If write_bytes fails on the temp file, the except handler cleans it up.
    # We intercept tempfile.mkstemp to track the temp path.
    created_temps: list[str] = []
    _orig_mkstemp = __import__("tempfile").mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, path = _orig_mkstemp(*args, **kwargs)
        created_temps.append(path)
        return fd, path

    with (
        patch("tempfile.mkstemp", tracking_mkstemp),
        patch.object(Path, "write_bytes", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        store.finalize("org1", "run1", "node1", "attempt1", "stdout")
    # Temp file should have been cleaned up by the except handler
    for tmp in created_temps:
        assert not Path(tmp).exists()


# ── reset_store ──────────────────────────────────────────────────────


def test_reset_store_clears_singleton(tmp_path):
    """reset_store clears the module-level singleton."""
    from modulo.core.artifacts.store import get_store, reset_store

    # Ensure singleton is set
    store = get_store()
    assert store is not None
    # Reset it
    reset_store()
    from modulo.core.artifacts.store import _store_instance as inst_after

    assert inst_after is None
