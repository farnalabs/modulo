"""Integration test for artifact lifecycle (FAR-582).

Tests the full lifecycle: store creation, append, finalize, DB persistence,
read-back via API endpoint.  Uses the real local filesystem store.

NOTE: This test does NOT require a running Postgres instance — it tests the
store and writer directly.  The DB persistence path (persist_artifact_pointers)
is tested separately in unit tests with a mock session.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from modulo.core.artifacts.store import LocalArtifactStore
from modulo.core.artifacts.writer import ArtifactWriter

pytestmark = pytest.mark.integration


def _make_store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


# ── end-to-end lifecycle ────────────────────────────────────────────────


def test_full_lifecycle(tmp_path):
    """Full lifecycle: create store, write, finalize, read, delete."""
    store = _make_store(tmp_path)
    org_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    node_id = "sandbox_1"
    attempt_key = f"run:{run_id}:node:{node_id}:0"

    # Create writer and append data
    writer = ArtifactWriter(
        org_id=org_id,
        run_id=run_id,
        node_id=node_id,
        attempt_key=attempt_key,
        store=store,
        enabled=True,
    )
    writer.append("Starting pipeline...\n", "stdout")
    writer.append("Processing item 1\n", "stdout")
    writer.append("Error: something went wrong\n", "stderr")
    writer.append("Processing item 2\n", "stdout")

    # Finalize
    pointers = writer.finalize()
    assert len(pointers) == 2
    streams = {p["stream"] for p in pointers}
    assert streams == {"stdout", "stderr"}

    # Read back
    stdout_ptr = next(p for p in pointers if p["stream"] == "stdout")
    stderr_ptr = next(p for p in pointers if p["stream"] == "stderr")

    stdout_content = store.read_bytes(stdout_ptr).decode("utf-8")
    stderr_content = store.read_bytes(stderr_ptr).decode("utf-8")

    assert "Starting pipeline..." in stdout_content
    assert "Processing item 1" in stdout_content
    assert "Processing item 2" in stdout_content
    assert "Error: something went wrong" in stderr_content

    # Verify pointer contract
    for ptr in pointers:
        assert "stream" in ptr
        assert "rel_path" in ptr
        assert "size_bytes" in ptr
        assert "sha256" in ptr
        assert "compression" in ptr
        assert ptr["compression"] == "zstd"

    # Verify SHA256
    assert stdout_ptr["sha256"] == hashlib.sha256(stdout_content.encode("utf-8")).hexdigest()

    # Delete the run
    count = store.delete_run(org_id, run_id)
    assert count == 2  # stdout + stderr zst files


def test_multiple_nodes(tmp_path):
    """Multiple nodes in the same run have separate artifacts."""
    store = _make_store(tmp_path)
    org_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    for node_idx in range(3):
        node_id = f"node_{node_idx}"
        attempt_key = f"run:{run_id}:node:{node_id}:0"
        writer = ArtifactWriter(
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            attempt_key=attempt_key,
            store=store,
            enabled=True,
        )
        writer.append(f"output from node {node_idx}\n", "stdout")
        pointers = writer.finalize()
        assert len(pointers) == 1
        content = store.read_bytes(pointers[0]).decode("utf-8")
        assert f"output from node {node_idx}" in content

    # Delete only one node
    count = store.delete_node(org_id, run_id, "node_1")
    assert count == 1

    # Other nodes still exist
    for node_idx in [0, 2]:
        node_id = f"node_{node_idx}"
        attempt_key = f"run:{run_id}:node:{node_id}:0"
        ptr_path = store.root / org_id / run_id / node_id / f"{attempt_key}.stdout.zst"
        assert ptr_path.exists()


def test_compression_ratio(tmp_path):
    """Verify zstd compression reduces size for repetitive content."""
    store = _make_store(tmp_path)
    org_id = "org1"
    run_id = "run1"
    node_id = "node1"
    attempt_key = "attempt1"

    # 1 MB of repetitive content compresses very well
    repetitive = "line of repeated text\n" * 50000
    store.append(org_id, run_id, node_id, attempt_key, "stdout", repetitive)
    ptr = store.finalize(org_id, run_id, node_id, attempt_key, "stdout")

    assert ptr is not None
    # Compressed file should be much smaller than uncompressed
    zst_path = store.root / ptr["rel_path"]
    compressed_size = zst_path.stat().st_size
    assert compressed_size < len(repetitive.encode("utf-8")) // 10  # At least 10x compression
