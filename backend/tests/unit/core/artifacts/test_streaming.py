"""Unit tests for modulo.core.artifacts.streaming (FAR-811).

Tests StreamingArtifactWriter: incremental SHA256, byte-cap enforcement,
finalize round-trip, cleanup, context manager, and edge cases.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from modulo.core.artifacts.store import LocalArtifactStore
from modulo.core.artifacts.streaming import StreamingArtifactWriter


def _make_store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def _make_writer(
    tmp_path,
    *,
    max_bytes: int | None = None,
    stream: str = "stdout",
) -> tuple[StreamingArtifactWriter, LocalArtifactStore]:
    store = _make_store(tmp_path)
    writer = StreamingArtifactWriter(
        store,
        org_id="org1",
        run_id="run1",
        node_id="node1",
        attempt_key="attempt1",
        stream=stream,
        max_bytes=max_bytes,
    )
    return writer, store


# ── basic round-trip ──────────────────────────────────────────────────


def test_write_and_finalize(tmp_path):
    """Write chunks, finalize, verify artifact on disk."""
    writer, store = _make_writer(tmp_path)
    writer.write("hello\n")
    writer.write("world\n")

    ptr = writer.finalize()

    assert ptr is not None
    assert ptr["stream"] == "stdout"
    assert store.read_bytes(ptr) == b"hello\nworld\n"
    assert ptr["size_bytes"] == len(b"hello\nworld\n")
    assert ptr["sha256"] == hashlib.sha256(b"hello\nworld\n").hexdigest()


def test_incremental_sha_matches_full_hash(tmp_path):
    """Incremental SHA256 matches computing it over the full content."""
    writer, _ = _make_writer(tmp_path)
    chunks = ["line1\n", "line2\n", "line3\n"]
    for chunk in chunks:
        writer.write(chunk)

    full_content = "".join(chunks).encode("utf-8")
    expected_sha = hashlib.sha256(full_content).hexdigest()

    ptr = writer.finalize()
    assert ptr["sha256"] == expected_sha
    assert ptr["size_bytes"] == len(full_content)


def test_single_chunk(tmp_path):
    """A single write works correctly."""
    writer, store = _make_writer(tmp_path)
    writer.write("single chunk\n")
    ptr = writer.finalize()
    assert ptr is not None
    assert store.read_bytes(ptr) == b"single chunk\n"


def test_finalize_no_data(tmp_path):
    """Finalize with no writes returns None."""
    writer, _ = _make_writer(tmp_path)
    ptr = writer.finalize()
    assert ptr is None


def test_empty_chunks_ignored(tmp_path):
    """Empty strings don't count toward bytes_written."""
    writer, _ = _make_writer(tmp_path)
    writer.write("")
    writer.write("")
    assert writer.bytes_written == 0
    ptr = writer.finalize()
    assert ptr is None


# ── byte-cap enforcement ──────────────────────────────────────────────


def test_cap_enforced(tmp_path):
    """Writes beyond max_bytes are silently dropped."""
    writer, store = _make_writer(tmp_path, max_bytes=10)
    writer.write("hello")  # 5 bytes
    writer.write(" world!")  # 7 bytes — would be 12 total, capped at 10
    writer.write(" extra")  # all dropped

    assert writer.bytes_written == 10
    assert writer.truncated

    ptr = writer.finalize()
    assert ptr is not None
    content = store.read_bytes(ptr)
    assert len(content) == 10
    assert content == b"hello worl"


def test_cap_exact_fit(tmp_path):
    """Writing exactly max_bytes — truncated is True (at cap boundary)."""
    writer, _ = _make_writer(tmp_path, max_bytes=5)
    writer.write("hello")
    assert writer.bytes_written == 5
    assert writer.truncated
    # Further writes are dropped
    writer.write("more")
    assert writer.bytes_written == 5


def test_cap_not_hit(tmp_path):
    """When content is under the cap, truncated is False."""
    writer, _ = _make_writer(tmp_path, max_bytes=100)
    writer.write("short")
    assert not writer.truncated
    assert writer.bytes_written == 5


def test_no_cap(tmp_path):
    """When max_bytes is None, no cap is enforced."""
    writer, _ = _make_writer(tmp_path, max_bytes=None)
    writer.write("x" * 100_000)
    assert writer.bytes_written == 100_000
    assert not writer.truncated


def test_cap_truncates_mid_utf8(tmp_path):
    """Cap truncation respects UTF-8 byte boundaries."""
    # "e\u0301" is 2 bytes in UTF-8 (0xC3 0xA9)
    writer, store = _make_writer(tmp_path, max_bytes=5)
    writer.write("ab")  # 2 bytes
    writer.write("c\u00e9d")  # "c" = 1, "\u00e9d" = 3 bytes -> total 6, capped at 5

    assert writer.bytes_written == 5
    ptr = writer.finalize()
    content = store.read_bytes(ptr)
    # "abc" (3 bytes) + first 2 bytes of "\u00e9d" -> may produce replacement char
    assert len(content) == 5


# ── finalize lifecycle ────────────────────────────────────────────────


def test_finalize_sets_finalized(tmp_path):
    """After finalize(), finalized is True."""
    writer, _ = _make_writer(tmp_path)
    writer.write("data")
    writer.finalize()
    assert writer.finalized


def test_finalize_raises_on_double_call(tmp_path):
    """Calling finalize() twice raises RuntimeError."""
    writer, _ = _make_writer(tmp_path)
    writer.write("data")
    writer.finalize()
    with pytest.raises(RuntimeError, match=r"finalize.*called twice"):
        writer.finalize()


def test_write_raises_after_finalize(tmp_path):
    """Calling write() after finalize() raises RuntimeError."""
    writer, _ = _make_writer(tmp_path)
    writer.write("data")
    writer.finalize()
    with pytest.raises(RuntimeError, match=r"write.*called after"):
        writer.write("more")


# ── cleanup ───────────────────────────────────────────────────────────


def test_cleanup_removes_raw_file(tmp_path):
    """cleanup() removes the orphaned .tmp file."""
    writer, store = _make_writer(tmp_path)
    writer.write("data")
    # The raw file should exist after write (via store.append)
    raw = store._raw_path("org1", "run1", "node1", "attempt1", "stdout")
    assert raw.exists()

    writer.cleanup()
    assert not raw.exists()
    assert writer.finalized


def test_cleanup_no_raw_file(tmp_path):
    """cleanup() is safe even when no data was written."""
    writer, _ = _make_writer(tmp_path)
    writer.cleanup()
    assert writer.finalized


def test_cleanup_idempotent(tmp_path):
    """cleanup() can be called multiple times safely."""
    writer, store = _make_writer(tmp_path)
    writer.write("data")
    raw = store._raw_path("org1", "run1", "node1", "attempt1", "stdout")
    assert raw.exists()

    writer.cleanup()
    assert not raw.exists()
    assert writer.finalized
    writer.cleanup()  # no error
    assert not raw.exists()
    assert writer.finalized


def test_cleanup_after_finalize(tmp_path):
    """cleanup() after finalize is safe (no-op for raw file)."""
    writer, store = _make_writer(tmp_path)
    writer.write("data")
    ptr = writer.finalize()
    assert ptr is not None
    raw = store._raw_path("org1", "run1", "node1", "attempt1", "stdout")
    assert not raw.exists()

    writer.cleanup()  # raw already gone, should not error
    assert not raw.exists()
    assert writer.finalized


# ── context manager ───────────────────────────────────────────────────


def test_context_manager_cleanup_on_error(tmp_path):
    """__exit__ calls cleanup when not finalized (exception path)."""
    store = _make_store(tmp_path)
    with (  # noqa: PT012
        pytest.raises(ValueError, match="simulated crash"),
        StreamingArtifactWriter(
            store,
            org_id="org1",
            run_id="run1",
            node_id="node1",
            attempt_key="attempt1",
            stream="stdout",
        ) as writer,
    ):
        writer.write("data")
        raise ValueError("simulated crash")

    # The raw file should be cleaned up
    raw = store._raw_path("org1", "run1", "node1", "attempt1", "stdout")
    assert not raw.exists()


def test_context_manager_no_cleanup_after_finalize(tmp_path):
    """__exit__ does not double-cleanup when already finalized."""
    store = _make_store(tmp_path)
    with StreamingArtifactWriter(
        store,
        org_id="org1",
        run_id="run1",
        node_id="node1",
        attempt_key="attempt1",
        stream="stdout",
    ) as writer:
        writer.write("data")
        ptr = writer.finalize()
        assert ptr is not None
    # No error, finalization was successful


# ── stderr stream ─────────────────────────────────────────────────────


def test_stderr_stream(tmp_path):
    """Stderr works identically to stdout."""
    writer, store = _make_writer(tmp_path, stream="stderr")
    writer.write("error output\n")
    ptr = writer.finalize()
    assert ptr is not None
    assert ptr["stream"] == "stderr"
    assert store.read_bytes(ptr) == b"error output\n"


# ── large content ─────────────────────────────────────────────────────


def test_large_content(tmp_path):
    """Multi-MB content writes and finalizes correctly."""
    writer, store = _make_writer(tmp_path)
    large_chunk = "x" * (2 * 1024 * 1024)  # 2 MB
    writer.write(large_chunk)
    writer.write(large_chunk)

    ptr = writer.finalize()
    assert ptr is not None
    assert ptr["size_bytes"] == 4 * 1024 * 1024
    content = store.read_bytes(ptr)
    assert len(content) == 4 * 1024 * 1024


def test_large_content_with_cap(tmp_path):
    """Cap works correctly with large chunks."""
    cap = 1024 * 1024  # 1 MB
    writer, _store = _make_writer(tmp_path, max_bytes=cap)
    writer.write("x" * (2 * 1024 * 1024))  # 2 MB — should be capped

    assert writer.bytes_written == cap
    assert writer.truncated
    ptr = writer.finalize()
    assert ptr is not None
    assert ptr["size_bytes"] == cap


# ── properties ────────────────────────────────────────────────────────


def test_bytes_written_tracks_accurately(tmp_path):
    """bytes_written accurately tracks total bytes."""
    writer, _ = _make_writer(tmp_path)
    assert writer.bytes_written == 0
    writer.write("abc")
    assert writer.bytes_written == 3
    writer.write("de")
    assert writer.bytes_written == 5
    writer.write("")
    assert writer.bytes_written == 5


def test_max_bytes_property(tmp_path):
    """max_bytes property returns the configured cap."""
    writer, _ = _make_writer(tmp_path, max_bytes=42)
    assert writer.max_bytes == 42


def test_max_bytes_none_property(tmp_path):
    """max_bytes=None means unlimited."""
    writer, _ = _make_writer(tmp_path, max_bytes=None)
    assert writer.max_bytes is None


# ── finalize with cap: incremental sha matches actual bytes ───────────


def test_finalize_sha_matches_capped_content(tmp_path):
    """SHA256 is computed over the capped bytes, not the full attempted content."""
    writer, _ = _make_writer(tmp_path, max_bytes=5)
    writer.write("hello world extra")

    # Only "hello" (5 bytes) should be hashed
    expected_sha = hashlib.sha256(b"hello").hexdigest()
    ptr = writer.finalize()
    assert ptr["sha256"] == expected_sha
    assert ptr["size_bytes"] == 5


# ── store append delegation ──────────────────────────────────────────


def test_write_delegates_to_store_append(tmp_path):
    """write() calls store.append with the correct arguments."""
    writer, store = _make_writer(tmp_path)
    with patch.object(store, "append") as mock_append:
        writer.write("test data")
        mock_append.assert_called_once_with("org1", "run1", "node1", "attempt1", "stdout", "test data")


def test_write_cap_truncated_delegates_partial(tmp_path):
    """When capped, only the truncated portion is delegated to store.append."""
    writer, store = _make_writer(tmp_path, max_bytes=3)
    with patch.object(store, "append") as mock_append:
        writer.write("hello world")
        # Only "hel" (3 bytes) should be written
        mock_append.assert_called_once()
        call_args = mock_append.call_args
        assert call_args[0][5] == "hel"  # 6th positional arg is text
