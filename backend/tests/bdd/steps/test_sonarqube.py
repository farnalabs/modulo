"""BDD step definitions for SonarQube connector scenarios."""

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, HealthResult

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/sonarqube.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for SonarQube connector tests."""
    return {}


def _build_connector(unhealthy: bool = False) -> AsyncMock:
    """Build a mock SonarQube connector mirroring the real connector's contract.

    ``query``/``write``/``health_check`` are async and raise ValueError for
    unsupported resources or missing required filters, matching
    ``SonarQubeConnector`` in ``src/modulo/connectors/sonarqube/``.
    """
    mock = AsyncMock()
    mock.connector_type = "sonarqube"

    async def mock_health_check() -> HealthResult:
        if unhealthy:
            return HealthResult(ok=False, detail="SonarQube health: RED")
        return HealthResult(ok=True, detail="SonarQube health: GREEN")

    async def mock_query(q: ConnectorQuery) -> ConnectorResult:
        if q.resource == "projects":
            return ConnectorResult(records=[{"key": "my-project", "name": "My Project"}], total=1)
        if q.resource == "project_analyses":
            if not q.filters.get("project"):
                raise ValueError("SonarQube project_analyses query requires 'project' filter")
            return ConnectorResult(records=[{"key": "analysis-1"}], total=1)
        if q.resource == "measures":
            if not q.filters.get("component"):
                raise ValueError("SonarQube measures query requires 'component' filter")
            if not q.filters.get("metricKeys"):
                raise ValueError("SonarQube measures query requires 'metricKeys' filter")
            return ConnectorResult(records=[{"metric": "coverage", "value": "87.5"}], total=1)
        if q.resource == "issues":
            return ConnectorResult(records=[{"key": "ISSUE1", "status": "OPEN"}], total=1)
        if q.resource == "quality_gates":
            return ConnectorResult(records=[{"name": "Sonar way", "id": 1}], total=1)
        if q.resource == "quality_gate":
            if not q.filters.get("id"):
                raise ValueError("SonarQube quality_gate query requires 'id' filter")
            return ConnectorResult(records=[{"name": "Strict Gate", "id": q.filters["id"]}], total=1)
        if q.resource == "plugins":
            return ConnectorResult(records=[{"key": "python", "name": "Python"}], total=1)
        if q.resource == "hotspots":
            if not q.filters.get("project"):
                raise ValueError("SonarQube hotspots query requires 'project' filter")
            return ConnectorResult(records=[{"key": "hotspot-1"}], total=1)
        raise ValueError(f"Unsupported SonarQube resource: {q.resource!r}")

    async def mock_write(payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource == "issue_comment":
            if not payload.data.get("issue") or not payload.data.get("text"):
                raise ValueError("SonarQube issue_comment write requires 'issue' and 'text' in data")
            return {"key": payload.data["issue"], "text": payload.data["text"]}
        if payload.resource == "issue_status":
            issue = payload.data.get("issue")
            transition = payload.data.get("transition")
            if not issue or not transition:
                raise ValueError("SonarQube issue_status write requires 'issue' and 'transition' in data")
            valid = {"confirm", "resolve", "reopen", "falsepositive", "wontfix"}
            if transition not in valid:
                raise ValueError(
                    f"Invalid SonarQube transition {transition!r}. Must be one of: {', '.join(sorted(valid))}"
                )
            return {"key": issue, "transition": transition}
        if payload.resource == "gate":
            if not payload.data.get("name"):
                raise ValueError("SonarQube gate write requires 'name' in data")
            return {"name": payload.data["name"]}
        raise ValueError(f"Unsupported SonarQube write resource: {payload.resource!r}")

    mock.health_check = mock_health_check
    mock.query = mock_query
    mock.write = mock_write
    return mock


@given("a SonarQube connector with valid token")
def given_valid_connector(ctx) -> None:
    ctx["connector"] = _build_connector()


@given("the SonarQube API returns unhealthy status")
def given_unhealthy(ctx) -> None:
    ctx["connector"] = _build_connector(unhealthy=True)


@when("I perform a health check")
def when_health_check(ctx) -> None:
    ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when(parsers.parse('I query resource "{resource}" with limit {limit:d}'))
def when_query_with_limit(ctx, resource, limit) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, limit=limit))


@when(parsers.parse('I query resource "{resource}" with project "{project}"'))
def when_query_with_project(ctx, resource, project) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"project": project}, limit=10))


@when(parsers.parse('I query resource "{resource}" with component "{component}" and metricKeys "{keys}"'))
def when_query_measures(ctx, resource, component, keys) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"component": component, "metricKeys": keys}))


@when(parsers.parse('I query resource "{resource}" with component "{component}"'))
def when_query_issues(ctx, resource, component) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"component": component}, limit=10))


@when(parsers.parse('I query resource "{resource}" with id "{gate_id}"'))
def when_query_quality_gate(ctx, resource, gate_id) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"id": gate_id}))


@when(parsers.parse('I query resource "{resource}" without project filter'))
def when_query_without_project(ctx, resource) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource))


@when(parsers.parse('I write SonarQube resource "{resource}" with issue "{issue}" and text "{text}"'))
def when_write_comment(ctx, resource, issue, text) -> None:
    _run_write(ctx, ConnectorPayload(resource=resource, data={"issue": issue, "text": text}))


@when(parsers.parse('I write SonarQube resource "{resource}" with issue "{issue}" and transition "{transition}"'))
def when_write_transition(ctx, resource, issue, transition) -> None:
    _run_write(ctx, ConnectorPayload(resource=resource, data={"issue": issue, "transition": transition}))


@when(parsers.parse('I write SonarQube resource "{resource}" with name "{name}"'))
def when_write_gate(ctx, resource, name) -> None:
    _run_write(ctx, ConnectorPayload(resource=resource, data={"name": name}))


def _run_query(ctx: dict[str, Any], q: ConnectorQuery) -> None:
    try:
        ctx["query_result"] = asyncio.run(ctx["connector"].query(q))
        ctx["error"] = None
    except ValueError as exc:
        ctx["error"] = exc
        ctx["query_result"] = None


def _run_write(ctx: dict[str, Any], payload: ConnectorPayload) -> None:
    try:
        ctx["write_result"] = asyncio.run(ctx["connector"].write(payload))
        ctx["error"] = None
    except ValueError as exc:
        ctx["error"] = exc
        ctx["write_result"] = None


@then("the health result is ok")
def then_health_ok(ctx) -> None:
    result = ctx.get("health_result")
    assert result is not None, "No health check result"
    assert result.ok is True


@then("the health result is not ok")
def then_health_not_ok(ctx) -> None:
    result = ctx.get("health_result")
    assert result is not None, "No health check result"
    assert result.ok is False


@then("the result has records")
def then_result_has_records(ctx) -> None:
    result = ctx.get("query_result")
    assert result is not None, "No query result"
    assert result.records, "Query result has no records"


@then("the write succeeds")
def then_write_succeeds(ctx) -> None:
    assert ctx.get("write_result") is not None, "Write result is None"


@then("the result is an error")
def then_result_is_error(ctx) -> None:
    assert ctx.get("error") is not None, "Expected an error but operation succeeded"
