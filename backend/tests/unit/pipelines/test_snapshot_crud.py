"""Unit tests for snapshot CRUD functions (rollback, delete, tag, detail, list)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.pipeline_snapshot_versioning import (
    delete_snapshot,
    get_snapshot_detail,
    list_snapshots,
    rollback_to_snapshot,
    tag_snapshot,
)
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


class TestRollbackToSnapshot:
    async def test_rollback_to_snapshot_creates_new_snapshot(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        target = MagicMock(spec=PipelineSnapshot)
        target.id = target_sid
        target.pipeline_id = pid
        target.snapshot_version = 1

        pipeline = MagicMock()
        pipeline.id = pid
        pipeline.graph_nodes_json = [{"id": "a", "agent_id": "ag1"}]

        new_snapshot = MagicMock(spec=PipelineSnapshot)
        new_snapshot.snapshot_version = 2
        new_snapshot.tag = None
        new_snapshot.notes = None

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = target
            elif call_count == 2:
                result.scalar_one_or_none.return_value = pipeline
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = new_snapshot

            result = await rollback_to_snapshot(session, pid, target_sid)

            assert result is not None
            assert result.tag == "rollback-v1"
            assert result.notes == "Rollback to snapshot version 1"
            mock_create.assert_awaited_once_with(session, pipeline_id=pid, account_id=None)

    async def test_rollback_to_snapshot_different_pipeline_returns_none(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        other_pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        target = MagicMock(spec=PipelineSnapshot)
        target.id = target_sid
        target.pipeline_id = other_pid

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = target
        session.execute = AsyncMock(return_value=result_mock)

        result = await rollback_to_snapshot(session, pid, target_sid)
        assert result is None

    async def test_rollback_to_snapshot_missing_target_returns_none(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        result = await rollback_to_snapshot(session, pid, target_sid)
        assert result is None


class TestDeleteSnapshot:
    async def test_delete_snapshot_returns_true(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        pid = uuid.uuid4()

        target = MagicMock(spec=PipelineSnapshot)
        target.id = sid
        target.pipeline_id = pid

        latest = MagicMock(spec=PipelineSnapshot)
        latest.id = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = target
            elif call_count == 2:
                result.scalar_one_or_none.return_value = latest
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await delete_snapshot(session, sid)
        assert result is True
        session.delete.assert_called_once()
        session.flush.assert_awaited_once()

    async def test_delete_snapshot_latest_returns_false(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        pid = uuid.uuid4()

        target = MagicMock(spec=PipelineSnapshot)
        target.id = sid
        target.pipeline_id = pid

        latest = MagicMock(spec=PipelineSnapshot)
        latest.id = sid

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = target
            elif call_count == 2:
                result.scalar_one_or_none.return_value = latest
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await delete_snapshot(session, sid)
        assert result is False
        session.delete.assert_not_called()

    async def test_delete_snapshot_missing_returns_false(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        result = await delete_snapshot(session, sid)
        assert result is False
        session.delete.assert_not_called()


class TestTagSnapshot:
    async def test_tag_snapshot_sets_tag_and_notes(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        snapshot = MagicMock(spec=PipelineSnapshot)
        snapshot.tag = None
        snapshot.notes = None

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = snapshot
        session.execute = AsyncMock(return_value=result_mock)

        result = await tag_snapshot(session, sid, tag="v1", notes="First release")
        assert result is not None
        assert result.tag == "v1"
        assert result.notes == "First release"
        session.flush.assert_awaited_once()

    async def test_tag_snapshot_missing_returns_none(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        result = await tag_snapshot(session, sid, tag="v1")
        assert result is None


class TestGetSnapshotDetail:
    async def test_get_snapshot_detail_calls_get_snapshot(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        snapshot = MagicMock(spec=PipelineSnapshot)
        snapshot.id = sid

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = snapshot
        session.execute = AsyncMock(return_value=result_mock)

        result = await get_snapshot_detail(session, sid)
        assert result is snapshot
        assert result.id == sid


class TestListSnapshots:
    async def test_list_snapshots_empty_returns_empty(self):
        session = AsyncMock()
        pid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value = []
            else:
                result.scalar.return_value = 0
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        snapshots, total = await list_snapshots(session, pid)
        assert len(snapshots) == 0
        assert total == 0
