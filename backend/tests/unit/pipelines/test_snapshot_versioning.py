"""Unit tests for snapshot versioning CRUD (no DB — tests logic directly)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.pipeline_snapshot_versioning import (
    _compute_edge_changes,
    _compute_node_changes,
    _rebuild_graph,
    delete_snapshot,
    diff_snapshots,
    list_snapshots,
)


def _mock_snapshot(
    sid: uuid.UUID,
    version: int,
    tag: str | None = None,
    created: str | None = None,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    *,
    organisation_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> MagicMock:
    s = MagicMock()
    s.id = sid
    s.organisation_id = organisation_id or uuid.uuid4()
    s.pipeline_id = pipeline_id or uuid.uuid4()
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


def _in_scope(row: MagicMock, params: dict[str, object]) -> bool:
    """True when *row* satisfies every tenant/pipeline bind the statement carries."""
    for name, value in params.items():
        key = str(name)
        if "organisation_id" in key and row.organisation_id != value:
            return False
        if "pipeline_id" in key and row.pipeline_id != value:
            return False
    return True


def _scoped_diff_session(snapshots: list[MagicMock]) -> AsyncMock:
    """Session double that HONOURS the compiled scoping binds of a snapshot read.

    Each statement is resolved by the fixture whose id appears in its bind
    values; the row is returned only when every ``organisation_id`` /
    ``pipeline_id`` bind it carries equals the row's own value. That makes a
    scope-mismatch test discriminating: a row outside the scope answers
    ``None`` exactly as the real predicate would, while an unscoped read still
    finds it.
    """
    session = AsyncMock()

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        params = dict(stmt.compile().params)  # type: ignore[attr-defined]
        result = MagicMock()
        result.scalar_one_or_none.return_value = next(
            (snap for snap in snapshots if snap.id in params.values() and _in_scope(snap, params)),
            None,
        )
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


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

    async def test_organisation_scope_is_applied_to_page_and_count(self):
        """Both statements carry the org predicate, so they cannot disagree.

        A page and its count tell the client "showing N of M"; if only one were
        scoped, a caller inside an org-scoped context would read a total that
        counts rows it can never page to.
        """
        org_id = uuid.uuid4()
        pid = uuid.uuid4()
        statements: list[dict[str, object]] = []
        session = AsyncMock()

        async def execute_side(stmt, *args, **kwargs):
            statements.append(dict(stmt.compile().params))
            result = MagicMock()
            result.scalars.return_value = []
            result.scalar.return_value = 0
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        _snapshots, total = await list_snapshots(session, pid, organisation_id=org_id)

        assert total == 0
        assert len(statements) == 2
        for params in statements:
            org_binds = [value for name, value in params.items() if "organisation_id" in str(name)]
            assert org_binds == [org_id]

    async def test_organisation_scope_is_optional(self):
        """Omitting the scope keeps the existing unscoped read working."""
        pid = uuid.uuid4()
        statements: list[dict[str, object]] = []
        session = AsyncMock()

        async def execute_side(stmt, *args, **kwargs):
            statements.append(dict(stmt.compile().params))
            result = MagicMock()
            result.scalars.return_value = []
            result.scalar.return_value = 0
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        _snapshots, total = await list_snapshots(session, pid)

        assert total == 0
        assert statements
        for params in statements:
            assert not [name for name in params if "organisation_id" in str(name)]


class TestDiffSnapshots:
    async def test_diff_same_no_changes(self):
        session = AsyncMock()
        sid = uuid.uuid4()
        snap = _mock_snapshot(sid, 1)
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: snap))

        result = await diff_snapshots(session, sid, sid)
        assert result is not None
        assert not result["nodes_added"]
        assert not result["nodes_removed"]
        assert not result["nodes_modified"]
        assert not result["edges_added"]
        assert not result["edges_removed"]
        assert not result["edges_modified"]

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
        assert not result["nodes_removed"]
        assert not result["nodes_modified"]

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
        assert not result["nodes_added"]
        assert not result["nodes_modified"]

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
        assert not result["edges_removed"]
        assert not result["edges_modified"]
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
        assert not result["edges_added"]
        assert not result["edges_modified"]

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
        assert not result["edges_added"]
        assert not result["edges_removed"]

    async def test_diff_modified_edge_hitl_review_config(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_review_config": {"human_only": True}}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_review_config": None}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_modified"]) == 1
        modified = result["edges_modified"][0]
        assert modified["changes"]["hitl_review_config"] == {"old": {"human_only": True}, "new": None}
        assert "edge_type" not in modified["changes"]

    async def test_diff_modified_edge_hitl_review_config_value_change(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_review_config": {"human_only": True}}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            edges=[{"source": "a", "target": "b", "type": "normal", "hitl_review_config": {"human_only": False}}],
        )

        session = _diff_session(snap_a, snap_b)

        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["edges_modified"]) == 1
        modified = result["edges_modified"][0]
        assert modified["changes"]["hitl_review_config"] == {
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
        assert edge["hitl_review_config"] is None

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
        assert not _compute_node_changes(node, node)


class TestComputeEdgeChanges:
    """Every field that decides what an edge DOES must be reported."""

    def test_condition_expression_change_is_reported(self):
        changes = _compute_edge_changes(
            {"source": "a", "target": "b", "condition_expression": "state.x == 1"},
            {"source": "a", "target": "b", "condition_expression": "state.x == 2"},
        )
        assert changes["condition_expression"] == {"old": "state.x == 1", "new": "state.x == 2"}

    def test_legacy_condition_key_is_compared(self):
        """A historic snapshot may carry ``condition`` instead of ``condition_expression``."""
        changes = _compute_edge_changes(
            {"source": "a", "target": "b", "condition": "state.x == 1"},
            {"source": "a", "target": "b", "condition_expression": "state.x == 1"},
        )
        # Same routing expression under the two spellings is NOT a change.
        assert "condition_expression" not in changes

    def test_identical_edges_report_nothing(self):
        edge = {"source": "a", "target": "b", "edge_type": "normal", "condition_expression": "state.x == 1"}
        assert not _compute_edge_changes(edge, edge)

    def test_edge_type_change_still_reported(self):
        changes = _compute_edge_changes(
            {"source": "a", "target": "b"}, {"source": "a", "target": "b", "type": "conditional"}
        )
        assert "edge_type" in changes

    def test_edge_type_aliases_are_equivalent(self):
        changes = _compute_edge_changes(
            {"source": "a", "target": "b", "type": "conditional"},
            {"source": "a", "target": "b", "edge_type": "conditional"},
        )
        assert "edge_type" not in changes


class TestComputeNodeChangesExecutionSemantics:
    """A node differing only in behaviour-bearing config is MODIFIED, not equal."""

    def test_hitl_config_change_is_reported(self):
        changes = _compute_node_changes(
            {"id": "a", "node_type": "hitl", "hitl_config": {"human_only": True}},
            {"id": "a", "node_type": "hitl", "hitl_config": {"human_only": False}},
        )
        assert changes["hitl_config"] == {"old": {"human_only": True}, "new": {"human_only": False}}

    def test_role_only_change_is_reported(self):
        changes = _compute_node_changes(
            {"id": "a", "role": "context_setter"},
            {"id": "a", "role": "agent"},
        )
        assert changes == {"role": {"old": "context_setter", "new": "agent"}}

    def test_hitl_gate_removal_is_reported(self):
        """Removing a node-level gate must never read as "no differences"."""
        changes = _compute_node_changes(
            {"id": "a", "node_type": "hitl", "hitl_config": {"label": "approve"}},
            {"id": "a", "node_type": "hitl"},
        )
        assert changes["hitl_config"]["old"] == {"label": "approve"}
        assert changes["hitl_config"]["new"] is None

    def test_execution_bound_fields_are_reported(self):
        changes = _compute_node_changes(
            {
                "id": "a",
                "timeout_seconds": 300,
                "template_id": "tpl-1",
                "model_class": "builder",
                "single_pr_per_run": False,
            },
            {
                "id": "a",
                "timeout_seconds": 600,
                "template_id": "tpl-2",
                "model_class": "reviewer",
                "single_pr_per_run": True,
            },
        )
        assert changes["timeout_seconds"] == {"old": 300, "new": 600}
        assert changes["template_id"] == {"old": "tpl-1", "new": "tpl-2"}
        assert changes["model_class"] == {"old": "builder", "new": "reviewer"}
        assert changes["single_pr_per_run"] == {"old": False, "new": True}

    def test_behaviour_fields_are_reported(self):
        node_a = {
            "id": "a",
            "agent_commands": ["python old.py"],
            "agent_prompt": "old prompt",
            "capability_scope": {"allowed_tools": ["read"]},
            "egress_policy": "default",
            "resource_limits": {"cpu": 1},
            "policy_gate": {"action": "warn", "eval_id": "old"},
        }
        node_b = {
            "id": "a",
            "agent_commands": ["python new.py"],
            "agent_prompt": "new prompt",
            "capability_scope": {"allowed_tools": ["write"]},
            "egress_policy": "deny_all",
            "resource_limits": {"cpu": 2},
            "policy_gate": {"action": "block", "eval_id": "new"},
        }

        changes = _compute_node_changes(node_a, node_b)

        for field in (
            "agent_commands",
            "agent_prompt",
            "capability_scope",
            "egress_policy",
            "resource_limits",
            "policy_gate",
        ):
            assert field in changes

    def test_absent_execution_fields_are_not_a_change(self):
        """Both sides omitting a field is equal - only a real difference is reported."""
        node_a = {"id": "a", "agent_id": "ag1"}
        node_b = {"id": "a", "agent_id": "ag1"}
        assert not _compute_node_changes(node_a, node_b)


class TestDiffSnapshotsSemanticFidelity:
    """End-to-end through ``diff_snapshots``: a config-only edit is a change."""

    async def test_diff_reports_a_node_hitl_gate_change(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            nodes=[{"id": "a", "node_type": "hitl", "hitl_config": {"human_only": True}}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[{"id": "a", "node_type": "hitl", "hitl_config": {"human_only": False}}],
        )

        result = await diff_snapshots(_diff_session(snap_a, snap_b), sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        assert "hitl_config" in result["nodes_modified"][0]["changes"]

    async def test_diff_reports_an_edge_condition_change(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, edges=[{"source": "a", "target": "b", "condition_expression": "x == 1"}])
        snap_b = _mock_snapshot(sid_b, 2, edges=[{"source": "a", "target": "b", "condition_expression": "x == 2"}])

        result = await diff_snapshots(_diff_session(snap_a, snap_b), sid_a, sid_b)
        assert result is not None
        assert not result["nodes_modified"]
        assert len(result["edges_modified"]) == 1
        assert "condition_expression" in result["edges_modified"][0]["changes"]

    async def test_rebuilt_graph_carries_the_edge_condition_expression(self):
        """The frozen side must show the routing expression the diff reports."""
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        edges = [{"source": "a", "target": "b", "condition_expression": "x == 1"}]
        snap_a = _mock_snapshot(sid_a, 1, edges=edges)
        snap_b = _mock_snapshot(sid_b, 2, edges=edges)

        result = await diff_snapshots(_diff_session(snap_a, snap_b), sid_a, sid_b)
        assert result is not None
        rebuilt = result["snapshot_a"]["graph"]["edges"][0]
        assert rebuilt["condition_expression"] == "x == 1"


class TestDiffSnapshotsOrgScoping:
    """``organisation_id`` is a real predicate, not documentation."""

    async def test_a_foreign_org_snapshot_is_not_visible(self):
        org_id = uuid.uuid4()
        other_org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, organisation_id=org_id, pipeline_id=pipeline_id)
        snap_b = _mock_snapshot(sid_b, 2, organisation_id=other_org_id, pipeline_id=pipeline_id)
        session = _scoped_diff_session([snap_a, snap_b])

        assert await diff_snapshots(session, sid_a, sid_b, organisation_id=org_id, pipeline_id=pipeline_id) is None

    async def test_the_same_pair_is_visible_inside_one_org(self):
        """The discriminator: the SAME session answers when both sides are in scope."""
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, organisation_id=org_id, pipeline_id=pipeline_id)
        snap_b = _mock_snapshot(sid_b, 2, organisation_id=org_id, pipeline_id=pipeline_id)
        session = _scoped_diff_session([snap_a, snap_b])

        result = await diff_snapshots(session, sid_a, sid_b, organisation_id=org_id, pipeline_id=pipeline_id)
        assert result is not None
        assert result["snapshot_a"]["id"] == str(sid_a)

    async def test_a_foreign_pipeline_snapshot_is_not_visible(self):
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        other_pipeline_id = uuid.uuid4()
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, organisation_id=org_id, pipeline_id=pipeline_id)
        snap_b = _mock_snapshot(sid_b, 2, organisation_id=org_id, pipeline_id=other_pipeline_id)
        session = _scoped_diff_session([snap_a, snap_b])

        assert await diff_snapshots(session, sid_a, sid_b, pipeline_id=pipeline_id) is None


class TestDeleteSnapshotScope:
    """The delete CRUD honours the same tenant/pipeline scope its siblings do."""

    async def test_out_of_scope_snapshot_is_not_deleted(self):
        org_id = uuid.uuid4()
        other_org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        sid = uuid.uuid4()
        snap = _mock_snapshot(sid, 1, organisation_id=other_org_id, pipeline_id=pipeline_id)
        session = _scoped_diff_session([snap])

        deleted = await delete_snapshot(session, sid, organisation_id=org_id, pipeline_id=pipeline_id)

        assert deleted is False
        session.delete.assert_not_called()

    async def test_in_scope_non_latest_snapshot_is_deleted(self):
        """The discriminator: the same double deletes the row when it IS in scope."""
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        sid = uuid.uuid4()
        older = _mock_snapshot(sid, 1, organisation_id=org_id, pipeline_id=pipeline_id)
        latest = _mock_snapshot(uuid.uuid4(), 2, organisation_id=org_id, pipeline_id=pipeline_id)

        session = AsyncMock()
        call_count = 0

        async def execute_side(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.scalar_one_or_none.return_value = older if call_count == 1 else latest
            return result

        session.execute = AsyncMock(side_effect=execute_side)

        deleted = await delete_snapshot(session, sid, organisation_id=org_id, pipeline_id=pipeline_id)

        assert deleted is True
        session.delete.assert_awaited_once_with(older)


class TestRebuildGraph:
    """The frozen side reported to the client: faithful, and never silently lossy."""

    def test_carries_a_legacy_condition_key_as_condition_expression(self):
        """A historic snapshot stores ``condition``; the frozen edge normalises it."""
        snap = _mock_snapshot(uuid.uuid4(), 1, edges=[{"source": "a", "target": "b", "condition": "x == 1"}])

        rebuilt = _rebuild_graph(snap)

        assert rebuilt["edges"][0]["condition_expression"] == "x == 1"

    def test_an_id_less_node_fails_loudly(self):
        """A corrupt graph is reported, never collapsed under one ``None`` key.

        The write validator refuses id-less nodes (TOPOLOGY_NODE_MISSING_ID), so
        one can only appear through an unvalidated writer. Tolerating it here
        would merge several such nodes under the same key and hide the
        corruption - the recorded decision in the product map is to fail loudly.
        """
        snap = _mock_snapshot(uuid.uuid4(), 1, nodes=[{"agent_id": "ag1"}])

        with pytest.raises(KeyError):
            _rebuild_graph(snap)
