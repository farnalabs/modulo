"""Step definitions for the Jenkins connector BDD feature.

Wires ``features/connectors/jenkins.feature`` into the executing BDD
suite by driving the REAL ``JenkinsConnector``
against a respx-mocked Jenkins REST API — mirroring
``backend/tests/unit/connectors/test_jenkins.py`` so the executing BDD surface
locks the same contract the unit suite does: token validation via ``/api/json``,
trigger build (plain + parameterised) / get build status / list builds / fetch
console logs.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/jenkins.feature")

_BASE = "http://jenkins.example.com"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Jenkins connector scenarios."""
    return {}


@given(parsers.parse('a Jenkins connector configured with job "{job}"'))
def jenkins_connector_job(ctx: dict, job: str) -> None:
    from modulo.connectors.jenkins import JenkinsConnector

    ctx["connector"] = JenkinsConnector(username="admin", token="secret", base_url=_BASE)
    ctx["job"] = job
    ctx["valid"] = True


@given("a Jenkins connector configured with invalid credentials")
def jenkins_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.jenkins import JenkinsConnector

    ctx["connector"] = JenkinsConnector(username="admin", token="secret", base_url=_BASE)
    ctx["valid"] = False


@when("the connector checks health")
def jenkins_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    if ctx["valid"]:
        response = httpx.Response(200, json={"nodeName": "master"})
    else:
        response = httpx.Response(401, text="Unauthorized")
    with respx.mock:
        respx.get(f"{_BASE}/api/json").mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@when("the connector triggers a build")
def jenkins_trigger_run(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    with respx.mock:
        respx.post(f"{_BASE}/job/{ctx['job']}/build").mock(
            return_value=httpx.Response(201, headers={"Location": f"{_BASE}/job/{ctx['job']}/42/"})
        )
        run = asyncio.run(ctx["connector"].trigger_run(pipeline_id=ctx["job"]))
    assert run.status == CIRunStatus.QUEUED, run
    ctx["run"] = run


@when("the connector triggers a build with parameters")
def jenkins_trigger_run_with_parameters(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    with respx.mock:
        respx.post(f"{_BASE}/job/{ctx['job']}/buildWithParameters").mock(
            return_value=httpx.Response(201, headers={"Location": f"{_BASE}/job/{ctx['job']}/43/"})
        )
        run = asyncio.run(
            ctx["connector"].trigger_run(pipeline_id=ctx["job"], variables={"BRANCH": "main", "TAG": "v1"})
        )
    assert run.status == CIRunStatus.QUEUED, run
    ctx["run"] = run


@then("the build is queued successfully")
def jenkins_build_queued(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    run = ctx["run"]
    assert run is not None, "No triggered build"
    assert run.pipeline_id == ctx["job"], run
    assert run.status == CIRunStatus.QUEUED, run


@then("the build is queued with parameters")
def jenkins_build_queued_with_parameters(ctx: dict) -> None:
    jenkins_build_queued(ctx)


@when(parsers.parse('the connector checks status of build "{build}"'))
def jenkins_get_run_status(ctx: dict, build: str) -> None:
    from modulo.connectors.base import CIRunStatus

    body = {
        "id": build,
        "result": "SUCCESS",
        "url": f"{_BASE}/job/{ctx['job']}/{build}/",
        "timestamp": 1700000000000,
        "duration": 120000,
    }
    with respx.mock:
        respx.get(f"{_BASE}/job/{ctx['job']}/{build}/api/json").mock(return_value=httpx.Response(200, json=body))
        run = asyncio.run(ctx["connector"].get_run_status(f"{ctx['job']}/{build}"))
    assert run.status == CIRunStatus.SUCCESS, run
    assert run.id == build, run
    ctx["run"] = run


@then("the build status is returned")
def jenkins_build_status(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    run = ctx["run"]
    assert run is not None, "No build status"
    assert run.id == "42", run
    assert run.status == CIRunStatus.SUCCESS, run


@when("the connector lists recent builds")
def jenkins_list_runs(ctx: dict) -> None:
    from modulo.connectors.base import CIRunStatus

    runs = [
        {
            "number": 1,
            "result": "SUCCESS",
            "timestamp": 1700000000000,
            "duration": 60000,
            "url": f"{_BASE}/job/{ctx['job']}/1/",
        },
        {
            "number": 2,
            "result": "FAILURE",
            "timestamp": 1700000100000,
            "duration": 30000,
            "url": f"{_BASE}/job/{ctx['job']}/2/",
        },
    ]
    with respx.mock:
        respx.get(f"{_BASE}/job/{ctx['job']}/api/json").mock(return_value=httpx.Response(200, json={"builds": runs}))
        ctx["runs"] = asyncio.run(ctx["connector"].list_runs(pipeline_id=ctx["job"]))
    assert ctx["runs"][0].status == CIRunStatus.SUCCESS, ctx["runs"][0]
    assert ctx["runs"][1].status == CIRunStatus.FAILURE, ctx["runs"][1]


@then("the result contains builds")
def jenkins_result_contains_builds(ctx: dict) -> None:
    runs = ctx["runs"]
    assert runs is not None, "No listed builds"
    assert len(runs) == 2, runs


@when(parsers.parse('the connector fetches logs for build "{build}"'))
def jenkins_get_run_logs(ctx: dict, build: str) -> None:
    with respx.mock:
        respx.get(f"{_BASE}/job/{ctx['job']}/{build}/consoleText").mock(
            return_value=httpx.Response(200, text="line1\nline2\nline3\n")
        )
        ctx["logs"] = asyncio.run(ctx["connector"].get_run_logs(f"{ctx['job']}/{build}"))


@then("the logs contain console output")
def jenkins_logs_contain_output(ctx: dict) -> None:
    logs = ctx["logs"]
    assert logs is not None, "No run logs"
    assert any("line2" in line for line in logs.lines), logs.lines


@then(parsers.parse('the health check returns "{status}"'))
def jenkins_health_result(ctx: dict, status: str) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"
