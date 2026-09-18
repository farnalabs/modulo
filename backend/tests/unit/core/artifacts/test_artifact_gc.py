"""Unit tests for modulo.core.artifacts.gc (FAR-811).

Tests delete_orphaned_run_artifacts: orphan detection, batched DB lookups,
best-effort error handling, and the iter_run_ids / delete_run integration.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.artifacts.gc import delete_orphaned_run_artifacts
from modulo.core.artifacts.store import LocalArtifactStore, _encode_segment


def _make_store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def _create_run_dirs(store: LocalArtifactStore, org_id: str, run_ids: list[str]) -> None:
    """Create on-disk run directories with at least one file each."""
    for run_id in run_ids:
        store.append(org_id, run_id, "n1", "attempt1", "stdout", "content\n")
        store.finalize(org_id, run_id, "n1", "attempt1", "stdout")


def _make_session(existing_run_ids: list[str] | None = None) -> MagicMock:
    """Return a mock AsyncSession whose execute returns the given run IDs."""
    session = MagicMock()
    if existing_run_ids is None:
        existing_run_ids = []
    result_mock = MagicMock()
    result_mock.all.return_value = [(uuid.UUID(rid),) for rid in existing_run_ids]
    session.execute = AsyncMock(return_value=result_mock)
    return session


# ── happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deletes_only_orphaned_runs(tmp_path) -> None:
    """Runs whose IDs are NOT in the DB are deleted; surviving runs are kept."""
    store = _make_store(tmp_path)
    org_id = "org-a"
    surviving_run = str(uuid.uuid4())
    orphan_run = str(uuid.uuid4())
    _create_run_dirs(store, org_id, [surviving_run, orphan_run])

    session = _make_session(existing_run_ids=[surviving_run])
    result = await delete_orphaned_run_artifacts(session, store=store)

    assert len(result["orphan_runs"]) == 1
    assert result["orphan_runs"][0] == (org_id, orphan_run)
    assert result["files_deleted"] >= 1

    # Surviving run's directory still exists
    surviving_dir = store._root / _encode_segment(org_id) / _encode_segment(surviving_run)
    assert surviving_dir.exists()


@pytest.mark.asyncio
async def test_no_orphans_returns_empty(tmp_path) -> None:
    """When every on-disk run exists in the DB, nothing is deleted."""
    store = _make_store(tmp_path)
    org_id = "org-b"
    run_id = str(uuid.uuid4())
    _create_run_dirs(store, org_id, [run_id])

    session = _make_session(existing_run_ids=[run_id])
    result = await delete_orphaned_run_artifacts(session, store=store)

    assert not result["orphan_runs"]
    assert result["files_deleted"] == 0


@pytest.mark.asyncio
async def test_empty_store_returns_empty(tmp_path) -> None:
    """An empty artifact store produces an empty report with no DB call."""
    store = _make_store(tmp_path)
    session = _make_session()
    result = await delete_orphaned_run_artifacts(session, store=store)

    assert result == {"orphan_runs": [], "files_deleted": 0}
    session.execute.assert_not_awaited()


# ── batched lookups ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batched_lookup_splits_large_store(tmp_path) -> None:
    """More than _GC_BATCH_SIZE runs are checked in multiple DB queries."""
    from modulo.core.artifacts.gc import _GC_BATCH_SIZE

    store = _make_store(tmp_path)
    org_id = "org-c"
    # Create _GC_BATCH_SIZE + 10 runs, all surviving
    run_ids = [str(uuid.uuid4()) for _ in range(_GC_BATCH_SIZE + 10)]
    _create_run_dirs(store, org_id, run_ids)

    session = _make_session(existing_run_ids=run_ids)
    result = await delete_orphaned_run_artifacts(session, store=store)

    assert not result["orphan_runs"]
    assert result["files_deleted"] == 0
    # Should have been called at least twice (batched)
    assert session.execute.await_count >= 2


# ── error handling ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enumeration_failure_returns_empty() -> None:
    """If store.iter_run_ids() raises, the GC returns an empty report."""
    store = MagicMock()
    store.iter_run_ids.side_effect = OSError("disk error")
    session = _make_session()
    result = await delete_orphaned_run_artifacts(session, store=store)

    assert result == {"orphan_runs": [], "files_deleted": 0}
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_lookup_failure_skips_batch_safely(tmp_path) -> None:
    """A DB error on a batch skips that batch (safe: never deletes without
    a confirmed lookup). Runs on disk are preserved when the lookup fails."""
    store = _make_store(tmp_path)
    org_id = "org-d"
    run_ids = [str(uuid.uuid4()) for _ in range(3)]
    _create_run_dirs(store, org_id, run_ids)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=Exception("DB connection lost"))

    result = await delete_orphaned_run_artifacts(session, store=store)

    # Lookup failed → batch skipped → no orphans deleted (safe behaviour)
    assert not result["orphan_runs"]
    assert result["files_deleted"] == 0
    # All runs still on disk
    for run_id in run_ids:
        run_dir = store._root / _encode_segment(org_id) / _encode_segment(run_id)
        assert run_dir.exists()


@pytest.mark.asyncio
async def test_all_lookups_fail_skips_everything(tmp_path) -> None:
    """When every DB lookup fails, no runs are deleted (safe: skip, don't
    assume orphans without a confirmed lookup)."""
    store = _make_store(tmp_path)
    org_id = "org-d2"
    run_ids = [str(uuid.uuid4()) for _ in range(3)]
    _create_run_dirs(store, org_id, run_ids)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=Exception("DB down"))

    result = await delete_orphaned_run_artifacts(session, store=store)

    assert not result["orphan_runs"]
    assert result["files_deleted"] == 0
    # All 3 runs still on disk (nothing deleted)
    for run_id in run_ids:
        run_dir = store._root / _encode_segment(org_id) / _encode_segment(run_id)
        assert run_dir.exists()


@pytest.mark.asyncio
async def test_delete_failure_is_skipped(tmp_path) -> None:
    """If store.delete_run() fails for one orphan, other orphans are still deleted."""
    store = _make_store(tmp_path)
    org_id = "org-e"
    orphan1 = str(uuid.uuid4())
    orphan2 = str(uuid.uuid4())
    _create_run_dirs(store, org_id, [orphan1, orphan2])

    session = _make_session(existing_run_ids=[])

    original_delete_run = store.delete_run
    call_count = 0

    def _fail_first_delete(org_id_arg, run_id_arg):
        nonlocal call_count
        call_count += 1
        if run_id_arg == orphan1:
            raise OSError("permission denied")
        return original_delete_run(org_id_arg, run_id_arg)

    store.delete_run = _fail_first_delete
    result = await delete_orphaned_run_artifacts(session, store=store)

    # orphan1 delete failed but orphan2 was still cleaned up
    assert len(result["orphan_runs"]) == 1
    assert result["orphan_runs"][0] == (org_id, orphan2)


# ── store defaults ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_store_is_imported() -> None:
    """When store=None, the function imports and calls get_store()."""
    mock_store = MagicMock()
    mock_store.iter_run_ids.return_value = []

    with patch("modulo.core.artifacts.store.get_store", return_value=mock_store):
        session = _make_session()
        result = await delete_orphaned_run_artifacts(session)

    assert result == {"orphan_runs": [], "files_deleted": 0}
    mock_store.iter_run_ids.assert_called_once()
