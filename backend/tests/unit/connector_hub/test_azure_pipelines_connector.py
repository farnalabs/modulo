"""Unit tests for AzurePipelinesConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.base import (
    CIRun,
    CIRunLog,
    CIRunStatus,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorType,
)

TOKEN = "apt_test_token"
_ORG = "myorg"
_PROJECT = "myproject"
_BASE = "https://dev.azure.com"
_PIPELINES = f"{_BASE}/{_ORG}/{_PROJECT}/_apis/pipelines"


@pytest.fixture
def connector():
    return AzurePipelinesConnector(token=TOKEN, organization=_ORG, project=_PROJECT)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.AZURE_PIPELINES


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/{_ORG}/_apis/projects").mock(return_value=httpx.Response(200, json={"value": []}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/{_ORG}/_apis/projects").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid or expired PAT" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/{_ORG}/_apis/projects").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/{_ORG}/_apis/projects").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — projects / pipelines
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    body = {"value": [{"id": "proj-guid", "name": "My Project"}], "count": 1}
    respx.get(f"{_BASE}/{_ORG}/_apis/projects").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.total == 1
    assert result.records[0]["id"] == "proj-guid"


@respx.mock
async def test_query_pipelines(connector):
    body = {"value": [{"id": 1, "name": "CI"}], "count": 1}
    respx.get(f"{_PIPELINES}").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="pipelines"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — runs / releases
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_runs(connector):
    body = {"value": [{"id": 100}], "count": 1}
    respx.get(f"{_PIPELINES}/1/runs").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="runs", filters={"pipeline_id": "1"}))
    assert result.total == 1


async def test_query_runs_missing_pipeline_id(connector):
    with pytest.raises(ValueError, match="'pipeline_id' filter"):
        await connector.query(ConnectorQuery(resource="runs"))


@respx.mock
async def test_query_releases(connector):
    body = {"value": [{"id": 200}], "count": 1}
    respx.get(f"{_BASE}/{_ORG}/{_PROJECT}/_apis/release/releases").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(ConnectorQuery(resource="releases"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — run
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_run(connector):
    created = {"id": 300, "state": "notStarted"}
    respx.post(f"{_PIPELINES}/1/runs").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="run",
            data={"pipeline_id": "1", "branch": "main", "variables": {"ENV": "prod"}},
        ),
    )
    assert result["id"] == 300


@respx.mock
async def test_write_run_without_variables(connector):
    created = {"id": 301, "state": "notStarted"}
    respx.post(f"{_PIPELINES}/1/runs").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="run", data={"pipeline_id": "1"}),
    )
    assert result["id"] == 301


# ---------------------------------------------------------------------------
# write — release
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_release(connector):
    created = {"id": 400, "name": "Release-1"}
    respx.post(f"{_BASE}/{_ORG}/{_PROJECT}/_apis/release/releases").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(resource="release", data={"definition_id": "1", "description": "v1.0"}),
    )
    assert result["id"] == 400


@respx.mock
async def test_write_release_minimal(connector):
    created = {"id": 401, "name": "Release-2"}
    respx.post(f"{_BASE}/{_ORG}/{_PROJECT}/_apis/release/releases").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(ConnectorPayload(resource="release", data={"definition_id": "1"}))
    assert result["id"] == 401


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# CI runner methods
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(connector):
    created = {
        "id": 500,
        "state": "inProgress",
        "pipeline": {"id": 1},
        "_links": {"web": {"href": "https://dev.azure.com/myorg/myproject/_build/results?buildId=500"}},
        "resources": {"repositories": {"self": {"refName": "refs/heads/main", "version": "abc123"}}},
        "templateParameters": {"triggeredBy": "agent"},
    }
    respx.post(f"{_PIPELINES}/1/runs").mock(return_value=httpx.Response(200, json=created))
    run = await connector.trigger_run("1", branch="main", variables={"K": "V"})
    assert isinstance(run, CIRun)
    assert run.id == "500"
    assert run.pipeline_id == "1"
    assert run.status == CIRunStatus.IN_PROGRESS
    assert run.branch == "main"
    assert run.commit_sha == "abc123"
    assert run.triggered_by == "agent"


@respx.mock
async def test_get_run_status(connector):
    body = {
        "id": 500,
        "state": "completed",
        "result": "succeeded",
        "pipeline": {"id": 1},
    }
    respx.get(f"{_PIPELINES}/1/runs/500").mock(return_value=httpx.Response(200, json=body))
    run = await connector.get_run_status("1/500")
    assert run.id == "500"
    assert run.status == CIRunStatus.SUCCESS


async def test_get_run_status_invalid_run_id(connector):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await connector.get_run_status("500")


@respx.mock
async def test_get_run_status_failed(connector):
    body = {"id": 500, "state": "completed", "result": "failed", "pipeline": {"id": 1}}
    respx.get(f"{_PIPELINES}/1/runs/500").mock(return_value=httpx.Response(200, json=body))
    run = await connector.get_run_status("1/500")
    assert run.status == CIRunStatus.FAILURE


@respx.mock
async def test_get_run_logs(connector):
    logs_body = {
        "value": [
            {"id": 1, "name": "Job 1", "url": "https://dev.azure.com/logs/1"},
        ],
    }
    respx.get(f"{_PIPELINES}/1/runs/500/logs").mock(return_value=httpx.Response(200, json=logs_body))
    respx.get("https://dev.azure.com/logs/1").mock(
        return_value=httpx.Response(200, text="line1\nline2"),
    )
    logs = await connector.get_run_logs("1/500")
    assert isinstance(logs, CIRunLog)
    assert logs.run_id == "1/500"
    assert "line1" in "".join(logs.lines)
    assert logs.next_cursor is None


@respx.mock
async def test_get_run_logs_with_cursor(connector):
    logs_body = {
        "value": [
            {"id": 1, "name": "Job 1", "url": "https://dev.azure.com/logs/1"},
        ],
    }
    respx.get(f"{_PIPELINES}/1/runs/500/logs").mock(return_value=httpx.Response(200, json=logs_body))
    respx.get("https://dev.azure.com/logs/1").mock(
        return_value=httpx.Response(200, text="line1\nline2"),
    )
    logs = await connector.get_run_logs("1/500", cursor="0")
    assert logs.next_cursor is not None


async def test_get_run_logs_invalid_run_id(connector):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await connector.get_run_logs("500")


@respx.mock
async def test_list_runs(connector):
    body = {
        "value": [
            {"id": 1, "state": "completed", "result": "succeeded", "pipeline": {"id": 1}},
            {"id": 2, "state": "completed", "result": "failed", "pipeline": {"id": 1}},
        ],
    }
    respx.get(f"{_PIPELINES}/1/runs").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id="1")
    assert len(runs) == 2


@respx.mock
async def test_list_runs_filtered_by_status(connector):
    body = {
        "value": [
            {"id": 1, "state": "completed", "result": "succeeded", "pipeline": {"id": 1}},
            {"id": 2, "state": "completed", "result": "failed", "pipeline": {"id": 1}},
        ],
    }
    respx.get(f"{_PIPELINES}/1/runs").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id="1", status=CIRunStatus.FAILURE)
    assert len(runs) == 1
    assert runs[0].id == "2"


async def test_list_runs_without_pipeline_id(connector):
    runs = await connector.list_runs()
    assert runs == []


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/{_ORG}/_apis/projects").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="projects"))
