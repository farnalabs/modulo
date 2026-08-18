"""BDD step definitions for Code Climate connector scenarios."""

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, HealthResult

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/codeclimate.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for Code Climate connector tests."""
    return {}


def _build_connector(unhealthy: bool = False) -> AsyncMock:
    """Build a mock Code Climate connector mirroring the real connector's contract.

    ``query``/``write``/``health_check`` are async and raise ValueError for
    unsupported resources or missing required filters, matching
    ``CodeClimateConnector`` in ``src/modulo/connectors/codeclimate/``.
    """
    mock = AsyncMock()
    mock.connector_type = "codeclimate"

    async def mock_health_check() -> HealthResult:
        if unhealthy:
            return HealthResult(ok=False, detail="Invalid Code Climate auth token")
        return HealthResult(ok=True, detail="Code Climate API token validated")

    async def mock_query(q: ConnectorQuery) -> ConnectorResult:
        if q.resource == "repos":
            return ConnectorResult(records=[{"id": "repo-123", "attributes": {"name": "My Repo"}}], total=1)
        if q.resource == "repo":
            if not q.filters.get("id"):
                raise ValueError("Code Climate repo query requires 'id' in filters")
            return ConnectorResult(records=[{"id": q.filters["id"], "attributes": {"name": "My Repo"}}], total=1)
        if q.resource == "snapshots":
            if not q.filters.get("repo_id"):
                raise ValueError("Code Climate snapshots query requires 'repo_id' in filters")
            return ConnectorResult(records=[{"id": "ss-456", "type": "snapshots"}], total=1)
        if q.resource == "snapshot":
            if not q.filters.get("repo_id"):
                raise ValueError("Code Climate snapshot query requires 'repo_id' in filters")
            if not q.filters.get("id"):
                raise ValueError("Code Climate snapshot query requires 'id' in filters")
            return ConnectorResult(records=[{"id": q.filters["id"], "type": "snapshots"}], total=1)
        if q.resource == "test_reports":
            if not q.filters.get("repo_id"):
                raise ValueError("Code Climate test_reports query requires 'repo_id' in filters")
            return ConnectorResult(records=[{"id": "tr-789", "type": "test_reports"}], total=1)
        if q.resource == "test_report":
            if not q.filters.get("repo_id"):
                raise ValueError("Code Climate test_report query requires 'repo_id' in filters")
            if not q.filters.get("id"):
                raise ValueError("Code Climate test_report query requires 'id' in filters")
            return ConnectorResult(records=[{"id": q.filters["id"], "type": "test_reports"}], total=1)
        raise ValueError(f"Unsupported Code Climate resource: {q.resource!r}")

    async def mock_write(payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource == "test_report":
            if not payload.data.get("repo_id"):
                raise ValueError("Code Climate test_report write requires 'repo_id' in data")
            if payload.data.get("duration") is None:
                raise ValueError("Code Climate test_report write requires 'duration' in data")
            if payload.data.get("exit_code") is None:
                raise ValueError("Code Climate test_report write requires 'exit_code' in data")
            if not payload.data.get("commit_sha"):
                raise ValueError("Code Climate test_report write requires 'commit_sha' in data")
            return {"data": {"id": "tr-999", "type": "test_reports"}}
        raise ValueError(f"Unsupported Code Climate write resource: {payload.resource!r}")

    mock.health_check = mock_health_check
    mock.query = mock_query
    mock.write = mock_write
    return mock


@given("a Code Climate connector with valid token")
def given_valid_connector(ctx) -> None:
    ctx["connector"] = _build_connector()


@given("the Code Climate API returns unhealthy status")
def given_unhealthy(ctx) -> None:
    ctx["connector"] = _build_connector(unhealthy=True)


@when("I perform a health check")
def when_health_check(ctx) -> None:
    ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when(parsers.parse('I query resource "{resource}" with limit {limit:d}'))
def when_query_with_limit(ctx, resource, limit) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, limit=limit))


@when(parsers.parse('I query resource "{resource}" with github_slug "{github_slug}"'))
def when_query_with_github_slug(ctx, resource, github_slug) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"github_slug": github_slug}))


@when(parsers.parse('I query resource "{resource}" with id "{item_id}"'))
def when_query_with_id(ctx, resource, item_id) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"id": item_id}))


@when(parsers.parse('I query resource "{resource}" with repo_id "{repo_id}"'))
def when_query_with_repo_id(ctx, resource, repo_id) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"repo_id": repo_id}))


@when(parsers.parse('I query resource "{resource}" with repo_id "{repo_id}" and snapshot_id "{snapshot_id}"'))
def when_query_snapshot(ctx, resource, repo_id, snapshot_id) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"repo_id": repo_id, "id": snapshot_id}))


@when(parsers.parse('I query resource "{resource}" with repo_id "{repo_id}" and report_id "{report_id}"'))
def when_query_test_report(ctx, resource, repo_id, report_id) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"repo_id": repo_id, "id": report_id}))


@when(
    parsers.parse(
        'I write a test report for repo "{repo_id}" duration {duration:d} '
        'exit_code {exit_code:d} branch "{branch}" sha "{commit_sha}"'
    )
)
def when_write_test_report(ctx, repo_id, duration, exit_code, branch, commit_sha) -> None:
    _run_write(
        ctx,
        ConnectorPayload(
            resource="test_report",
            data={
                "repo_id": repo_id,
                "duration": duration,
                "exit_code": exit_code,
                "branch": branch,
                "commit_sha": commit_sha,
            },
        ),
    )


@when(parsers.parse('I query resource "{resource}" without id filter'))
def when_query_without_id(ctx, resource) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource))


@when(parsers.parse('I query resource "{resource}" without repo_id filter'))
def when_query_without_repo_id(ctx, resource) -> None:
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
