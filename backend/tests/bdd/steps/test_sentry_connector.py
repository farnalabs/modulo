"""Step definitions for the Sentry connector BDD feature.

Wires ``features/connectors/sentry.feature`` into the executing BDD
suite by driving the REAL
``SentryConnector`` against a respx-mocked Sentry API — mirroring
``backend/tests/unit/connectors/test_sentry.py`` so the executing BDD surface
locks the same contract the unit suite does: token validation via ``/``,
list issues/projects, issue-status update and release creation.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/sentry.feature")

TOKEN = "sntry_test_token"
ORG = "test-org"
_BASE = "https://sentry.io/api/0"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Sentry connector scenarios."""
    return {}


@given("a Sentry connector configured with valid credentials")
def sentry_connector_valid(ctx: dict) -> None:
    from modulo.connectors.sentry import SentryConnector

    ctx["connector"] = SentryConnector(token=TOKEN, organization=ORG)
    ctx["valid"] = True


@given("a Sentry connector configured with invalid credentials")
def sentry_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.sentry import SentryConnector

    ctx["connector"] = SentryConnector(token=TOKEN, organization=ORG)
    ctx["valid"] = False


@when("the connector checks health")
def sentry_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    response = httpx.Response(200, json={}) if ctx["valid"] else httpx.Response(401, text="Unauthorized")
    with respx.mock:
        respx.get(f"{_BASE}/").mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@when("the connector queries issues")
def sentry_query_issues(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    issues = [
        {"id": "1", "title": "Crash in login", "status": "unresolved"},
        {"id": "2", "title": "Memory leak", "status": "resolved"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/projects/{ORG}/project-alpha/issues/").mock(return_value=httpx.Response(200, json=issues))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="issues", filters={"project": "project-alpha"}))
        )


@when("the connector queries projects")
def sentry_query_projects(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    projects = [
        {"id": "p1", "slug": "project-alpha"},
        {"id": "p2", "slug": "project-beta"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/projects/").mock(return_value=httpx.Response(200, json=projects))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="projects")))


@when(parsers.parse('the connector updates issue status to "{status}"'))
def sentry_write_issue_status(status: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.put(f"{_BASE}/issues/42/").mock(return_value=httpx.Response(200, json={"id": "42", "status": status}))
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="issue_status", data={"issue_id": "42", "status": status}))
        )


@when(parsers.parse('the connector creates a release with version "{version}"'))
def sentry_write_release(version: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/organizations/{ORG}/releases/").mock(
            return_value=httpx.Response(201, json={"id": "r1", "version": version})
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="release", data={"version": version}))
        )


@then(parsers.parse('the health check returns "{status}"'))
def sentry_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("the result contains Sentry issues")
def sentry_result_contains_issues(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected issue records"
    assert result.records[0]["title"] == "Crash in login", result.records[0]


@then("the result contains Sentry projects")
def sentry_result_contains_projects(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected project records"
    assert result.records[0]["slug"] == "project-alpha", result.records[0]


@then("the issue status is updated")
def sentry_issue_status_updated(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["status"] == "resolved", result


@then("the release is created successfully")
def sentry_release_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["version"] == "1.0.0", result
