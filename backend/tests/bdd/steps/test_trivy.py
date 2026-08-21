"""BDD step definitions for Trivy connector scenarios."""

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, HealthResult

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/trivy.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for Trivy connector tests."""
    return {}


def _build_connector(unreachable: bool = False) -> AsyncMock:
    """Build a mock Trivy connector that mirrors the real connector's contract.

    ``query``/``write``/``health_check`` are async and raise ValueError for
    unsupported resources or missing required filters, matching
    ``TrivyConnector`` in ``src/modulo/connectors/trivy/``.
    """
    mock = AsyncMock()
    mock.connector_type = "trivy"

    async def mock_health_check() -> HealthResult:
        if unreachable:
            return HealthResult(ok=False, detail="Cannot connect to Trivy server")
        return HealthResult(ok=True, detail="Trivy server is healthy")

    async def mock_query(q: ConnectorQuery) -> ConnectorResult:
        if q.resource == "artifact":
            if not any(k in q.filters for k in ("image", "filesystem", "repository")):
                raise ValueError(
                    "Trivy artifact query requires one of 'image', 'filesystem', or 'repository' in filters"
                )
            return ConnectorResult(records=[{"ArtifactName": "scan"}], total=1)
        if q.resource == "reports":
            return ConnectorResult(records=[{"ArtifactName": "alpine:3.18"}], total=1)
        if q.resource == "report":
            if not q.filters.get("digest"):
                raise ValueError("Trivy report query requires 'digest' in filters")
            return ConnectorResult(records=[{"ArtifactDigest": q.filters["digest"]}], total=1)
        if q.resource == "status":
            return ConnectorResult(records=[{"status": "ok"}], total=1)
        if q.resource == "plugins":
            return ConnectorResult(records=[{"Name": "nodejs"}], total=1)
        raise ValueError(f"Unsupported Trivy resource: {q.resource!r}")

    async def mock_write(payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource == "scan":
            if not any(k in payload.data for k in ("image", "filesystem", "repository")):
                raise ValueError("Trivy scan write requires one of 'image', 'filesystem', or 'repository' in data")
            return {"ArtifactName": "alpine:3.18"}
        raise ValueError(f"Unsupported Trivy write resource: {payload.resource!r}")

    mock.health_check = mock_health_check
    mock.query = mock_query
    mock.write = mock_write
    return mock


@given("a Trivy connector")
def given_valid_connector(ctx) -> None:
    ctx["connector"] = _build_connector()


@given("the Trivy server is unreachable")
def given_unreachable(ctx) -> None:
    ctx["connector"] = _build_connector(unreachable=True)


@when("I perform a health check")
def when_health_check(ctx) -> None:
    ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when(parsers.parse('I query Trivy resource "{resource}" with image "{image}"'))
def when_query_artifact_image(ctx, resource, image) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"image": image}, limit=10))


@when(parsers.parse('I query Trivy resource "{resource}" with filesystem "{fs}"'))
def when_query_artifact_filesystem(ctx, resource, fs) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"filesystem": fs}, limit=10))


@when(parsers.parse('I query Trivy resource "{resource}" with repository "{repo}"'))
def when_query_artifact_repo(ctx, resource, repo) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, filters={"repository": repo}, limit=10))


@when(parsers.parse('I query Trivy resource "{resource}" without target'))
def when_query_artifact_no_target(ctx, resource) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource))


@when(parsers.parse('I query Trivy resource "{resource}" with limit {limit:d}'))
def when_query_reports(ctx, resource, limit) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource, limit=limit))


@when(parsers.parse('I query Trivy resource "report" with digest "{digest}"'))
def when_query_report_digest(ctx, digest) -> None:
    _run_query(ctx, ConnectorQuery(resource="report", filters={"digest": digest}))


@when(parsers.parse('I query Trivy resource "report" without digest'))
def when_query_report_no_digest(ctx) -> None:
    _run_query(ctx, ConnectorQuery(resource="report"))


@when(parsers.re(r'I query Trivy resource "(?P<resource>[^"]+)"$'))
def when_query_generic(ctx, resource) -> None:
    _run_query(ctx, ConnectorQuery(resource=resource))


@when(parsers.parse('I write Trivy resource "scan" with image "{image}"'))
def when_write_scan_image(ctx, image) -> None:
    _run_write(ctx, ConnectorPayload(resource="scan", data={"image": image}))


@when(parsers.parse('I write Trivy resource "scan" without target'))
def when_write_scan_no_target(ctx) -> None:
    _run_write(ctx, ConnectorPayload(resource="scan", data={}))


@when(parsers.re(r'I write Trivy resource "(?P<resource>[^"]+)"$'))
def when_write_invalid(ctx, resource) -> None:
    _run_write(ctx, ConnectorPayload(resource=resource, data={}))


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
