"""Step definitions for the Buildkite connector BDD feature.

Wires ``features/connectors/buildkite.feature`` into the executing suite (the
2026-09-14 improve-architecture product-map walk) by driving the REAL
``BuildkiteConnector`` against a respx-mocked Buildkite REST API v2 — mirroring
``backend/tests/unit/connectors/test_buildkite.py`` so the executing BDD surface
locks the same contract the unit suite does: token validation via ``/user``,
trigger build / get run status / list runs / get run logs.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/buildkite.feature")

TOKEN = "bkt_test_token"
_BASE = "https://api.buildkite.com/v2"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Buildkite connector scenarios."""
    return {}


@given("a Buildkite connector configured with valid credentials")
def buildkite_connector_valid(ctx: dict) -> None:
    from modulo.connectors.buildkite import BuildkiteConnector

    ctx["connector"] = BuildkiteConnector(token=TOKEN)
    ctx["valid"] = True


@given("a Buildkite connector configured with invalid credentials")
def buildkite_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.buildkite import BuildkiteConnector

    ctx["connector"] = BuildkiteConnector(token=TOKEN)
    ctx["valid"] = False


@when("the connector checks health")
def buildkite_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    response = httpx.Response(200, json={"id": "test-user"}) if ctx["valid"] else httpx.Response(401, text="Unauthorized")
    with respx.mock:
        respx.get(f"{_BASE}/user").mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@when(parsers.parse('the connector triggers a build on branch "{branch}"'))
def buildkite_trigger_run(branch: str, ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    body = {
        "number": 42,
        "pipeline": {"slug": "my-pipeline"},
        "state": "scheduled",
        "web_url": "https://buildkite.com/my-org/my-pipeline/builds/42",
        "branch": branch,
        "commit": "abc123",
        "created_at": "2026-01-01T00:00:00Z",
        "creator": {"name": "dev"},
    }
    with respx.mock:
        respx.post(f"{_BASE}/organizations/my-org/pipelines/my-pipeline/builds").mock(
            return_value=httpx.Response(201, json=body)
        )
        run = asyncio.run(ctx["connector"].trigger_run(pipeline_id="my-org/my-pipeline", branch=branch))
    assert run.status == CIRunStatus.QUEUED, run
    assert run.branch == branch, run
    ctx["run"] = run


@when(parsers.parse('the connector checks status of build "{build}"'))
def buildkite_get_run_status(build: str, ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    body = {
        "number": 42,
        "pipeline": {"slug": "my-pipeline"},
        "state": "running",
        "web_url": "https://buildkite.com/my-org/my-pipeline/builds/42",
        "branch": "main",
        "commit": "abc123",
        "created_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
        "creator": {"name": "dev"},
    }
    with respx.mock:
        respx.get(f"{_BASE}/organizations/my-org/pipelines/my-pipeline/builds/{build}").mock(
            return_value=httpx.Response(200, json=body)
        )
        run = asyncio.run(ctx["connector"].get_run_status(f"my-org/my-pipeline/{build}"))
    assert run.status == CIRunStatus.IN_PROGRESS, run
    assert run.id == build, run
    ctx["run"] = run


@when("the connector lists recent builds")
def buildkite_list_runs(ctx: dict) -> None:
    runs = [
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
    ]
    with respx.mock:
        respx.get(
            f"{_BASE}/organizations/my-org/pipelines/my-pipeline/builds",
            params={"per_page": 20},
        ).mock(return_value=httpx.Response(200, json=runs))
        ctx["runs"] = asyncio.run(ctx["connector"].list_runs(pipeline_id="my-org/my-pipeline"))


@when(parsers.parse('the connector fetches logs for build "{build}"'))
def buildkite_get_run_logs(build: str, ctx: dict) -> None:
    jobs = [
        {"id": "job-uuid-1", "name": "build"},
        {"id": "job-uuid-2", "name": "test"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/organizations/my-org/pipelines/my-pipeline/builds/{build}/jobs").mock(
            return_value=httpx.Response(200, json=jobs)
        )
        respx.get(
            f"{_BASE}/organizations/my-org/pipelines/my-pipeline/builds/{build}/jobs/job-uuid-1/log",
        ).mock(return_value=httpx.Response(200, text="Build log line 1\nBuild log line 2\n"))
        respx.get(
            f"{_BASE}/organizations/my-org/pipelines/my-pipeline/builds/{build}/jobs/job-uuid-2/log",
        ).mock(return_value=httpx.Response(200, text="Test log line 1\nTest log line 2\n"))
        ctx["logs"] = asyncio.run(ctx["connector"].get_run_logs(f"my-org/my-pipeline/{build}"))


@then(parsers.parse('the health check returns "{status}"'))
def buildkite_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("a build is created successfully")
def buildkite_build_created(ctx: dict) -> None:
    run = ctx["run"]
    assert run is not None, "No triggered run"
    assert run.id == "42", run
    assert run.pipeline_id == "my-pipeline", run
    assert run.branch == "main", run


@then("the build status is returned")
def buildkite_build_status(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    run = ctx["run"]
    assert run is not None, "No run status"
    assert run.id == "42", run
    assert run.status == CIRunStatus.IN_PROGRESS, run


@then("the result contains builds")
def buildkite_result_contains_builds(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    runs = ctx["runs"]
    assert runs is not None, "No listed runs"
    assert len(runs) == 2, runs
    assert runs[0].status == CIRunStatus.SUCCESS, runs[0]
    assert runs[1].status == CIRunStatus.FAILURE, runs[1]


@then("the logs contain job output")
def buildkite_logs_contain_output(ctx: dict) -> None:
    logs = ctx["logs"]
    assert logs is not None, "No run logs"
    assert any("Job: build" in line for line in logs.lines), logs.lines
    assert any("Build log line 1" in line for line in logs.lines), logs.lines
    assert any("Test log line 1" in line for line in logs.lines), logs.lines
