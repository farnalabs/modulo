"""Unit tests for the TeamCity connector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import CIRunStatus, ConnectorPayload, ConnectorQuery
from modulo.connectors.teamcity import TeamCityConnector, _TeamCityTestDouble

_TC_BASE = "http://teamcity.example.com"


@pytest.fixture
def teamcity():
    return TeamCityConnector(token="secret", base_url=_TC_BASE)


@pytest.fixture
def teamcity_double():
    return _TeamCityTestDouble()


def test_connector_type(teamcity):
    assert teamcity.connector_type.value == "teamcity"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/server").mock(return_value=httpx.Response(200, json={"version": "2024.07"}))
    result = await teamcity.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail_401(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/server").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await teamcity.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


@respx.mock
async def test_health_check_fail_500(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/server").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    result = await teamcity.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# trigger_run
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(teamcity):
    route = respx.post(f"{_TC_BASE}/app/rest/buildQueue").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "buildTypeId": "MyBuild",
                "href": "/app/rest/builds/id:42",
                "state": "queued",
            },
        )
    )
    run = await teamcity.trigger_run(pipeline_id="MyBuild")
    assert run.pipeline_id == "MyBuild"
    assert run.status == CIRunStatus.QUEUED
    assert run.id == "42"
    assert route.called


@respx.mock
async def test_trigger_run_null_id_maps_to_empty_string(teamcity):
    respx.post(f"{_TC_BASE}/app/rest/buildQueue").mock(
        return_value=httpx.Response(200, json={"id": None, "buildTypeId": "MyBuild", "href": ""})
    )
    run = await teamcity.trigger_run(pipeline_id="MyBuild")
    assert not run.id
    assert "None" not in run.id


@respx.mock
async def test_trigger_run_with_branch(teamcity):
    route = respx.post(f"{_TC_BASE}/app/rest/buildQueue").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 43,
                "buildTypeId": "MyBuild",
                "href": "/app/rest/builds/id:43",
                "state": "queued",
            },
        )
    )
    run = await teamcity.trigger_run(pipeline_id="MyBuild", branch="feature/foo")
    assert run.branch == "feature/foo"
    assert route.called


@respx.mock
async def test_trigger_run_with_variables(teamcity):
    route = respx.post(f"{_TC_BASE}/app/rest/buildQueue").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 44,
                "buildTypeId": "MyBuild",
                "href": "/app/rest/builds/id:44",
                "state": "queued",
            },
        )
    )
    run = await teamcity.trigger_run(pipeline_id="MyBuild", variables={"ENV": "prod"})
    assert run.status == CIRunStatus.QUEUED
    assert route.called


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_success(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "state": "finished",
                "status": "SUCCESS",
                "buildType": {"buildTypeId": "MyBuild", "id": "MyBuild"},
                "href": "/app/rest/builds/id:42",
                "branchName": "main",
                "startDate": "2024-07-01T12:00:00+0000",
                "finishDate": "2024-07-01T12:05:00+0000",
                "duration": 300000,
            },
        )
    )
    run = await teamcity.get_run_status("42")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "42"
    assert run.pipeline_id == "MyBuild"
    assert run.duration_seconds == 300000


@respx.mock
async def test_get_run_status_uses_requested_id_when_response_id_is_null(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42").mock(
        return_value=httpx.Response(200, json={"id": None, "state": "queued"})
    )

    run = await teamcity.get_run_status("42")

    assert run.id == "42"


@respx.mock
async def test_get_run_status_failure(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "state": "finished",
                "status": "FAILURE",
                "buildType": {"buildTypeId": "MyBuild"},
            },
        )
    )
    run = await teamcity.get_run_status("42")
    assert run.status == CIRunStatus.FAILURE


def test_run_from_build_non_dict_build_type(teamcity):
    run = teamcity._run_from_build({"id": 42, "buildType": "MyBuild"})
    assert not run.pipeline_id


def test_run_from_build_null_build_type(teamcity):
    run = teamcity._run_from_build({"id": 42, "buildType": None})
    assert not run.pipeline_id


def test_run_from_build_corrupt_duration(teamcity):
    run = teamcity._run_from_build({"id": 42, "duration": "not-a-number"})
    assert run.duration_seconds == 0


def test_run_from_build_null_dates_map_to_empty_strings(teamcity):
    run = teamcity._run_from_build({"id": 42, "startDate": None, "finishDate": None})
    assert not run.created_at
    assert not run.updated_at
    assert "None" not in run.created_at
    assert "None" not in run.updated_at


@respx.mock
async def test_get_run_status_running(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "state": "running",
                "buildType": {"buildTypeId": "MyBuild"},
            },
        )
    )
    run = await teamcity.get_run_status("42")
    assert run.status == CIRunStatus.IN_PROGRESS


@respx.mock
async def test_get_run_status_queued(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "state": "queued",
                "buildType": {"buildTypeId": "MyBuild"},
            },
        )
    )
    run = await teamcity.get_run_status("42")
    assert run.status == CIRunStatus.QUEUED


# ---------------------------------------------------------------------------
# get_run_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42/text").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3\n")
    )
    logs = await teamcity.get_run_logs("42")
    assert len(logs.lines) == 3
    assert logs.lines == ["line1", "line2", "line3"]


@respx.mock
async def test_get_run_logs_with_cursor(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds/id:42/text").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3\n")
    )
    logs = await teamcity.get_run_logs("42", cursor="2")
    assert logs.lines == ["line3"]


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds").mock(
        return_value=httpx.Response(
            200,
            json={
                "build": [
                    {
                        "id": 1,
                        "state": "finished",
                        "status": "SUCCESS",
                        "buildType": {"buildTypeId": "MyBuild"},
                        "href": "/app/rest/builds/id:1",
                    },
                    {
                        "id": 2,
                        "state": "finished",
                        "status": "FAILURE",
                        "buildType": {"buildTypeId": "MyBuild"},
                        "href": "/app/rest/builds/id:2",
                    },
                ]
            },
        )
    )
    runs = await teamcity.list_runs(pipeline_id="MyBuild")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


@respx.mock
async def test_list_runs_filter_by_status(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds").mock(
        return_value=httpx.Response(
            200,
            json={
                "build": [
                    {
                        "id": 1,
                        "state": "finished",
                        "status": "SUCCESS",
                        "buildType": {"buildTypeId": "MyBuild"},
                    },
                    {
                        "id": 2,
                        "state": "finished",
                        "status": "FAILURE",
                        "buildType": {"buildTypeId": "MyBuild"},
                    },
                ]
            },
        )
    )
    runs = await teamcity.list_runs(pipeline_id="MyBuild", status=CIRunStatus.FAILURE)
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.FAILURE


# ---------------------------------------------------------------------------
# query — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "project": [
                    {"id": "ProjectA", "name": "Project A"},
                    {"id": "ProjectB", "name": "Project B"},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="projects")
    result = await teamcity.query(q)
    assert len(result.records) == 2
    assert result.records[0]["id"] == "ProjectA"


@respx.mock
async def test_query_build_types(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/buildTypes").mock(
        return_value=httpx.Response(
            200,
            json={
                "buildType": [
                    {"id": "BT1", "name": "Build Type 1", "projectId": "ProjectA"},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="buildTypes", filters={"project_id": "ProjectA"})
    result = await teamcity.query(q)
    assert len(result.records) == 1
    assert result.records[0]["id"] == "BT1"


@respx.mock
async def test_query_builds(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/builds").mock(
        return_value=httpx.Response(
            200,
            json={
                "build": [
                    {"id": 1, "state": "finished", "status": "SUCCESS", "buildType": {"buildTypeId": "BT1"}},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="builds", filters={"buildTypeId": "BT1"})
    result = await teamcity.query(q)
    assert len(result.records) == 1
    assert result.records[0]["id"] == 1


@respx.mock
async def test_query_agents(teamcity):
    respx.get(f"{_TC_BASE}/app/rest/agents").mock(
        return_value=httpx.Response(
            200,
            json={
                "agent": [
                    {"name": "agent-1", "connected": True},
                    {"name": "agent-2", "connected": False},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="agents")
    result = await teamcity.query(q)
    assert len(result.records) == 2
    assert result.records[0]["name"] == "agent-1"


@respx.mock
async def test_list_runs_corrupt_body_no_crash(teamcity):
    """A non-dict body from the builds list endpoint must degrade to an empty
    page instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_TC_BASE}/app/rest/builds").mock(return_value=httpx.Response(200, json=["garbage"]))
    runs = await teamcity.list_runs()
    assert runs == []


@respx.mock
async def test_list_runs_non_list_build_value_no_crash(teamcity):
    """A corrupt body placing a non-list in ``build`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_TC_BASE}/app/rest/builds").mock(return_value=httpx.Response(200, json={"build": "not-a-list"}))
    runs = await teamcity.list_runs()
    assert runs == []


@respx.mock
async def test_query_projects_corrupt_body_no_crash(teamcity):
    """A non-dict projects body must degrade to an empty page, not crash."""
    respx.get(f"{_TC_BASE}/app/rest/projects").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await teamcity.query(ConnectorQuery(resource="projects"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_build_types_corrupt_body_no_crash(teamcity):
    """A non-dict buildTypes body must degrade to an empty page, not crash."""
    respx.get(f"{_TC_BASE}/app/rest/buildTypes").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await teamcity.query(ConnectorQuery(resource="buildTypes"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_builds_corrupt_body_no_crash(teamcity):
    """A non-dict builds body must degrade to an empty page, not crash."""
    respx.get(f"{_TC_BASE}/app/rest/builds").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await teamcity.query(ConnectorQuery(resource="builds"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_agents_corrupt_body_no_crash(teamcity):
    """A non-dict agents body must degrade to an empty page, not crash."""
    respx.get(f"{_TC_BASE}/app/rest/agents").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await teamcity.query(ConnectorQuery(resource="agents"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_unsupported_resource(teamcity):
    q = ConnectorQuery(resource="invalid", filters={})
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await teamcity.query(q)


# ---------------------------------------------------------------------------
# write — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(teamcity):
    route = respx.post(f"{_TC_BASE}/app/rest/buildQueue").mock(
        return_value=httpx.Response(200, json={"id": 42, "buildTypeId": "MyBuild"})
    )
    payload = ConnectorPayload(resource="build", data={"buildTypeId": "MyBuild"})
    result = await teamcity.write(payload)
    assert result["id"] == "42"
    assert result["buildTypeId"] == "MyBuild"
    assert route.called


@respx.mock
async def test_write_build_null_id_maps_to_empty_string(teamcity):
    respx.post(f"{_TC_BASE}/app/rest/buildQueue").mock(
        return_value=httpx.Response(200, json={"id": None, "buildTypeId": "MyBuild"})
    )
    payload = ConnectorPayload(resource="build", data={"buildTypeId": "MyBuild"})
    result = await teamcity.write(payload)
    assert not result["id"]
    assert result["buildTypeId"] == "MyBuild"


@respx.mock
async def test_write_build_type(teamcity):
    route = respx.post(f"{_TC_BASE}/app/rest/buildTypes").mock(
        return_value=httpx.Response(200, json={"id": "BT_New", "name": "New Build Type"})
    )
    payload = ConnectorPayload(
        resource="buildType",
        data={"buildTypeId": "BT_New", "projectId": "ProjectA", "name": "New Build Type"},
    )
    result = await teamcity.write(payload)
    assert result["id"] == "BT_New"
    assert result["name"] == "New Build Type"
    assert route.called


@respx.mock
async def test_write_build_type_missing_fields(teamcity):
    payload = ConnectorPayload(resource="buildType", data={"buildTypeId": "BT_New"})
    with pytest.raises(ValueError, match="buildType write requires"):
        await teamcity.write(payload)


@respx.mock
async def test_write_unsupported_resource(teamcity):
    payload = ConnectorPayload(resource="invalid", data={})
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await teamcity.write(payload)


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


async def test_double_health_check(teamcity_double):
    result = await teamcity_double.health_check()
    assert result.ok is True


async def test_double_trigger_run(teamcity_double):
    run = await teamcity_double.trigger_run(pipeline_id="MyBuild")
    assert run.status == CIRunStatus.QUEUED
    assert len(teamcity_double._builds) == 1


async def test_double_get_run_status(teamcity_double):
    run = await teamcity_double.get_run_status("42")
    assert run.status == CIRunStatus.SUCCESS


async def test_double_get_run_logs(teamcity_double):
    logs = await teamcity_double.get_run_logs("42")
    assert logs.lines == ["line1", "line2"]


async def test_double_list_runs(teamcity_double):
    runs = await teamcity_double.list_runs(pipeline_id="MyBuild")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS
