"""Unit tests for snapshot CRUD functions (rollback, delete, tag, detail, list)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.hitl_gate_guard import (
    DiffResult,
    EdgeWeakening,
    HitlGateWeakeningDenied,
    resolve_effective_privilege,
)
from modulo.db.crud.pipeline_snapshot_versioning import (
    delete_snapshot,
    get_snapshot_detail,
    list_snapshots,
    rollback_to_snapshot,
    tag_snapshot,
)
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


def _sequenced_session(results: list[MagicMock]) -> AsyncMock:
    """Return an AsyncMock session whose ``execute()`` pops results in order.

    Extra statements (e.g. the post-diff edge DELETE) fall back to a bare
    MagicMock instead of exhausting the sequence.
    """
    session = AsyncMock()
    session.add = MagicMock()
    pending = list(results)

    async def execute_side(*_args: object, **_kwargs: object) -> MagicMock:
        return pending.pop(0) if pending else MagicMock()

    session.execute = AsyncMock(side_effect=execute_side)
    return session


def _row_result(**attrs: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(**attrs)
    return result


def _missing_result() -> MagicMock:
    """An execute() result whose row lookup returns None."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    return result


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
            mock_create.assert_awaited_once_with(
                session, pipeline_id=pid, account_id=None, version_kind="edit", created_kind="rollback"
            )

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
        """A missing pipeline row must abort the rollback before any mutation."""
        pid = uuid.uuid4()
        session = _sequenced_session(
            [
                _row_result(id=uuid.uuid4(), pipeline_id=pid, snapshot_version=1, graph_json={"nodes": []}),
                _missing_result(),  # pipeline lookup returns None
            ]
        )

        result = await rollback_to_snapshot(session, pid, uuid.uuid4(), is_privileged=True, caller_type="rest")

        assert result is None
        session.add.assert_not_called()

    async def test_rollback_to_snapshot_invokes_lock_acquired_callback(self):
        """The caller-provided ``_on_lock_acquired`` hook must run under the lock."""
        pid = uuid.uuid4()
        session = _sequenced_session(
            [
                _row_result(id=uuid.uuid4(), pipeline_id=pid, snapshot_version=1, graph_json={"nodes": []}),
                _row_result(organisation_id=uuid.uuid4(), graph_nodes_json=[]),
            ]
        )
        on_lock = AsyncMock()

        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
            new_callable=AsyncMock,
        ) as mock_diff:
            mock_diff.return_value = DiffResult(
                weakened_edges=[],
                has_weakening=False,
                denied=False,
                reason_code=None,
                caller_type="rest",
            )
            with patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
            ):
                await rollback_to_snapshot(
                    session,
                    pid,
                    uuid.uuid4(),
                    is_privileged=True,
                    caller_type="rest",
                    _on_lock_acquired=on_lock,
                )

        on_lock.assert_awaited_once()

    async def test_rollback_to_snapshot_forwards_caller_type_to_privilege_resolution(self):
        """caller_type must reach resolve_effective_privilege (mcp => forced False)."""
        pid = uuid.uuid4()
        session = _sequenced_session(
            [
                _row_result(id=uuid.uuid4(), pipeline_id=pid, snapshot_version=1, graph_json={"nodes": []}),
                _row_result(organisation_id=uuid.uuid4(), graph_nodes_json=[]),
            ]
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
            ) as mock_diff,
        ):
            mock_diff.return_value = DiffResult(
                weakened_edges=[],
                has_weakening=False,
                denied=False,
                reason_code=None,
                caller_type="mcp",
            )
            with patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
            ):
                await rollback_to_snapshot(
                    session,
                    pid,
                    uuid.uuid4(),
                    is_privileged=True,
                    caller_type="mcp",
                )

        mock_resolve.assert_awaited_once()
        assert mock_resolve.await_args.kwargs["caller_type"] == "mcp"
        assert mock_resolve.await_args.kwargs["is_privileged"] is True

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

    async def test_rollback_to_snapshot_raises_when_gate_weakening_denied(self):
        """A denied gate-weakening diff must raise and leave the graph untouched."""
        pid = uuid.uuid4()
        session = _sequenced_session(
            [
                _row_result(
                    id=uuid.uuid4(),
                    pipeline_id=pid,
                    graph_json={"nodes": [{"id": "a", "agent_id": "ag1"}], "edges": []},
                ),
                _row_result(organisation_id=uuid.uuid4(), graph_nodes_json=[{"id": "a", "agent_id": "ag1"}]),
            ]
        )

        denied_diff = DiffResult(
            weakened_edges=[
                EdgeWeakening(
                    correlation_key=("a", "b", "normal"),
                    weakening_types=["structural:gate_removed"],
                    reason_code="legacy-snapshot-ambiguous",
                )
            ],
            has_weakening=True,
            denied=True,
            reason_code="legacy-snapshot-ambiguous",
            caller_type="rest",
        )
        with (
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
                new_callable=AsyncMock,
                return_value=denied_diff,
            ),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
            ) as mock_create,
            pytest.raises(HitlGateWeakeningDenied) as excinfo,
        ):
            await rollback_to_snapshot(session, pid, uuid.uuid4(), is_privileged=False, caller_type="rest")

        assert excinfo.value.reason_code == "legacy-snapshot-ambiguous"
        assert excinfo.value.correlation_keys == [("a", "b", "normal")]
        mock_create.assert_not_awaited()
        session.add.assert_not_called()

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

    async def test_rollback_to_snapshot_audits_gate_weakening_when_allowed(self):
        """A non-denied weakening must record a hitl_gate_removed audit event."""
        pid = uuid.uuid4()
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        session = _sequenced_session(
            [
                _row_result(
                    id=uuid.uuid4(),
                    pipeline_id=pid,
                    snapshot_version=1,
                    graph_json={"nodes": [{"id": "a", "agent_id": "ag1"}], "edges": []},
                ),
                _row_result(organisation_id=org_id, graph_nodes_json=[{"id": "a", "agent_id": "ag1"}]),
            ]
        )
        weakened_diff = DiffResult(
            weakened_edges=[
                EdgeWeakening(
                    correlation_key=("a", "b", "normal"),
                    weakening_types=["human_only"],
                    reason_code="legacy-snapshot-ambiguous",
                )
            ],
            has_weakening=True,
            denied=False,
            reason_code=None,
            caller_type="rest",
        )
        with (
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
                new_callable=AsyncMock,
                return_value=weakened_diff,
            ),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.append_audit_event",
                new_callable=AsyncMock,
            ) as mock_audit,
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            mock_create.return_value = MagicMock(spec=PipelineSnapshot)
            await rollback_to_snapshot(
                session,
                pid,
                uuid.uuid4(),
                account_id=account_id,
                is_privileged=True,
                caller_type="rest",
            )

        mock_audit.assert_awaited_once()
        audit_kwargs = mock_audit.await_args.kwargs
        assert audit_kwargs["event_type"] == "hitl_gate_removed"
        assert audit_kwargs["org_id"] == org_id
        assert audit_kwargs["actor_user_id"] == account_id
        assert audit_kwargs["resource_type"] == "pipeline"
        assert audit_kwargs["resource_id"] == pid

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

    async def test_rollback_to_snapshot_replaces_graph_nodes_and_edges(self):
        """Rollback must overwrite the live graph and rebuild edges before snapshotting."""
        pid = uuid.uuid4()
        org_id = uuid.uuid4()
        target_sid = uuid.uuid4()
        nodes = [{"id": "a", "agent_id": "ag1"}]
        new_edge_cfg = {"human_only": True}
        target = MagicMock(spec=PipelineSnapshot)
        target.id = target_sid
        target.pipeline_id = pid
        target.graph_json = {
            "nodes": nodes,
            "edges": [{"source": "a", "target": "b", "type": "normal", "hitl_gate_config": new_edge_cfg}],
        }
        pipeline = MagicMock()
        pipeline.organisation_id = org_id
        pipeline.graph_nodes_json = [{"id": "old", "agent_id": "ag1"}]

        edges_result = MagicMock()
        edges_result.scalars.return_value = [
            SimpleNamespace(
                source_node_id=uuid.uuid4(),
                target_node_id=uuid.uuid4(),
                edge_type="normal",
                hitl_gate_config=None,
            )
        ]
        target_result = MagicMock()
        target_result.scalar_one_or_none.return_value = target
        pipeline_result = MagicMock()
        pipeline_result.scalar_one_or_none.return_value = pipeline
        session = _sequenced_session([target_result, pipeline_result, edges_result])

        new_snapshot = MagicMock(spec=PipelineSnapshot)
        with patch(
            "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=new_snapshot,
        ) as mock_create:
            result = await rollback_to_snapshot(session, pid, target_sid, is_privileged=True, caller_type="rest")

        assert result is new_snapshot
        assert pipeline.graph_nodes_json == nodes

        deleted_stmt = session.execute.call_args_list[4][0][0]
        assert "DELETE" in str(deleted_stmt)

        add_calls = [c.args[0] for c in session.add.call_args_list if c.args]
        assert len(add_calls) == 1
        new_edge = add_calls[0]
        assert isinstance(new_edge, PipelineEdge)
        assert new_edge.organisation_id == org_id
        assert new_edge.pipeline_id == pid
        assert str(new_edge.source_node_id) == "a"
        assert str(new_edge.target_node_id) == "b"
        assert new_edge.edge_type == "normal"
        assert new_edge.hitl_gate_config == new_edge_cfg

        mock_create.assert_awaited_once_with(
            session, pipeline_id=pid, account_id=None, version_kind="edit", created_kind="rollback"
        )

    async def test_rollback_to_snapshot_returns_none_when_new_snapshot_none(self):
        """A rollback whose follow-up snapshot creation fails must return None."""
        session = _sequenced_session(
            [
                _row_result(id=uuid.uuid4(), pipeline_id=uuid.uuid4(), snapshot_version=1, graph_json={"nodes": []}),
                _row_result(organisation_id=uuid.uuid4(), graph_nodes_json=[]),
            ]
        )
        with (
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
                new_callable=AsyncMock,
            ) as mock_diff,
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_diff.return_value = DiffResult(
                weakened_edges=[],
                has_weakening=False,
                denied=False,
                reason_code=None,
                caller_type="rest",
            )
            result = await rollback_to_snapshot(
                session, uuid.uuid4(), uuid.uuid4(), is_privileged=True, caller_type="rest"
            )

        assert result is None


class TestResolveEffectivePrivilege:
    async def test_mcp_always_returns_false_without_db_query(self):
        """caller_type='mcp' must short-circuit to False — no DB read at all."""
        session = AsyncMock()
        assert (
            await resolve_effective_privilege(
                session,
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                is_privileged=True,
                caller_type="mcp",
            )
            is False
        )
        session.execute.assert_not_awaited()

    async def test_no_account_id_uses_caller_supplied_flag(self):
        """Without account_id the caller-supplied is_privileged is used as-is."""
        session = AsyncMock()
        assert (
            await resolve_effective_privilege(
                session,
                org_id=uuid.uuid4(),
                account_id=None,
                is_privileged=True,
                caller_type="rest",
            )
            is True
        )
        session.execute.assert_not_awaited()


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

    async def test_tag_snapshot_only_sets_tag_preserving_notes(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        snapshot = MagicMock(spec=PipelineSnapshot)
        snapshot.tag = None
        snapshot.notes = "keep me"

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = snapshot
        session.execute = AsyncMock(return_value=result_mock)

        result = await tag_snapshot(session, sid, tag="v2")

        assert result is not None
        assert result.tag == "v2"
        assert result.notes == "keep me"
        session.flush.assert_awaited_once()

    async def test_tag_snapshot_only_sets_notes_preserving_tag(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        snapshot = MagicMock(spec=PipelineSnapshot)
        snapshot.tag = "v1"
        snapshot.notes = None

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = snapshot
        session.execute = AsyncMock(return_value=result_mock)

        result = await tag_snapshot(session, sid, notes="a note")

        assert result is not None
        assert result.tag == "v1"
        assert result.notes == "a note"
        session.flush.assert_awaited_once()


class TestGetSnapshotDetail:
    async def test_get_snapshot_detail_returns_snapshot(self):
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

    async def test_get_snapshot_detail_applies_org_filter(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        oid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = MagicMock(spec=PipelineSnapshot)
        session.execute = AsyncMock(return_value=result_mock)

        await get_snapshot_detail(session, sid, organisation_id=oid)

        stmt = session.execute.call_args[0][0]
        assert stmt.whereclause is not None
        assert "organisation_id" in str(stmt.whereclause)

    async def test_get_snapshot_detail_applies_pipeline_filter(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        pid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = MagicMock(spec=PipelineSnapshot)
        session.execute = AsyncMock(return_value=result_mock)

        await get_snapshot_detail(session, sid, pipeline_id=pid)

        stmt = session.execute.call_args[0][0]
        assert stmt.whereclause is not None
        assert "pipeline_id" in str(stmt.whereclause)

    async def test_get_snapshot_detail_without_scoping_has_no_org_predicate(self):
        session = AsyncMock()
        sid = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = MagicMock(spec=PipelineSnapshot)
        session.execute = AsyncMock(return_value=result_mock)

        await get_snapshot_detail(session, sid)

        stmt = session.execute.call_args[0][0]
        assert "organisation_id" not in str(stmt.whereclause)
        assert "pipeline_id" not in str(stmt.whereclause)


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
        assert not snapshots
        assert total == 0

    async def test_list_snapshots_applies_pagination(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        s1 = MagicMock(spec=PipelineSnapshot)

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value = [s1]
            else:
                result.scalar.return_value = 3
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        snapshots, total = await list_snapshots(session, pid, page=3, page_size=10)

        assert len(snapshots) == 1
        assert total == 3
        select_stmt = session.execute.call_args_list[0][0][0]
        sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 10" in sql
        assert "OFFSET 20" in sql
        # Rows are always ordered newest-version-first.
        assert "snapshot_version DESC" in sql.replace("\n", " ")
        # Both queries are scoped to non-deleted pipelines.
        assert "deleted_at IS NULL" in sql.replace("\n", " ")

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

    async def test_list_snapshots_excludes_deleted_pipelines(self):
        """Snapshots for soft-deleted pipelines must be filtered out of both queries."""
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

        await list_snapshots(session, pid)

        for call_args in session.execute.call_args_list:
            stmt = call_args[0][0]
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            assert "deleted_at" in sql
            assert "pipelines" in sql

    async def test_list_snapshots_falls_back_to_empty_on_count_error(self):
        """A ProgrammingError from the count query must degrade to ([], 0)."""
        session = AsyncMock()
        pid = uuid.uuid4()
        s1 = MagicMock(spec=PipelineSnapshot)

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value = [s1]
            else:
                raise ProgrammingError("SELECT count(*) ...", {}, RuntimeError("relation does not exist"))
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        snapshots, total = await list_snapshots(session, pid)

        assert snapshots == []
        assert total == 0
