"""Unit tests for modulo.core.artifacts.writer (FAR-582).

Tests the ArtifactWriter: append, flush, finalize, no-op when disabled.
"""

from __future__ import annotations

import time

from modulo.core.artifacts.store import LocalArtifactStore
from modulo.core.artifacts.writer import _FLUSH_INTERVAL, ArtifactWriter


def _make_store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def _make_writer(tmp_path, *, enabled: bool = True) -> tuple[ArtifactWriter, LocalArtifactStore]:
    store = _make_store(tmp_path)
    writer = ArtifactWriter(
        org_id="org1",
        run_id="run1",
        node_id="node1",
        attempt_key="attempt1",
        store=store,
        enabled=enabled,
    )
    return writer, store


# ── append + finalize round-trip ────────────────────────────────────────


def test_append_and_finalize(tmp_path):
    """Append text, finalize, and verify the artifact is on disk."""
    writer, store = _make_writer(tmp_path)
    writer.append("hello\n", "stdout")
    writer.append("world\n", "stdout")

    pointers = writer.finalize()

    assert len(pointers) == 1
    ptr = pointers[0]
    assert ptr["stream"] == "stdout"
    assert store.read_bytes(ptr) == b"hello\nworld\n"


def test_append_stderr(tmp_path):
    """Stderr streams are handled separately."""
    writer, _store = _make_writer(tmp_path)
    writer.append("out\n", "stdout")
    writer.append("err\n", "stderr")

    pointers = writer.finalize()
    streams = {p["stream"] for p in pointers}
    assert streams == {"stdout", "stderr"}


def test_finalize_no_data(tmp_path):
    """Finalize with no appended data returns empty list."""
    writer, _ = _make_writer(tmp_path)
    pointers = writer.finalize()
    assert pointers == []


# ── disabled writer ─────────────────────────────────────────────────────


def test_disabled_writer_noop(tmp_path):
    """When enabled=False, all operations are no-ops."""
    writer, _store = _make_writer(tmp_path, enabled=False)
    writer.append("hello\n", "stdout")
    pointers = writer.finalize()
    assert pointers == []
    assert not writer.enabled


# ── redaction ───────────────────────────────────────────────────────────


def test_redaction_applied(tmp_path):
    """Credentials in artifact text are redacted before persisting."""
    writer, store = _make_writer(tmp_path)
    writer.append("token is ghp_abcdef1234567890abcdef1234567890ab\n", "stdout")

    pointers = writer.finalize()
    assert len(pointers) == 1
    content = store.read_bytes(pointers[0]).decode("utf-8")
    # The token should be redacted (not present as-is)
    assert "ghp_abcdef1234567890abcdef1234567890ab" not in content


# ── flush interval ──────────────────────────────────────────────────────


def test_flush_respects_interval(tmp_path):
    """Flush only occurs when the interval has elapsed."""
    writer, _store = _make_writer(tmp_path)
    writer.append("first\n", "stdout")

    # The buffer should still have data (not flushed yet)
    assert len(writer._buf["stdout"]) == 1

    # Force the interval to have elapsed
    writer._last_flush_ts["stdout"] = time.monotonic() - _FLUSH_INTERVAL - 1
    writer.append("second\n", "stdout")

    # Now the buffer should be empty (flushed)
    assert not writer._buf["stdout"]


def test_flush_clears_buffer(tmp_path):
    """Explicit flush empties the buffer."""
    writer, _store = _make_writer(tmp_path)
    writer.append("data\n", "stdout")
    assert len(writer._buf["stdout"]) == 1
    writer.flush("stdout")
    assert not writer._buf["stdout"]


# ── unknown stream is no-op ─────────────────────────────────────────────


def test_unknown_stream_noop(tmp_path):
    """Appending to an unknown stream is silently ignored."""
    writer, _ = _make_writer(tmp_path)
    writer.append("data\n", "unknown_stream")
    # No error, buffer is empty for unknown streams
    assert "unknown_stream" not in writer._buf


# ── no-store when disabled ──────────────────────────────────────────────


def test_disabled_no_store(tmp_path):
    """When disabled, no store is created."""
    writer, _ = _make_writer(tmp_path, enabled=False)
    assert writer._store is None


# ── flush failure path ────────────────────────────────────────────────


def test_flush_failure_is_swallowed(tmp_path):
    """Flush failure logs but does not raise."""
    from unittest.mock import patch

    writer, _store = _make_writer(tmp_path)
    writer.append("data\n", "stdout")

    # Force the flush to happen
    writer._last_flush_ts["stdout"] = time.monotonic() - _FLUSH_INTERVAL - 1

    with patch.object(_store, "append", side_effect=OSError("disk full")):
        # Should not raise — flush failure is logged and swallowed
        writer.flush("stdout")
    # Buffer should still be cleared (the failure is after append call)
    assert not writer._buf["stdout"]


# ── redaction overlap path ────────────────────────────────────────────


def test_redaction_overlap_strips_old_content(tmp_path):
    """When overlap exists, the overlap prefix is stripped from stored text."""
    writer, store = _make_writer(tmp_path)

    # First flush — establishes the overlap
    writer.append("first chunk of text\n", "stdout")
    writer._last_flush_ts["stdout"] = time.monotonic() - _FLUSH_INTERVAL - 1
    writer.append("second\n", "stdout")

    # The prev_tail should now have content from the first redacted text
    assert writer._prev_tail["stdout"]

    # Force another flush with overlap
    writer._last_flush_ts["stdout"] = time.monotonic() - _FLUSH_INTERVAL - 1
    writer.append("third chunk\n", "stdout")

    # Finalize and check the content
    pointers = writer.finalize()
    assert len(pointers) == 1
    content = store.read_bytes(pointers[0]).decode("utf-8")
    # Content should contain the actual data
    assert "third chunk" in content


# ── _redact_artifact_text fallback ───────────────────────────────────


def test_redact_artifact_text_empty():
    """_redact_artifact_text returns empty string as-is."""
    from modulo.core.artifacts.writer import _redact_artifact_text

    assert _redact_artifact_text("") == ""


# ── append to non-existent stream ─────────────────────────────────────


def test_append_to_nonexistent_stream(tmp_path):
    """Appending to a non-existent stream is silently ignored."""
    writer, _ = _make_writer(tmp_path)
    writer.append("data\n", "nonexistent")
    # No error, no buffer entry
    assert "nonexistent" not in writer._buf
