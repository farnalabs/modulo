"""BDD step definitions: ``modulo apply`` declarative configuration CLI (feat-apply).

Drives the REAL ``modulo.cli.apply`` seams network-free and DB-free:

- **loading** goes through the real ``loader.parse_apply_documents`` and the
  real ``models.ApplyConfig`` validators (duplicate-name / api_version /
  secret-refs-only gates);
- **secret refs** resolve through the real ``executor.resolve_secret_refs``;
- **planning** goes through the real ``plan.plan_entity`` / ``plan.build_plan``;
- **apply + health-check verification** go through the real
  ``executor.ApplyExecutor`` against a respx-mocked API (the same mock shape
  the unit suite drives in ``tests.unit.cli.test_apply_executor``);
- **drift** goes through the real ``drift.build_drift_detail`` /
  ``drift.has_drift`` and the real CLI ``render_table`` drift rendering.

HTTP-mocked scenarios wrap the whole request inside a single ``When`` step in
a ``with respx.mock`` block, storing the report on the node so the ``Then``
verdicts can inspect it.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.cli.apply import render_table
from modulo.cli.apply.drift import build_drift_detail, has_drift
from modulo.cli.apply.executor import PAGE_SIZE, ApplyExecutor, resolve_secret_refs
from modulo.cli.apply.loader import ApplyLoadError, parse_apply_documents
from modulo.cli.apply.models import ModelBackendEntity, SchemaEntity
from modulo.cli.apply.plan import build_plan, has_blockers, plan_entity

scenarios("../features/cli/apply.feature")

_API_BASE = "https://api.test"
_API_KEY = "key"
_DOC_SEP = "---\n"
_BACKENDS_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE), "include_in_dev": "true"}


def _schema_item(name: str, description: str | None, schema_id: str | None = None) -> dict:
    return {
        "id": schema_id or str(uuid.uuid4()),
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "description": description,
        "abstract_name": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _mock_current(schemas: list[dict], backends: list[dict]) -> dict:
    """Mock the list/create endpoints for a schema/backend config (unit-suite shape)."""
    routes: dict[str, Any] = {}
    routes["schemas_get"] = respx.get(
        f"{_API_BASE}/api/v1/schemas", params={"page": "1", "page_size": str(PAGE_SIZE)}
    ).respond(json={"items": schemas, "total": len(schemas), "page": 1, "page_size": PAGE_SIZE})
    routes["backends_get"] = respx.get(f"{_API_BASE}/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
        json={"items": backends, "total": len(backends), "page": 1, "page_size": PAGE_SIZE}
    )
    routes["versions_get"] = respx.get(
        re.compile(rf"{re.escape(_API_BASE)}/api/v1/schemas/[^/]+/versions"),
        params={"page": "1", "page_size": str(PAGE_SIZE)},
    ).respond(json={"items": [], "total": 0})
    routes["schemas_post"] = respx.post(f"{_API_BASE}/api/v1/schemas").mock(
        return_value=httpx.Response(201, json=_schema_item("applied", None))
    )
    routes["backends_post"] = respx.post(f"{_API_BASE}/api/v1/model-backends").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": str(uuid.uuid4()),
                "organisation_id": str(uuid.uuid4()),
                "name": "applied",
                "display_name": "applied",
                "provider": "applied-provider",
                "model_id": "some-model",
                "has_credentials": True,
                "default_params": {},
                "visibility": "org",
                "tier": "native",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    )
    routes["health_post"] = respx.post(
        re.compile(rf"{re.escape(_API_BASE)}/api/v1/model-backends/[^/]+/health-check")
    ).mock(
        return_value=httpx.Response(200, json={"status": "ok", "detail": None, "checked_at": "2026-01-01T00:00:00Z"})
    )
    return routes


def _state(request) -> dict[str, Any]:
    if not hasattr(request.node, "_apply_state"):
        request.node._apply_state = {}
    return request.node._apply_state


def _config_text(state: dict[str, Any]) -> str:
    return "\n".join(state["docs"])


def _schema_doc(name: str, description: str = "Alpha schema") -> str:
    return (
        _DOC_SEP
        + "api_version: modulo.dev/v1\n"
        + "entities:\n"
        + "  schemas:\n"
        + f"    - name: {name}\n"
        + f"      description: {description}\n"
    )


def _backend_doc(name: str, *, api_key: str) -> str:
    return (
        _DOC_SEP
        + "api_version: modulo.dev/v1\n"
        + "entities:\n"
        + "  model_backends:\n"
        + f"    - name: {name}\n"
        + f"      display_name: {name}\n"
        + "      provider: openai\n"
        + "      model_id: gpt-x\n"
        + f"      api_key: {api_key}\n"
    )


def _desired_view(entity: SchemaEntity | ModelBackendEntity) -> dict[str, Any]:
    """Canonical managed-field view for a *desired* entity (api_key excluded)."""
    return entity.managed_view()


@contextmanager
def _apply_env(state: dict[str, Any]) -> Any:
    """Apply the scenario's env refs to ``os.environ`` for an executor run.

    The real ``executor.run`` resolves ``${env:VAR}`` refs from ``os.environ``,
    so a scenario's declared env (or deliberate missing-var) must be mirrored
    there for the duration of the (mocked) HTTP run. ``MonkeyPatch.context``
    restores the original process environment on exit.
    """
    environ = state.get("environ", {})
    with pytest.MonkeyPatch.context() as monkeypatch:
        for key, value in environ.items():
            monkeypatch.setenv(key, value)
        if "SK" not in environ:
            monkeypatch.delenv("SK", raising=False)
        yield


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("a modulo apply test context")
def step_apply_context(request) -> None:
    _state(request).clear()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@given(parsers.parse('an apply config declaring the schema "{name}"'))
def step_config_declares_schema(request, name: str) -> None:
    _state(request)["docs"] = [_schema_doc(name)]


@given(parsers.parse('an apply config declaring the schema "{name}" twice'))
def step_config_schema_twice(request, name: str) -> None:
    text = (
        _DOC_SEP
        + "api_version: modulo.dev/v1\n"
        + "entities:\n"
        + "  schemas:\n"
        + f"    - name: {name}\n"
        + f"    - name: {name}\n"
    )
    _state(request)["docs"] = [text]


@given(parsers.parse('an apply config whose first document declares the schema "{name}"'))
def step_first_doc_schema(request, name: str) -> None:
    _state(request)["docs"] = [_schema_doc(name)]


@given(parsers.parse('a second apply document declaring the schema "{name}"'))
def step_second_doc_schema(request, name: str) -> None:
    _state(request)["docs"].append(_schema_doc(name))


@given(parsers.parse('a second apply document declaring the model backend "{name}"'))
def step_second_doc_backend(request, name: str) -> None:
    _state(request)["docs"].append(_backend_doc(name, api_key="${env:SK}"))


@given(parsers.parse('an apply config whose first document declares only a trigger for pipeline "{pipeline}"'))
def step_first_doc_trigger(request, pipeline: str) -> None:
    text = (
        _DOC_SEP
        + "api_version: modulo.dev/v1\n"
        + "entities:\n"
        + "  triggers:\n"
        + f"    - pipeline: {pipeline}\n"
        + "      name: nightly\n"
        + "      trigger_type: cron\n"
        + "      cron_expression: '0 3 * * *'\n"
    )
    _state(request)["docs"] = [text]


@given(parsers.parse('a second apply document declaring pipeline "{pipeline}"'))
def step_second_doc_pipeline(request, pipeline: str) -> None:
    text = _DOC_SEP + "api_version: modulo.dev/v1\n" + "entities:\n" + "  pipelines:\n" + f"    - name: {pipeline}\n"
    _state(request)["docs"].append(text)


@given("an empty apply config file")
def step_empty_config(request) -> None:
    _state(request)["docs"] = ["# only a comment\n"]


@given(parsers.parse('an apply config declaring the model backend "{name}" with the raw api_key "{raw}"'))
def step_config_raw_backend(request, name: str, raw: str) -> None:
    _state(request)["docs"] = [_backend_doc(name, api_key=raw)]


@given(
    parsers.re(
        r'an apply config declaring the model backend "(?P<name>[^"]+)" '
        r'with the env api_key "\$\{env:(?P<var>[A-Za-z_]\w*)\}"'
    )
)
def step_config_env_backend(request, name: str, var: str) -> None:
    state = _state(request)
    state.setdefault("docs", []).append(_backend_doc(name, api_key=f"${{env:{var}}}"))


@given(parsers.parse('an apply config declaring the model backend "{name}" with the api_key "{ref}"'))
def step_config_secretref_backend(request, name: str, ref: str) -> None:
    _state(request)["docs"] = [_backend_doc(name, api_key=ref)]


@given(parsers.parse('the environment variable "{var}" is "{value}"'))
def step_env_set(request, var: str, value: str) -> None:
    _state(request).setdefault("environ", {})[var] = value


@given(parsers.parse('the environment variable "{var}" is missing'))
def step_env_missing(request, var: str) -> None:
    _state(request)["environ"] = {}


@when("I load the apply config")
@when("I load the combined documents")
def step_load_config(request) -> None:
    state = _state(request)
    try:
        state["config"] = parse_apply_documents(_config_text(state))
        state["load_error"] = None
    except ApplyLoadError as exc:
        state["load_error"] = str(exc)
        state["config"] = None


@when("I resolve secret refs")
def step_resolve_secret_refs(request) -> None:
    state = _state(request)
    config = state.get("config") or parse_apply_documents(_config_text(state))
    resolved, blocked = resolve_secret_refs(config, environ=state.get("environ", {}))
    state["resolved_keys"] = resolved
    state["blocked_refs"] = blocked


@then(parsers.parse('the config carries the schema "{name}"'))
def step_config_has_schema(request, name: str) -> None:
    config = _state(request)["config"]
    assert config is not None, _state(request).get("load_error")
    assert [s.name for s in config.entities.schemas] == [name]


@then(parsers.parse('the config carries the model backend "{name}"'))
def step_config_has_backend(request, name: str) -> None:
    config = _state(request)["config"]
    assert config is not None, _state(request).get("load_error")
    assert [b.name for b in config.entities.model_backends] == [name]


@then(parsers.parse('the load fails with an error mentioning "{fragment}"'))
def step_load_fails(request, fragment: str) -> None:
    error = _state(request)["load_error"]
    assert error is not None, "expected a load-time error"
    assert fragment in error, f"expected {fragment!r} in load error: {error}"


@then(parsers.parse('the backend "{name}" api_key resolves to "{value}"'))
def step_backend_resolved(request, name: str, value: str) -> None:
    assert _state(request)["resolved_keys"].get(name) == value


@then("nothing is blocked")
def step_nothing_blocked(request) -> None:
    assert not _state(request)["blocked_refs"]


@then(parsers.parse('the backend "{name}" is blocked mentioning "{fragment}"'))
def step_backend_blocked(request, name: str, fragment: str) -> None:
    blocked = _state(request)["blocked_refs"]
    entries = [b for b in blocked if b[0] == "model_backend" and b[1] == name]
    assert entries, f"expected {name!r} to be blocked, got {blocked}"
    assert fragment in entries[0][2], f"expected {fragment!r} in block reason: {entries[0][2]}"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@given(parsers.parse('a schema entity "{name}" with description "{description}"'))
def step_desired_schema(request, name: str, description: str) -> None:
    state = _state(request)
    state["entity"] = SchemaEntity.model_validate({"name": name, "description": description})
    state["live"] = None


@given(parsers.parse('a model backend entity "{name}" with provider "{provider}"'))
def step_desired_backend(request, name: str, provider: str) -> None:
    state = _state(request)
    state["entity"] = ModelBackendEntity.model_validate(
        {
            "name": name,
            "display_name": name,
            "provider": provider,
            "model_id": "gpt-x",
            "api_key": "${env:SK}",
        }
    )
    state["live"] = None


@given(parsers.parse('a schema entity "{name}" with version "{version}" whose content is {content}'))
def step_desired_schema_version(request, name: str, version: str, content) -> None:
    state = _state(request)
    state["entity"] = SchemaEntity.model_validate(
        {
            "name": name,
            "description": "Schema with versions",
            "versions": [
                {"version": version, "version_number": 1, "definition_json": json.loads(content), "published": False}
            ],
        }
    )
    state["live"] = None


@given(parsers.parse('the live schema "{name}" has description "{description}"'))
def step_live_schema(request, name: str, description: str) -> None:
    _state(request)["live"] = {"description": description, "abstract_name": None, "versions": []}


@given(parsers.parse('the live model backend "{name}" has provider "{provider}"'))
def step_live_backend(request, name: str, provider: str) -> None:
    _state(request)["live"] = {
        "provider": provider,
        "display_name": name,
        "model_id": "gpt-x",
        "default_params": {},
        "visibility": "org",
        "tier": "native",
    }


@given(parsers.parse('the live schema "{name}" has version "{version}" whose content is {content}'))
def step_live_schema_version(request, name: str, version: str, content) -> None:
    _state(request)["live"] = {
        "description": "Schema with versions",
        "abstract_name": None,
        "versions": [
            {"version": version, "version_number": 1, "definition_json": json.loads(content), "published": False}
        ],
    }


@when("I plan the entity")
def step_plan(request) -> None:
    state = _state(request)
    entity = state["entity"]
    kind = "schema" if isinstance(entity, SchemaEntity) else "model_backend"
    state["decision"] = plan_entity(kind, entity.name, _desired_view(entity), state.get("live"))


@then(parsers.parse('the decision status is "{status}"'))
def step_decision_status(request, status: str) -> None:
    assert _state(request)["decision"].status == status


@then('the decision reason mentions "provider mismatch"')
def step_decision_provider_mismatch(request) -> None:
    assert "provider mismatch" in (_state(request)["decision"].reason or "")


@then('the decision reason mentions "immutable"')
def step_decision_immutable(request) -> None:
    assert "immutable" in (_state(request)["decision"].reason or "")


# ---------------------------------------------------------------------------
# Apply + verification (HTTP via respx)
# ---------------------------------------------------------------------------


@given("the live org has no schemas and no model backends")
def step_live_empty(request) -> None:
    _state(request)["live_state"] = ([], [])


@given(parsers.parse('the live org matches the config exactly for the schema "{name}"'))
def step_live_matches(request, name: str) -> None:
    _state(request)["live_state"] = ([_schema_item(name, "Alpha schema")], [])


@given(
    parsers.parse('the API accepts the backend create but its health check reports "{status}" with detail "{detail}"')
)
def step_api_broken_backend(request, status: str, detail: str) -> None:
    _state(request)["health"] = {"status": status, "detail": detail}


@when("I run apply (not dry-run)")
def step_run_apply(request) -> None:
    state = _state(request)
    config = parse_apply_documents(_config_text(state))
    with respx.mock, _apply_env(state):
        schemas, backends = state.get("live_state", ([], []))
        routes = _mock_current(schemas, backends)
        health = state.get("health")
        if health is not None:
            routes["health_post"].mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": health["status"],
                        "detail": health["detail"],
                        "checked_at": "2026-01-01T00:00:00Z",
                    },
                )
            )
        with httpx.Client() as client:
            executor = ApplyExecutor(_API_BASE, _API_KEY, client=client)
            try:
                report = executor.run(config, dry_run=False, drift=False)
            finally:
                executor.close()
    state["report"] = report
    state["routes"] = routes


@when("I run apply as dry-run")
def step_run_apply_dry_run(request) -> None:
    state = _state(request)
    config = parse_apply_documents(_config_text(state))
    with respx.mock, _apply_env(state):
        schemas, backends = state.get("live_state", ([], []))
        _mock_current(schemas, backends)
        with httpx.Client() as client:
            executor = ApplyExecutor(_API_BASE, _API_KEY, client=client)
            try:
                report = executor.run(config, dry_run=True, drift=False)
            finally:
                executor.close()
    state["report"] = report


@when("I run apply in drift mode")
def step_run_apply_drift(request) -> None:
    state = _state(request)
    config = parse_apply_documents(_config_text(state))
    with respx.mock, _apply_env(state):
        schemas, backends = state.get("live_state", ([], []))
        _mock_current(schemas, backends)
        with httpx.Client() as client:
            executor = ApplyExecutor(_API_BASE, _API_KEY, client=client)
            try:
                report = executor.run(config, dry_run=False, drift=True)
            finally:
                executor.close()
        written = [c for c in respx.calls if c.request.method in ("POST", "PATCH", "PUT", "DELETE")]
    state["report"] = report
    state["written"] = written


@then(parsers.parse('the report contains "{name}" in "{bucket}"'))
def step_report_contains(request, name: str, bucket: str) -> None:
    report = _state(request)["report"]
    assert any(e["name"] == name for e in report.get(bucket, [])), f"expected {name!r} in report[{bucket}]: {report}"


@then(parsers.parse('the failed entry error mentions "{fragment}"'))
def step_failed_error(request, fragment: str) -> None:
    report = _state(request)["report"]
    errors = [e["error"] for e in report.get("failed", []) if isinstance(e.get("error"), str)]
    assert any(fragment in e for e in errors), f"expected {fragment!r} in failed errors: {errors}"


@then("the report has blockers")
def step_report_blockers(request) -> None:
    report = _state(request)["report"]
    assert report.get("blocked") or report.get("failed"), f"expected blockers in report: {report}"


@then("the report has no blockers")
def step_report_no_blockers(request) -> None:
    report = _state(request)["report"]
    assert not report.get("blocked") and not report.get("failed"), f"expected no blockers: {report}"


@then("the report is a dry-run report")
def step_report_dry_run(request) -> None:
    assert _state(request)["report"].get("dry_run") is True


@then('the report is labelled "drift"')
def step_report_labelled_drift(request) -> None:
    assert _state(request)["report"].get("mode") == "drift"


@then("the report is a read-only report with no write requests")
def step_report_read_only(request) -> None:
    state = _state(request)
    assert state["report"].get("dry_run") is None, "drift mode must not look like a dry-run"
    assert not state["written"], f"expected no POST/PATCH/PUT/DELETE calls in drift mode: {state['written']}"


@then("the report has drift")
def step_report_has_drift(request) -> None:
    assert has_drift(_state(request)["report"])


@then("the report has no drift")
def step_report_no_drift(request) -> None:
    assert not has_drift(_state(request)["report"])


@then(parsers.parse('the report lists "{name}" as "{status}"'))
def step_report_lists(request, name: str, status: str) -> None:
    report = _state(request)["report"]
    assert any(e["name"] == name for e in report.get(status, [])), f"expected {name!r} in {status}: {report}"


@then("the API received the backend create request")
def step_api_backend_created(request) -> None:
    assert _state(request)["routes"]["backends_post"].call_count == 1


# ---------------------------------------------------------------------------
# Exit-code semantics
# ---------------------------------------------------------------------------


@given(parsers.parse('a plan report containing a blocked model backend "{name}"'))
def step_plan_report_blocked(request, name: str) -> None:
    _state(request)["report"] = build_plan(
        desired_entities={},
        current_entities={},
        blocked_entities=[("model_backend", name, "provider mismatch: cannot change provider via apply")],
    )


@then(parsers.parse('a report with only "unchanged" entries has no blockers'))
def step_unchanged_no_blockers(request) -> None:
    entity = SchemaEntity.model_validate({"name": "alpha", "description": "Same"})
    live = {"description": "Same", "abstract_name": None, "versions": []}
    report = build_plan({"schema": [("alpha", _desired_view(entity))]}, {"schema": {"alpha": live}})
    assert not has_blockers(report)


# ---------------------------------------------------------------------------
# Drift detail + rendering
# ---------------------------------------------------------------------------


@given(parsers.parse('a desired pipeline "{name}" whose graph adds the node "{node}"'))
def step_desired_pipeline_graph(request, name: str, node: str) -> None:
    state = _state(request)
    state["desired"] = {
        "pipeline": [(name, {"graph": {"nodes": [{"id": node, "label": "New"}], "edges": []}})],
    }
    state["live_pipelines"] = {name: {"graph": {"nodes": [], "edges": []}}}
    state["pipeline_report"] = {"updated": [{"kind": "pipeline", "name": name}]}


@given(parsers.parse('the live pipeline "{name}" has a graph without the node "{node}"'))
def step_live_pipeline_graph(request, name: str, node: str) -> None:
    _state(request)["live_pipelines"] = {name: {"graph": {"nodes": [], "edges": []}}}


@when("I build the drift detail")
def step_build_drift_detail(request) -> None:
    state = _state(request)
    state["drift_detail"] = build_drift_detail(state["live_pipelines"], state["desired"], state["pipeline_report"])


@then(parsers.parse('the drift detail for pipeline "{name}" has node additions {added}'))
def step_drift_detail_added(request, name: str, added) -> None:
    detail = _state(request)["drift_detail"]
    assert name in detail, f"expected drift detail for {name!r}: {detail}"
    assert detail[name]["nodes"]["added"] == json.loads(added)


@given(parsers.parse('a drift report containing a created schema "{name}"'))
def step_drift_report_created(request, name: str) -> None:
    _state(request)["report"] = {
        "mode": "drift",
        "created": [{"kind": "schema", "name": name}],
        "updated": [],
        "unchanged": [],
        "blocked": [],
        "failed": [],
        "drift_detail": {},
    }


@when("I render the drift report")
def step_render_drift(request) -> None:
    _state(request)["table"] = render_table(_state(request)["report"])


@then('the rendered table contains "drift create"')
def step_table_drift_verb(request) -> None:
    assert "drift create" in _state(request)["table"]


@then('the rendered summary reads "drift summary"')
def step_table_drift_summary(request) -> None:
    assert "drift summary" in _state(request)["table"]
