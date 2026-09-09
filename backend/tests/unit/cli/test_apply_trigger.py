"""Unit tests for modulo.cli.apply.trigger_apply (FAR-681 slice 2).

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

from modulo.api.routes.triggers import TriggerCreate, TriggerUpdate
from modulo.cli.apply.executor import PAGE_SIZE, ApplyExecutor
from modulo.cli.apply.loader import ApplyLoadError, parse_apply_documents
from tests.unit.cli.test_apply_executor import _mock_current

LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE)}

CRON_TRIGGER_TEXT = """
api_version: modulo.dev/v1
entities:
  triggers:
    - pipeline: nightly-report
      name: nightly
      trigger_type: cron
      cron_expression: "0 3 * * *"
"""

_CONFIG_WITH_SECRET = """
api_version: modulo.dev/v1
entities:
  triggers:
    - pipeline: nightly-report
      name: hook
      trigger_type: webhook
      config_json:
        hmac_secret: ${env:HS}
        note: plain value
"""

_CONFIG_WITH_ENV_REF = """
api_version: modulo.dev/v1
entities:
  triggers:
    - pipeline: nightly-report
      name: hook
      trigger_type: webhook
      config_json:
        url: ${env:URL}
        note: plain value
"""

_UPSTREAM_BLOCKED_PIPELINE_TEXT = """
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: nightly-report
      graph:
        nodes:
          - id: 00000000-0000-0000-0000-0000000000a1
            node_type: agent
            agent: missing-agent
            position: {x: 0, y: 0}
        edges: []
  triggers:
    - pipeline: nightly-report
      name: nightly
      trigger_type: cron
      cron_expression: "0 3 * * *"
"""


def _pipeline_row(name: str, pipeline_id: str) -> dict:
    return {
        "id": pipeline_id,
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "description": None,
        "visibility": "org",
        "max_concurrent_runs": 5,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _trigger_row(
    *,
    pipeline_id: str,
    name: str,
    trigger_type: str = "cron",
    trigger_id: str = "00000000-0000-0000-0000-0000000000cc",
    cron_expression: str | None = "0 3 * * *",
    config_json: dict | None = None,
) -> dict:
    return {
        "id": trigger_id,
        "pipeline_id": pipeline_id,
        "name": name,
        "trigger_type": trigger_type,
        "active": True,
        "max_concurrent_runs": 1,
        "daily_spend_limit": None,
        "config_json": config_json if config_json is not None else {},
        "cron_expression": cron_expression,
        "cron_timezone": None,
        "last_fired_at": None,
        "next_fire_at": None,
        "in_flight": 0,
        "streak_status": {"state": "ok"},
        "created_by": str(uuid.uuid4()),
    }


def _apply_trigger_routes(
    pipelines: list[dict],
    triggers: list[dict],
) -> dict[str, respx.Route]:
    """Full list mocks; returns mutation routes for overrides."""
    _mock_current([], [])
    routes: dict[str, respx.Route] = {}
    routes["pipelines_get"] = respx.get("https://api.test/api/v1/pipelines", params=LIST_PARAMS).respond(
        json={"items": pipelines, "total": len(pipelines), "page": 1, "page_size": 100}
    )
    routes["agents_get"] = respx.get("https://api.test/api/v1/agents", params=LIST_PARAMS).respond(
        json={"items": [], "total": 0, "page": 1, "page_size": 100}
    )
    routes["triggers_get"] = respx.get("https://api.test/api/v1/triggers", params=LIST_PARAMS).respond(
        json={"items": triggers, "total": len(triggers), "page": 1, "page_size": 100}
    )
    routes["trigger_post"] = respx.post(re.compile(r"https://api\.test/api/v1/pipelines/[0-9a-f-]+/triggers")).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": str(uuid.uuid4()),
                "pipeline_id": pipelines[0]["id"] if pipelines else str(uuid.uuid4()),
                "name": "new",
                "trigger_type": "cron",
                "active": True,
                "max_concurrent_runs": 1,
                "daily_spend_limit": None,
                "config_json": {},
                "cron_expression": None,
                "cron_timezone": None,
                "next_fire_at": None,
                "in_flight": 0,
                "streak_status": {"state": "ok"},
            },
        )
    )
    routes["trigger_put"] = respx.put(re.compile(r"https://api\.test/api/v1/triggers/[0-9a-f-]+")).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "pipeline_id": pipelines[0]["id"] if pipelines else str(uuid.uuid4()),
                "name": "nightly",
                "trigger_type": "cron",
                "active": True,
                "max_concurrent_runs": 1,
                "daily_spend_limit": None,
                "config_json": {},
                "cron_expression": "0 3 * * *",
                "cron_timezone": None,
                "next_fire_at": None,
                "in_flight": 0,
                "streak_status": {"state": "ok"},
            },
        )
    )
    return routes


class TestPlanDecisions:
    @respx.mock
    def test_created_when_pipeline_exists_in_org(self) -> None:
        _apply_trigger_routes([_pipeline_row("nightly-report", "00000000-0000-0000-0000-0000000000aa")], [])
        config = parse_apply_documents(CRON_TRIGGER_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        created = [e["name"] for e in report["created"] if e["kind"] == "trigger"]
        assert created == ["nightly-report/nightly"]
        assert not report["blocked"]

    @respx.mock
    def test_created_when_pipeline_declared_in_same_config(self) -> None:
        _apply_trigger_routes([], [])
        config = parse_apply_documents(
            """
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: nightly-report
      description: Nightly pipeline
  triggers:
    - pipeline: nightly-report
      name: nightly
      trigger_type: cron
      cron_expression: "0 3 * * *"
"""
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        created_kinds = sorted(e["kind"] for e in report["created"])
        assert created_kinds == ["pipeline", "trigger"]
        assert not report["blocked"]

    @respx.mock
    def test_missing_pipeline_blocks_with_hint(self) -> None:
        _apply_trigger_routes([], [])
        config = parse_apply_documents(CRON_TRIGGER_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "nightly-report/nightly"]
        assert len(blocked) == 1
        assert "pipeline 'nightly-report' not found" in blocked[0]["reason"]

    @respx.mock
    def test_forward_reference_rejected_at_load(self) -> None:
        """A trigger referencing a pipeline declared in a LATER document is a
        load error (merge_entities), not a plan block."""
        text = (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  triggers:\n"
            "    - pipeline: nightly-report\n"
            "      name: nightly\n"
            "      trigger_type: cron\n"
            "---\n"
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  pipelines:\n"
            "    - name: nightly-report\n"
        )
        with pytest.raises(ApplyLoadError):
            parse_apply_documents(text)

    @respx.mock
    def test_forward_reference_within_one_document_is_allowed(self) -> None:
        """Phase order is engine-defined (pipelines run first), so a trigger
        referencing a pipeline declared in the SAME document is not forward."""
        _apply_trigger_routes([], [])
        config = parse_apply_documents(
            """
api_version: modulo.dev/v1
entities:
  triggers:
    - pipeline: nightly-report
      name: nightly
      trigger_type: cron
  pipelines:
    - name: nightly-report
"""
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        created_kinds = sorted(e["kind"] for e in report["created"])
        assert created_kinds == ["pipeline", "trigger"]

    @respx.mock
    def test_unchanged_rerun_including_masked_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stored hmac_secret arrives MASKED; both sides strip it -> unchanged."""
        monkeypatch.setenv("HS", "resolved-never-compared")
        existing = _trigger_row(
            pipeline_id="00000000-0000-0000-0000-0000000000aa",
            name="hook",
            trigger_type="webhook",
            cron_expression=None,
            config_json={"hmac_secret": "••••••", "note": "plain value"},
        )
        _apply_trigger_routes([_pipeline_row("nightly-report", "00000000-0000-0000-0000-0000000000aa")], [existing])
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(parse_apply_documents(_CONFIG_WITH_SECRET), dry_run=True)
        unchanged = [e["name"] for e in report["unchanged"] if e["kind"] == "trigger"]
        assert unchanged == ["nightly-report/hook"]
        assert not report["updated"]
        assert not report["created"]

    @respx.mock
    def test_env_ref_config_converges_on_second_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-681 QA (env-ref false drift): the desired view is hashed on the
        RESOLVED config, so a ${env:VAR} under a non-sensitive key compares
        equal to the server's stored resolved literal -> unchanged on rerun
        (previously the raw ref vs the stored literal drifted as 'updated'
        every run, sending a pointless PUT each time)."""
        monkeypatch.setenv("URL", "https://resolved.example")
        existing = _trigger_row(
            pipeline_id="00000000-0000-0000-0000-0000000000aa",
            name="hook",
            trigger_type="webhook",
            cron_expression=None,
            config_json={"url": "https://resolved.example", "note": "plain value"},
        )
        _apply_trigger_routes([_pipeline_row("nightly-report", "00000000-0000-0000-0000-0000000000aa")], [existing])
        config = parse_apply_documents(_CONFIG_WITH_ENV_REF)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            first = executor.run(config, dry_run=True)
        unchanged = [e["name"] for e in first["unchanged"] if e["kind"] == "trigger"]
        assert unchanged == ["nightly-report/hook"]
        assert not first["updated"]
        # Second apply (fresh executor, same current state) is still unchanged.
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            second = executor.run(config, dry_run=True)
        assert second["unchanged"] == first["unchanged"]
        assert not second["updated"]

    @respx.mock
    def test_pipeline_blocked_upstream_blocks_trigger(self) -> None:
        """FAR-681 QA (dry-run honesty): a trigger whose pipeline was BLOCKED
        in the pipeline build phase is blocked with the upstream reason — it
        must not be planned as creatable against a pipeline that will never
        apply."""
        _apply_trigger_routes([], [])
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(parse_apply_documents(_UPSTREAM_BLOCKED_PIPELINE_TEXT), dry_run=True)
        blocked = {e["name"]: e["reason"] for e in report["blocked"]}
        assert "nightly-report" in blocked
        assert "agent 'missing-agent' not found" in blocked["nightly-report"]
        assert "nightly-report/nightly" in blocked
        assert "pipeline 'nightly-report' blocked upstream" in blocked["nightly-report/nightly"]
        assert blocked["nightly-report/nightly"] == (
            "pipeline 'nightly-report' blocked upstream: agent 'missing-agent' not found in target org - "
            "declare the agent in this config or create it first; apply never auto-creates agents"
        )
        assert not report["created"]

    @respx.mock
    def test_duplicate_live_trigger_name_blocks(self) -> None:
        """FAR-681 QA (identity uniqueness): two LIVE rows sharing the
        (pipeline, name) identity make name-based upsert ambiguous — the
        entity is blocked for manual resolution instead of last-wins."""
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        first = _trigger_row(pipeline_id=pipeline_id, name="nightly", trigger_id="00000000-0000-0000-0000-0000000000cc")
        second = _trigger_row(
            pipeline_id=pipeline_id, name="nightly", trigger_id="00000000-0000-0000-0000-0000000000dd"
        )
        _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [first, second])
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(parse_apply_documents(CRON_TRIGGER_TEXT), dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "nightly-report/nightly"]
        assert len(blocked) == 1
        assert blocked[0]["reason"] == "duplicate live trigger name - resolve manually"
        assert not report["created"]
        assert not report["updated"]
        assert not report["unchanged"]


class TestRefreshSecrets:
    """FAR-681 QA (secret-only rotation): the server masks stored secrets on
    read, so the drift hash cannot see a rotated ${env:SECRET} value. Default
    behaviour stays 'unchanged' (documented); --refresh-secrets re-sends the
    config for entities declaring secret-shaped entries."""

    @respx.mock
    def test_refresh_secrets_re_sends_masked_secret_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HS", "rotated-secret-value")
        existing = _trigger_row(
            pipeline_id="00000000-0000-0000-0000-0000000000aa",
            name="hook",
            trigger_type="webhook",
            cron_expression=None,
            config_json={"hmac_secret": "••••••", "note": "plain value"},
        )
        routes = _apply_trigger_routes(
            [_pipeline_row("nightly-report", "00000000-0000-0000-0000-0000000000aa")], [existing]
        )
        config = parse_apply_documents(_CONFIG_WITH_SECRET)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False, refresh_secrets=True)
        updated = [e["name"] for e in report["updated"] if e["kind"] == "trigger"]
        assert updated == ["nightly-report/hook"]
        payload = json.loads(routes["trigger_put"].calls.last.request.content)
        TriggerUpdate.model_validate(payload)
        # The ROTATED resolved secret reached the server.
        assert payload["config_json"]["hmac_secret"] == "rotated-secret-value"
        assert payload["config_json"]["note"] == "plain value"

    @respx.mock
    def test_refresh_secrets_leaves_secret_free_triggers_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("URL", "https://resolved.example")
        existing = _trigger_row(
            pipeline_id="00000000-0000-0000-0000-0000000000aa",
            name="hook",
            trigger_type="webhook",
            cron_expression=None,
            config_json={"url": "https://resolved.example", "note": "plain value"},
        )
        _apply_trigger_routes([_pipeline_row("nightly-report", "00000000-0000-0000-0000-0000000000aa")], [existing])
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(parse_apply_documents(_CONFIG_WITH_ENV_REF), dry_run=True, refresh_secrets=True)
        assert [e["name"] for e in report["unchanged"] if e["kind"] == "trigger"] == ["nightly-report/hook"]
        assert not report["updated"]


class TestExecution:
    @respx.mock
    def test_create_contract_and_env_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HS", "resolved-hmac-secret")
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", "00000000-0000-0000-0000-0000000000aa")], [])
        config = parse_apply_documents(_CONFIG_WITH_SECRET)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert not report["failed"]
        payload = json.loads(routes["trigger_post"].calls.last.request.content)
        # The body validates against the REAL TriggerCreate (incl. name).
        created = TriggerCreate.model_validate(payload)
        assert created.name == "hook"
        assert created.trigger_type == "webhook"
        # The secret was resolved client-side from the env ref.
        assert payload["config_json"]["hmac_secret"] == "resolved-hmac-secret"
        assert payload["config_json"]["note"] == "plain value"

    @respx.mock
    def test_update_payload_validates_against_real_model(self) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        existing = _trigger_row(pipeline_id=pipeline_id, name="nightly", cron_expression="0 9 * * *")
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [existing])
        config = parse_apply_documents(CRON_TRIGGER_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        updated = [e["name"] for e in report["updated"] if e["kind"] == "trigger"]
        assert updated == ["nightly-report/nightly"]
        payload = json.loads(routes["trigger_put"].calls.last.request.content)
        TriggerUpdate.model_validate(payload)
        # The cron expression was converging to the declared value.
        assert payload["cron_expression"] == "0 3 * * *"
        assert payload["daily_spend_limit"] is None

    @respx.mock
    def test_missing_pipeline_id_at_execute_is_contained(self) -> None:
        """The referenced pipeline was declared but its apply FAILED — the
        trigger fails (containment) instead of crashing the run."""
        routes = _apply_trigger_routes([], [])
        routes["pipelines_post"] = respx.post("https://api.test/api/v1/pipelines").mock(
            side_effect=httpx.Response(500, json={"detail": "pipeline boom"})
        )
        config = parse_apply_documents(
            """
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: nightly-report
      description: Nightly pipeline
  triggers:
    - pipeline: nightly-report
      name: nightly
      trigger_type: cron
"""
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed_kinds = {e["kind"] for e in report["failed"]}
        assert failed_kinds == {"pipeline", "trigger"}
        trigger_failures = [e for e in report["failed"] if e["kind"] == "trigger"]
        # FAR-681 QA: the pipeline IS declared here — its apply failed upstream,
        # so the reason says so instead of the misleading "not found" text.
        assert "apply failed upstream" in trigger_failures[0]["error"]
        assert "not found" not in trigger_failures[0]["error"]


class TestFailureIsolation:
    def _two_trigger_config_text(self) -> str:
        return (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  triggers:\n"
            "    - pipeline: nightly-report\n"
            "      name: first\n"
            "      trigger_type: cron\n"
            '      cron_expression: "0 3 * * *"\n'
            "    - pipeline: nightly-report\n"
            "      name: second\n"
            "      trigger_type: cron\n"
            '      cron_expression: "0 4 * * *"\n'
        )

    @respx.mock
    def test_create_400_isolates_entity(self) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        routes["trigger_post"].mock(
            side_effect=[
                httpx.Response(400, json={"detail": "injected failure"}),
                httpx.Response(201, json={"id": str(uuid.uuid4()), "name": "second"}),
            ]
        )
        config = parse_apply_documents(self._two_trigger_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed_names = [e["name"] for e in report["failed"]]
        assert failed_names == ["nightly-report/first"]
        created_names = [e["name"] for e in report["created"] if e["kind"] == "trigger"]
        assert created_names == ["nightly-report/second"]

    @respx.mock
    def test_create_500_isolates_entity(self) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        routes["trigger_post"].mock(
            side_effect=[
                httpx.Response(500, json={"detail": "boom"}),
                httpx.Response(201, json={"id": str(uuid.uuid4()), "name": "second"}),
            ]
        )
        config = parse_apply_documents(self._two_trigger_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert [e["name"] for e in report["failed"]] == ["nightly-report/first"]
        assert [e["name"] for e in report["created"] if e["kind"] == "trigger"] == ["nightly-report/second"]

    @respx.mock
    def test_create_409_hint_and_connect_error(self) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        routes["trigger_post"].mock(
            side_effect=[
                httpx.Response(409, json={"detail": "conflict"}),
                httpx.ConnectError("connection refused"),
            ]
        )
        config = parse_apply_documents(self._two_trigger_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed = report["failed"]
        assert [e["name"] for e in failed] == ["nightly-report/first", "nightly-report/second"]
        assert "rerun to apply as update" in failed[0]["error"]

    @respx.mock
    def test_put_failure_isolates_entity(self) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        first = _trigger_row(pipeline_id=pipeline_id, name="first", cron_expression="0 9 * * *")
        second = _trigger_row(
            pipeline_id=pipeline_id,
            name="second",
            cron_expression="0 9 * * *",
            trigger_id="00000000-0000-0000-0000-0000000000dd",
        )
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [first, second])

        def _put_by_id(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = {
                "id": "00000000-0000-0000-0000-0000000000dd",
                "name": "second",
                "trigger_type": "cron",
                "cron_expression": "0 4 * * *",
            }
            if path.endswith(first["id"]):
                return httpx.Response(500, json={"detail": "put boom"})
            return httpx.Response(200, json=body)

        routes["trigger_put"].mock(side_effect=_put_by_id)
        config = parse_apply_documents(self._two_trigger_config_text())
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert [e["name"] for e in report["failed"]] == ["nightly-report/first"]
        updated_names = [e["name"] for e in report["updated"] if e["kind"] == "trigger"]
        assert updated_names == ["nightly-report/second"]

    @respx.mock
    def test_ongoing_validation_rejection_surfaces_server_reason(self) -> None:
        """validate_ongoing_config rejection (422) -> failed with the server
        detail (never silenced)."""
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        routes["trigger_post"].mock(
            return_value=httpx.Response(422, json={"detail": "ongoing triggers require daily_spend_limit"})
        )
        config = parse_apply_documents(
            """
api_version: modulo.dev/v1
entities:
  triggers:
    - pipeline: nightly-report
      name: watch
      trigger_type: ongoing
"""
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed = report["failed"]
        assert [e["name"] for e in failed] == ["nightly-report/watch"]
        assert "daily_spend_limit" in failed[0]["error"]


class TestRefResolution:
    @respx.mock
    def test_unresolved_env_ref_blocks_entity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        routes = _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        monkeypatch.delenv("HS", raising=False)
        config = parse_apply_documents(_CONFIG_WITH_SECRET)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "nightly-report/hook"]
        assert len(blocked) == 1
        assert "unresolved" in blocked[0]["reason"]
        assert not report["created"]
        assert not report["failed"]
        assert not report["updated"]
        assert routes["trigger_post"].call_count == 0

    @respx.mock
    def test_empty_env_ref_blocking_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HS", "   ")
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        config = parse_apply_documents(_CONFIG_WITH_SECRET)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "nightly-report/hook"]
        assert "empty" in blocked[0]["reason"]
        assert "not set" not in blocked[0]["reason"]

    @respx.mock
    def test_secretref_blocked_at_plan_time(self) -> None:
        pipeline_id = "00000000-0000-0000-0000-0000000000aa"
        _apply_trigger_routes([_pipeline_row("nightly-report", pipeline_id)], [])
        config = parse_apply_documents(_CONFIG_WITH_SECRET.replace("${env:HS}", "secretref://vault/hook"))
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "nightly-report/hook"]
        assert len(blocked) == 1
        assert "secretref resolution not supported yet" in blocked[0]["reason"]
        assert not report["failed"]
