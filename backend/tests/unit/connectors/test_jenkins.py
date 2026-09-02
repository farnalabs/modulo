"""Unit tests for the Jenkins connector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import CIRunStatus, ConnectorPayload, ConnectorQuery
from modulo.connectors.jenkins import JenkinsConnector, _JenkinsTestDouble

_JENKINS_BASE = "http://jenkins.example.com"


@pytest.fixture
def jenkins():
    return JenkinsConnector(username="admin", token="secret", base_url=_JENKINS_BASE)


@pytest.fixture
def jenkins_double():
    return _JenkinsTestDouble()


def test_connector_type(jenkins):
    assert jenkins.connector_type.value == "jenkins"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(jenkins):
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(200, json={"nodeName": "master"}))
    result = await jenkins.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail_401(jenkins):
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await jenkins.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


@respx.mock
async def test_health_check_fail_500(jenkins):
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    result = await jenkins.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# trigger_run
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(jenkins):
    route = respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/42/"})
    )
    run = await jenkins.trigger_run(pipeline_id="my-job")
    assert run.pipeline_id == "my-job"
    assert run.status == CIRunStatus.QUEUED
    assert route.called


@respx.mock
async def test_trigger_run_with_parameters(jenkins):
    route = respx.post(f"{_JENKINS_BASE}/job/my-job/buildWithParameters").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/43/"})
    )
    run = await jenkins.trigger_run(pipeline_id="my-job", variables={"BRANCH": "main", "TAG": "v1"})
    assert run.pipeline_id == "my-job"
    assert run.status == CIRunStatus.QUEUED
    assert route.called


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_success(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "result": "SUCCESS",
                "url": "http://jenkins.example.com/job/my-job/42/",
                "timestamp": 1700000000000,
                "duration": 120000,
            },
        )
    )
    run = await jenkins.get_run_status("my-job/42")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "42"


@respx.mock
async def test_get_run_status_failure(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "result": "FAILURE",
                "url": "http://jenkins.example.com/job/my-job/42/",
            },
        )
    )
    run = await jenkins.get_run_status("my-job/42")
    assert run.status == CIRunStatus.FAILURE


def test_parse_build_corrupt_duration(jenkins):
    run = jenkins._parse_build({"id": "42", "duration": "not-a-number"})
    assert run.duration_seconds is None


def test_parse_build_zero_duration(jenkins):
    run = jenkins._parse_build({"id": "42", "duration": 0})
    assert run.duration_seconds is None


def test_parse_build_null_id_and_timestamp_map_to_empty_strings(jenkins):
    run = jenkins._parse_build({"id": None, "timestamp": None})
    assert not run.id
    assert not run.created_at
    assert "None" not in run.id
    assert "None" not in run.created_at


@respx.mock
async def test_get_run_status_running(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "result": None,
                "url": "http://jenkins.example.com/job/my-job/42/",
            },
        )
    )
    run = await jenkins.get_run_status("my-job/42")
    assert run.status == CIRunStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# get_run_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3\n")
    )
    logs = await jenkins.get_run_logs("my-job/42")
    assert len(logs.lines) == 3
    assert logs.lines == ["line1", "line2", "line3"]


@respx.mock
async def test_get_run_logs_with_cursor(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3\n")
    )
    logs = await jenkins.get_run_logs("my-job/42", cursor="2")
    assert logs.lines == ["line3"]


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "builds": [
                    {
                        "number": 1,
                        "result": "SUCCESS",
                        "timestamp": 1700000000000,
                        "duration": 60000,
                        "url": "http://jenkins.example.com/job/my-job/1/",
                    },
                    {
                        "number": 2,
                        "result": "FAILURE",
                        "timestamp": 1700000100000,
                        "duration": 30000,
                        "url": "http://jenkins.example.com/job/my-job/2/",
                    },
                ]
            },
        )
    )
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


# ---------------------------------------------------------------------------
# query — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_jobs(jenkins):
    respx.get(f"{_JENKINS_BASE}/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"name": "my-job", "url": "http://jenkins.example.com/job/my-job/", "color": "blue"},
                    {"name": "other-job", "url": "http://jenkins.example.com/job/other-job/", "color": "red"},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="jobs")
    result = await jenkins.query(q)
    assert len(result.records) == 2
    assert result.records[0]["name"] == "my-job"


@respx.mock
async def test_query_builds(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "builds": [
                    {"number": 1, "result": "SUCCESS", "timestamp": 1700000000000, "duration": 60000, "url": ""},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="builds", filters={"job_name": "my-job"})
    result = await jenkins.query(q)
    assert len(result.records) == 1
    assert result.records[0]["number"] == 1


@respx.mock
async def test_query_nodes(jenkins):
    respx.get(f"{_JENKINS_BASE}/computer/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "computer": [
                    {"displayName": "master", "offline": False},
                    {"displayName": "agent-1", "offline": True},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="nodes")
    result = await jenkins.query(q)
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "master"


@respx.mock
async def test_query_unsupported_resource(jenkins):
    q = ConnectorQuery(resource="invalid", filters={})
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await jenkins.query(q)


@respx.mock
async def test_list_runs_non_list_builds_no_crash(jenkins):
    """A corrupt body placing a non-list in ``builds`` must fall back to an empty run list."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json={"builds": "corrupt"}))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert runs == []


@respx.mock
async def test_list_runs_non_dict_body_no_crash(jenkins):
    """A corrupt/hostile non-dict body must degrade to an empty run list."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json=["not-a-dict"]))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert runs == []


@respx.mock
async def test_query_jobs_non_list_jobs_no_crash(jenkins):
    """A corrupt non-list ``jobs`` page field must degrade gracefully."""
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(200, json={"jobs": {"name": "x"}}))
    result = await jenkins.query(ConnectorQuery(resource="jobs"))
    assert not result.records
    assert result.total == 0


# ---------------------------------------------------------------------------
# write — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(jenkins):
    route = respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/42/"})
    )
    payload = ConnectorPayload(resource="build", data={"job_name": "my-job"})
    result = await jenkins.write(payload)
    assert result["location"] == "http://jenkins.example.com/job/my-job/42/"
    assert result["job_name"] == "my-job"
    assert route.called


@respx.mock
async def test_write_unsupported_resource(jenkins):
    payload = ConnectorPayload(resource="invalid", data={})
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await jenkins.write(payload)


# ---------------------------------------------------------------------------
# Missing required params
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_missing_pipeline_id(jenkins):
    respx.post(f"{_JENKINS_BASE}/job//build").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(httpx.HTTPError):
        await jenkins.trigger_run(pipeline_id="")


@respx.mock
async def test_list_runs_missing_pipeline_id(jenkins):
    respx.get(f"{_JENKINS_BASE}/job//api/json").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(httpx.HTTPError):
        await jenkins.list_runs(pipeline_id="")


@respx.mock
async def test_query_builds_missing_job_name(jenkins):
    q = ConnectorQuery(resource="builds", filters={})
    respx.get(f"{_JENKINS_BASE}/job//api/json").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(httpx.HTTPError):
        await jenkins.query(q)


# ---------------------------------------------------------------------------
# Corrupt list payload hardening
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs_corrupt_body_no_crash(jenkins):
    """A non-dict body from the builds endpoint must degrade to an empty run
    list instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert not runs


@respx.mock
async def test_list_runs_non_list_builds_value_no_crash(jenkins):
    """A corrupt body placing a non-list in ``builds`` must fall back to an
    empty run list instead of iterating a bare string."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json={"builds": "boom"}))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert not runs


@respx.mock
async def test_query_jobs_corrupt_body_no_crash(jenkins):
    """A non-dict body from the jobs endpoint must degrade to an empty page."""
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await jenkins.query(ConnectorQuery(resource="jobs"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_builds_corrupt_body_no_crash(jenkins):
    """A non-dict body from the builds endpoint must degrade to an empty page."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await jenkins.query(ConnectorQuery(resource="builds", filters={"job_name": "my-job"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_nodes_corrupt_body_no_crash(jenkins):
    """A non-dict body from the nodes endpoint must degrade to an empty page."""
    respx.get(f"{_JENKINS_BASE}/computer/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await jenkins.query(ConnectorQuery(resource="nodes"))
    assert not result.records
    assert result.total == 0


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


async def test_double_health_check(jenkins_double):
    result = await jenkins_double.health_check()
    assert result.ok is True


async def test_double_trigger_run(jenkins_double):
    run = await jenkins_double.trigger_run(pipeline_id="my-job")
    assert run.status == CIRunStatus.QUEUED
    assert len(jenkins_double._builds) == 1


async def test_double_get_run_status(jenkins_double):
    run = await jenkins_double.get_run_status("my-job/42")
    assert run.status == CIRunStatus.SUCCESS


async def test_double_get_run_logs(jenkins_double):
    logs = await jenkins_double.get_run_logs("my-job/42")
    assert logs.lines == ["line1", "line2"]


async def test_double_list_runs(jenkins_double):
    runs = await jenkins_double.list_runs(pipeline_id="my-job")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS
