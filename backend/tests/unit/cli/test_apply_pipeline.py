"""Unit tests for modulo.cli.apply.pipeline_apply (FAR-681 slice 2).

HTTP mocked via respx. Round-trip payloads are validated against the REAL
API pydantic models (contract round-trip), not hand-built response fixtures.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
import respx

from modulo.api.routes.pipelines import PipelineCreate, PipelineUpdate
from modulo.cli.apply.executor import (
    PAGE_SIZE,
    ApplyExecutor,
)
from modulo.cli.apply.loader import parse_apply_documents
from tests.unit.cli.test_apply_executor import _backend_item, _mock_current, _schema_item

CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3
      graph:
        nodes:
          - id: 00000000-0000-0000-0000-0000000000a1
            node_type: agent
            agent: worker
            label: Do work
            position: {x: 0, y: 0}
        edges: []
"""

GRAPHLESS_CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3
"""

_AGENT_ID = "00000000-0000-0000-0000-0000000000ff"
_AGENT_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE)}
_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE)}


def _pipeline_item(name: str, pipeline_id: str, *, description: str | None = "Sample pipeline") -> dict:
    return {
        "id": pipeline_id,
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "description": description,
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


def _graph_payload(agent_id: str, node_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": node_id,
                "node_type": "agent",
                "agent_id": agent_id,
                "label": "Do work",
                "position": {"x": 0, "y": 0},
            }
        ],
        "edges": [],
        "validation_issues": [],
    }


def _mock_current_with_pipelines(
    pipelines: list[dict],
    *,
    agent_id: str = _AGENT_ID,
    graphs: dict[str, dict] | None = None,
) -> dict[str, respx.Route]:
    """List/create mocks for pipeline tests; returns the mutation routes."""
    _mock_current([], [])
    routes: dict[str, respx.Route] = {}
    routes["pipelines_get"] = respx.get("https://api.test/api/v1/pipelines", params=_LIST_PARAMS).respond(
        json={"items": pipelines, "total": len(pipelines), "page": 1, "page_size": 100}
    )
    routes["agents_get"] = respx.get("https://api.test/api/v1/agents", params=_AGENT_LIST_PARAMS).respond(
        json={"items": [{"id": agent_id, "name": "worker"}], "total": 1, "page": 1, "page_size": 100}
    )
    respx.get("https://api.test/api/v1/triggers", params=_LIST_PARAMS).respond(
        json={"items": [], "total": 0, "page": 1, "page_size": 100}
    )
    for name, graph in (graphs or {}).items():
        row = next(item for item in pipelines if item["name"] == name)
        respx.get(f"https://api.test/api/v1/pipelines/{row['id']}/graph").respond(json=dict(graph))
    routes["pipelines_post"] = respx.post("https://api.test/api/v1/pipelines").mock(
        return_value=httpx.Response(201, json=_pipeline_item("sample", str(uuid.uuid4())))
    )
    routes["pipeline_patch"] = respx.patch(re.compile(r"https://api\.test/api/v1/pipelines/[^/]+$")).mock(
        return_value=httpx.Response(200, json=_pipeline_item("sample", str(uuid.uuid4())))
    )
    return routes


class TestPlanDecisions:
    @respx.mock
    def test_missing_agent_ref_blocks_entity(self) -> None:
        _mock_current_with_pipelines([])
        respx.get("https://api.test/api/v1/agents", params=_AGENT_LIST_PARAMS).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "sample"]
        assert len(blocked) == 1
        assert "agent 'worker' not found" in blocked[0]["reason"]
        assert "never auto-creates" in blocked[0]["reason"]
        assert not report["created"]
        assert not report["failed"]

    @respx.mock
    def test_created_when_absent(self) -> None:
        _mock_current_with_pipelines([])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        created = [e["name"] for e in report["created"] if e["kind"] == "pipeline"]
        assert created == ["sample"]
        assert not report["blocked"]

    @respx.mock
    def test_unchanged_rerun(self) -> None:
        row = _pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa")
        _mock_current_with_pipelines(
            [row],
            graphs={"sample": _graph_payload(_AGENT_ID, "00000000-0000-0000-0000-0000000000a1")},
        )
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        unchanged = [e["name"] for e in report["unchanged"] if e["kind"] == "pipeline"]
        assert unchanged == ["sample"]
        assert not report["created"]
        assert not report["updated"]
        assert not report["blocked"]

    @respx.mock
    def test_changed_top_level_is_updated(self) -> None:
        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        existing["max_concurrent_runs"] = 5
        _mock_current_with_pipelines(
            [existing],
            graphs={"sample": _graph_payload(_AGENT_ID, "00000000-0000-0000-0000-0000000000a1")},
        )
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        updated = [e["name"] for e in report["updated"] if e["kind"] == "pipeline"]
        assert updated == ["sample"]

    @respx.mock
    def test_graph_drift_is_updated(self) -> None:
        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        graph = _graph_payload(_AGENT_ID, "00000000-0000-0000-0000-0000000000a1")
        graph["nodes"][0]["label"] = "Renamed elsewhere"
        _mock_current_with_pipelines([existing], graphs={"sample": graph})
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        updated = [e["name"] for e in report["updated"] if e["kind"] == "pipeline"]
        assert updated == ["sample"]

    @respx.mock
    def test_graphless_config_never_fetches_graphs(self) -> None:
        """A graph-less config does not manage the graph: no graph fetch, no drift."""
        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        existing["max_concurrent_runs"] = 3
        _mock_current_with_pipelines([existing])
        config = parse_apply_documents(GRAPHLESS_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        unchanged = [e["name"] for e in report["unchanged"] if e["kind"] == "pipeline"]
        assert unchanged == ["sample"]
        graph_gets = [c for c in respx.calls if "/graph" in str(c.request.url)]
        assert not graph_gets

    @respx.mock
    def test_invalid_sandbox_node_blocked_client_side(self) -> None:
        """Missing sandbox template_id is caught by the cheap client-side
        normalisation through the real API node model (never a silent save)."""
        _mock_current_with_pipelines([])
        config = parse_apply_documents(
            """
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: buggy
      graph:
        nodes:
          - id: 00000000-0000-0000-0000-0000000000b1
            node_type: sandbox_agent
            mode: llm
            position: {x: 1, y: 1}
        edges: []
"""
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = list(report["blocked"])
        assert len(blocked) == 1
        assert blocked[0]["name"] == "buggy"
        assert "invalid pipeline graph" in blocked[0]["reason"]
        assert not report["created"]
        assert not report["failed"]


class TestExecution:
    @respx.mock
    def test_create_posts_and_applies_graph(self) -> None:
        routes = _mock_current_with_pipelines([])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert not report["failed"]
        created = [e["name"] for e in report["created"] if e["kind"] == "pipeline"]
        assert created == ["sample"]
        assert routes["pipelines_post"].call_count == 1
        # POST body validates against the REAL PipelineCreate.
        create_payload = json.loads(routes["pipelines_post"].calls.last.request.content)
        PipelineCreate.model_validate(create_payload)
        # The graph is applied via PATCH /{id} graph_json; validates through
        # the REAL PipelineUpdate (nested PipelineGraphUpdate shape).
        assert routes["pipeline_patch"].call_count == 1
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        update = PipelineUpdate.model_validate(patch_payload)
        assert update.graph_json is not None
        agent_nodes = [n for n in update.graph_json.nodes if n.node_type == "agent"]
        assert agent_nodes[0].agent_id is not None

    @respx.mock
    def test_create_without_graph_skips_patch(self) -> None:
        """A graph-less pipeline create sends only the POST (no graph write)."""
        routes = _mock_current_with_pipelines([])
        config = parse_apply_documents(GRAPHLESS_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert not report["failed"]
        assert routes["pipelines_post"].call_count == 1
        assert routes["pipeline_patch"].call_count == 0

    @respx.mock
    def test_update_patches_top_fields_and_unchanged_graph_is_omitted(self) -> None:
        existing_id = "00000000-0000-0000-0000-0000000000aa"
        existing = _pipeline_item("sample", existing_id)
        existing["max_concurrent_runs"] = 5
        routes = _mock_current_with_pipelines(
            [existing],
            graphs={"sample": _graph_payload(_AGENT_ID, "00000000-0000-0000-0000-0000000000a1")},
        )
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        updated_names = [e["name"] for e in report["updated"] if e["kind"] == "pipeline"]
        assert updated_names == ["sample"]
        assert routes["pipeline_patch"].call_count == 1
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        update = PipelineUpdate.model_validate(patch_payload)
        assert update.max_concurrent_runs == 3
        # The graph is unchanged -> no graph_json in the PATCH (no snapshot churn).
        assert "graph_json" not in patch_payload

    @respx.mock
    def test_update_with_graph_drift_sends_graph_json(self) -> None:
        existing_id = "00000000-0000-0000-0000-0000000000aa"
        existing = _pipeline_item("sample", existing_id)
        drifted = _graph_payload(_AGENT_ID, "00000000-0000-0000-0000-0000000000a1")
        drifted["nodes"][0]["label"] = "Renamed elsewhere"
        routes = _mock_current_with_pipelines([existing], graphs={"sample": drifted})
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert not report["failed"]
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        update = PipelineUpdate.model_validate(patch_payload)
        assert update.graph_json is not None
        agent_nodes = [n for n in update.graph_json.nodes if n.node_type == "agent"]
        assert agent_nodes[0].agent_id is not None


class TestFailureIsolation:
    """Failure-injection: one pipeline failing must not abort the others."""

    def _two_pipeline_config_text(self) -> str:
        return (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  pipelines:\n"
            "    - name: first\n"
            "      description: First\n"
            "    - name: second\n"
            "      description: Second\n"
        )

    @respx.mock
    def test_post_400_isolates_entity(self) -> None:
        report = self._run_isolated_http_case(400, "injected failure")
        failed = report["failed"]
        assert len(failed) == 1
        assert failed[0]["name"] == "first"
        assert [e["name"] for e in report["created"] if e["kind"] == "pipeline"] == ["second"]

    @respx.mock
    def test_post_500_isolates_entity(self) -> None:
        report = self._run_isolated_http_case(500, "boom")
        failed = report["failed"]
        assert len(failed) == 1
        assert failed[0]["name"] == "first"
        assert [e["name"] for e in report["created"] if e["kind"] == "pipeline"] == ["second"]

    @respx.mock
    def test_post_409_isolates_entity_with_hint(self) -> None:
        routes = _mock_current_with_pipelines([])
        routes["pipelines_post"].mock(
            side_effect=[
                httpx.Response(409, json={"detail": "duplicate name"}),
                httpx.Response(201, json=_pipeline_item("second", str(uuid.uuid4()))),
            ]
        )
        config = parse_apply_documents(self._two_pipeline_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed = report["failed"]
        assert len(failed) == 1
        assert failed[0]["name"] == "first"
        assert "rerun to apply as update" in failed[0]["error"]
        assert [e["name"] for e in report["created"] if e["kind"] == "pipeline"] == ["second"]

    @respx.mock
    def test_connect_error_isolates_entity(self) -> None:
        routes = _mock_current_with_pipelines([])
        routes["pipelines_post"].mock(
            side_effect=[
                httpx.ConnectError("connection refused"),
                httpx.Response(201, json=_pipeline_item("second", str(uuid.uuid4()))),
            ]
        )
        config = parse_apply_documents(self._two_pipeline_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed_names = [e["name"] for e in report["failed"]]
        assert failed_names == ["first"]
        created_names = [e["name"] for e in report["created"] if e["kind"] == "pipeline"]
        assert created_names == ["second"]

    @respx.mock
    def test_patch_failure_isolates_entity(self) -> None:
        first_id = "00000000-0000-0000-0000-0000000000aa"
        second_id = "00000000-0000-0000-0000-0000000000bb"
        first = dict(_pipeline_item("first", first_id))
        first["description"] = "First"
        second = dict(_pipeline_item("second", second_id))
        second["description"] = "Second"
        routes = _mock_current_with_pipelines([first, second])

        def _patch_by_id(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith(first_id):
                return httpx.Response(500, json={"detail": "patch boom"})
            return httpx.Response(200, json=second)

        routes["pipeline_patch"].mock(side_effect=_patch_by_id)
        config = parse_apply_documents(
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  pipelines:\n"
            "    - name: first\n"
            "      description: Updated first\n"
            "    - name: second\n"
            "      description: Updated second\n"
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed_names = [e["name"] for e in report["failed"]]
        assert failed_names == ["first"]
        updated_names = [e["name"] for e in report["updated"] if e["kind"] == "pipeline"]
        assert updated_names == ["second"]

    def _run_isolated_http_case(self, status_code: int, detail: str) -> dict:
        routes = _mock_current_with_pipelines([])
        routes["pipelines_post"].mock(
            side_effect=[
                httpx.Response(status_code, json={"detail": detail}),
                httpx.Response(201, json=_pipeline_item("second", str(uuid.uuid4()))),
            ]
        )
        config = parse_apply_documents(self._two_pipeline_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            return executor.run(config, dry_run=False)


class TestGraphlessUpdateDoesNotDriftOnGraph:
    @respx.mock
    def test_graphless_config_is_unchanged_even_with_ui_graph(self) -> None:
        """The declared graph-less config never manages the graph: a stored
        (UI-authored) graph causes no update churn."""
        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        _mock_current_with_pipelines([existing])
        config = parse_apply_documents(GRAPHLESS_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        unchanged = [e["name"] for e in report["unchanged"] if e["kind"] == "pipeline"]
        assert unchanged == ["sample"]
        assert not report["updated"]


class TestMixedKindsPhaseOrder:
    @respx.mock
    def test_backend_pipeline_and_schema_report_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The report keeps phase order: schemas -> model_backends -> pipelines."""
        monkeypatch.setenv("SK", "resolved-secret")
        _mock_current_with_pipelines([])
        respx.post("https://api.test/api/v1/model-backends").mock(
            return_value=httpx.Response(201, json=_backend_item("openai", "openai"))
        )
        respx.post(re.compile(r"https://api\.test/api/v1/model-backends/[0-9a-f-]+/health-check")).mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "detail": None, "checked_at": "2026-01-01T00:00:00Z"}
            )
        )
        respx.post("https://api.test/api/v1/schemas").mock(
            return_value=httpx.Response(201, json=_schema_item("alpha", None))
        )
        config = parse_apply_documents(
            """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: alpha
      description: Alpha schema
  model_backends:
    - name: openai
      display_name: OpenAI
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
  pipelines:
    - name: sample
      description: Sample pipeline
"""
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        created_kinds = [e["kind"] for e in report["created"] if e["name"] in ("alpha", "openai", "sample")]
        assert created_kinds == ["schema", "model_backend", "pipeline"]
        assert not report["failed"]
