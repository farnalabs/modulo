"""Unit tests for the Azure Pipelines connector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors._safe_page import safe_paging_total as _paging_total
from modulo.connectors.azure_pipelines import (
    AzurePipelinesConnector,
    _AzurePipelinesTestDouble,
)
from modulo.connectors.base import (
    CIRunStatus,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorType,
)

_AZURE_DEVOPS_API = "https://dev.azure.com"


@pytest.fixture
def ap_runner():
    return AzurePipelinesConnector(token="apt_test", organization="myorg", project="myproject")


@pytest.fixture
def ap_double():
    return _AzurePipelinesTestDouble()


def test_connector_type(ap_runner):
    assert ap_runner.connector_type == ConnectorType.AZURE_PIPELINES


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(ap_runner):
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/_apis/projects", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json={"value": [{"id": "proj-1"}], "count": 1})
    )
    result = await ap_runner.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail_401(ap_runner):
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/_apis/projects", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await ap_runner.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


@respx.mock
async def test_health_check_fail_500(ap_runner):
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/_apis/projects", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    result = await ap_runner.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# trigger_run
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_default(ap_runner):
    respx.post(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 101,
                "pipeline": {"id": 1},
                "state": "inProgress",
                "_links": {"web": {"href": "https://dev.azure.com/myorg/myproject/_build/results?buildId=101"}},
                "createdDate": "2026-01-01T00:00:00Z",
            },
        )
    )
    run = await ap_runner.trigger_run(pipeline_id="1")
    assert run.pipeline_id == "1"
    assert run.status == CIRunStatus.IN_PROGRESS


@respx.mock
async def test_trigger_run_with_branch_and_variables(ap_runner):
    respx.post(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 102,
                "pipeline": {"id": 1},
                "state": "inProgress",
                "resources": {
                    "repositories": {
                        "self": {
                            "refName": "refs/heads/develop",
                            "version": "abc123",
                        }
                    }
                },
                "_links": {"web": {"href": "https://dev.azure.com/myorg/myproject/_build/results?buildId=102"}},
                "createdDate": "2026-01-01T00:00:00Z",
            },
        )
    )
    run = await ap_runner.trigger_run(
        pipeline_id="1",
        branch="develop",
        variables={"ENV": "staging"},
    )
    assert run.status == CIRunStatus.IN_PROGRESS
    assert run.branch == "develop"


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_success(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/101",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 101,
                "pipeline": {"id": 1},
                "state": "completed",
                "result": "succeeded",
                "_links": {"web": {"href": "https://dev.azure.com/myorg/myproject/_build/results?buildId=101"}},
                "createdDate": "2026-01-01T00:00:00Z",
                "finishedDate": "2026-01-01T00:05:00Z",
            },
        )
    )
    run = await ap_runner.get_run_status("1/101")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "101"


@respx.mock
async def test_get_run_status_failure(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/102",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 102,
                "pipeline": {"id": 1},
                "state": "completed",
                "result": "failed",
            },
        )
    )
    run = await ap_runner.get_run_status("1/102")
    assert run.status == CIRunStatus.FAILURE


def test_parse_run_non_dict_pipeline(ap_runner):
    run = ap_runner._parse_run({"id": 101, "pipeline": 1})
    assert not run.pipeline_id


def test_parse_run_null_links(ap_runner):
    run = ap_runner._parse_run({"id": 101, "_links": None})
    assert not run.url


def test_parse_run_non_dict_resources(ap_runner):
    run = ap_runner._parse_run({"id": 101, "resources": "none"})
    assert not run.branch
    assert not run.commit_sha


def test_parse_run_non_dict_template_parameters(ap_runner):
    run = ap_runner._parse_run({"id": 101, "templateParameters": "none"})
    assert not run.triggered_by


def test_parse_run_null_id_maps_to_empty_string(ap_runner):
    run = ap_runner._parse_run({"id": None})
    assert not run.id
    assert "None" not in run.id


def test_parse_run_falsy_int_id_preserved(ap_runner):
    run = ap_runner._parse_run({"id": 0})
    assert run.id == "0"


@respx.mock
async def test_get_run_status_cancelled(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/103",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 103,
                "pipeline": {"id": 1},
                "state": "completed",
                "result": "canceled",
            },
        )
    )
    run = await ap_runner.get_run_status("1/103")
    assert run.status == CIRunStatus.CANCELLED


# ---------------------------------------------------------------------------
# get_run_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/101/logs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 1,
                        "name": "Job 1",
                        "url": (f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/101/logs/1"),
                    },
                ]
            },
        )
    )
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/101/logs/1").mock(
        return_value=httpx.Response(200, text="Build step 1\nBuild step 2\n")
    )
    logs = await ap_runner.get_run_logs("1/101")
    assert len(logs.lines) >= 2
    assert any("Log: Job 1" in line for line in logs.lines)
    assert any("Build step 1" in line for line in logs.lines)


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 1,
                        "pipeline": {"id": 1},
                        "state": "completed",
                        "result": "succeeded",
                    },
                    {
                        "id": 2,
                        "pipeline": {"id": 1},
                        "state": "completed",
                        "result": "failed",
                    },
                ]
            },
        )
    )
    runs = await ap_runner.list_runs(pipeline_id="1")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


@respx.mock
async def test_list_runs_no_pipeline_id(ap_runner):
    runs = await ap_runner.list_runs(pipeline_id=None)
    assert runs == []


@respx.mock
async def test_list_runs_with_status_filter(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": 1, "pipeline": {"id": 1}, "state": "completed", "result": "succeeded"},
                    {"id": 2, "pipeline": {"id": 1}, "state": "completed", "result": "failed"},
                ]
            },
        )
    )
    runs = await ap_runner.list_runs(pipeline_id="1", status=CIRunStatus.SUCCESS)
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


# ---------------------------------------------------------------------------
# query — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/_apis/projects",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": "proj-1", "name": "Project 1"}], "count": 1},
        )
    )
    q = ConnectorQuery(resource="projects")
    result = await ap_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["id"] == "proj-1"


@respx.mock
async def test_query_pipelines(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": 1, "name": "CI Pipeline"}], "count": 1},
        )
    )
    q = ConnectorQuery(resource="pipelines")
    result = await ap_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["name"] == "CI Pipeline"


@respx.mock
async def test_query_runs(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": 1, "state": "inProgress"}], "count": 1},
        )
    )
    q = ConnectorQuery(resource="runs", filters={"pipeline_id": "1"})
    result = await ap_runner.query(q)
    assert len(result.records) == 1


@respx.mock
async def test_query_releases(ap_runner):
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/release/releases",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": 1, "name": "Release 1"}], "count": 1},
        )
    )
    q = ConnectorQuery(resource="releases")
    result = await ap_runner.query(q)
    assert len(result.records) == 1


@respx.mock
async def test_query_unsupported_resource(ap_runner):
    q = ConnectorQuery(resource="invalid")
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await ap_runner.query(q)


@respx.mock
async def test_query_pipelines_corrupt_total(ap_runner):
    """A non-finite ``count`` must not poison the reported total."""
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines",
        params={"api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, content=b'{"value": [{"id": 1}], "count": 1e999}'))
    result = await ap_runner.query(ConnectorQuery(resource="pipelines"))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_projects_corrupt_body_no_crash(ap_runner):
    """A corrupt/hostile response returning a non-dict body must not crash the
    connector — it falls back to an empty page with no total."""
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/_apis/projects", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await ap_runner.query(ConnectorQuery(resource="projects"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_pipelines_corrupt_body_no_crash(ap_runner):
    """A non-dict pipelines body must degrade to an empty page, not crash."""
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await ap_runner.query(ConnectorQuery(resource="pipelines"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_pipelines_non_list_value_no_crash(ap_runner):
    """A corrupt body placing a non-list in ``value`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json={"value": "not-a-list", "count": 2})
    )
    result = await ap_runner.query(ConnectorQuery(resource="pipelines"))
    assert not result.records
    assert result.total == 2


@respx.mock
async def test_query_runs_corrupt_body_no_crash(ap_runner):
    """A non-dict runs body must degrade to an empty page, not crash."""
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await ap_runner.query(ConnectorQuery(resource="runs", filters={"pipeline_id": "1"}))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_releases_corrupt_body_no_crash(ap_runner):
    """A non-dict releases body must degrade to an empty page, not crash."""
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/release/releases", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await ap_runner.query(ConnectorQuery(resource="releases"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_list_runs_corrupt_body_no_crash(ap_runner):
    """A non-dict runs body must degrade to an empty list, not crash."""
    respx.get(f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    runs = await ap_runner.list_runs(pipeline_id="1")
    assert not runs


@respx.mock
async def test_get_run_logs_corrupt_body_no_crash(ap_runner):
    """A non-dict, non-list logs body must degrade to empty log lines, not crash."""
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/101/logs",
        params={"api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json="garbage"))
    logs = await ap_runner.get_run_logs("1/101")
    assert not logs.lines


@respx.mock
async def test_get_run_logs_list_body_skips_non_dict_entries(ap_runner):
    """A list logs body carrying non-dict entries must skip them, not crash."""
    respx.get(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs/101/logs",
        params={"api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json=["garbage"]))
    logs = await ap_runner.get_run_logs("1/101")
    assert not logs.lines


# ---------------------------------------------------------------------------
# Pagination helpers — direct unit coverage
# ---------------------------------------------------------------------------


def test_paging_total() -> None:
    assert _paging_total({"count": 25}, "count") == 25
    assert _paging_total({"count": "25"}, "count") == 25
    assert _paging_total({"count": 1e999}, "count") == 0
    assert _paging_total({"count": float("nan")}, "count") == 0
    assert _paging_total({"count": True}, "count") == 0
    assert _paging_total({"count": "garbage"}, "count") == 0
    assert _paging_total({}, "count") is None
    assert _paging_total(["garbage"], "count") is None


# ---------------------------------------------------------------------------
# write — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_run(ap_runner):
    respx.post(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/pipelines/1/runs",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": 101, "state": "inProgress"},
        )
    )
    payload = ConnectorPayload(
        resource="run",
        data={"pipeline_id": "1", "branch": "main"},
    )
    result = await ap_runner.write(payload)
    assert result["state"] == "inProgress"
    assert result["id"] == 101


@respx.mock
async def test_write_release(ap_runner):
    respx.post(
        f"{_AZURE_DEVOPS_API}/myorg/myproject/_apis/release/releases",
        params={"api-version": "7.0"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": 1, "name": "Release 1"},
        )
    )
    payload = ConnectorPayload(
        resource="release",
        data={"definition_id": "1", "description": "Test release"},
    )
    result = await ap_runner.write(payload)
    assert result["name"] == "Release 1"


@respx.mock
async def test_write_unsupported_resource(ap_runner):
    payload = ConnectorPayload(resource="invalid", data={})
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await ap_runner.write(payload)


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


async def test_double_trigger_run(ap_double):
    run = await ap_double.trigger_run(
        pipeline_id="1",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.QUEUED
    assert len(ap_double._runs) == 1


async def test_double_get_run_status(ap_double):
    run = await ap_double.get_run_status("1/101")
    assert run.status == CIRunStatus.QUEUED


async def test_double_get_run_logs(ap_double):
    ap_double._run_logs = ["line1", "line2"]
    logs = await ap_double.get_run_logs("1/101")
    assert logs.lines == ["line1", "line2"]


async def test_double_list_runs(ap_double):
    runs = await ap_double.list_runs(pipeline_id="1")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


async def test_double_health_check(ap_double):
    result = await ap_double.health_check()
    assert result.ok is True


async def test_double_query(ap_double):
    result = await ap_double.query(ConnectorQuery(resource="projects"))
    assert not result.records


async def test_double_write(ap_double):
    result = await ap_double.write(ConnectorPayload(resource="run", data={"pipeline_id": "1"}))
    assert result == {}
