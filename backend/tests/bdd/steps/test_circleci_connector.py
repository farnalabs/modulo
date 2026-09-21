"""Step definitions for the CircleCI connector BDD feature.

Wires ``features/connectors/circleci.feature`` into the executing suite (the
product-map review pass) by driving the REAL ``CircleCIConnector``
against a respx-mocked CircleCI REST API v2 — mirroring
``backend/tests/unit/connectors/test_circleci.py`` so the executing BDD surface
locks the same contract the unit suite does: token validation via ``/me``,
trigger pipeline / get pipeline status / list pipelines / fetch workflow+job
logs.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/circleci.feature")

TOKEN = "cct_test_token"
_API = "https://circleci.com/api/v2"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the CircleCI connector scenarios."""
    return {}


@given(parsers.parse('a CircleCI connector configured with project "{project}"'))
def circleci_connector_project(ctx: dict, project: str) -> None:
    from modulo.connectors.circleci import CircleCIConnector

    ctx["connector"] = CircleCIConnector(token=TOKEN)
    ctx["project"] = project
    ctx["valid"] = True


@given("a CircleCI connector configured with invalid credentials")
def circleci_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.circleci import CircleCIConnector

    ctx["connector"] = CircleCIConnector(token=TOKEN)
    ctx["valid"] = False


@when("the connector checks health")
def circleci_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    if ctx["valid"]:
        response = httpx.Response(200, json={"login": "testuser"})
    else:
        response = httpx.Response(401, text="Unauthorized")
    with respx.mock:
        respx.get(f"{_API}/me").mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@when(parsers.parse('the connector triggers a pipeline on branch "{branch}"'))
def circleci_trigger_run(ctx: dict, branch: str) -> None:
    from modulo.connectors.base import CIRunStatus

    body = {
        "id": "pipe-uuid-123",
        "number": 42,
        "project_slug": ctx["project"],
        "state": "queued",
        "created_at": "2026-01-01T00:00:00Z",
        "trigger": {"actor": {"login": "dev"}},
        "vcs": {"branch": branch, "revision": "abc123"},
    }
    with respx.mock:
        respx.post(f"{_API}/project/{ctx['project']}/pipeline").mock(return_value=httpx.Response(201, json=body))
        run = asyncio.run(ctx["connector"].trigger_run(pipeline_id=ctx["project"], branch=branch))
    assert run.status == CIRunStatus.QUEUED, run
    assert run.branch == branch, run
    ctx["run"] = run


@then("the pipeline run is created successfully")
def circleci_run_created(ctx: dict) -> None:
    run = ctx["run"]
    assert run is not None, "No triggered run"
    assert run.pipeline_id == ctx["project"], run
    assert run.branch == "main", run


@when(parsers.parse('the connector checks status of pipeline "{run_id}"'))
def circleci_get_run_status(ctx: dict, run_id: str) -> None:
    from modulo.connectors.base import CIRunStatus

    body = {
        "id": run_id,
        "number": 42,
        "project_slug": ctx["project"],
        "state": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "trigger": {"actor": {"login": "dev"}},
        "vcs": {"branch": "main", "revision": "abc123"},
    }
    with respx.mock:
        respx.get(f"{_API}/pipeline/{run_id}").mock(return_value=httpx.Response(200, json=body))
        run = asyncio.run(ctx["connector"].get_run_status(run_id))
    assert run.status == CIRunStatus.IN_PROGRESS, run
    assert run.id == run_id, run
    ctx["run"] = run


@then("the pipeline status is returned")
def circleci_run_status(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    run = ctx["run"]
    assert run is not None, "No run status"
    assert run.id == "pipe-uuid-123", run
    assert run.status == CIRunStatus.IN_PROGRESS, run


@when("the connector lists recent pipeline runs")
def circleci_list_runs(ctx: dict) -> None:
    runs = [
        {
            "id": "pipe-1",
            "number": 1,
            "project_slug": ctx["project"],
            "state": "success",
            "trigger": {"actor": {"login": "dev"}},
            "vcs": {"branch": "main", "revision": "abc"},
        },
        {
            "id": "pipe-2",
            "number": 2,
            "project_slug": ctx["project"],
            "state": "failed",
            "trigger": {"actor": {"login": "dev"}},
            "vcs": {"branch": "main", "revision": "def"},
        },
    ]
    with respx.mock:
        respx.get(f"{_API}/project/{ctx['project']}/pipeline").mock(
            return_value=httpx.Response(200, json={"items": runs})
        )
        ctx["runs"] = asyncio.run(ctx["connector"].list_runs(pipeline_id=ctx["project"]))


@then("the result contains pipeline runs")
def circleci_result_contains_runs(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    runs = ctx["runs"]
    assert runs is not None, "No listed runs"
    assert len(runs) == 2, runs
    assert runs[0].status == CIRunStatus.SUCCESS, runs[0]
    assert runs[1].status == CIRunStatus.FAILURE, runs[1]


@when(parsers.parse('the connector fetches logs for pipeline "{run_id}"'))
def circleci_get_run_logs(ctx: dict, run_id: str) -> None:
    with respx.mock:
        respx.get(f"{_API}/pipeline/{run_id}/workflow").mock(
            return_value=httpx.Response(200, json={"items": [{"id": "wf-uuid-1", "name": "build"}]})
        )
        respx.get(f"{_API}/workflow/wf-uuid-1/job").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "job-uuid-1",
                            "job_number": 1,
                            "name": "test",
                            "project_slug": ctx["project"],
                        }
                    ]
                },
            )
        )
        respx.get(f"{_API}/project/{ctx['project']}/1/outputs").mock(
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
        ctx["logs"] = asyncio.run(ctx["connector"].get_run_logs(run_id))


@then("the logs contain workflow and job output")
def circleci_logs_contain_output(ctx: dict) -> None:
    logs = ctx["logs"]
    assert logs is not None, "No run logs"
    assert any("Workflow:" in line for line in logs.lines), logs.lines
    assert any("All tests passed!" in line for line in logs.lines), logs.lines


@then(parsers.parse('the health check returns "{status}"'))
def circleci_health_result(ctx: dict, status: str) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"
