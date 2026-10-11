"""FAR-1109: config-as-code for node-attached policy gates.

The gate is a ``policy_gate`` block on a pipeline graph node. Before this
chunk the server-side ``PipelineGraphNode`` model had no field for it, so
Pydantic's default ``extra="ignore"`` silently DROPPED the block — before the
drift hash and before the graph write. The declarative feature was therefore
permanently inert (the plan always reported "unchanged" and the write never
carried the gate).

The tests here prove, at unit level, the criteria that must hold on today's
codebase:

- the block SURVIVES the server node model, the graph write representation and
  the graph read representation (the round-trip proof — this is the test that
  fails without the fix);
- a field the server does not accept shows as PERMANENT DRIFT on the next plan,
  never a false "unchanged";
- unchanged config reports no drift; a changed gate is reported as drift before
  apply; applying is idempotent;
- gate removal is in scope and is reported as a distinct change class;
- a declared ``eval_id`` resolves against the org's evals, or the entity is
  blocked.

HTTP is mocked via respx, and payloads are validated against the REAL API
pydantic models (contract round-trip), not hand-built fixtures.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
import respx
from pydantic import ValidationError

from modulo.api.routes.pipelines import (
    PipelineGraphNode,
    PipelineGraphUpdate,
    _graph_response,
    _prepare_graph_write,
)
from modulo.cli.apply import pipeline_apply, render_table
from modulo.cli.apply.drift import build_drift_detail, policy_gate_changes
from modulo.cli.apply.executor import PAGE_SIZE, ApplyExecutor
from modulo.cli.apply.loader import ApplyLoadError, parse_apply_documents
from modulo.cli.apply.models import ApplyGraphNode, ApplyGraphPolicyGate, PipelineEntity
from modulo.cli.apply.plan import KIND_PIPELINE, plan_entity

NODE_ID = "00000000-0000-0000-0000-0000000000a1"
EVAL_ID = "660e8400-e29b-41d4-a716-446655440001"
AGENT_ID = "00000000-0000-0000-0000-0000000000ff"
PIPELINE_ID = "00000000-0000-0000-0000-0000000000aa"
_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE)}

_GATE_BLOCK = f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3
      graph:
        nodes:
          - id: "{NODE_ID}"
            node_type: agent
            agent: worker
            position: {{x: 0, y: 0}}
            policy_gate:
              action: block
              eval_id: "{EVAL_ID}"
        edges: []
"""

_REMOVAL_BLOCK = f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3
      graph:
        nodes:
          - id: "{NODE_ID}"
            node_type: agent
            agent: worker
            position: {{x: 0, y: 0}}
        edges: []
"""


def _gate(action: str = "block", eval_id: str = EVAL_ID) -> dict[str, str]:
    return {"action": action, "eval_id": eval_id}


def _entity(*, gate: dict[str, str] | None) -> PipelineEntity:
    node: dict[str, object] = {
        "id": NODE_ID,
        "node_type": "agent",
        "agent": "worker",
        "position": {"x": 0, "y": 0},
    }
    if gate is not None:
        node["policy_gate"] = gate
    return PipelineEntity.model_validate({"name": "sample", "graph": {"nodes": [node], "edges": []}})


def _desired_graph(*, gate: dict[str, str] | None) -> dict:
    return pipeline_apply.resolve_graph(_entity(gate=gate), {"worker": AGENT_ID})


def _current_graph(*, gate: dict[str, str] | None) -> dict:
    """The normalised read-side graph, as ``fetch_current`` builds it.

    ``gate=None`` simulates a server that DID NOT persist the block (the
    silent-drop case): ``normalize_current_graph`` adds the model's
    ``policy_gate: None`` default, so the block is absent from the read.
    """
    node: dict[str, object] = {
        "id": NODE_ID,
        "node_type": "agent",
        "agent_id": AGENT_ID,
        "position": {"x": 0, "y": 0},
    }
    if gate is not None:
        node["policy_gate"] = gate
    return pipeline_apply.normalize_current_graph({"nodes": [node], "edges": []})


def _plan(gate: dict[str, str] | None, current_gate: dict[str, str] | None) -> str:
    entity = _entity(gate=gate)
    view = entity.managed_view(graph=_desired_graph(gate=gate))
    current = {
        "description": None,
        "max_concurrent_runs": 5,
        "graph": _current_graph(gate=current_gate),
    }
    return plan_entity(KIND_PIPELINE, "sample", view, current).status


class TestServerModelRoundTrip:
    """Criteria 9/14 (mechanism): the block survives write and read."""

    def test_server_node_model_preserves_policy_gate(self) -> None:
        node = PipelineGraphNode.model_validate(
            {
                "id": NODE_ID,
                "node_type": "agent",
                "agent_id": AGENT_ID,
                "position": {"x": 0, "y": 0},
                "policy_gate": _gate(),
            }
        )
        assert node.policy_gate is not None
        assert node.policy_gate.action == "block"
        dumped = node.model_dump(mode="json")
        assert dumped["policy_gate"] == {"action": "block", "eval_id": EVAL_ID}

    def test_server_node_model_defaults_absent_gate_to_none(self) -> None:
        node = PipelineGraphNode.model_validate(
            {
                "id": NODE_ID,
                "node_type": "agent",
                "agent_id": AGENT_ID,
                "position": {"x": 0, "y": 0},
            }
        )
        assert node.policy_gate is None

    def test_graph_write_then_read_round_trips_policy_gate(self) -> None:
        node = PipelineGraphNode.model_validate(
            {
                "id": NODE_ID,
                "node_type": "agent",
                "agent_id": AGENT_ID,
                "position": {"x": 0, "y": 0},
                "policy_gate": _gate("warn"),
            }
        )
        node_data, edge_data, _validator, _bindings = _prepare_graph_write(PipelineGraphUpdate(nodes=[node], edges=[]))
        # The write representation (what replace_pipeline_graph persists into
        # pipeline.graph_nodes_json) carries the block.
        assert node_data[0]["policy_gate"] == {"action": "warn", "eval_id": EVAL_ID}
        # The read representation (GET /pipelines/{id}/graph) round-trips it.
        response = _graph_response(node_data, edge_data)
        read_back = response.nodes[0].policy_gate
        assert read_back is not None
        assert read_back.action == "warn"
        assert str(read_back.eval_id) == EVAL_ID

    def test_normalize_current_graph_round_trips_policy_gate(self) -> None:
        graph = _current_graph(gate=_gate("block"))
        assert graph["nodes"][0]["policy_gate"] == {"action": "block", "eval_id": EVAL_ID}

    def test_normalize_current_graph_drops_truly_unknown_node_field(self) -> None:
        """Control: the model still ignores a field it does not declare.

        This is the mechanism that produced the defect — proving it for an
        undeclared field shows the round-trip test above is not vacuous.
        """
        graph = pipeline_apply.normalize_current_graph(
            {"nodes": [{"id": NODE_ID, "node_type": "manual", "position": {"x": 0, "y": 0}, "mystery": 1}], "edges": []}
        )
        assert "mystery" not in graph["nodes"][0]


class TestModelValidation:
    def test_valid_policy_gate_accepted(self) -> None:
        node = ApplyGraphNode.model_validate(
            {
                "id": NODE_ID,
                "node_type": "agent",
                "agent": "worker",
                "position": {"x": 0, "y": 0},
                "policy_gate": _gate(),
            }
        )
        assert node.policy_gate == ApplyGraphPolicyGate(action="block", eval_id=uuid.UUID(EVAL_ID))

    def test_absent_policy_gate_accepted(self) -> None:
        node = ApplyGraphNode.model_validate(
            {"id": NODE_ID, "node_type": "agent", "agent": "worker", "position": {"x": 0, "y": 0}}
        )
        assert node.policy_gate is None

    @pytest.mark.parametrize("action", ["halt", "BLOCK", "", "warn "])
    def test_invalid_action_rejected(self, action: str) -> None:
        with pytest.raises(ValidationError):
            ApplyGraphNode.model_validate(
                {
                    "id": NODE_ID,
                    "node_type": "agent",
                    "agent": "worker",
                    "position": {"x": 0, "y": 0},
                    "policy_gate": {"action": action, "eval_id": EVAL_ID},
                }
            )

    def test_invalid_eval_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApplyGraphNode.model_validate(
                {
                    "id": NODE_ID,
                    "node_type": "agent",
                    "agent": "worker",
                    "position": {"x": 0, "y": 0},
                    "policy_gate": {"action": "block", "eval_id": "not-a-uuid"},
                }
            )

    def test_unknown_gate_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApplyGraphNode.model_validate(
                {
                    "id": NODE_ID,
                    "node_type": "agent",
                    "agent": "worker",
                    "position": {"x": 0, "y": 0},
                    "policy_gate": {"action": "block", "eval_id": EVAL_ID, "enabled": True},
                }
            )

    def test_duplicate_eval_ids_across_nodes_rejected(self) -> None:
        second_node = "00000000-0000-0000-0000-0000000000a2"
        with pytest.raises(ApplyLoadError):
            parse_apply_documents(
                f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      graph:
        nodes:
          - id: "{NODE_ID}"
            node_type: agent
            agent: worker
            position: {{x: 0, y: 0}}
            policy_gate: {{action: block, eval_id: "{EVAL_ID}"}}
          - id: "{second_node}"
            node_type: agent
            agent: worker
            position: {{x: 0, y: 0}}
            policy_gate: {{action: warn, eval_id: "{EVAL_ID}"}}
        edges: []
"""
            )


class TestPlanDrift:
    def test_unchanged_gate_reports_no_drift(self) -> None:
        assert _plan(_gate("block"), _gate("block")) == "unchanged"

    def test_changed_action_reports_drift(self) -> None:
        assert _plan(_gate("warn"), _gate("block")) == "updated"

    def test_changed_eval_reports_drift(self) -> None:
        other_eval = "660e8400-e29b-41d4-a716-446655440099"
        assert _plan(_gate("block", other_eval), _gate("block")) == "updated"

    def test_removed_gate_reports_drift(self) -> None:
        assert _plan(None, _gate("block")) == "updated"

    def test_added_gate_reports_drift(self) -> None:
        assert _plan(_gate("block"), None) == "updated"

    def test_silent_drop_reports_permanent_drift_never_unchanged(self) -> None:
        """Criterion 14: a gate the server did not persist is permanent drift.

        The desired side carries the declared gate; the current side (as if the
        server silently dropped it on write) does not. The plan MUST report
        ``updated`` forever — never a false ``unchanged`` — so the defect is
        visible on the next ``modulo apply --diff``.
        """
        first = _plan(_gate("block"), None)
        second = _plan(_gate("block"), None)
        assert first == "updated"
        assert second == "updated"

    def test_graphless_config_ignores_policy_gates(self) -> None:
        """Criterion 8: a graph-less config does not manage nodes."""
        entity = PipelineEntity.model_validate(
            {"name": "sample", "description": "Sample pipeline", "max_concurrent_runs": 3}
        )
        view = entity.managed_view()
        current = {
            "description": "Sample pipeline",
            "max_concurrent_runs": 3,
            "graph": _current_graph(gate=_gate("block")),
        }
        assert plan_entity(KIND_PIPELINE, "sample", view, current).status == "unchanged"


class TestDriftDetail:
    def test_drift_detail_lists_policy_gate_change(self) -> None:
        entity = _entity(gate=_gate("warn"))
        desired = {
            KIND_PIPELINE: [("sample", entity.managed_view(graph=_desired_graph(gate=_gate("warn"))))],
        }
        current = {KIND_PIPELINE: {"sample": {"graph": _current_graph(gate=_gate("block"))}}}
        report = {"updated": [{"kind": KIND_PIPELINE, "name": "sample"}]}
        detail = build_drift_detail(current, desired, report)
        assert detail["sample"]["policy_gates"] == [
            {
                "change": "changed",
                "node": NODE_ID,
                "action": "warn",
                "previous_action": "block",
                "eval_id": EVAL_ID,
                "previous_eval_id": EVAL_ID,
            }
        ]

    def test_drift_detail_lists_removed_gate(self) -> None:
        entity = _entity(gate=None)
        desired = {KIND_PIPELINE: [("sample", entity.managed_view(graph=_desired_graph(gate=None)))]}
        current = {KIND_PIPELINE: {"sample": {"graph": _current_graph(gate=_gate("block"))}}}
        report = {"updated": [{"kind": KIND_PIPELINE, "name": "sample"}]}
        detail = build_drift_detail(current, desired, report)
        assert detail["sample"]["policy_gates"][0]["change"] == "removed"

    def test_render_table_shows_gate_change_in_drift_detail(self) -> None:
        report = {
            "mode": "drift",
            "created": [],
            "updated": [{"kind": KIND_PIPELINE, "name": "sample"}],
            "unchanged": [],
            "blocked": [],
            "failed": [],
            "drift_detail": {
                "sample": {
                    "nodes": {"added": [], "removed": [], "modified": [NODE_ID]},
                    "edges": {"added": [], "removed": [], "modified": []},
                    "policy_gates": [
                        {
                            "change": "changed",
                            "node": NODE_ID,
                            "action": "warn",
                            "previous_action": "block",
                            "eval_id": EVAL_ID,
                            "previous_eval_id": EVAL_ID,
                        }
                    ],
                }
            },
        }
        text = render_table(report)
        assert f"gate change pipeline 'sample' node {NODE_ID} (block->warn)" in text

    def test_render_table_shows_apply_gate_change(self) -> None:
        report = {
            "created": [],
            "updated": [{"kind": KIND_PIPELINE, "name": "sample"}],
            "unchanged": [],
            "blocked": [],
            "failed": [],
            "gate_changes": [
                {"pipeline": "sample", "change": "added", "node": NODE_ID, "action": "block", "eval_id": EVAL_ID}
            ],
        }
        text = render_table(report)
        assert f"gate add pipeline 'sample' node {NODE_ID} (block, eval {EVAL_ID})" in text

    def test_policy_gate_changes_empty_when_equal(self) -> None:
        assert not policy_gate_changes(_desired_graph(gate=_gate()), _current_graph(gate=_gate()))


class TestEvalResolution:
    def _resolve(self, *, evals: dict, gate_pipeline_id: str | None = PIPELINE_ID) -> str | None:
        entity = _entity(gate=_gate())
        current_entities = {
            "evals": evals,
            "evals_error": None,
            KIND_PIPELINE: {"sample": {"id": gate_pipeline_id}} if gate_pipeline_id else {},
        }
        return pipeline_apply.resolve_policy_gate_evals(entity, current_entities)

    def test_missing_eval_blocks_entity(self) -> None:
        reason = self._resolve(evals={})
        assert reason is not None
        assert EVAL_ID in reason
        assert "does not exist" in reason

    def test_eval_belonging_to_other_pipeline_blocks(self) -> None:
        reason = self._resolve(evals={EVAL_ID: {"id": EVAL_ID, "pipeline_id": "00000000-0000-0000-0000-0000000000bb"}})
        assert reason is not None
        assert "different pipeline" in reason

    def test_eval_error_blocks_gate_declaring_entity(self) -> None:
        entity = _entity(gate=_gate())
        reason = pipeline_apply.resolve_policy_gate_evals(
            entity, {"evals": {}, "evals_error": "403 forbidden", KIND_PIPELINE: {}}
        )
        assert reason is not None
        assert "cannot resolve" in reason

    def test_resolved_eval_does_not_block(self) -> None:
        assert self._resolve(evals={EVAL_ID: {"id": EVAL_ID, "pipeline_id": PIPELINE_ID}}) is None

    def test_no_gate_never_needs_evals(self) -> None:
        entity = _entity(gate=None)
        assert pipeline_apply.resolve_policy_gate_evals(entity, {"evals_error": "boom"}) is None


def _pipeline_item(name: str, pipeline_id: str) -> dict:
    return {
        "id": pipeline_id,
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "description": "Sample pipeline",
        "visibility": "org",
        "max_concurrent_runs": 3,
        "lock_wait_timeout_seconds": 300,
        "node_timeout_seconds": 300,
        "run_context_defaults": {},
        "default_autonomy_level": "manual_approval",
        "max_duration_seconds": 3600,
        "stale_run_timeout_minutes": 30,
        "rate_limit_config": None,
        "retry_policy": {},
        "snapshot_count": 0,
        "node_count": 1,
        "archived_at": None,
        "owner_team_id": None,
        "folder_id": None,
    }


def _graph_payload(*, gate: dict[str, str] | None) -> dict:
    node: dict[str, object] = {
        "id": NODE_ID,
        "node_type": "agent",
        "agent_id": AGENT_ID,
        "position": {"x": 0, "y": 0},
    }
    if gate is not None:
        node["policy_gate"] = gate
    return {"nodes": [node], "edges": [], "validation_issues": []}


def _mock_current(
    pipelines: list[dict],
    *,
    graph: dict | None = None,
    evals: list[dict] | None = None,
) -> dict[str, respx.Route]:
    """Mock every endpoint a gate-declaring pipeline apply touches."""
    from tests.unit.cli.test_apply_executor import _mock_current as _mock_base

    _mock_base([], [])
    routes: dict[str, respx.Route] = {}
    routes["pipelines_get"] = respx.get("https://api.test/api/v1/pipelines", params=_LIST_PARAMS).respond(
        json={"items": pipelines, "total": len(pipelines), "page": 1, "page_size": PAGE_SIZE}
    )
    respx.get("https://api.test/api/v1/agents", params=_LIST_PARAMS).respond(
        json={"items": [{"id": AGENT_ID, "name": "worker"}], "total": 1, "page": 1, "page_size": PAGE_SIZE}
    )
    respx.get("https://api.test/api/v1/evals", params=_LIST_PARAMS).respond(
        json={"items": list(evals or []), "total": len(evals or []), "page": 1, "page_size": PAGE_SIZE}
    )
    for row in pipelines:
        payload = graph if graph is not None else {"nodes": [], "edges": [], "validation_issues": []}
        respx.get(f"https://api.test/api/v1/pipelines/{row['id']}/graph").respond(json=dict(payload))
    routes["pipelines_post"] = respx.post("https://api.test/api/v1/pipelines").mock(
        return_value=httpx.Response(201, json=_pipeline_item("sample", str(uuid.uuid4())))
    )
    routes["pipeline_patch"] = respx.patch(re.compile(r"https://api\.test/api/v1/pipelines/[^/]+$")).mock(
        return_value=httpx.Response(200, json=_pipeline_item("sample", str(uuid.uuid4())))
    )
    return routes


class TestApplyReport:
    @respx.mock
    def test_apply_records_added_gate_change(self) -> None:
        routes = _mock_current([], evals=[{"id": EVAL_ID, "pipeline_id": PIPELINE_ID, "node_id": NODE_ID}])
        config = parse_apply_documents(_GATE_BLOCK)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert not report["failed"]
        assert report["gate_changes"] == [
            {"pipeline": "sample", "change": "added", "node": NODE_ID, "action": "block", "eval_id": EVAL_ID}
        ]
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        graph_json = patch_payload["graph_json"]
        assert graph_json["nodes"][0]["policy_gate"] == {"action": "block", "eval_id": EVAL_ID}

    @respx.mock
    def test_apply_records_removed_gate_change(self) -> None:
        existing = _pipeline_item("sample", PIPELINE_ID)
        routes = _mock_current(
            [existing],
            graph=_graph_payload(gate=_gate("block")),
            evals=[{"id": EVAL_ID, "pipeline_id": PIPELINE_ID, "node_id": NODE_ID}],
        )
        config = parse_apply_documents(_REMOVAL_BLOCK)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert not report["failed"]
        assert report["gate_changes"] == [
            {"pipeline": "sample", "change": "removed", "node": NODE_ID, "action": "block", "eval_id": EVAL_ID}
        ]
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        graph_json = patch_payload["graph_json"]
        assert graph_json["nodes"][0].get("policy_gate") is None

    @respx.mock
    def test_double_apply_is_idempotent(self) -> None:
        """Criterion 5: with the gate already persisted, the config is unchanged."""
        existing = _pipeline_item("sample", PIPELINE_ID)
        routes = _mock_current(
            [existing],
            graph=_graph_payload(gate=_gate("block")),
            evals=[{"id": EVAL_ID, "pipeline_id": PIPELINE_ID, "node_id": NODE_ID}],
        )
        config = parse_apply_documents(_GATE_BLOCK)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert [e["name"] for e in report["unchanged"]] == ["sample"]
        assert not report["updated"]
        assert not report.get("gate_changes")
        assert routes["pipeline_patch"].call_count == 0

    @respx.mock
    def test_missing_eval_blocks_pipeline_without_writing(self) -> None:
        routes = _mock_current([], evals=[])
        config = parse_apply_documents(_GATE_BLOCK)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        blocked = [e for e in report["blocked"] if e["kind"] == KIND_PIPELINE]
        assert len(blocked) == 1
        assert EVAL_ID in blocked[0]["reason"]
        assert routes["pipelines_post"].call_count == 0
