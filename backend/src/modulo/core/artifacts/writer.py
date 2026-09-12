"""Artifact writer — buffers per-stream text and flushes to the artifact store.

Bound to a specific ``(org, run, node, attempt)`` tuple.  Buffers per stream
(``stdout``/``stderr``) and flushes periodically (mirroring the live stream's
``_STREAM_FLUSH_INTERVAL``).

Redaction: every flush redacts credentials via ``_redact_raw_output`` with a
carry-over overlap so a secret split across a flush boundary is still caught.

The writer is a no-op when ``MODULO_ARTIFACTS_ENABLED`` is ``False``.

Finalize returns ``list[pointer]`` — one per stream that had data.
"""

from __future__ import annotations

import logging
import time

from modulo.core.artifacts.store import ArtifactPointer, LocalArtifactStore, get_store

_log = logging.getLogger(__name__)

# Mirror of the live stream throttle (node_runner._STREAM_FLUSH_INTERVAL)
_FLUSH_INTERVAL = 1.0

# Carry-over overlap for redaction: the last N characters of the previous
# flush are prepended to the next flush so a secret split across a boundary
# is still redacted.  256 chars covers the longest token pattern
# (github_pat_ + 82 chars = 91 chars) with margin.
_REDACT_OVERLAP = 256


def _redact_artifact_text(text: str) -> str:
    """Best-effort credential scrub for artifact text.

    Imports ``_redact_raw_output`` lazily to avoid circular imports with
    ``node_runner``.
    """
    if not text:
        return text
    try:
        from modulo.core.pipeline_engine.node_runner import _redact_raw_output

        return _redact_raw_output(text)
    except ImportError:
        return text


class ArtifactWriter:
    """Per-node artifact writer.

    Parameters
    ----------
    org_id, run_id, node_id, attempt_key:
        Identify the node run this writer is bound to.
    store:
        The artifact store to write to.
    enabled:
        When ``False``, all methods are no-ops (no I/O).
    """

    def __init__(
        self,
        *,
        org_id: str,
        run_id: str,
        node_id: str,
        attempt_key: str,
        store: LocalArtifactStore | None = None,
        enabled: bool = True,
    ) -> None:
        self._org_id = org_id
        self._run_id = run_id
        self._node_id = node_id
        self._attempt_key = attempt_key
        self._enabled = enabled
        self._store = store or get_store() if enabled else None
        # Per-stream state
        self._buf: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self._last_flush_ts: dict[str, float] = {"stdout": time.monotonic(), "stderr": time.monotonic()}
        # Carry-over for redaction overlap: last _REDACT_OVERLAP chars of the
        # previous flush that were already redacted.  When the next flush
        # arrives, this overlap is prepended to catch split-boundary secrets.
        self._prev_tail: dict[str, str] = {"stdout": "", "stderr": ""}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def append(self, text: str, stream: str) -> None:
        """Append *text* to the in-memory buffer for *stream*.

        Flushes to disk when the flush interval has elapsed.
        """
        if not self._enabled or not text:
            return
        buf = self._buf.get(stream)
        if buf is None:
            return
        buf.append(text)
        now = time.monotonic()
        if now - self._last_flush_ts.get(stream, 0.0) >= _FLUSH_INTERVAL:
            self.flush(stream)

    def flush(self, stream: str) -> None:
        """Flush the in-memory buffer for *stream* to disk (redacted)."""
        if not self._enabled or self._store is None:
            return
        buf = self._buf.get(stream)
        if not buf:
            return
        raw_text = "".join(buf)
        buf.clear()
        self._last_flush_ts[stream] = time.monotonic()

        # Prepend the carry-over overlap from the previous flush so that a
        # secret split across two flushes is still redacted.
        overlap = self._prev_tail.get(stream, "")
        combined = overlap + raw_text if overlap else raw_text

        # Redact the combined text
        redacted = _redact_artifact_text(combined)

        # The overlap portion of the redacted text becomes the new tail
        if overlap:
            # We only want the NEW portion after the overlap in the stored text
            # But the overlap may have changed the redaction, so store the
            # full redacted text and remember the tail for next time.
            stored_text = redacted
            self._prev_tail[stream] = redacted[-_REDACT_OVERLAP:] if len(redacted) > _REDACT_OVERLAP else redacted
        else:
            stored_text = redacted
            self._prev_tail[stream] = redacted[-_REDACT_OVERLAP:] if len(redacted) > _REDACT_OVERLAP else redacted

        try:
            self._store.append(
                self._org_id,
                self._run_id,
                self._node_id,
                self._attempt_key,
                stream,
                stored_text,
            )
        except Exception:
            _log.exception(
                "artifact_writer.flush_failed",
                extra={
                    "org_id": self._org_id,
                    "run_id": self._run_id,
                    "node_id": self._node_id,
                    "stream": stream,
                },
            )

    def finalize(self) -> list[ArtifactPointer]:
        """Flush remaining buffers, compress, and return pointers.

        One pointer per stream that had data (may be empty).
        """
        if not self._enabled or self._store is None:
            return []

        pointers: list[ArtifactPointer] = []
        for stream in ("stdout", "stderr"):
            # Flush any remaining buffered data
            self.flush(stream)
            ptr = self._store.finalize(
                self._org_id,
                self._run_id,
                self._node_id,
                self._attempt_key,
                stream,
            )
            if ptr is not None:
                pointers.append(ptr)
        return pointers
