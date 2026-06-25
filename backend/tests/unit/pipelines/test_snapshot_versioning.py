"""Unit tests for snapshot versioning CRUD (no DB — tests logic directly)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.pipeline_snapshot_versioning import diff_snapshots, list_snapshots


def _mock_snapshot(
    sid: uuid.UUID,
    version: int,
    tag: str | None = None,
    created: str | None = None,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
) -> MagicMock:
    s = MagicMock()
    s.id = sid
    s.pipeline_id = uuid.uuid4()
    s.snapshot_version = version
    s.tag = tag
    s.notes = None
    s.created_by = None
    s.created_at = datetime.now(UTC)
    s.graph_json = {
        "nodes": nodes or [{"id": "a", "agent_id": "ag1", "label": "Node A"}],
        "edges": edges or [{"source": "a", "target": "b"}],
    }
    return s


class TestListSnapshots:
    async def test_returns_snapshots_ordered_by_version(self):
        session = AsyncMock()
        pid = uuid.uuid4()
        s1 = _mock_snapshot(uuid.uuid4(), 2)
        s2 = _mock_snapshot(uuid.uuid4(), 1)

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value = [s1, s2]
            else:
                result.scalar.return_value = 5
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        snapshots, total = await list_snapshots(session, pid)
        assert len(snapshots) == 2
        assert snapshots[0].snapshot_version == 2
        assert total == 5


class TestDiffSnapshots:
    async def test_diff_same_no_changes(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        snap = _mock_snapshot(sid, 1)
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: snap))

        result = await diff_snapshots(session, sid, sid)
        assert result is not None
        assert len(result["nodes_added"]) == 0
        assert len(result["nodes_removed"]) == 0
        assert len(result["nodes_modified"]) == 0
        assert len(result["edges_added"]) == 0
        assert len(result["edges_removed"]) == 0
        assert len(result["edges_modified"]) == 0

    async def test_diff_with_added_node(self):
        session = AsyncMock()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        nodes_b = [
            {"id": "a", "agent_id": "ag1", "label": "A"},
            {"id": "b", "agent_id": "ag2", "label": "B"},
        ]
        snap_b = _mock_snapshot(sid_b, 2, nodes=nodes_b)

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = snap_a
            else:
                result.scalar_one_or_none.return_value = snap_b
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_added"]) == 1
        assert result["nodes_added"][0]["id"] == "b"
        assert len(result["nodes_removed"]) == 0
        assert len(result["nodes_modified"]) == 0

    async def test_diff_with_removed_node(self):
        session = AsyncMock()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        nodes_a = [
            {"id": "a", "agent_id": "ag1", "label": "A"},
            {"id": "b", "agent_id": "ag2", "label": "B"},
        ]
        snap_a = _mock_snapshot(sid_a, 1, nodes=nodes_a)
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = snap_a
            else:
                result.scalar_one_or_none.return_value = snap_b
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_removed"]) == 1
        assert result["nodes_removed"][0]["id"] == "b"
        assert len(result["nodes_added"]) == 0
        assert len(result["nodes_modified"]) == 0

    async def test_diff_modified_node(self):
        session = AsyncMock()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag2", "label": "A Changed"}])

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = snap_a
            else:
                result.scalar_one_or_none.return_value = snap_b
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        modified = result["nodes_modified"][0]
        assert modified["node_id"] == "a"
        assert "agent_id" in modified["changes"]
        assert "label" in modified["changes"]

    async def test_diff_returns_none_for_missing(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        result = await diff_snapshots(session, sid, sid)
        assert result is None

    async def test_diff_edge_changes(self):
        session = AsyncMock()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a, 1,
            edges=[{"source": "a", "target": "b"}],
        )
        snap_b = _mock_snapshot(
            sid_b, 2,
            edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        )

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = snap_a
            else:
                result.scalar_one_or_none.return_value = snap_b
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_added"]) == 1
        assert len(result["edges_removed"]) == 0
        assert len(result["edges_modified"]) == 0
        assert result["edges_added"][0]["source"] == "b"
        assert result["edges_added"][0]["target"] == "c"

    async def test_diff_returns_full_graph(self):
        session = AsyncMock()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = snap_a
            else:
                result.scalar_one_or_none.return_value = snap_b
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert "graph" in result["snapshot_a"]
        assert "graph" in result["snapshot_b"]
        assert len(result["snapshot_a"]["graph"]["nodes"]) == 1
        assert result["snapshot_a"]["graph"]["nodes"][0]["id"] == "a"
        assert result["snapshot_a"]["graph"]["nodes"][0]["agent_id"] == "ag1"
        assert result["snapshot_b"]["graph"]["nodes"][0]["id"] == "a"

    async def test_diff_node_changes_schema_id(self):
        session = AsyncMock()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a, 1,
            nodes=[{"id": "a", "agent_id": "ag1", "label": "A", "output_schema_id": "sch1"}],
        )
        snap_b = _mock_snapshot(
            sid_b, 2,
            nodes=[{"id": "a", "agent_id": "ag1", "label": "A", "output_schema_id": "sch2"}],
        )

        call_count = 0

        async def execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = snap_a
            else:
                result.scalar_one_or_none.return_value = snap_b
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        m = result["nodes_modified"][0]
        assert m["node_id"] == "a"
        assert "schema_id" in m["changes"]
        assert m["changes"]["schema_id"]["old"] == "sch1"
        assert m["changes"]["schema_id"]["new"] == "sch2"
