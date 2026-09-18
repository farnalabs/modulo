"""Unit tests for JenkinsConnector — HTTP responses are mocked via httpx + respx."""

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
from modulo.connectors.jenkins import JenkinsConnector

USERNAME = "jenkins_user"
TOKEN = "jenkins_test_token"
_BASE = "http://jenkins.example.com"
_JOB = "build-job"


@pytest.fixture
def connector():
    return JenkinsConnector(username=USERNAME, token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.JENKINS


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/api/json").mock(return_value=httpx.Response(200, json={"nodeName": "master"}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/api/json").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid username or token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/api/json").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/api/json").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — jobs / builds / nodes
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_jobs(connector):
    body = {"jobs": [{"name": _JOB, "color": "blue"}]}
    respx.get(f"{_BASE}/api/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="jobs"))
    assert result.total == 1
    assert result.records[0]["name"] == _JOB


@respx.mock
async def test_query_builds(connector):
    body = {"builds": [{"number": 1, "result": "SUCCESS"}]}
    respx.get(f"{_BASE}/job/{_JOB}/api/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="builds", filters={"job_name": _JOB}))
    assert result.total == 1


@respx.mock
async def test_query_nodes(connector):
    body = {"computer": [{"displayName": "agent-1", "offline": False}]}
    respx.get(f"{_BASE}/computer/api/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="nodes"))
    assert result.total == 1


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — build
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(connector):
    respx.get(f"{_BASE}/crumbIssuer/api/json").mock(
        return_value=httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"}),
    )
    respx.post(f"{_BASE}/job/{_JOB}/build").mock(
        return_value=httpx.Response(
            201,
            headers={"Location": "http://jenkins.example.com/queue/item/42"},
        ),
    )
    result = await connector.write(ConnectorPayload(resource="build", data={"job_name": _JOB}))
    assert result["job_name"] == _JOB
    assert "42" in result["location"]


@respx.mock
async def test_write_build_with_parameters(connector):
    respx.get(f"{_BASE}/crumbIssuer/api/json").mock(return_value=httpx.Response(404, text="Not found"))
    respx.post(f"{_BASE}/job/{_JOB}/buildWithParameters").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/queue/item/43"}),
    )
    result = await connector.write(
        ConnectorPayload(resource="build", data={"job_name": _JOB, "parameters": {"ENV": "prod"}}),
    )
    assert "43" in result["location"]


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# CI runner methods
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(connector):
    respx.get(f"{_BASE}/crumbIssuer/api/json").mock(
        return_value=httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"}),
    )
    respx.post(f"{_BASE}/job/{_JOB}/build").mock(
        return_value=httpx.Response(
            201,
            headers={"Location": "http://jenkins.example.com/queue/item/7"},
        ),
    )
    run = await connector.trigger_run(_JOB)
    assert isinstance(run, CIRun)
    assert run.id == "7"
    assert run.pipeline_id == _JOB
    assert run.status == CIRunStatus.QUEUED


@respx.mock
async def test_get_run_status(connector):
    body = {"id": "5", "number": 5, "result": "SUCCESS", "fullDisplayName": f"{_JOB} #5"}
    respx.get(f"{_BASE}/job/{_JOB}/5/api/json").mock(return_value=httpx.Response(200, json=body))
    run = await connector.get_run_status(f"{_JOB}/5")
    assert run.id == "5"
    assert run.pipeline_id == f"{_JOB} #5"
    assert run.status == CIRunStatus.SUCCESS


@respx.mock
async def test_get_run_logs(connector):
    respx.get(f"{_BASE}/job/{_JOB}/5/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3"),
    )
    logs = await connector.get_run_logs(f"{_JOB}/5")
    assert isinstance(logs, CIRunLog)
    assert logs.run_id == f"{_JOB}/5"
    assert len(logs.lines) == 3


@respx.mock
async def test_get_run_logs_with_cursor(connector):
    respx.get(f"{_BASE}/job/{_JOB}/5/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3"),
    )
    logs = await connector.get_run_logs(f"{_JOB}/5", cursor="1")
    assert len(logs.lines) == 2
    assert logs.next_cursor == "3"


@respx.mock
async def test_list_runs(connector):
    body = {
        "builds": [
            {"id": "1", "number": 1, "result": "SUCCESS"},
            {"id": "2", "number": 2, "result": "FAILURE"},
        ],
    }
    respx.get(f"{_BASE}/job/{_JOB}/api/json").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id=_JOB)
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


@respx.mock
async def test_list_runs_filtered_by_status(connector):
    body = {
        "builds": [
            {"id": "1", "number": 1, "result": "SUCCESS"},
            {"id": "2", "number": 2, "result": "FAILURE"},
        ],
    }
    respx.get(f"{_BASE}/job/{_JOB}/api/json").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id=_JOB, status=CIRunStatus.SUCCESS)
    assert len(runs) == 1
    assert runs[0].id == "1"


@respx.mock
async def test_list_runs_duration_seconds(connector):
    body = {
        "builds": [
            {"id": "1", "number": 1, "result": "SUCCESS", "duration": 12000},
        ],
    }
    respx.get(f"{_BASE}/job/{_JOB}/api/json").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id=_JOB)
    assert runs[0].duration_seconds == 12


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/api/json").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="jobs"))
