"""Step definitions for the Azure Pipelines connector BDD feature.

Wires ``features/connectors/azure_pipelines.feature`` into the executing
BDD suite by driving the REAL
``AzurePipelinesConnector`` against a respx-mocked Azure DevOps REST API v7.0
— mirroring ``backend/tests/unit/connectors/test_azure_pipelines.py`` so the
executing BDD surface locks the same contract the unit suite does: listing
projects / pipelines / runs / releases, triggering a pipeline run or a
release, and failing closed on an unsupported query resource.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/azure_pipelines.feature")

_API = "https://dev.azure.com"
_API_VERSION = "7.0"
ORG = "myorg"
PROJECT = "myproject"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Azure Pipelines connector scenarios."""
    return {}


@given("an Azure Pipelines connector with valid credentials")
def azp_connector(ctx: dict) -> None:
    from modulo.connectors.azure_pipelines import AzurePipelinesConnector

    ctx["connector"] = AzurePipelinesConnector(token="apt_test", organization=ORG, project=PROJECT)


@when('I query resource "projects"')
def azp_query_projects(ctx: dict) -> None:
    _azp_query(
        ctx,
        "projects",
        url=f"{_API}/{ORG}/_apis/projects",
        body={"value": [{"id": "proj-1", "name": "Project 1"}], "count": 1},
    )


@when('I query resource "pipelines"')
def azp_query_pipelines(ctx: dict) -> None:
    _azp_query(
        ctx,
        "pipelines",
        url=f"{_API}/{ORG}/{PROJECT}/_apis/pipelines",
        body={"value": [{"id": 1, "name": "CI Pipeline"}], "count": 1},
    )


@when('I query resource "releases"')
def azp_query_releases(ctx: dict) -> None:
    _azp_query(
        ctx,
        "releases",
        url=f"{_API}/{ORG}/{PROJECT}/_apis/release/releases",
        body={"value": [{"id": 1, "name": "Release 1"}], "count": 1},
    )


@when(parsers.parse('I query resource "runs" with pipeline_id "{pipeline_id}"'))
def azp_query_runs(ctx: dict, pipeline_id: str) -> None:
    _azp_query(
        ctx,
        "runs",
        url=f"{_API}/{ORG}/{PROJECT}/_apis/pipelines/{pipeline_id}/runs",
        body={"value": [{"id": 1, "state": "inProgress"}], "count": 1},
        filters={"pipeline_id": pipeline_id},
    )


@when(parsers.parse('I write resource "run" with pipeline_id "{pipeline_id}" and branch "{branch}"'))
def azp_write_run(ctx: dict, pipeline_id: str, branch: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    connector = ctx["connector"]
    body = {
        "id": 101,
        "pipeline": {"id": int(pipeline_id)},
        "state": "inProgress",
        "createdDate": "2026-01-01T00:00:00Z",
    }
    with respx.mock:
        respx.post(
            f"{_API}/{ORG}/{PROJECT}/_apis/pipelines/{pipeline_id}/runs",
            params={"api-version": _API_VERSION},
        ).mock(return_value=httpx.Response(200, json=body))
        result = asyncio.run(
            connector.write(ConnectorPayload(resource="run", data={"pipeline_id": pipeline_id, "branch": branch}))
        )
    ctx["write_result"] = result
    assert result["state"] == "inProgress", result


@when(parsers.parse('I write resource "release" with definition_id "{definition_id}"'))
def azp_write_release(ctx: dict, definition_id: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    connector = ctx["connector"]
    body = {"id": 1, "name": "Release 1"}
    with respx.mock:
        respx.post(
            f"{_API}/{ORG}/{PROJECT}/_apis/release/releases",
            params={"api-version": _API_VERSION},
        ).mock(return_value=httpx.Response(200, json=body))
        result = asyncio.run(
            connector.write(ConnectorPayload(resource="release", data={"definition_id": definition_id}))
        )
    ctx["write_result"] = result
    assert result["name"] == "Release 1", result


@when('I query resource "invalid"')
def azp_query_invalid(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    with respx.mock, pytest.raises(ValueError, match="Unsupported query resource"):
        asyncio.run(connector.query(ConnectorQuery(resource="invalid")))
    ctx["error"] = True


@then("the result has records")
def azp_records_present(ctx: dict) -> None:
    assert ctx["records"], "expected records, got none"


@then("the records contain project metadata")
def azp_records_projects(ctx: dict) -> None:
    assert ctx["records"][0]["id"] == "proj-1", ctx["records"]


@then("the records contain pipeline metadata")
def azp_records_pipelines(ctx: dict) -> None:
    assert ctx["records"][0]["name"] == "CI Pipeline", ctx["records"]


@then("the write succeeds")
def azp_write_succeeds(ctx: dict) -> None:
    assert ctx["write_result"] is not None, "No write result"


@then("the result is an error")
def azp_is_error(ctx: dict) -> None:
    assert ctx["error"] is not None, "Expected an error result"


def _azp_query(ctx: dict, resource: str, url: str, body: dict, filters: dict | None = None) -> None:
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    with respx.mock:
        respx.get(url, params={"api-version": _API_VERSION}).mock(return_value=httpx.Response(200, json=body))
        result = asyncio.run(connector.query(ConnectorQuery(resource=resource, filters=filters)))
    ctx["records"] = result.records
    assert ctx["records"], f"no records returned for resource {resource!r}"
