"""Unit tests for FAR-402 P6 — live-edit history, semantic diff/impact, channels.

DB-free: tests the pure oracles (``compute_port_change_impact``,
``check_port_change_breaking``, ``should_rollback``, ``resolve_channel_binding``)
directly and exercises ``diff_snapshots`` / ``create_snapshot_edit`` /
``rollback_to_snapshot`` discriminator behaviour with ASGI-mocked sessions (the
same pattern as ``tests/unit/pipelines/test_snapshot_versioning.py``).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_impact import (
    check_port_change_breaking,
    compute_port_change_impact,
    diff_edge_ports,
    diff_node_ports,
    node_port_signature,
)
from modulo.core.release_channels import (
    ChannelMetrics,
    ReleaseChannelThresholds,
    resolve_channel_binding,
    should_rollback,
)
from modulo.db.crud.pipeline_snapshot import create_snapshot_edit
from modulo.db.crud.pipeline_snapshot_versioning import diff_snapshots


def _mock_snapshot(
    sid: uuid.UUID,
    version: int,
    tag: str | None = None,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
) -> MagicMock:
    s = MagicMock()
    s.id = sid
    s.pipeline_id = uuid.uuid4()
    s.snapshot_version = version
    s.tag = tag
    s.notes = None
    s.graph_json = {
        "nodes": nodes or [{"id": "a", "agent_id": "ag1", "label": "Node A"}],
        "edges": edges or [{"source": "a", "target": "b"}],
    }
    return s


def _diff_session(snap_a: MagicMock, snap_b: MagicMock) -> AsyncMock:
    return AsyncMock(
        execute=AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=lambda: snap_a),
                MagicMock(scalar_one_or_none=lambda: snap_b),
            ]
        )
    )


class TestPortSignature:
    def test_sparse_signature_uses_default_out_in(self):
        sig = node_port_signature({"id": "a"})
        assert sig == {"input": {}, "output": {}}

    def test_signature_with_ports(self):
        node = {
            "id": "a",
            "inputs": [{"name": "in1", "schema_ref": "in-s"}],
            "outputs": {"out1": "out-s"},
        }
        sig = node_port_signature(node)
        assert sig["input"] == {"in1": "in-s"}
        assert sig["output"] == {"out1": "out-s"}

    def test_diff_node_ports_detects_added_removed_modified(self):
        na = {"id": "a", "outputs": [{"name": "keep", "schema_ref": "s1"}]}
        nb = {"id": "a", "outputs": [{"name": "keep", "schema_ref": "s2"}, {"name": "new", "schema_ref": "s3"}]}
        changes = diff_node_ports(na, nb)
        by_port = {c["port"]: c for c in changes}
        assert by_port["keep"]["change"] == "modified"
        assert by_port["keep"]["old"] == "s1"
        assert by_port["keep"]["new"] == "s2"
        assert by_port["new"]["change"] == "added"

    def test_diff_edge_ports_detects_source_target(self):
        ea = {"source": "a", "target": "b", "source_port": "out"}
        eb = {"source": "a", "target": "b", "source_port": "out2", "target_port": "in1"}
        changes = diff_edge_ports(ea, eb)
        assert changes["source_port"] == {"old": "out", "new": "out2"}
        assert changes["target_port"] == {"old": None, "new": "in1"}


class TestComputePortChangeImpact:
    def test_downstream_reachability_single_chain(self):
        graph = {
            "nodes": [{"id": n} for n in ("a", "b", "c", "d")],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "c"},
                {"source": "c", "target": "d"},
            ],
        }
        impacted = compute_port_change_impact(graph, [{"node_id": "b", "direction": "output", "port": "res"}])
        assert impacted == {"b", "c", "d"}

    def test_fan_out_reaches_all_branches(self):
        graph = {
            "nodes": [{"id": n} for n in ("a", "b", "c", "d")],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "a", "target": "c"},
                {"source": "b", "target": "d"},
                {"source": "c", "target": "d"},
            ],
        }
        impacted = compute_port_change_impact(graph, [("a",)])
        assert impacted == {"a", "b", "c", "d"}

    def test_unknown_node_skipped(self):
        graph = {"nodes": [{"id": "a"}], "edges": []}
        impacted = compute_port_change_impact(graph, [{"node_id": "missing"}])
        assert impacted == set()

    def test_multiple_changed_ports_union(self):
        graph = {
            "nodes": [{"id": n} for n in ("a", "b", "c", "d", "e")],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "c"},
                {"source": "d", "target": "e"},
            ],
        }
        impacted = compute_port_change_impact(
            graph,
            [
                {"node_id": "a", "direction": "output", "port": "p1"},
                {"node_id": "d", "direction": "output", "port": "p2"},
            ],
        )
        assert impacted == {"a", "b", "c", "d", "e"}


class TestCheckPortChangeBreaking:
    def test_removed_output_port_blocks_consuming_edge(self):
        graph_new = {
            "nodes": [{"id": "a", "outputs": []}],
            "edges": [{"source": "a", "target": "b", "source_port": "res"}],
        }
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "a", "direction": "output", "port": "res", "change": "removed"}]
        )
        assert findings, "expected a block finding"
        assert findings[0]["severity"] == "block"

    def test_schema_ref_change_is_warning_when_edge_reads_port(self):
        graph_new = {
            "nodes": [{"id": "a", "outputs": [{"name": "res", "schema_ref": "s2"}]}],
            "edges": [{"source": "a", "target": "b", "source_port": "res"}],
        }
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "a", "direction": "output", "port": "res", "change": "modified"}]
        )
        assert findings, "expected a warning finding"
        assert findings[0]["severity"] == "warning"

    def test_port_less_edge_still_breaks_when_default_output_removed(self):
        graph_new = {"nodes": [{"id": "a", "outputs": []}], "edges": [{"source": "a", "target": "b"}]}
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "a", "direction": "output", "port": "out", "change": "removed"}]
        )
        assert findings, "expected a block finding"
        assert findings[0]["severity"] == "block"

    def test_unrelated_edge_not_flagged(self):
        graph_new = {
            "nodes": [{"id": "a", "outputs": []}],
            "edges": [{"source": "a", "target": "b", "source_port": "other"}],
        }
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "a", "direction": "output", "port": "res", "change": "removed"}]
        )
        assert findings == []


class TestReleaseChannels:
    def test_should_rollback_below_min_runs_is_false(self):
        assert should_rollback(ChannelMetrics(observed_runs=3, error_runs=3)) is False

    def test_should_rollback_over_threshold_is_true(self):
        assert should_rollback(ChannelMetrics(observed_runs=10, error_runs=6)) is True

    def test_should_rollback_below_threshold_is_false(self):
        assert should_rollback(ChannelMetrics(observed_runs=100, error_runs=2)) is False

    def test_should_rollback_at_exact_threshold_is_true(self):
        assert should_rollback(ChannelMetrics(observed_runs=20, error_runs=1)) is True

    def test_should_rollback_empty_channel_is_false(self):
        assert should_rollback(ChannelMetrics(observed_runs=0, error_runs=0)) is False

    def test_should_rollback_custom_thresholds(self):
        thresholds = ReleaseChannelThresholds(
            rollback_threshold_error_rate_pct=30.0,
            rollback_min_observed_runs=2,
            promotion_min_observed_runs=5,
        )
        assert should_rollback(ChannelMetrics(observed_runs=2, error_runs=1), thresholds) is True

    def test_resolve_channel_binding_default(self):
        assert resolve_channel_binding(None) == "none"
        assert resolve_channel_binding({}) == "none"
        assert resolve_channel_binding({"release_channel": "canary"}) == "canary"
        assert resolve_channel_binding({"release_channel": "STABLE"}) == "stable"
        assert resolve_channel_binding({"release_channel": "bogus"}) == "none"


class TestCreateSnapshotEdit:
    @patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", new_callable=AsyncMock)
    async def test_forwards_edit_kind(self, mock_create):
        pipeline_id = uuid.uuid4()
        account_id = uuid.uuid4()
        session = AsyncMock()
        await create_snapshot_edit(
            session, pipeline_id=pipeline_id, account_id=account_id, draft=True, channel="canary"
        )
        mock_create.assert_awaited_once_with(
            session,
            pipeline_id=pipeline_id,
            account_id=account_id,
            version_kind="edit",
            created_kind="edit",
            draft=True,
            channel="canary",
        )


class TestRollbackDiscriminator:
    @patch(
        "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
        new_callable=AsyncMock,
    )
    @patch(
        "modulo.db.crud.pipeline_snapshot_versioning.resolve_effective_privilege",
        new_callable=AsyncMock,
    )
    @patch(
        "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
        new_callable=AsyncMock,
    )
    @patch(
        "modulo.db.crud.pipeline_snapshot_versioning.enforce_guardrail_binding_strip",
        new_callable=AsyncMock,
    )
    async def test_rollback_tags_created_kind_rollback(self, mock_enforce, mock_apply, mock_resolve, mock_create):
        from modulo.db.crud.pipeline_snapshot_versioning import rollback_to_snapshot

        mock_resolve.return_value = True
        mock_apply.return_value = SimpleNamespace(denied=False, has_weakening=False, reason_code=None)
        new_snapshot = MagicMock()
        mock_create.return_value = new_snapshot

        pipeline_id = uuid.uuid4()
        target = MagicMock()
        target.pipeline_id = pipeline_id
        target.graph_json = {"nodes": [], "edges": []}
        target.snapshot_version = 2

        pipeline = MagicMock()
        pipeline.id = pipeline_id
        pipeline.organisation_id = uuid.uuid4()

        def _execute_side(*args, **kwargs):
            return MagicMock()

        # First two execute calls resolve the target snapshot + pipeline; the
        # rest (old-rows select, edge delete) return generic results.
        session = AsyncMock()
        call_count = 0

        async def execute_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = target
            elif call_count == 2:
                result.scalar_one_or_none.return_value = pipeline
            else:
                result.scalars.return_value = []
            return result

        session.execute = AsyncMock(side_effect=execute_side_effect)

        result = await rollback_to_snapshot(
            session,
            pipeline_id,
            target.id,
            account_id=uuid.uuid4(),
            is_privileged=True,
            caller_type="rest",
        )
        assert result is new_snapshot
        mock_create.assert_awaited_once()
        kwargs = mock_create.await_args.kwargs
        assert kwargs["version_kind"] == "edit"
        assert kwargs["created_kind"] == "rollback"


class TestDiffSnapshotsSemantic:
    async def test_diff_surfaces_port_signature_delta_and_impact(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            nodes=[{"id": "a", "agent_id": "ag1", "label": "A", "outputs": [{"name": "res", "schema_ref": "s1"}]}],
            edges=[{"source": "a", "target": "b"}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[{"id": "a", "agent_id": "ag1", "label": "A", "outputs": [{"name": "res", "schema_ref": "s2"}]}],
            edges=[{"source": "a", "target": "b"}],
        )

        session = _diff_session(snap_a, snap_b)
        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        semantic = result["semantic"]
        assert semantic["port_changes"], "expected a port-signature delta"
        ports = semantic["port_changes"]
        assert any(p["port"] == "res" and p["change"] == "modified" for p in ports)
        assert "a" in semantic["impacted_nodes"]
        assert "b" in semantic["impacted_nodes"]

    async def test_diff_no_port_changes_emits_empty_semantic(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        snap_b = _mock_snapshot(sid_b, 2, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])

        session = _diff_session(snap_a, snap_b)
        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert not result["semantic"]["port_changes"]
        assert not result["semantic"]["impacted_nodes"]
        assert not result["semantic"]["breaking_changes"]

    async def test_diff_edge_source_port_repoint_reflects_impact(self):
        # FAR-420 P6: an edge that only changes which output port it reads must
        # surface in port_changes AND propagate downstream impact — previously
        # it was silently dropped (false "safe" signal).
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        na = {
            "id": "a",
            "agent_id": "ag1",
            "label": "A",
            "outputs": [{"name": "out1", "schema_ref": "s"}, {"name": "out2", "schema_ref": "s"}],
        }
        nb = {"id": "b", "agent_id": "ag2", "label": "B"}
        nc = {"id": "c", "agent_id": "ag3", "label": "C"}
        snap_a = _mock_snapshot(
            sid_a,
            1,
            nodes=[na, nb, nc],
            edges=[
                {"source": "a", "target": "b", "source_port": "out1"},
                {"source": "b", "target": "c", "source_port": "x"},
            ],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[na, nb, nc],
            edges=[
                {"source": "a", "target": "b", "source_port": "out2"},
                {"source": "b", "target": "c", "source_port": "x"},
            ],
        )

        session = _diff_session(snap_a, snap_b)
        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        semantic = result["semantic"]
        assert any(
            p.get("change") == "edge_source_port_repoint" and p.get("node_id") == "a" for p in semantic["port_changes"]
        ), "edge source_port repoint must appear in port_changes"
        # The repoint is attributed to the source node's output, so impact must
        # reach the source and its full downstream (b, c).
        assert semantic["impacted_nodes"] == ["a", "b", "c"]
        # out1 -> out2 with equal schema_ref is not breaking on its own.
        assert not semantic["breaking_changes"]

    async def test_diff_edge_source_port_repoint_to_missing_port_is_breaking(self):
        # Repointing an edge at a port the new graph does not declare must be
        # reported as a blocking change.
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(
            sid_a,
            1,
            nodes=[
                {"id": "a", "agent_id": "ag1", "label": "A", "outputs": [{"name": "out1", "schema_ref": "s"}]},
                {"id": "b", "agent_id": "ag2", "label": "B"},
            ],
            edges=[{"source": "a", "target": "b", "source_port": "out1"}],
        )
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[
                {"id": "a", "agent_id": "ag1", "label": "A", "outputs": [{"name": "out1", "schema_ref": "s"}]},
                {"id": "b", "agent_id": "ag2", "label": "B"},
            ],
            edges=[{"source": "a", "target": "b", "source_port": "out2"}],
        )

        session = _diff_session(snap_a, snap_b)
        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        semantic = result["semantic"]
        assert any(p.get("change") == "edge_source_port_repoint" for p in semantic["port_changes"])
        assert semantic["breaking_changes"], "repoint to a missing port must be breaking"
        assert semantic["breaking_changes"][0]["severity"] == "block"

    async def test_diff_report_node_modification_with_ports(self):
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        snap_a = _mock_snapshot(sid_a, 1, nodes=[{"id": "a", "agent_id": "ag1", "label": "A"}])
        snap_b = _mock_snapshot(
            sid_b,
            2,
            nodes=[{"id": "a", "agent_id": "ag2", "label": "A", "inputs": [{"name": "in1", "schema_ref": "s"}]}],
        )

        session = _diff_session(snap_a, snap_b)
        result = await diff_snapshots(session, sid_a, sid_b)
        assert result is not None
        assert len(result["nodes_modified"]) == 1
        modified = result["nodes_modified"][0]
        assert "agent_id" in modified["changes"]
