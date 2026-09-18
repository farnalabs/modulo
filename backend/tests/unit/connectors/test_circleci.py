"""Unit tests for the CircleCI connector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import CIRunStatus, ConnectorPayload, ConnectorQuery
from modulo.connectors.circleci import CircleCIConnector, _CircleCITestDouble


@pytest.fixture
def cc_runner():
    return CircleCIConnector(token="cct_test")


@pytest.fixture
def cc_double():
    return _CircleCITestDouble()


_CIRCLECI_API = "https://circleci.com/api/v2"


def test_connector_type(cc_runner):
    assert cc_runner.connector_type.value == "ci-runner"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(cc_runner):
    respx.get(f"{_CIRCLECI_API}/me").mock(return_value=httpx.Response(200, json={"login": "testuser"}))
    result = await cc_runner.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail_401(cc_runner):
    respx.get(f"{_CIRCLECI_API}/me").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await cc_runner.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


@respx.mock
async def test_health_check_fail_500(cc_runner):
    respx.get(f"{_CIRCLECI_API}/me").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    result = await cc_runner.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# trigger_run
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_default_branch(cc_runner):
    respx.post(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "pipe-uuid-123",
                "number": 42,
                "project_slug": "gh/owner/repo",
                "state": "created",
                "created_at": "2026-01-01T00:00:00Z",
                "trigger": {"actor": {"login": "dev"}},
                "vcs": {"branch": "main", "revision": "abc123"},
            },
        )
    )
    run = await cc_runner.trigger_run(pipeline_id="gh/owner/repo")
    assert run.pipeline_id == "gh/owner/repo"
    assert run.status == CIRunStatus.PENDING


@respx.mock
async def test_trigger_run_with_variables(cc_runner):
    respx.post(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "pipe-uuid-456",
                "number": 43,
                "project_slug": "gh/owner/repo",
                "state": "queued",
                "created_at": "2026-01-01T00:00:00Z",
                "trigger": {"actor": {"login": "dev"}},
                "vcs": {"branch": "develop", "revision": "def456"},
            },
        )
    )
    run = await cc_runner.trigger_run(
        pipeline_id="gh/owner/repo",
        branch="develop",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.QUEUED
    assert run.branch == "develop"


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_success(cc_runner):
    respx.get(f"{_CIRCLECI_API}/pipeline/pipe-uuid-123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pipe-uuid-123",
                "number": 42,
                "project_slug": "gh/owner/repo",
                "state": "success",
                "created_at": "2026-01-01T00:00:00Z",
                "trigger": {"actor": {"login": "dev"}},
                "vcs": {"branch": "main", "revision": "abc123"},
            },
        )
    )
    run = await cc_runner.get_run_status("pipe-uuid-123")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "pipe-uuid-123"
    assert run.pipeline_id == "gh/owner/repo"


@respx.mock
async def test_get_run_status_failure(cc_runner):
    respx.get(f"{_CIRCLECI_API}/pipeline/pipe-uuid-456").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pipe-uuid-456",
                "number": 43,
                "project_slug": "gh/owner/repo",
                "state": "failed",
            },
        )
    )
    run = await cc_runner.get_run_status("pipe-uuid-456")
    assert run.status == CIRunStatus.FAILURE


@respx.mock
async def test_get_run_status_cancelled(cc_runner):
    respx.get(f"{_CIRCLECI_API}/pipeline/pipe-uuid-789").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pipe-uuid-789",
                "number": 44,
                "project_slug": "gh/owner/repo",
                "state": "canceled",
            },
        )
    )
    run = await cc_runner.get_run_status("pipe-uuid-789")
    assert run.status == CIRunStatus.CANCELLED


def test_parse_run_non_dict_vcs(cc_runner):
    run = cc_runner._parse_run({"id": "p1", "vcs": "gh/owner/repo"})
    assert not run.branch
    assert not run.commit_sha


def test_parse_run_null_vcs(cc_runner):
    run = cc_runner._parse_run({"id": "p1", "vcs": None})
    assert not run.branch
    assert not run.commit_sha


def test_parse_run_non_dict_trigger(cc_runner):
    run = cc_runner._parse_run({"id": "p1", "trigger": "manual"})
    assert not run.triggered_by


def test_parse_run_corrupt_actor(cc_runner):
    run = cc_runner._parse_run({"id": "p1", "trigger": {"actor": "alice"}})
    assert not run.triggered_by


def test_parse_run_null_id_maps_to_empty_string(cc_runner):
    run = cc_runner._parse_run({"id": None, "project_slug": "gh/owner/repo"})
    assert not run.id
    assert "None" not in run.id


# ---------------------------------------------------------------------------
# get_run_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs(cc_runner):
    pipeline_uuid = "pipe-uuid-123"
    respx.get(f"{_CIRCLECI_API}/pipeline/{pipeline_uuid}/workflow").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "wf-uuid-1",
                        "name": "build",
                    }
                ]
            },
        )
    )
    respx.get(f"{_CIRCLECI_API}/workflow/wf-uuid-1/job").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "job-uuid-1",
                        "job_number": 1,
                        "name": "test",
                        "project_slug": "gh/owner/repo",
                    }
                ]
            },
        )
    )
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/1/outputs").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"message": "Running tests...", "time": "2026-01-01T00:00:00Z", "type": "stdout"},
                    {"message": "All tests passed!", "time": "2026-01-01T00:01:00Z", "type": "stdout"},
                ]
            },
        )
    )
    logs = await cc_runner.get_run_logs(pipeline_uuid)
    assert len(logs.lines) >= 4
    assert any("Workflow:" in line for line in logs.lines)
    assert any("All tests passed!" in line for line in logs.lines)


@respx.mock
async def test_get_run_logs_corrupt_body_no_crash(cc_runner):
    """A corrupt/hostile non-dict workflow body must not crash get_run_logs."""
    pipeline_uuid = "pipe-uuid-123"
    respx.get(f"{_CIRCLECI_API}/pipeline/{pipeline_uuid}/workflow").mock(
        return_value=httpx.Response(200, json=["corrupt", "body"])
    )
    logs = await cc_runner.get_run_logs(pipeline_uuid)
    assert not logs.lines


@respx.mock
async def test_get_run_logs_non_list_items_no_crash(cc_runner):
    """A corrupt body placing a non-list in ``items`` must fall back to no lines."""
    pipeline_uuid = "pipe-uuid-123"
    respx.get(f"{_CIRCLECI_API}/pipeline/{pipeline_uuid}/workflow").mock(
        return_value=httpx.Response(200, json={"items": "corrupt"})
    )
    logs = await cc_runner.get_run_logs(pipeline_uuid)
    assert not logs.lines


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(cc_runner):
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "pipe-1",
                        "number": 1,
                        "project_slug": "gh/owner/repo",
                        "state": "success",
                        "trigger": {"actor": {"login": "dev"}},
                        "vcs": {"branch": "main", "revision": "abc"},
                    },
                    {
                        "id": "pipe-2",
                        "number": 2,
                        "project_slug": "gh/owner/repo",
                        "state": "failed",
                        "trigger": {"actor": {"login": "dev"}},
                        "vcs": {"branch": "main", "revision": "def"},
                    },
                ]
            },
        )
    )
    runs = await cc_runner.list_runs(pipeline_id="gh/owner/repo")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


# ---------------------------------------------------------------------------
# query — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pipelines(cc_runner):
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "p1", "state": "success"}],
                "next_page_token": "token-abc",
            },
        )
    )
    q = ConnectorQuery(resource="pipelines", filters={"slug": "gh/owner/repo"})
    result = await cc_runner.query(q)
    assert len(result.records) == 1
    assert result.next_cursor == "token-abc"


@respx.mock
async def test_query_pipelines_non_string_cursor_not_emitted(cc_runner):
    """A corrupt non-string next_page_token must not be emitted as a pagination cursor."""
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "p1", "state": "success"}],
                "next_page_token": {"page": 2},
            },
        )
    )
    q = ConnectorQuery(resource="pipelines", filters={"slug": "gh/owner/repo"})
    result = await cc_runner.query(q)
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_workflows(cc_runner):
    respx.get(f"{_CIRCLECI_API}/pipeline/pipe-uuid/workflow").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "wf-1", "name": "build", "status": "running"}],
            },
        )
    )
    q = ConnectorQuery(resource="workflows", filters={"pipeline_id": "pipe-uuid"})
    result = await cc_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["name"] == "build"


@respx.mock
async def test_query_jobs(cc_runner):
    respx.get(f"{_CIRCLECI_API}/workflow/wf-1/job").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "job-1", "name": "test", "status": "running"}],
            },
        )
    )
    q = ConnectorQuery(resource="jobs", filters={"workflow_id": "wf-1"})
    result = await cc_runner.query(q)
    assert len(result.records) == 1
    assert result.records[0]["name"] == "test"


@respx.mock
async def test_query_runs(cc_runner):
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "p1", "state": "success"}],
            },
        )
    )
    q = ConnectorQuery(resource="runs", filters={"slug": "gh/owner/repo"})
    result = await cc_runner.query(q)
    assert len(result.records) == 1


# ---------------------------------------------------------------------------
# corrupt/hostile list payload hardening
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs_corrupt_body_no_crash(cc_runner):
    """A non-dict body from the pipeline list endpoint must degrade to an
    empty page instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    runs = await cc_runner.list_runs(pipeline_id="gh/owner/repo")
    assert runs == []


@respx.mock
async def test_list_runs_non_list_items_no_crash(cc_runner):
    """A corrupt body placing a non-list in ``items`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(200, json={"items": "not-a-list"})
    )
    runs = await cc_runner.list_runs(pipeline_id="gh/owner/repo")
    assert runs == []


@respx.mock
async def test_query_pipelines_corrupt_body_no_crash(cc_runner):
    """A non-dict pipelines body must degrade to an empty page, not crash."""
    respx.get(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await cc_runner.query(ConnectorQuery(resource="pipelines", filters={"slug": "gh/owner/repo"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_workflows_corrupt_body_no_crash(cc_runner):
    """A non-dict workflows body must degrade to an empty page, not crash."""
    respx.get(f"{_CIRCLECI_API}/pipeline/pipe-uuid/workflow").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await cc_runner.query(ConnectorQuery(resource="workflows", filters={"pipeline_id": "pipe-uuid"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_jobs_corrupt_body_no_crash(cc_runner):
    """A non-dict jobs body must degrade to an empty page, not crash."""
    respx.get(f"{_CIRCLECI_API}/workflow/wf-1/job").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await cc_runner.query(ConnectorQuery(resource="jobs", filters={"workflow_id": "wf-1"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_unsupported_resource(cc_runner):
    q = ConnectorQuery(resource="invalid", filters={})
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await cc_runner.query(q)


# ---------------------------------------------------------------------------
# write — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_trigger_pipeline(cc_runner):
    respx.post(f"{_CIRCLECI_API}/project/gh/owner/repo/pipeline").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "pipe-uuid",
                "number": 42,
                "project_slug": "gh/owner/repo",
                "state": "created",
            },
        )
    )
    payload = ConnectorPayload(
        resource="trigger_pipeline",
        data={"project_slug": "gh/owner/repo", "branch": "main"},
    )
    result = await cc_runner.write(payload)
    assert result["state"] == "created"
    assert result["id"] == "pipe-uuid"


@respx.mock
async def test_write_unsupported_resource(cc_runner):
    payload = ConnectorPayload(resource="invalid", data={})
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await cc_runner.write(payload)


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


async def test_double_trigger_run(cc_double):
    run = await cc_double.trigger_run(
        pipeline_id="gh/owner/repo",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.QUEUED
    assert len(cc_double._triggered) == 1


async def test_double_get_run_status(cc_double):
    run = await cc_double.get_run_status("pipe-uuid")
    assert run.status == CIRunStatus.QUEUED


async def test_double_get_run_logs(cc_double):
    cc_double._run_logs = ["line1", "line2"]
    logs = await cc_double.get_run_logs("pipe-uuid")
    assert logs.lines == ["line1", "line2"]


async def test_double_list_runs(cc_double):
    runs = await cc_double.list_runs(pipeline_id="gh/owner/repo")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


async def test_double_health_check(cc_double):
    result = await cc_double.health_check()
    assert result.ok is True
