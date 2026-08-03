"""Unit tests for snapshot CRUD functions (rollback, delete, tag, detail, list)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.hitl_gate_guard import DiffResult, EdgeWeakening, HitlGateWeakeningDenied
from modulo.db.crud.pipeline_snapshot_versioning import (
    delete_snapshot,
    get_snapshot_detail,
    list_snapshots,
    rollback_to_snapshot,
    tag_snapshot,
)
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


def _denied_diff(reason_code: str, *, caller_type: str = "rest") -> DiffResult:
    return DiffResult(
        weakened_edges=[
            EdgeWeakening(
                correlation_key=("a", "b", "normal"),
                weakening_types=["structural:gate_removed"],
                reason_code=reason_code,
            )
        ],
        has_weakening=True,
        denied=True,
        reason_code=reason_code,
        caller_type=caller_type,
    )


def _target_snapshot(sid: uuid.UUID, pipeline_id: uuid.UUID, *, version: int = 1) -> MagicMock:
    target = MagicMock(spec=PipelineSnapshot)
    target.id = sid
    target.pipeline_id = pipeline_id
    target.snapshot_version = version
    target.graph_json = {"nodes": [], "edges": []}
    return target


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

            result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="rest")

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

        result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="rest")
        assert result is None

    async def test_rollback_to_snapshot_missing_target_returns_none(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="rest")
        assert result is None

    async def test_rollback_to_snapshot_missing_pipeline_returns_none(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = _target_snapshot(target_sid, pid)
            else:
                result.scalar_one_or_none.return_value = None
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
        ) as mock_create:
            result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="rest")

        assert result is None
        mock_create.assert_not_awaited()

    async def test_rollback_to_snapshot_invokes_lock_acquired_callback(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = _target_snapshot(target_sid, pid)
            elif call_count == 2:
                pipeline = MagicMock()
                pipeline.id = pid
                pipeline.organisation_id = uuid.uuid4()
                pipeline.graph_nodes_json = []
                result.scalar_one_or_none.return_value = pipeline
            return result

        session.execute = AsyncMock(side_effect=execute_side)
        on_lock_acquired = AsyncMock()

        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
        ):
            await rollback_to_snapshot(
                session,
                pid,
                target_sid,
                is_privileged=True,
                caller_type="rest",
                _on_lock_acquired=on_lock_acquired,
            )

        on_lock_acquired.assert_awaited_once()

    async def test_rollback_to_snapshot_denied_weakening_raises_before_mutation(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = _target_snapshot(target_sid, pid)
            elif call_count == 2:
                pipeline = MagicMock()
                pipeline.id = pid
                pipeline.organisation_id = uuid.uuid4()
                pipeline.graph_nodes_json = []
                result.scalar_one_or_none.return_value = pipeline
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        with (
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
                new_callable=AsyncMock,
                return_value=_denied_diff("legacy-snapshot-ambiguous"),
            ),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
            ) as mock_create,
            pytest.raises(HitlGateWeakeningDenied) as exc_info,
        ):
            await rollback_to_snapshot(session, pid, target_sid, is_privileged=False, caller_type="rest")

        assert exc_info.value.reason_code == "legacy-snapshot-ambiguous"
        # The graph mutation must never run when the gate guard denies.
        session.delete.assert_not_called()
        session.add.assert_not_called()
        mock_create.assert_not_awaited()

    async def test_rollback_mcp_caller_forced_unprivileged(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = _target_snapshot(target_sid, pid)
            elif call_count == 2:
                pipeline = MagicMock()
                pipeline.id = pid
                pipeline.organisation_id = uuid.uuid4()
                pipeline.graph_nodes_json = []
                result.scalar_one_or_none.return_value = pipeline
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        new_snapshot = MagicMock(spec=PipelineSnapshot)
        allowed_diff = DiffResult(
            weakened_edges=[],
            has_weakening=False,
            denied=False,
            reason_code=None,
            caller_type="mcp",
        )

        with (
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.resolve_effective_privilege",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_resolve,
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
                new_callable=AsyncMock,
                return_value=allowed_diff,
            ) as mock_diff,
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=new_snapshot,
            ),
        ):
            result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="mcp")

        assert result is new_snapshot
        # Privilege is resolved under the lock and must not leak through for MCP.
        mock_resolve.assert_awaited_once()
        assert mock_resolve.await_args.kwargs["is_privileged"] is True
        assert mock_resolve.await_args.kwargs["caller_type"] == "mcp"
        assert mock_diff.await_args.kwargs["is_privileged"] is False
        assert mock_diff.await_args.kwargs["caller_type"] == "mcp"

    async def test_rollback_appends_audit_event_on_weakening(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        target_sid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = _target_snapshot(target_sid, pid)
            elif call_count == 2:
                pipeline = MagicMock()
                pipeline.id = pid
                pipeline.organisation_id = uuid.uuid4()
                pipeline.graph_nodes_json = []
                result.scalar_one_or_none.return_value = pipeline
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        weakening_diff = DiffResult(
            weakened_edges=[
                EdgeWeakening(
                    correlation_key=("a", "b", "normal"),
                    weakening_types=["human_only"],
                    reason_code="insufficient-role",
                )
            ],
            has_weakening=True,
            denied=False,
            reason_code=None,
            caller_type="rest",
        )
        new_snapshot = MagicMock(spec=PipelineSnapshot)

        with (
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
                new_callable=AsyncMock,
                return_value=weakening_diff,
            ),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.append_audit_event",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=new_snapshot,
            ),
        ):
            result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="rest")

        assert result is new_snapshot
        mock_audit.assert_awaited_once()
        assert mock_audit.await_args.kwargs["event_type"] == "hitl_gate_removed"
        assert mock_audit.await_args.kwargs["resource_id"] == pid
        payload = mock_audit.await_args.kwargs["payload_json"]
        assert payload["denied"] is False
        assert payload["caller_type"] == "rest"
        assert payload["affected_edges"][0]["weakening_types"] == ["human_only"]


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

    async def test_list_snapshots_programming_error_returns_empty(self):
        session = AsyncMock()
        pid = uuid.uuid4()

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                result = MagicMock()
                result.scalars.return_value = []
                return result
            raise ProgrammingError("select count(*)", {}, Exception("boom"))

        session.execute = AsyncMock(side_effect=execute_side)

        snapshots, total = await list_snapshots(session, pid)
        assert snapshots == []
        assert total == 0

    async def test_list_snapshots_applies_pagination(self):
        session = AsyncMock()
        pid = uuid.uuid4()

        executed: list = []

        async def execute_side(stmt, *args, **kwargs):
            executed.append(stmt)
            result = MagicMock()
            if len(executed) == 1:
                result.scalars.return_value = []
            else:
                result.scalar.return_value = 0
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        snapshots, total = await list_snapshots(session, pid, page=3, page_size=25)
        assert snapshots == []
        assert total == 0

        list_sql = str(executed[0].compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 25" in list_sql
        assert "OFFSET 50" in list_sql
        # Rows are always ordered newest-version-first.
        assert "snapshot_version DESC" in list_sql.replace("\n", " ")
        # Both queries are scoped to the requested pipeline.
        assert pid.hex in list_sql
        assert "deleted_at IS NULL" in list_sql.replace("\n", " ")
