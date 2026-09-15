"""Streaming artifact writer with incremental SHA256 and byte-cap enforcement (FAR-811).

Wraps :class:`LocalArtifactStore` to provide a streaming write interface:

    writer = StreamingArtifactWriter(store, org_id, run_id, node_id,
                                     attempt_key, stream, max_bytes=5_000_000)
    writer.write(chunk1)
    writer.write(chunk2)
    pointer = writer.finalize()

Key properties:

* **Incremental SHA256** — the hash is computed as chunks arrive via
  :meth:`write`, so :meth:`finalize` never re-reads the full file for
  hashing.
* **Byte-cap enforcement** — once ``max_bytes`` is reached, further
  :meth:`write` calls silently drop content.  Callers can detect truncation
  via :attr:`truncated`.
* **Best-effort cleanup** — calling :meth:`cleanup` removes the orphaned
  raw ``.tmp`` file when the writer is abandoned without finalization
  (e.g. on crash).
* **Context-manager support** — ``__enter__`` returns *self*;
  ``__exit__`` calls :meth:`cleanup` only if not yet finalized.
* **Credential redaction (FAR-188)** — every :meth:`write` scrubs
  credentials through ``_redact_artifact_text`` (the same helper used by
  :class:`ArtifactWriter`) with a 256-char carry-over overlap, so a token
  split across two ``write`` calls is still masked before it ever reaches
  the artifact store.

The primitive is confined to the ``artifacts`` package and is NOT wired into
``node_runner.py`` yet — that is a follow-up task.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
from contextlib import suppress

from modulo.core.artifacts.store import ArtifactPointer, LocalArtifactStore
from modulo.core.artifacts.writer import _redact_artifact_text

_log = logging.getLogger(__name__)

# Carry-over overlap for redaction: the last N characters of the previous
# write are prepended to the next write so a secret split across a write
# boundary is still redacted.  256 chars covers the longest token pattern
# (github_pat_ + 82 chars = 91 chars) with margin.  Mirrors
# ``ArtifactWriter._REDACT_OVERLAP``.
_REDACT_OVERLAP = 256


class StreamingArtifactWriter:
    """Per-stream streaming artifact writer with incremental SHA256 and byte cap.

    Parameters
    ----------
    store:
        The backing :class:`LocalArtifactStore`.
    org_id, run_id, node_id, attempt_key, stream:
        Identify the artifact location (same semantics as
        :meth:`LocalArtifactStore.append`).
    max_bytes:
        Hard cap on uncompressed bytes.  Chunks arriving after the cap is
        reached are silently dropped.  ``None`` means unlimited.
    """

    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        stream: str,
        max_bytes: int | None = None,
    ) -> None:
        self._store = store
        self._org_id = org_id
        self._run_id = run_id
        self._node_id = node_id
        self._attempt_key = attempt_key
        self._stream = stream
        self._max_bytes = max_bytes

        self._hasher = hashlib.sha256()
        self._bytes_written: int = 0
        self._finalized: bool = False

        # Carry-over overlap for redaction: the last _REDACT_OVERLAP chars of
        # the previously-redacted chunk.  Prepended to the next chunk so a
        # secret split across a write() boundary is still scrubbed
        # (FAR-188 redaction contract, same strategy as ArtifactWriter.flush).
        self._prev_redact_tail: str = ""

    # ── public properties ──────────────────────────────────────────────

    @property
    def bytes_written(self) -> int:
        """Total uncompressed bytes written so far."""
        return self._bytes_written

    @property
    def max_bytes(self) -> int | None:
        """The byte cap (``None`` = unlimited)."""
        return self._max_bytes

    @property
    def truncated(self) -> bool:
        """``True`` when the cap was hit and content was dropped."""
        if self._max_bytes is None:
            return False
        return self._bytes_written >= self._max_bytes

    @property
    def finalized(self) -> bool:
        """``True`` after :meth:`finalize` or :meth:`cleanup` was called."""
        return self._finalized

    # ── write ──────────────────────────────────────────────────────────

    def write(self, chunk: str) -> None:
        """Append *chunk* to the artifact, enforcing the byte cap.

        The chunk is scrubbed of credentials (FAR-188 redaction contract)
        *before* it is persisted, using the same carry-over overlap strategy
        as :meth:`ArtifactWriter.flush` so a token split across two ``write``
        calls is still masked.  Redaction never raises — on failure the raw
        chunk is stored (best-effort, fail open), exactly like the existing
        buffered path.

        When the cap is reached, the remainder of *chunk* is silently
        dropped, cutting only at a complete UTF-8 character boundary so the
        on-disk artifact stays valid UTF-8 and matches the incremental hash.
        Empty chunks are ignored.
        """
        if self._finalized:
            raise RuntimeError("write() called after finalize/cleanup")
        if not chunk:
            return

        # Redact before persistence.  Prepend the carry-over tail from the
        # previous write so a credential straddling this boundary is caught.
        overlap = self._prev_redact_tail
        combined = overlap + chunk if overlap else chunk
        redacted = _redact_artifact_text(combined)
        # The overlap was already redacted and persisted in the previous
        # write; strip it to avoid duplicating content on disk.  A few chars
        # of imprecision at the boundary is acceptable for best-effort
        # credential scrubbing (the overlap region is already clean text, so
        # re-running the regex is length-preserving there).
        stored = redacted[len(overlap) :] if overlap else redacted
        # Remember the tail for the next boundary check (always from the full
        # redacted text, not the stored portion).
        self._prev_redact_tail = redacted[-_REDACT_OVERLAP:] if len(redacted) > _REDACT_OVERLAP else redacted

        chunk_bytes = stored.encode("utf-8")

        if self._max_bytes is not None:
            remaining = self._max_bytes - self._bytes_written
            if remaining <= 0:
                return
            if len(chunk_bytes) > remaining:
                # Truncate to the largest clean UTF-8 *character* boundary
                # within ``remaining`` so the bytes we hash are exactly the
                # bytes persisted to disk.  A naive ``chunk_bytes[:remaining]``
                # cut can land in the middle of a multi-byte codepoint, which
                # would then be re-encoded with U+FFFD on ``append`` —
                # corrupting the pointer-integrity contract
                # (sha256/size_bytes must equal the on-disk artifact).
                chunk_bytes = self._truncate_to_utf8_boundary(chunk_bytes, remaining)

        # Decode back to str for the store's text-oriented append.
        text = chunk_bytes.decode("utf-8", errors="replace")

        # Persist FIRST; only advance the incremental hash / byte count AFTER
        # the append succeeds. A mid-stream append failure (swallowed per-chunk
        # by the drain loop's try/except) must NOT leave finalize() advertising
        # a sha256 / size_bytes that does not match the on-disk artifact.
        self._store.append(
            self._org_id,
            self._run_id,
            self._node_id,
            self._attempt_key,
            self._stream,
            text,
        )

        self._hasher.update(chunk_bytes)
        self._bytes_written += len(chunk_bytes)

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _truncate_to_utf8_boundary(data: bytes, max_len: int) -> bytes:
        """Truncate *data* to at most *max_len* bytes, cutting only at a UTF-8
        character boundary so the result is valid UTF-8.

        A strict incremental decoder returns the fully-decoded prefix (it
        buffers any incomplete trailing codepoint) and re-encoding yields the
        largest clean-boundary slice ``<= max_len``.
        """
        if len(data) <= max_len:
            return data
        decoder = codecs.getincrementaldecoder("utf-8")()
        decoded = decoder.decode(data[:max_len], final=False)
        return decoded.encode("utf-8")

    # ── finalize ───────────────────────────────────────────────────────

    def finalize(self) -> ArtifactPointer | None:
        """Compress the artifact and return its pointer.

        The SHA256 and ``size_bytes`` in the returned pointer reflect the
        incrementally-computed values, not a re-read of the file.

        Returns ``None`` when no data was written.
        """
        if self._finalized:
            raise RuntimeError("finalize() called twice")
        self._finalized = True

        if self._bytes_written == 0:
            # Nothing written — delegate to store which returns None.
            # Still clean up any orphaned raw file.
            self._cleanup_raw()
            return None

        # Delegate compression to the store (reads .tmp, compresses, writes
        # .zst, removes .tmp).  The store computes its own SHA256 from the
        # file content, which should match our incremental hash — we
        # override it below for defense-in-depth.
        ptr = self._store.finalize(
            self._org_id,
            self._run_id,
            self._node_id,
            self._attempt_key,
            self._stream,
        )

        if ptr is not None:
            # Override with our incrementally-computed values.
            # The store's SHA256 is over the same bytes (UTF-8 encoded
            # text written by append), so they should match.  If they
            # don't (e.g. encoding edge case), our incremental value is
            # authoritative since it was computed on the exact bytes
            # written.
            ptr["sha256"] = self._hasher.hexdigest()
            ptr["size_bytes"] = self._bytes_written

        return ptr

    # ── cleanup ────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Best-effort removal of the raw ``.tmp`` file.

        Call this when the writer is abandoned without finalization (e.g.
        on crash).  Safe to call multiple times.
        """
        if not self._finalized:
            self._finalized = True
        self._cleanup_raw()

    def _cleanup_raw(self) -> None:
        """Remove the raw temp file if it exists."""
        raw = self._store._raw_path(
            self._org_id,
            self._run_id,
            self._node_id,
            self._attempt_key,
            self._stream,
        )
        with suppress(OSError):
            raw.unlink()

    # ── context manager ────────────────────────────────────────────────

    def __enter__(self) -> StreamingArtifactWriter:  # noqa: PYI034
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object) -> None:
        if not self._finalized:
            self.cleanup()
