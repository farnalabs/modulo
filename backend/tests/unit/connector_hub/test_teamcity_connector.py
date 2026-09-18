"""Unit tests for TeamCityConnector — HTTP responses are mocked via httpx + respx."""

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
from modulo.connectors.teamcity import TeamCityConnector, _parse_teamcity_status

TOKEN = "tc_test_token"
_BASE = "http://localhost:8111"


@pytest.fixture
def connector():
    return TeamCityConnector(token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.TEAMCITY


# ---------------------------------------------------------------------------
# _parse_teamcity_status
# ---------------------------------------------------------------------------


def test_parse_status_mapping():
    assert _parse_teamcity_status("queued") == CIRunStatus.QUEUED
    assert _parse_teamcity_status("running") == CIRunStatus.IN_PROGRESS
    assert _parse_teamcity_status("finished", "SUCCESS") == CIRunStatus.SUCCESS
    assert _parse_teamcity_status("finished", "FAILURE") == CIRunStatus.FAILURE
    assert _parse_teamcity_status("finished", "ERROR") == CIRunStatus.FAILURE
    assert _parse_teamcity_status("finished", "CANCELLED") == CIRunStatus.UNKNOWN
    assert _parse_teamcity_status("something") == CIRunStatus.UNKNOWN


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/app/rest/server").mock(return_value=httpx.Response(200, json={}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/app/rest/server").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/app/rest/server").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/app/rest/server").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — projects
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    body = {"project": [{"id": "ProjectA", "name": "Project A"}]}
    respx.get(f"{_BASE}/app/rest/projects").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.total == 1
    assert result.records[0]["id"] == "ProjectA"


# ---------------------------------------------------------------------------
# query — buildTypes
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_build_types(connector):
    body = {"buildType": [{"id": "BT_1", "name": "Build One"}]}
    respx.get(f"{_BASE}/app/rest/buildTypes").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="buildTypes", filters={"project_id": "ProjectA"}))
    assert result.total == 1


@respx.mock
async def test_query_build_types_without_project(connector):
    body = {"buildType": [{"id": "BT_1"}]}
    respx.get(f"{_BASE}/app/rest/buildTypes").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="buildTypes"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — builds
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_builds(connector):
    body = {
        "build": [
            {"id": 1, "state": "finished", "status": "SUCCESS", "buildType": {"buildTypeId": "BT_1"}},
        ],
    }
    respx.get(f"{_BASE}/app/rest/builds").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="builds", filters={"buildTypeId": "BT_1"}, limit=20))
    assert result.total == 1
    assert result.records[0]["id"] == 1


# ---------------------------------------------------------------------------
# query — agents
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_agents(connector):
    body = {"agent": [{"id": 5, "name": "agent-1"}]}
    respx.get(f"{_BASE}/app/rest/agents").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="agents"))
    assert result.total == 1
    assert result.records[0]["name"] == "agent-1"


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — build
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(connector):
    created = {"id": 100, "buildTypeId": "MyBuild"}
    respx.post(f"{_BASE}/app/rest/buildQueue").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="build",
            data={"buildTypeId": "MyBuild", "branch": "main", "parameters": {"KEY": "value"}},
        ),
    )
    assert result["id"] == "100"
    assert result["buildTypeId"] == "MyBuild"


@respx.mock
async def test_write_build_minimal(connector):
    created = {"id": 101, "buildTypeId": "MyBuild"}
    respx.post(f"{_BASE}/app/rest/buildQueue").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="build", data={"buildTypeId": "MyBuild"}),
    )
    assert result["id"] == "101"


# ---------------------------------------------------------------------------
# write — buildType
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build_type(connector):
    created = {"id": "BT_New", "name": "New Build Type"}
    respx.post(f"{_BASE}/app/rest/buildTypes").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="buildType",
            data={"buildTypeId": "BT_New", "projectId": "ProjectA", "name": "New Build Type"},
        ),
    )
    assert result["id"] == "BT_New"


async def test_write_build_type_missing_fields(connector):
    with pytest.raises(ValueError, match="requires buildTypeId, projectId, and name"):
        await connector.write(ConnectorPayload(resource="buildType", data={"buildTypeId": "BT_New"}))


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
    created = {"id": 200, "href": "/app/rest/builds/id:200", "state": "queued"}
    respx.post(f"{_BASE}/app/rest/buildQueue").mock(return_value=httpx.Response(200, json=created))
    run = await connector.trigger_run("MyBuild", branch="feature", variables={"K": "V"})
    assert isinstance(run, CIRun)
    assert run.id == "200"
    assert run.pipeline_id == "MyBuild"
    assert run.status == CIRunStatus.QUEUED
    assert run.branch == "feature"
    assert run.url == f"{_BASE}/app/rest/builds/id:200"


@respx.mock
async def test_get_run_status(connector):
    body = {
        "id": 300,
        "state": "finished",
        "status": "SUCCESS",
        "buildType": {"buildTypeId": "MyBuild"},
        "branchName": "main",
        "href": "/app/rest/builds/id:300",
    }
    respx.get(f"{_BASE}/app/rest/builds/id:300").mock(return_value=httpx.Response(200, json=body))
    run = await connector.get_run_status("300")
    assert run.id == "300"
    assert run.status == CIRunStatus.SUCCESS
    assert run.pipeline_id == "MyBuild"
    assert run.branch == "main"


@respx.mock
async def test_get_run_logs(connector):
    respx.get(f"{_BASE}/app/rest/builds/id:300/text").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3"),
    )
    logs = await connector.get_run_logs("300")
    assert isinstance(logs, CIRunLog)
    assert logs.run_id == "300"
    assert logs.lines == ["line1", "line2", "line3"]
    assert logs.next_cursor == "3"


@respx.mock
async def test_get_run_logs_with_cursor(connector):
    respx.get(f"{_BASE}/app/rest/builds/id:300/text").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3"),
    )
    logs = await connector.get_run_logs("300", cursor="1")
    assert logs.lines == ["line2", "line3"]
    assert logs.next_cursor == "3"


@respx.mock
async def test_list_runs(connector):
    body = {
        "build": [
            {"id": 1, "state": "finished", "status": "SUCCESS", "buildType": {"buildTypeId": "MyBuild"}},
            {"id": 2, "state": "finished", "status": "FAILURE", "buildType": {"buildTypeId": "MyBuild"}},
        ],
    }
    respx.get(f"{_BASE}/app/rest/builds").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id="MyBuild")
    assert len(runs) == 2


@respx.mock
async def test_list_runs_filtered_by_status(connector):
    body = {
        "build": [
            {"id": 1, "state": "finished", "status": "SUCCESS", "buildType": {"buildTypeId": "MyBuild"}},
            {"id": 2, "state": "finished", "status": "FAILURE", "buildType": {"buildTypeId": "MyBuild"}},
        ],
    }
    respx.get(f"{_BASE}/app/rest/builds").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(status=CIRunStatus.FAILURE)
    assert len(runs) == 1
    assert runs[0].id == "2"


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/app/rest/projects").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="projects"))
