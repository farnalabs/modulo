"""Unit tests for ``modulo apply --diff`` drift mode (FAR-681 slice 3).

Drift mode is read-only by construction: the executor returns after the
plan phase (the same gate the dry-run return takes), proving no
POST/PATCH/PUT code path is reachable with --diff.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import click
import httpx
import pytest
import respx
from click.testing import CliRunner

from modulo.cli.apply import register_apply, render_table
from modulo.cli.apply.drift import has_drift
from modulo.cli.apply.executor import ApplyExecutor
from modulo.cli.apply.loader import parse_apply_documents
from tests.unit.cli.test_apply_executor import (
    _STABLE_CURRENT,
    CONFIG_TEXT,
    GOLDEN_CONFIG_TEXT,
    _mock_current,
    _mock_new_kind_lists,
    _schema_item,
)

_MATCH_SCHEMA_CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: alpha
      description: Alpha schema
"""

_TRIGGERS_ONLY_CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  triggers:
    - pipeline: sample
      name: nightly
      trigger_type: cron
      cron_expression: "0 3 * * *"
      daily_spend_limit: 0.5
    - pipeline: sample
      name: hook
      trigger_type: webhook
"""

_CLI_LIST_PARAMS = {"page": "1", "page_size": "100"}
_CLI_BACKENDS_LIST_PARAMS = {"page": "1", "page_size": "100", "include_in_dev": "true"}
_GOLDEN_PIPELINE_ID = "00000000-0000-0000-0000-0000000000bb"


def _cli_mock(schemas: list[dict]) -> list[respx.Route]:
    """Mock only what a schema-only config fetches."""
    return [
        respx.get("https://api.test/api/v1/schemas", params=_CLI_LIST_PARAMS).respond(
            json={"items": schemas, "total": len(schemas), "page": 1, "page_size": 100}
        ),
        respx.get("https://api.test/api/v1/model-backends", params=_CLI_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        ),
    ]


@click.group("test")
def _group() -> None:
    pass


register_apply(_group)


class TestDriftReportShape:
    @respx.mock
    def test_drift_detected_on_modified_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declared-but-absent entities are drift (the golden fixture state)."""
        monkeypatch.setenv("SK", "resolved-secret")
        _mock_current([], [])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, drift=True)
        assert report["mode"] == "drift"
        assert "dry_run" not in report
        created = sorted(e["name"] for e in report["created"])
        assert created == ["alpha", "fresh", "openai"]
        assert not report["updated"]
        assert not report["failed"]
        assert has_drift(report)

    @respx.mock
    def test_no_drift_reports_unchanged_only(self) -> None:
        """A config matching live state is not drift."""
        schemas = [_schema_item("alpha", "Alpha schema")]
        _cli_mock(schemas)
        config = parse_apply_documents(_MATCH_SCHEMA_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, drift=True)
        unchanged = sorted(e["name"] for e in report["unchanged"])
        assert unchanged == ["alpha"]
        assert not report["created"]
        assert not report["updated"]
        assert not report["blocked"]
        assert not report["drift_detail"]
        assert not has_drift(report)


class TestNodeLevelPipelineDrift:
    @respx.mock
    def test_modified_node_is_reported(self) -> None:
        from tests.unit.cli.test_apply_pipeline import (
            CONFIG_TEXT as PIPELINE_CONFIG_TEXT,
        )
        from tests.unit.cli.test_apply_pipeline import (
            _mock_current_with_pipelines,
            _pipeline_item,
        )

        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        current_graph = {
            "nodes": [
                {
                    "id": "00000000-0000-0000-0000-0000000000a1",
                    "node_type": "agent",
                    "agent_id": _AGENT_FF,
                    "label": "Renamed elsewhere",
                    "position": {"x": 0, "y": 0},
                }
            ],
            "edges": [],
        }
        _mock_current_with_pipelines([existing], graphs={"sample": current_graph})
        config = parse_apply_documents(PIPELINE_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, drift=True)
        detail = report["drift_detail"]
        assert detail["sample"]["nodes"]["modified"] == ["00000000-0000-0000-0000-0000000000a1"]
        assert not detail["sample"]["nodes"]["added"]
        assert not detail["sample"]["nodes"]["removed"]
        assert not detail["sample"]["edges"]["added"]

    @respx.mock
    def test_added_and_removed_nodes(self) -> None:
        from tests.unit.cli.test_apply_pipeline import (
            _mock_current_with_pipelines,
            _pipeline_item,
        )

        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        # Live graph has the configured node plus an extra node that the
        # config does not declare (removed by the next apply); the config
        # declares a node id the live graph lacks (added by the next apply).
        current_graph = {
            "nodes": [
                {
                    "id": "00000000-0000-0000-0000-0000000000a1",
                    "node_type": "agent",
                    "agent_id": _AGENT_FF,
                    "position": {"x": 0, "y": 0},
                },
                {
                    "id": "00000000-0000-0000-0000-0000000000a2",
                    "node_type": "agent",
                    "agent_id": _AGENT_FF,
                    "position": {"x": 1, "y": 1},
                },
            ],
            "edges": [],
        }
        _mock_current_with_pipelines([existing], graphs={"sample": current_graph})
        config = parse_apply_documents(_EXTRA_NODE_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, drift=True)
        detail = report["drift_detail"]["sample"]
        assert detail["nodes"]["removed"] == ["00000000-0000-0000-0000-0000000000a2"]
        assert detail["nodes"]["added"] == ["00000000-0000-0000-0000-0000000000a3"]
        assert not detail["nodes"]["modified"]

    @respx.mock
    def test_unrelated_field_drift_gets_no_graph_entry(self) -> None:
        """Top-level-only drift (no graph difference) gets no breakdown entry."""
        from tests.unit.cli.test_apply_pipeline import (
            _mock_current_with_pipelines,
            _pipeline_item,
        )

        existing = dict(_pipeline_item("sample", "00000000-0000-0000-0000-0000000000aa"))
        existing["max_concurrent_runs"] = 9
        _mock_current_with_pipelines(
            [existing],
            graphs={"sample": _SAME_GRAPH},
        )
        config = parse_apply_documents(_PLAIN_PIPELINE_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, drift=True)
        assert not report["drift_detail"]
        updated = [e["name"] for e in report["updated"] if e["kind"] == "pipeline"]
        assert updated == ["sample"]


class TestDiffNeverWrites:
    @respx.mock
    def test_drift_mode_makes_no_mutating_requests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        routes = _mock_current([], [])
        patch_any = respx.patch(re.compile(r"https://api\.test/api/v1/.*")).mock(
            return_value=httpx.Response(500, json={"detail": "must not be called"})
        )
        put_any = respx.put(re.compile(r"https://api\.test/api/v1/.*")).mock(
            return_value=httpx.Response(500, json={"detail": "must not be called"})
        )
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            executor.run(config, dry_run=False, drift=True)
        assert routes["schemas_post"].call_count == 0
        assert routes["backends_post"].call_count == 0
        assert routes["health_post"].call_count == 0
        assert patch_any.call_count == 0
        assert put_any.call_count == 0


class TestDriftRendering:
    def test_table_labels_drift_report(self) -> None:
        report = {
            "mode": "drift",
            "created": [{"kind": "schema", "name": "alpha"}],
            "updated": [{"kind": "pipeline", "name": "sample"}],
            "unchanged": [{"kind": "model_backend", "name": "openai"}],
            "blocked": [],
            "failed": [],
            "drift_detail": {
                "sample": {
                    "nodes": {"added": ["x"], "removed": [], "modified": ["y"]},
                    "edges": {"added": [], "removed": [], "modified": []},
                }
            },
        }
        text = render_table(report)
        assert "drift create schema 'alpha'" in text
        assert "drift update pipeline 'sample'" in text
        assert "unchanged model_backend 'openai'" in text
        assert "drift detail pipeline 'sample': graph +1/-0/~1 nodes, +0/-0/~0 edges" in text
        assert "drift summary: 1 created, 1 updated, 1 unchanged, 0 blocked, 0 failed" in text

    def test_table_keeps_plan_labels_without_drift_mode(self) -> None:
        report = {
            "created": [{"kind": "schema", "name": "alpha"}],
            "updated": [],
            "unchanged": [],
            "blocked": [],
            "failed": [],
        }
        text = render_table(report)
        assert "create schema 'alpha'" in text
        assert "summary: 1 created" in text
        assert "drift" not in text


def _golden_pipeline_row() -> dict:
    from tests.unit.cli.test_apply_pipeline import _pipeline_item

    return _pipeline_item("sample", _GOLDEN_PIPELINE_ID)


def _golden_trigger_row(
    name: str,
    *,
    trigger_type: str,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    daily_spend_limit: float | None = None,
    extras: dict | None = None,
) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "pipeline_id": _GOLDEN_PIPELINE_ID,
        "name": name,
        "trigger_type": trigger_type,
        "active": True,
        "max_concurrent_runs": 1,
        "daily_spend_limit": daily_spend_limit,
        "config_json": {},
        "cron_expression": cron_expression,
        "cron_timezone": cron_timezone,
    }
    item.update(extras or {})
    return item


def _golden_drift_report(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Deterministic drift report for the golden fixture config (absent state).

    Same mocks as the plan golden, but drift mode: the report is labelled
    ``mode=drift`` with an empty breakdown (nothing exists yet, so the
    pipelines are all ``created`` — never graph-drifted).
    """
    monkeypatch.setenv("SK", "golden-secret")
    with respx.mock:
        respx.get("https://api.test/api/v1/schemas", params=_CLI_LIST_PARAMS).respond(
            json={"items": [_STABLE_CURRENT], "total": 1, "page": 1, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_CLI_LIST_PARAMS).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        _mock_new_kind_lists(empty_state=True)
        config = parse_apply_documents(GOLDEN_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            return executor.run(config, dry_run=False, drift=True)


class TestDriftGoldenSnapshot:
    def test_drift_snapshot_matches_golden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = _golden_drift_report(monkeypatch)
        golden_path = Path(__file__).resolve().parent / "golden" / "apply_drift_snapshot.json"
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        assert report == golden
        assert has_drift(report)


class TestDiffExitCodes:
    @respx.mock
    def test_drift_exits_1(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_URL", "https://api.test")
        monkeypatch.setenv("MODULO_API_KEY", "key")
        monkeypatch.setenv("SK", "v")
        _mock_current([], [])
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG_TEXT, encoding="utf-8")
        result = CliRunner().invoke(_group, ["apply", "-f", str(path), "--diff", "--output", "json"])
        assert result.exit_code == 1, result.output
        parsed = json.loads(result.output)
        assert parsed["mode"] == "drift"
        assert len(parsed["created"]) == 3

    @respx.mock
    def test_no_drift_exits_0(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_URL", "https://api.test")
        monkeypatch.setenv("MODULO_API_KEY", "key")
        _cli_mock([_schema_item("alpha", "Alpha schema")])
        path = tmp_path / "config.yaml"
        path.write_text(_MATCH_SCHEMA_CONFIG_TEXT, encoding="utf-8")
        result = CliRunner().invoke(_group, ["apply", "-f", str(path), "--diff", "--output", "json"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["mode"] == "drift"
        assert not parsed["drift_detail"]
        assert all(not parsed[s] for s in ("created", "updated", "blocked", "failed"))

    @respx.mock
    def test_diff_table_says_drift_summary(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_URL", "https://api.test")
        monkeypatch.setenv("MODULO_API_KEY", "key")
        monkeypatch.setenv("SK", "v")
        _mock_current([], [])
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG_TEXT, encoding="utf-8")
        result = CliRunner().invoke(_group, ["apply", "-f", str(path), "--diff"])
        assert result.exit_code == 1, result.output
        assert "drift create schema" in result.output
        assert "drift summary" in result.output


class TestRuntimeStateExcluded:
    @respx.mock
    def test_trigger_runtime_fields_never_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """next_fire_at / streak_epoch / dead-letter counters (and 4dp spend
        quantisation) are runtime state: never part of drift."""
        monkeypatch.setenv("SK", "resolved-secret")
        _cli_mock([])
        respx.get("https://api.test/api/v1/pipelines", params=_CLI_LIST_PARAMS).respond(
            json={
                "items": [_golden_pipeline_row()],
                "total": 1,
                "page": 1,
                "page_size": 100,
            }
        )
        nightly = _golden_trigger_row(
            "nightly",
            trigger_type="cron",
            cron_expression="0 3 * * *",
            daily_spend_limit=0.5,
            extras={"next_fire_at": "2026-01-02T00:00:00Z", "streak_epoch": 7},
        )
        hook = _golden_trigger_row(
            "hook",
            trigger_type="webhook",
            extras={"consecutive_dead_letter_count": 2},
        )
        respx.get("https://api.test/api/v1/triggers", params=_CLI_LIST_PARAMS).respond(
            json={"items": [nightly, hook], "total": 2, "page": 1, "page_size": 100}
        )
        config = parse_apply_documents(_TRIGGERS_ONLY_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, drift=True)
        unchanged = sorted(e["name"] for e in report["unchanged"] if e["kind"] == "trigger")
        assert unchanged == ["sample/hook", "sample/nightly"]
        assert not report["created"]
        assert not report["updated"]
        assert not report["blocked"]
        assert not has_drift(report)


_AGENT_FF = "00000000-0000-0000-0000-0000000000ff"

_EXTRA_NODE_CONFIG_TEXT = """
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
            position: {x: 0, y: 0}
          - id: 00000000-0000-0000-0000-0000000000a3
            node_type: agent
            agent: worker
            position: {x: 2, y: 2}
        edges: []
"""

_SAME_GRAPH = {
    "nodes": [
        {
            "id": "00000000-0000-0000-0000-0000000000a1",
            "node_type": "agent",
            "agent_id": _AGENT_FF,
            "label": "Do work",
            "position": {"x": 0, "y": 0},
        }
    ],
    "edges": [],
}

_PLAIN_PIPELINE_CONFIG_TEXT = """
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
"""
