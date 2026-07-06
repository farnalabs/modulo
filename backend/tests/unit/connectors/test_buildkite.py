"""Unit tests for the Buildkite connector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import (
    CIRunStatus,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorType,
)
from modulo.connectors.buildkite import BuildkiteConnector, _BuildkiteTestDouble


@pytest.fixture()
def bk_runner():
    return BuildkiteConnector(token="bkt_test")


@pytest.fixture()
def bk_double():
    return _BuildkiteTestDouble()


_BUILDKITE_API = "https://api.buildkite.com/v2"


def test_connector_type(bk_runner):
    assert bk_runner.connector_type == ConnectorType.BUILDKITE


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(bk_runner):
    respx.get(f"{_BUILDKITE_API}/user").mock(return_value=httpx.Response(200, json={"id": "test-user"}))
    result = await bk_runner.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail_401(bk_runner):
    respx.get(f"{_BUILDKITE_API}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await bk_runner.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


@respx.mock
async def test_health_check_fail_500(bk_runner):
    respx.get(f"{_BUILDKITE_API}/user").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    result = await bk_runner.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# trigger_run
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_default_branch(bk_runner):
    respx.post(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 42,
                "pipeline": {"slug": "my-pipeline"},
                "state": "scheduled",
                "web_url": "https://buildkite.com/my-org/my-pipeline/builds/42",
                "branch": "main",
                "commit": "abc123",
                "created_at": "2026-01-01T00:00:00Z",
                "creator": {"name": "dev"},
            },
        )
    )
    run = await bk_runner.trigger_run(pipeline_id="my-org/my-pipeline")
    assert run.pipeline_id == "my-pipeline"
    assert run.status == CIRunStatus.QUEUED


@respx.mock
async def test_trigger_run_with_variables(bk_runner):
    respx.post(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 43,
                "pipeline": {"slug": "my-pipeline"},
                "state": "running",
                "web_url": "https://buildkite.com/my-org/my-pipeline/builds/43",
                "branch": "develop",
                "commit": "def456",
                "created_at": "2026-01-01T00:00:00Z",
                "creator": {"name": "dev"},
            },
        )
    )
    run = await bk_runner.trigger_run(
        pipeline_id="my-org/my-pipeline",
        branch="develop",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.IN_PROGRESS
    assert run.branch == "develop"


@respx.mock
async def test_trigger_run_invalid_id_raises(bk_runner):
    with pytest.raises(ValueError, match="Invalid pipeline_id format"):
        await bk_runner.trigger_run(pipeline_id="bogus")


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_success(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "pipeline": {"slug": "my-pipeline"},
                "state": "passed",
                "web_url": "https://buildkite.com/my-org/my-pipeline/builds/42",
                "branch": "main",
                "commit": "abc123",
                "created_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:01:00Z",
                "creator": {"name": "dev"},
            },
        )
    )
    run = await bk_runner.get_run_status("my-org/my-pipeline/42")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "42"
    assert run.pipeline_id == "my-pipeline"


@respx.mock
async def test_get_run_status_failure(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/43").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 43,
                "pipeline": {"slug": "my-pipeline"},
                "state": "failed",
                "branch": "main",
                "creator": {},
            },
        )
    )
    run = await bk_runner.get_run_status("my-org/my-pipeline/43")
    assert run.status == CIRunStatus.FAILURE


@respx.mock
async def test_get_run_status_cancelled(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/44").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 44,
                "pipeline": {"slug": "my-pipeline"},
                "state": "canceled",
                "branch": "main",
                "creator": {},
            },
        )
    )
    run = await bk_runner.get_run_status("my-org/my-pipeline/44")
    assert run.status == CIRunStatus.CANCELLED


@respx.mock
async def test_get_run_status_invalid_id_raises(bk_runner):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await bk_runner.get_run_status("bogus")


# ---------------------------------------------------------------------------
# get_run_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/42/jobs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "job-uuid-1", "name": "build"},
                {"id": "job-uuid-2", "name": "test"},
            ],
        )
    )
    respx.get(
        f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/42/jobs/job-uuid-1/log",
    ).mock(return_value=httpx.Response(200, text="Build log line 1\nBuild log line 2\n"))
    respx.get(
        f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/42/jobs/job-uuid-2/log",
    ).mock(return_value=httpx.Response(200, text="Test log line 1\nTest log line 2\n"))
    logs = await bk_runner.get_run_logs("my-org/my-pipeline/42")
    assert len(logs.lines) >= 4
    assert any("Job: build" in line for line in logs.lines)
    assert any("Build log line 1" in line for line in logs.lines)


@respx.mock
async def test_get_run_logs_invalid_id_raises(bk_runner):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await bk_runner.get_run_logs("bogus")


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(bk_runner):
    respx.get(
        f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds",
        params={"per_page": 20},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "pipeline": {"slug": "my-pipeline"},
                    "state": "passed",
                    "branch": "main",
                    "commit": "abc",
                    "creator": {"name": "dev"},
                },
                {
                    "number": 2,
                    "pipeline": {"slug": "my-pipeline"},
                    "state": "failed",
                    "branch": "main",
                    "commit": "def",
                    "creator": {"name": "dev"},
                },
            ],
        )
    )
    runs = await bk_runner.list_runs(pipeline_id="my-org/my-pipeline")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


@respx.mock
async def test_list_runs_with_status_filter(bk_runner):
    respx.get(
        f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds",
        params={"per_page": 20, "state[]": "passed"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "pipeline": {"slug": "my-pipeline"},
                    "state": "passed",
                    "creator": {},
                },
            ],
        )
    )
    runs = await bk_runner.list_runs(
        pipeline_id="my-org/my-pipeline",
        status=CIRunStatus.SUCCESS,
    )
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


@respx.mock
async def test_list_runs_no_pipeline_id(bk_runner):
    runs = await bk_runner.list_runs(pipeline_id=None)
    assert runs == []


@respx.mock
async def test_list_runs_invalid_id_raises(bk_runner):
    with pytest.raises(ValueError, match="Invalid pipeline_id format"):
        await bk_runner.list_runs(pipeline_id="bogus")


# ---------------------------------------------------------------------------
# query — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_organizations(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations").mock(
        return_value=httpx.Response(
            200,
            json=[{"slug": "my-org", "name": "My Org"}],
        )
    )
    q = ConnectorQuery(resource="organizations")
    result = await bk_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["slug"] == "my-org"


@respx.mock
async def test_query_pipelines(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations/my-org/pipelines").mock(
        return_value=httpx.Response(
            200,
            json=[{"slug": "my-pipeline", "name": "My Pipeline"}],
        )
    )
    q = ConnectorQuery(resource="pipelines", filters={"organization": "my-org"})
    result = await bk_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["slug"] == "my-pipeline"


@respx.mock
async def test_query_builds(bk_runner):
    respx.get(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds").mock(
        return_value=httpx.Response(
            200,
            json=[{"number": 1, "state": "passed"}],
        )
    )
    q = ConnectorQuery(
        resource="builds",
        filters={"organization": "my-org", "pipeline": "my-pipeline"},
    )
    result = await bk_runner.query(q)
    assert len(result.records) == 1


@respx.mock
async def test_query_jobs(bk_runner):
    respx.get(
        f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds/42/jobs",
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "job-1", "name": "build", "state": "passed"}],
        )
    )
    q = ConnectorQuery(
        resource="jobs",
        filters={"organization": "my-org", "pipeline": "my-pipeline", "build": "42"},
    )
    result = await bk_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["name"] == "build"


@respx.mock
async def test_query_unsupported_resource(bk_runner):
    q = ConnectorQuery(resource="invalid")
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await bk_runner.query(q)


# ---------------------------------------------------------------------------
# write — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(bk_runner):
    respx.post(f"{_BUILDKITE_API}/organizations/my-org/pipelines/my-pipeline/builds").mock(
        return_value=httpx.Response(
            201,
            json={"number": 42, "state": "scheduled"},
        )
    )
    payload = ConnectorPayload(
        resource="build",
        data={"organization": "my-org", "pipeline": "my-pipeline", "branch": "main"},
    )
    result = await bk_runner.write(payload)
    assert result["state"] == "scheduled"
    assert result["number"] == 42


@respx.mock
async def test_write_unsupported_resource(bk_runner):
    payload = ConnectorPayload(resource="invalid", data={})
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await bk_runner.write(payload)


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


async def test_double_trigger_run(bk_double):
    run = await bk_double.trigger_run(
        pipeline_id="my-org/my-pipeline",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.QUEUED
    assert len(bk_double._triggered) == 1


async def test_double_get_run_status(bk_double):
    run = await bk_double.get_run_status("my-org/my-pipeline/42")
    assert run.status == CIRunStatus.QUEUED


async def test_double_get_run_logs(bk_double):
    bk_double._run_logs = ["line1", "line2"]
    logs = await bk_double.get_run_logs("my-org/my-pipeline/42")
    assert logs.lines == ["line1", "line2"]


async def test_double_list_runs(bk_double):
    runs = await bk_double.list_runs(pipeline_id="my-org/my-pipeline")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


async def test_double_health_check(bk_double):
    result = await bk_double.health_check()
    assert result.ok is True


async def test_double_query(bk_double):
    result = await bk_double.query(ConnectorQuery(resource="organizations"))
    assert result.records == []


async def test_double_write(bk_double):
    result = await bk_double.write(ConnectorPayload(resource="build", data={"organization": "my-org"}))
    assert result == {}
