"""Unit tests for snapshot versioning CRUD (no DB — tests logic directly)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.pipeline_snapshot_versioning import _compute_node_changes, diff_snapshots, list_snapshots


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


def _diff_session(snap_a: MagicMock, snap_b: MagicMock) -> AsyncMock:
    """Return an AsyncMock session resolving snapshot lookups to *snap_a*/*snap_b*."""
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=lambda: snap_a),
            MagicMock(scalar_one_or_none=lambda: snap_b),
        ]
    )
    return session


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
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        nodes_b = [
            {"id": "a", "agent_id": "ag1", "label": "A"},
            {"id": "b", "agent_id": "ag2", "label": "B"},
        ]
        snap_b = _mock_snapshot(sid_b, 2, nodes=nodes_b)

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_added"]) == 1
        assert result["nodes_added"][0]["id"] == "b"
        assert len(result["nodes_removed"]) == 0
        assert len(result["nodes_modified"]) == 0

    async def test_diff_with_removed_node(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        nodes_a = [
            {"id": "a", "agent_id": "ag1", "label": "A"},
            {"id": "b", "agent_id": "ag2", "label": "B"},
        ]
        snap_a = _mock_snapshot(sid_a, 1, nodes=nodes_a)
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_removed"]) == 1
        assert result["nodes_removed"][0]["id"] == "b"
        assert len(result["nodes_added"]) == 0
        assert len(result["nodes_modified"]) == 0

    async def test_diff_modified_node(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag2", "label": "A Changed"}])

        session = _diff_session(snap_a, snap_b)

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
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            edges=[{"source": "a", "target": "b"}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_added"]) == 1
        assert len(result["edges_removed"]) == 0
        assert len(result["edges_modified"]) == 0
        assert result["edges_added"][0]["source"] == "b"
        assert result["edges_added"][0]["target"] == "c"

    async def test_diff_removed_edge(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            edges=[{"source": "a", "target": "b"}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_removed"]) == 1
        assert result["edges_removed"][0]["source"] == "b"
        assert result["edges_removed"][0]["target"] == "c"
        assert len(result["edges_added"]) == 0
        assert len(result["edges_modified"]) == 0

    async def test_diff_modified_edge_type(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, edges=[{"source": "a", "target": "b", "type": "normal"}])
        snap_b = _mock_snapshot(sid_b, 2, edges=[{"source": "a", "target": "b", "type": "loop"}])

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_modified"]) == 1
        modified = result["edges_modified"][0]
        assert modified["edge"] == {"source": "a", "target": "b"}
        assert modified["changes"]["edge_type"] == {"old": "normal", "new": "loop"}
        assert len(result["edges_added"]) == 0
        assert len(result["edges_removed"]) == 0

    async def test_diff_modified_edge_hitl_gate_config(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_gate_config": {"human_only": True}}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_gate_config": None}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_modified"]) == 1
        modified = result["edges_modified"][0]
        assert modified["changes"]["hitl_gate_config"] == {"old": {"human_only": True}, "new": None}
        assert "edge_type" not in modified["changes"]

    async def test_diff_modified_edge_hitl_gate_config_value_change(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_gate_config": {"human_only": True}}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_gate_config": {"human_only": False}}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_modified"]) == 1
        modified = result["edges_modified"][0]
        assert modified["changes"]["hitl_gate_config"] == {
            "old": {"human_only": True},
            "new": {"human_only": False},
        }
        assert "edge_type" not in modified["changes"]

    async def test_diff_modified_node_connector_binding(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        base = {"id": "a", "agent_id": "ag1", "label": "A"}
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{**base, "connector_binding": {"instance_id": "ci-1"}}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{**base, "connector_binding": {"instance_id": "ci-2"}}])

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        changes = result["nodes_modified"][0]["changes"]
        assert changes["connector_binding"] == {
            "old": {"instance_id": "ci-1"},
            "new": {"instance_id": "ci-2"},
        }

    async def test_diff_modified_node_environment_binding(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        base = {"id": "a", "agent_id": "ag1", "label": "A"}
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{**base, "environment_binding": "staging"}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{**base, "environment_binding": "prod"}])

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        changes = result["nodes_modified"][0]["changes"]
        assert changes["environment_binding"] == {"old": "staging", "new": "prod"}

    async def test_diff_returns_full_graph(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert "graph" in result["snapshot_a"]
        assert "graph" in result["snapshot_b"]
        assert len(result["snapshot_a"]["graph"]["nodes"]) == 1
        assert result["snapshot_a"]["graph"]["nodes"][0]["id"] == "a"
        assert result["snapshot_a"]["graph"]["nodes"][0]["agent_id"] == "ag1"
        assert result["snapshot_b"]["graph"]["nodes"][0]["id"] == "a"

    async def test_diff_rebuilds_graph_with_defaults(self):
        """Rebuilt graph nodes/edges must carry canonical default fields."""
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            nodes=[{"id": "n1", "agent_id": "ag1"}],
            edges=[{"source": "n1", "target": "n2"}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[{"id": "n1", "agent_id": "ag1"}],
            edges=[{"source": "n1", "target": "n2"}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        node = result["snapshot_a"]["graph"]["nodes"][0]
        assert node["node_type"] == "agent"
        assert node["position"] == {"x": 0, "y": 0}
        assert node["output_schema_id"] is None
        assert node["connector_binding"] is None
        assert node["environment_binding"] is None

        edge = result["snapshot_a"]["graph"]["edges"][0]
        assert edge["id"] is None
        assert edge["source_node_id"] == "n1"
        assert edge["target_node_id"] == "n2"
        assert edge["edge_type"] == "normal"
        assert edge["hitl_gate_config"] is None

    async def test_diff_node_changes_schema_id(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            nodes=[{"id": "a", "agent_id": "ag1", "label": "A", "output_schema_id": "sch1"}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[{"id": "a", "agent_id": "ag1", "label": "A", "output_schema_id": "sch2"}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        m = result["nodes_modified"][0]
        assert m["node_id"] == "a"
        assert "schema_id" in m["changes"]
        assert m["changes"]["schema_id"]["old"] == "sch1"
        assert m["changes"]["schema_id"]["new"] == "sch2"


class TestComputeNodeChanges:
    def test_connector_binding_change(self):
        changes = _compute_node_changes(
            {"id": "a", "connector_binding": {"instance_id": "ci-1"}},
            {"id": "a", "connector_binding": {"instance_id": "ci-2"}},
        )
        assert changes["connector_binding"] == {"old": {"instance_id": "ci-1"}, "new": {"instance_id": "ci-2"}}

    def test_environment_binding_change(self):
        changes = _compute_node_changes(
            {"id": "a", "environment_binding": "staging"},
            {"id": "a", "environment_binding": "prod"},
        )
        assert changes["environment_binding"] == {"old": "staging", "new": "prod"}

    def test_no_changes_returns_empty(self):
        node = {"id": "a", "agent_id": "ag1", "label": "A", "node_type": "agent"}
        assert _compute_node_changes(node, node) == {}
