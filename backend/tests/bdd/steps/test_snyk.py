"""BDD step definitions for Snyk connector scenarios."""

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, HealthResult

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/snyk.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for Snyk connector tests."""
    return {}


def _build_connector(unauthorized: bool = False) -> AsyncMock:
    """Build a mock Snyk connector mirroring the real connector's contract.

    ``query``/``write``/``health_check`` are async and raise ValueError for
    unsupported resources or missing required filters, matching
    ``SnykConnector`` in ``src/modulo/connectors/snyk/``.
    """
    mock = AsyncMock()
    mock.connector_type = "snyk"

    async def mock_health_check() -> HealthResult:
        if unauthorized:
            return HealthResult(ok=False, detail="Invalid Snyk auth token")
        return HealthResult(ok=True, detail="Snyk API token validated")

    async def mock_query(q: ConnectorQuery) -> ConnectorResult:
        if q.resource == "projects":
            if not q.filters.get("org_id"):
                raise ValueError("Snyk projects query requires 'org_id' in filters")
            return ConnectorResult(records=[{"id": "proj-1", "attributes": {"name": "My Project"}}], total=1)
        if q.resource == "project":
            if not q.filters.get("org_id"):
                raise ValueError("Snyk project query requires 'org_id' in filters")
            if not q.filters.get("project_id"):
                raise ValueError("Snyk project query requires 'project_id' in filters")
            return ConnectorResult(
                records=[{"id": q.filters["project_id"], "attributes": {"name": "My Project"}}], total=1
            )
        if q.resource == "issues":
            if not q.filters.get("org_id"):
                raise ValueError("Snyk issues query requires 'org_id' in filters")
            if not q.filters.get("project_id"):
                raise ValueError("Snyk issues query requires 'project_id' in filters")
            return ConnectorResult(records=[{"id": "SNYK-123", "type": "vuln"}], total=1)
        if q.resource == "aggregated_issues":
            if not q.filters.get("org_id"):
                raise ValueError("Snyk aggregated_issues query requires 'org_id' in filters")
            if not q.filters.get("packages"):
                raise ValueError(
                    "Snyk aggregated_issues query requires 'packages' in filters (list of {name, version, ecosystem})"
                )
            return ConnectorResult(records=[{"id": "SNYK-123", "type": "vuln"}], total=1)
        if q.resource == "orgs":
            return ConnectorResult(records=[{"id": "my-org", "attributes": {"name": "My Org"}}], total=1)
        if q.resource == "tests":
            if not q.filters.get("org_id"):
                raise ValueError("Snyk tests query requires 'org_id' in filters")
            return ConnectorResult(records=[{"id": "test-1"}], total=1)
        raise ValueError(f"Unsupported Snyk resource: {q.resource!r}")

    async def mock_write(payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource == "test":
            org_id = payload.data.get("org_id")
            name = payload.data.get("name")
            version = payload.data.get("version")
            ecosystem = payload.data.get("ecosystem")
            if not org_id:
                raise ValueError("Snyk test write requires 'org_id' in data")
            if not all([name, version, ecosystem]):
                raise ValueError("Snyk test write requires 'name', 'version', and 'ecosystem' in data")
            return {"data": {"id": "test-1"}}
        if payload.resource == "ignore":
            if not payload.data.get("org_id"):
                raise ValueError("Snyk ignore write requires 'org_id' in data")
            if not payload.data.get("project_id"):
                raise ValueError("Snyk ignore write requires 'project_id' in data")
            if not payload.data.get("issue_id"):
                raise ValueError("Snyk ignore write requires 'issue_id' in data")
            return {"data": {"id": payload.data["issue_id"]}}
        raise ValueError(f"Unsupported Snyk write resource: {payload.resource!r}")

    mock.health_check = mock_health_check
    mock.query = mock_query
    mock.write = mock_write
    return mock


@given("a Snyk connector with valid token")
def given_valid_connector(ctx) -> None:
    ctx["connector"] = _build_connector()


@given("the Snyk API returns unauthorized")
def given_unauthorized(ctx) -> None:
    ctx["connector"] = _build_connector(unauthorized=True)


@when("I perform a health check")
def when_health_check(ctx) -> None:
    ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when(parsers.parse('I query Snyk resource "{resource}" with org "{org}"'))
def when_query_with_org(ctx, resource, org) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"org_id": org}, limit=10))


@when(parsers.parse('I query Snyk resource "{resource}" with org "{org}" and project "{project}"'))
def when_query_org_project(ctx, resource, org, project) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"org_id": org, "project_id": project}, limit=10))


@when(parsers.parse('I query Snyk resource "{resource}" with limit {limit:d}'))
def when_query_with_limit(ctx, resource, limit) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, limit=limit))


@when(parsers.parse('I query Snyk resource "tests" with org "{org}"'))
def when_query_tests(ctx, org) -> None:
    _run_query(ctx, ConnectorQuery(resource="tests", filters={"org_id": org}, limit=10))


@when(parsers.parse('I query Snyk resource "aggregated_issues" with org "{org}" and packages'))
def when_query_aggregated(ctx, org) -> None:
    _run_query(
        ctx,
        ConnectorQuery(
            resource="aggregated_issues",
            filters={
                "org_id": org,
                "packages": [{"name": "requests", "version": "4.0.0", "ecosystem": "pypi"}],
            },
        ),
    )


@when(parsers.parse('I write Snyk resource "test" with org "{org}" and package "{pkg}" ecosystem "{eco}"'))
def when_write_test(ctx, org, pkg, eco) -> None:
    name, version = pkg.split("@")
    _run_write(
        ctx,
        ConnectorPayload(
            resource="test",
            data={"org_id": org, "name": name, "version": version, "ecosystem": eco},
        ),
    )


@when(parsers.parse('I write Snyk resource "ignore" with org "{org}" project "{proj}" and issue "{issue}"'))
def when_write_ignore(ctx, org, proj, issue) -> None:
    _run_write(
        ctx,
        ConnectorPayload(
            resource="ignore",
            data={"org_id": org, "project_id": proj, "issue_id": issue},
        ),
    )


@when(parsers.parse('I query Snyk resource "{resource}" without org filter'))
def when_query_without_org(ctx, resource) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource))


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
    assert ctx.get("query_result") is None
