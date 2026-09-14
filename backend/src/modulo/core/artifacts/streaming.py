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

The primitive is confined to the ``artifacts`` package and is NOT wired into
``node_runner.py`` yet — that is a follow-up task.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import suppress

from modulo.core.artifacts.store import ArtifactPointer, LocalArtifactStore

_log = logging.getLogger(__name__)


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

        When the cap is reached, the remainder of *chunk* is silently
        dropped.  Empty chunks are ignored.
        """
        if self._finalized:
            raise RuntimeError("write() called after finalize/cleanup")
        if not chunk:
            return

        chunk_bytes = chunk.encode("utf-8")

        if self._max_bytes is not None:
            remaining = self._max_bytes - self._bytes_written
            if remaining <= 0:
                return
            if len(chunk_bytes) > remaining:
                chunk_bytes = chunk_bytes[:remaining]

        self._hasher.update(chunk_bytes)
        self._bytes_written += len(chunk_bytes)

        # Decode back to str for the store's text-oriented append.
        text = chunk_bytes.decode("utf-8", errors="replace")
        self._store.append(
            self._org_id,
            self._run_id,
            self._node_id,
            self._attempt_key,
            self._stream,
            text,
        )

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
