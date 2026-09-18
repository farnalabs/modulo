"""Unit tests for BuildkiteConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import (
    CIRun,
    CIRunLog,
    CIRunStatus,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorType,
)
from modulo.connectors.buildkite import BuildkiteConnector

TOKEN = "bk_test_token"
_BASE = "https://api.buildkite.com/v2"
_ORG = "myorg"
_PIPELINE = "my-pipeline"


@pytest.fixture
def connector():
    return BuildkiteConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.BUILDKITE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/user").mock(return_value=httpx.Response(200, json={"id": "u1"}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid or expired token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/user").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/user").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — organizations / pipelines / builds / jobs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_organizations(connector):
    body = [{"id": "org1", "slug": _ORG}]
    respx.get(f"{_BASE}/organizations").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="organizations"))
    assert result.total == 1


@respx.mock
async def test_query_pipelines(connector):
    body = [{"id": "p1", "slug": _PIPELINE}]
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="pipelines", filters={"organization": _ORG}))
    assert result.total == 1


@respx.mock
async def test_query_builds(connector):
    body = [{"id": "b1", "state": "passed"}]
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(resource="builds", filters={"organization": _ORG, "pipeline": _PIPELINE}),
    )
    assert result.total == 1


@respx.mock
async def test_query_jobs(connector):
    body = [{"id": "j1", "name": "Test"}]
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds/1/jobs").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="jobs",
            filters={"organization": _ORG, "pipeline": _PIPELINE, "build": "1"},
        ),
    )
    assert result.total == 1


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — build
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(connector):
    created = {"id": "b2", "state": "scheduled"}
    respx.post(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="build",
            data={"organization": _ORG, "pipeline": _PIPELINE, "branch": "main"},
        ),
    )
    assert result["id"] == "b2"


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# CI runner methods
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(connector):
    created = {
        "id": "b3",
        "number": 3,
        "state": "running",
        "branch": "main",
        "commit": "abc123",
        "pipeline": {"slug": _PIPELINE},
        "creator": {"name": "alice"},
        "web_url": "https://buildkite.com/myorg/my-pipeline/builds/3",
    }
    respx.post(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds").mock(
        return_value=httpx.Response(200, json=created),
    )
    run = await connector.trigger_run(f"{_ORG}/{_PIPELINE}", branch="main")
    assert isinstance(run, CIRun)
    assert run.id == "3"
    assert run.pipeline_id == _PIPELINE
    assert run.status == CIRunStatus.IN_PROGRESS
    assert run.triggered_by == "alice"


async def test_trigger_run_invalid_pipeline_id(connector):
    with pytest.raises(ValueError, match="Invalid pipeline_id format"):
        await connector.trigger_run("missing-pipeline")


@respx.mock
async def test_get_run_status(connector):
    body = {
        "id": "b3",
        "number": 3,
        "state": "passed",
        "pipeline": {"slug": _PIPELINE},
    }
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds/3").mock(
        return_value=httpx.Response(200, json=body),
    )
    run = await connector.get_run_status(f"{_ORG}/{_PIPELINE}/3")
    assert run.id == "3"
    assert run.status == CIRunStatus.SUCCESS


async def test_get_run_status_invalid_run_id(connector):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await connector.get_run_status("3")


@respx.mock
async def test_get_run_logs(connector):
    jobs_body = [{"id": "j1", "name": "Test"}]
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds/3/jobs").mock(
        return_value=httpx.Response(200, json=jobs_body),
    )
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds/3/jobs/j1/log").mock(
        return_value=httpx.Response(200, text="line1\nline2"),
    )
    logs = await connector.get_run_logs(f"{_ORG}/{_PIPELINE}/3")
    assert isinstance(logs, CIRunLog)
    assert logs.run_id == f"{_ORG}/{_PIPELINE}/3"
    assert "line1" in "".join(logs.lines)


async def test_get_run_logs_invalid_run_id(connector):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await connector.get_run_logs("3")


@respx.mock
async def test_list_runs(connector):
    body = [
        {"id": "b1", "number": 1, "state": "passed", "pipeline": {"slug": _PIPELINE}},
        {"id": "b2", "number": 2, "state": "failed", "pipeline": {"slug": _PIPELINE}},
    ]
    respx.get(f"{_BASE}/organizations/{_ORG}/pipelines/{_PIPELINE}/builds").mock(
        return_value=httpx.Response(200, json=body),
    )
    runs = await connector.list_runs(pipeline_id=f"{_ORG}/{_PIPELINE}")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


async def test_list_runs_without_pipeline_id(connector):
    runs = await connector.list_runs()
    assert runs == []


@respx.mock
async def test_list_runs_invalid_pipeline_id(connector):
    with pytest.raises(ValueError, match="Invalid pipeline_id format"):
        await connector.list_runs(pipeline_id="no-pipeline")


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/organizations").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="organizations"))
