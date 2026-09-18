"""Unit tests for CircleCIConnector — HTTP responses are mocked via httpx + respx."""

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
from modulo.connectors.circleci import CircleCIConnector

TOKEN = "cc_test_token"
_BASE = "https://circleci.com/api/v2"
_SLUG = "gh/acme/backend"


@pytest.fixture
def connector():
    return CircleCIConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.CI_RUNNER


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/me").mock(return_value=httpx.Response(200, json={"id": "u1"}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/me").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid or expired token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/me").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/me").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — pipelines / workflows / jobs / runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pipelines(connector):
    body = {"items": [{"id": "p1", "state": "success"}], "next_page_token": None}
    respx.get(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="pipelines", filters={"slug": _SLUG}))
    assert result.total == 1


@respx.mock
async def test_query_pipelines_pagination(connector):
    body = {"items": [{"id": "p1"}], "next_page_token": "token1"}
    respx.get(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="pipelines", filters={"slug": _SLUG}, cursor="c1"))
    assert result.next_cursor == "token1"


@respx.mock
async def test_query_workflows(connector):
    body = {"items": [{"id": "wf1", "status": "running"}], "next_page_token": None}
    respx.get(f"{_BASE}/pipeline/p1/workflow").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="workflows", filters={"pipeline_id": "p1"}))
    assert result.total == 1


@respx.mock
async def test_query_jobs(connector):
    body = {"items": [{"id": "job1", "name": "test"}], "next_page_token": None}
    respx.get(f"{_BASE}/workflow/wf1/job").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="jobs", filters={"workflow_id": "wf1"}))
    assert result.total == 1


@respx.mock
async def test_query_runs(connector):
    body = {"items": [{"id": "p2", "state": "failed"}], "next_page_token": None}
    respx.get(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="runs", filters={"slug": _SLUG}))
    assert result.total == 1


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — trigger_pipeline
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_trigger_pipeline(connector):
    created = {"id": "p3", "state": "created", "number": 3}
    respx.post(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="trigger_pipeline", data={"project_slug": _SLUG, "branch": "main"}),
    )
    assert result["id"] == "p3"


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# CI runner methods
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(connector):
    created = {
        "id": "pipeline-uuid",
        "number": 3,
        "state": "running",
        "project_slug": _SLUG,
        "vcs": {"branch": "main", "revision": "abc123"},
        "trigger": {"actor": {"login": "alice"}},
    }
    respx.post(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=created))
    run = await connector.trigger_run(_SLUG, branch="main")
    assert isinstance(run, CIRun)
    assert run.id == "pipeline-uuid"
    assert run.pipeline_id == _SLUG
    assert run.status == CIRunStatus.IN_PROGRESS
    assert run.branch == "main"
    assert run.triggered_by == "alice"


@respx.mock
async def test_get_run_status(connector):
    body = {"id": "pipeline-uuid", "state": "success", "project_slug": _SLUG}
    respx.get(f"{_BASE}/pipeline/pipeline-uuid").mock(return_value=httpx.Response(200, json=body))
    run = await connector.get_run_status("pipeline-uuid")
    assert run.id == "pipeline-uuid"
    assert run.status == CIRunStatus.SUCCESS


@respx.mock
async def test_get_run_logs(connector):
    wf_body = {"items": [{"id": "wf1", "name": "build"}]}
    job_body = {"items": [{"id": "job1", "name": "test", "job_number": 5, "project_slug": _SLUG}]}
    out_body = {"items": [{"message": "hello\nworld"}]}
    respx.get(f"{_BASE}/pipeline/pipeline-uuid/workflow").mock(return_value=httpx.Response(200, json=wf_body))
    respx.get(f"{_BASE}/workflow/wf1/job").mock(return_value=httpx.Response(200, json=job_body))
    respx.get(f"{_BASE}/project/{_SLUG}/5/outputs").mock(return_value=httpx.Response(200, json=out_body))
    logs = await connector.get_run_logs("pipeline-uuid")
    assert isinstance(logs, CIRunLog)
    assert logs.run_id == "pipeline-uuid"
    assert "hello" in "".join(logs.lines)


@respx.mock
async def test_list_runs(connector):
    body = {
        "items": [
            {"id": "p1", "state": "success", "project_slug": _SLUG},
            {"id": "p2", "state": "failed", "project_slug": _SLUG},
        ],
    }
    respx.get(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id=_SLUG)
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


@respx.mock
async def test_list_runs_filtered_by_status(connector):
    body = {
        "items": [
            {"id": "p2", "state": "failed", "project_slug": _SLUG},
        ],
    }
    respx.get(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(200, json=body))
    runs = await connector.list_runs(pipeline_id=_SLUG, status=CIRunStatus.FAILURE)
    assert len(runs) == 1
    assert runs[0].id == "p2"


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/project/{_SLUG}/pipeline").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="pipelines", filters={"slug": _SLUG}))
