"""Unit tests for the StreamingArtifactWriter integrity contract (FAR-844).

The writer's ``finalize()`` advertises an incremental ``sha256`` / ``size_bytes``
that MUST match the bytes actually persisted to the artifact store. A mid-stream
``store.append`` failure (swallowed per-chunk by the drain loop) must not leave
the writer having counted bytes the store never received.
"""

import hashlib

import pytest

from modulo.core.artifacts.store import LocalArtifactStore
from modulo.core.artifacts.streaming import StreamingArtifactWriter


def test_write_does_not_count_bytes_when_append_fails(tmp_path):
    """A failed ``store.append`` must NOT advance the incremental hash / byte
    count, so ``finalize()`` never advertises a ``sha256`` / ``size_bytes`` that
    disagrees with the on-disk artifact.

    Regression for the review finding that ``write()`` incremented the hasher /
    byte count BEFORE ``store.append`` succeeded — a swallowed per-chunk append
    failure left ``finalize()`` advertising bytes the store never persisted."""
    store = LocalArtifactStore(tmp_path)
    key = "run:r:node:n:key:full:1024"
    writer = StreamingArtifactWriter(
        store,
        org_id="o",
        run_id="r",
        node_id="n",
        attempt_key=key,
        stream="stdout",
        max_bytes=None,
    )

    good = "a" * 100
    writer.write(good)
    assert writer.bytes_written == 100

    # Simulate a transient store failure on the next append. The streaming
    # primitive never raises on append failure (the drain loop swallows it), so
    # write() must return normally — and must NOT count the lost bytes.
    real_append = store.append

    def _boom(*args, **kwargs):
        raise RuntimeError("transient store error")

    store.append = _boom
    with pytest.raises(RuntimeError):
        writer.write("b" * 50)

    # The lost chunk must not be counted: on the unfixed code the hasher /
    # byte count was already advanced before the append raised, so this would be
    # 150 instead of 100.
    assert writer.bytes_written == 100

    # Restore a working store and finalize what actually landed (only the good
    # chunk). The advertised pointer must agree with the on-disk artifact.
    store.append = real_append
    ptr = writer.finalize()
    assert ptr is not None
    assert ptr["size_bytes"] == 100
    assert ptr["sha256"] == hashlib.sha256(good.encode("utf-8")).hexdigest()


def test_finalize_sha256_matches_on_disk_after_partial_append(tmp_path):
    """End-to-end integrity: after a real append + a failed append, the
    compressed artifact read back from the store decodes to exactly the bytes
    the writer counted, and the advertised sha256 matches."""
    store = LocalArtifactStore(tmp_path)
    key = "run:r:node:n:key2:full:1024"
    writer = StreamingArtifactWriter(
        store,
        org_id="o",
        run_id="r",
        node_id="n",
        attempt_key=key,
        stream="stdout",
        max_bytes=None,
    )

    chunk_a = "hello "
    chunk_b = "world"
    writer.write(chunk_a)

    real_append = store.append

    def _boom(*args, **kwargs):
        raise RuntimeError("transient store error")

    store.append = _boom
    with pytest.raises(RuntimeError):
        writer.write("lost")  # never persisted

    store.append = real_append
    writer.write(chunk_b)  # actually persisted

    ptr = writer.finalize()
    assert ptr is not None
    assert ptr["size_bytes"] == len((chunk_a + chunk_b).encode("utf-8"))
    assert ptr["sha256"] == hashlib.sha256((chunk_a + chunk_b).encode("utf-8")).hexdigest()

    # Read back the compressed artifact and confirm it is the exact bytes counted.
    assert store.read_bytes(dict(ptr)) == (chunk_a + chunk_b).encode("utf-8")
