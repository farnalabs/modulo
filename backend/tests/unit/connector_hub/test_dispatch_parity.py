"""FAR-1141: provider-parity tests for CI dispatch across ALL SEVEN CI connectors.

Every CI connector must expose the same four dispatch operations
(``trigger_run``, ``get_run_status``, ``get_run_logs``, ``list_runs``) and behave
identically through the ``_TracedConnector`` proxy: concrete implementations,
traced spans carrying the right attributes, ACL gating (``trigger_run`` is a
write-class operation, the three reads are read-class), and the same result
types over their real HTTP calls (respx-mocked).

The parity matrix below is the contract: adding an eighth CI connector means
adding a row, and a connector that drifts (missing op, untraced call, wrong ACL
class, wrong return type) fails here before it can fail a pipeline run.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.base import CIRun, CIRunLog, CIRunStatus, ConnectorACL, ConnectorPermissionError
from modulo.connectors.buildkite import BuildkiteConnector
from modulo.connectors.ci_runner import GitHubActionsCIRunner, GitLabCIRunner
from modulo.connectors.ci_runner.base import CIRunnerBase
from modulo.connectors.circleci import CircleCIConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.core.connector_hub import _TracedConnector

_DISPATCH_OPS = ("trigger_run", "get_run_status", "get_run_logs", "list_runs")


@dataclass(frozen=True)
class _Route:
    """One mocked HTTP exchange: (method, url) -> response."""

    method: str
    url: str
    response: httpx.Response


@dataclass(frozen=True)
class _OpCase:
    """One dispatch op exercised against one connector over real (mocked) HTTP."""

    op: str
    kwargs: dict[str, Any]
    routes: tuple[_Route, ...]
    kind: str  # "run" | "log" | "run_list"
    expect_status: CIRunStatus | None = None
    expect_min_lines: int = 0
    expect_count: int = 0


@dataclass(frozen=True)
class _ConnectorSpec:
    """One row of the parity matrix: a connector factory plus its dispatch ids."""

    name: str
    factory: Callable[[], Any]
    pipeline_id: str
    run_id: str
    cases: tuple[_OpCase, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The parity matrix — one row per CI connector, one case per dispatch op.
# Response shapes mirror each provider's own unit tests (tests/unit/connectors/).
# ---------------------------------------------------------------------------

_GITHUB = _ConnectorSpec(
    name="github-actions",
    factory=lambda: GitHubActionsCIRunner(token="ghp_test"),
    pipeline_id="owner/repo/ci.yml",
    run_id="owner/repo/12345",
    cases=(
        _OpCase(
            op="trigger_run",
            # the uniform kwarg subset across all seven connectors — per-provider
            # extras (branch, variables) are covered by each provider's own tests
            kwargs={"pipeline_id": "owner/repo/ci.yml"},
            routes=(
                _Route(
                    "post",
                    "https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/dispatches",
                    httpx.Response(204),
                ),
                _Route(
                    "get",
                    "https://api.github.com/repos/owner/repo/actions/runs",
                    httpx.Response(
                        200,
                        json={
                            "workflow_runs": [
                                {
                                    "id": 12345,
                                    "workflow_id": "ci.yml",
                                    "status": "queued",
                                    "html_url": "https://github.com/owner/repo/actions/runs/12345",
                                    "head_branch": "main",
                                    "head_sha": "abc123",
                                    "created_at": "2026-01-01T00:00:00Z",
                                    "updated_at": "2026-01-01T00:00:00Z",
                                    "actor": {"login": "octocat"},
                                }
                            ]
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.QUEUED,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "owner/repo/12345"},
            routes=(
                _Route(
                    "get",
                    "https://api.github.com/repos/owner/repo/actions/runs/12345",
                    httpx.Response(
                        200,
                        json={
                            "id": 12345,
                            "workflow_id": "ci.yml",
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://github.com/owner/repo/actions/runs/12345",
                            "head_branch": "main",
                            "head_sha": "abc123",
                            "actor": {"login": "octocat"},
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.SUCCESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "owner/repo/12345"},
            routes=(
                _Route(
                    "get",
                    "https://api.github.com/repos/owner/repo/actions/runs/12345/logs",
                    httpx.Response(200, text="line1\nline2\n"),
                ),
            ),
            kind="log",
            expect_min_lines=2,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "owner/repo"},
            routes=(
                _Route(
                    "get",
                    "https://api.github.com/repos/owner/repo/actions/runs",
                    httpx.Response(
                        200,
                        json={
                            "workflow_runs": [
                                {
                                    "id": 1,
                                    "workflow_id": "ci.yml",
                                    "status": "completed",
                                    "conclusion": "success",
                                    "html_url": "",
                                    "head_branch": "main",
                                    "head_sha": "abc",
                                    "actor": {"login": "octocat"},
                                },
                                {
                                    "id": 2,
                                    "workflow_id": "ci.yml",
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "html_url": "",
                                    "head_branch": "main",
                                    "head_sha": "def",
                                    "actor": {"login": "octocat"},
                                },
                            ]
                        },
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_GITLAB = _ConnectorSpec(
    name="gitlab-ci",
    factory=lambda: GitLabCIRunner(token="glpat_test"),
    pipeline_id="12345",
    run_id="12345/67890",
    cases=(
        _OpCase(
            op="trigger_run",
            kwargs={"pipeline_id": "12345"},
            routes=(
                _Route(
                    "post",
                    "https://gitlab.com/api/v4/projects/12345/pipeline",
                    httpx.Response(
                        201,
                        json={
                            "id": 67890,
                            "project_id": "12345",
                            "status": "pending",
                            "web_url": "https://gitlab.com/owner/repo/-/pipelines/67890",
                            "ref": "main",
                            "sha": "abc123",
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": "2026-01-01T00:00:00Z",
                            "user": {"username": "developer"},
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.PENDING,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "12345/67890"},
            routes=(
                _Route(
                    "get",
                    "https://gitlab.com/api/v4/projects/12345/pipelines/67890",
                    httpx.Response(
                        200,
                        json={
                            "id": 67890,
                            "project_id": 12345,
                            "status": "running",
                            "web_url": "https://gitlab.com/owner/repo/-/pipelines/67890",
                            "ref": "main",
                            "sha": "abc123",
                            "user": {"username": "developer"},
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.IN_PROGRESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "12345/67890"},
            routes=(
                _Route(
                    "get",
                    "https://gitlab.com/api/v4/projects/12345/pipelines/67890/jobs",
                    httpx.Response(200, json=[{"id": 111, "name": "build"}]),
                ),
                _Route(
                    "get",
                    "https://gitlab.com/api/v4/projects/12345/jobs/111/trace",
                    httpx.Response(200, text="Build log line 1\nBuild log line 2\n"),
                ),
            ),
            kind="log",
            expect_min_lines=2,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "12345"},
            routes=(
                _Route(
                    "get",
                    "https://gitlab.com/api/v4/projects/12345/pipelines",
                    httpx.Response(
                        200,
                        json=[
                            {
                                "id": 1,
                                "project_id": 12345,
                                "status": "success",
                                "web_url": "",
                                "ref": "main",
                                "sha": "abc",
                                "user": {"username": "dev"},
                            },
                            {
                                "id": 2,
                                "project_id": 12345,
                                "status": "failed",
                                "web_url": "",
                                "ref": "main",
                                "sha": "def",
                                "user": {"username": "dev"},
                            },
                        ],
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_CIRCLECI = _ConnectorSpec(
    name="circleci",
    factory=lambda: CircleCIConnector(token="cct_test"),
    pipeline_id="gh/owner/repo",
    run_id="pipe-uuid-123",
    cases=(
        _OpCase(
            op="trigger_run",
            kwargs={"pipeline_id": "gh/owner/repo"},
            routes=(
                _Route(
                    "post",
                    "https://circleci.com/api/v2/project/gh/owner/repo/pipeline",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.PENDING,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "pipe-uuid-123"},
            routes=(
                _Route(
                    "get",
                    "https://circleci.com/api/v2/pipeline/pipe-uuid-123",
                    httpx.Response(
                        200,
                        json={
                            "id": "pipe-uuid-123",
                            "number": 42,
                            "project_slug": "gh/owner/repo",
                            "state": "success",
                            "trigger": {"actor": {"login": "dev"}},
                            "vcs": {"branch": "main", "revision": "abc123"},
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.SUCCESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "pipe-uuid-123"},
            routes=(
                _Route(
                    "get",
                    "https://circleci.com/api/v2/pipeline/pipe-uuid-123/workflow",
                    httpx.Response(200, json={"items": [{"id": "wf-uuid-1", "name": "build"}]}),
                ),
                _Route(
                    "get",
                    "https://circleci.com/api/v2/workflow/wf-uuid-1/job",
                    httpx.Response(
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
                    ),
                ),
                _Route(
                    "get",
                    "https://circleci.com/api/v2/project/gh/owner/repo/1/outputs",
                    httpx.Response(
                        200,
                        json={
                            "items": [
                                {"message": "Running tests...", "time": "2026-01-01T00:00:00Z", "type": "stdout"},
                                {"message": "All tests passed!", "time": "2026-01-01T00:01:00Z", "type": "stdout"},
                            ]
                        },
                    ),
                ),
            ),
            kind="log",
            expect_min_lines=4,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "gh/owner/repo"},
            routes=(
                _Route(
                    "get",
                    "https://circleci.com/api/v2/project/gh/owner/repo/pipeline",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_BUILDKITE = _ConnectorSpec(
    name="buildkite",
    factory=lambda: BuildkiteConnector(token="bkt_test"),
    pipeline_id="my-org/my-pipeline",
    run_id="my-org/my-pipeline/42",
    cases=(
        _OpCase(
            op="trigger_run",
            kwargs={"pipeline_id": "my-org/my-pipeline"},
            routes=(
                _Route(
                    "post",
                    "https://api.buildkite.com/v2/organizations/my-org/pipelines/my-pipeline/builds",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.QUEUED,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "my-org/my-pipeline/42"},
            routes=(
                _Route(
                    "get",
                    "https://api.buildkite.com/v2/organizations/my-org/pipelines/my-pipeline/builds/42",
                    httpx.Response(
                        200,
                        json={
                            "number": 42,
                            "pipeline": {"slug": "my-pipeline"},
                            "state": "passed",
                            "web_url": "https://buildkite.com/my-org/my-pipeline/builds/42",
                            "branch": "main",
                            "commit": "abc123",
                            "created_at": "2026-01-01T00:00:00Z",
                            "creator": {"name": "dev"},
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.SUCCESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "my-org/my-pipeline/42"},
            routes=(
                _Route(
                    "get",
                    "https://api.buildkite.com/v2/organizations/my-org/pipelines/my-pipeline/builds/42/jobs",
                    httpx.Response(200, json=[{"id": "job-uuid-1", "name": "build"}]),
                ),
                _Route(
                    "get",
                    "https://api.buildkite.com/v2/organizations/my-org/pipelines/my-pipeline/builds/42/jobs/job-uuid-1/log",
                    httpx.Response(200, text="Build log line 1\nBuild log line 2\n"),
                ),
            ),
            kind="log",
            expect_min_lines=2,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "my-org/my-pipeline"},
            routes=(
                _Route(
                    "get",
                    "https://api.buildkite.com/v2/organizations/my-org/pipelines/my-pipeline/builds",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_JENKINS = _ConnectorSpec(
    name="jenkins",
    factory=lambda: JenkinsConnector(username="admin", token="secret", base_url="http://jenkins.example.com"),
    pipeline_id="my-job",
    run_id="my-job/42",
    cases=(
        _OpCase(
            op="trigger_run",
            kwargs={"pipeline_id": "my-job"},
            routes=(
                _Route(
                    "post",
                    "http://jenkins.example.com/job/my-job/build",
                    httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/42/"}),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.QUEUED,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "my-job/42"},
            routes=(
                _Route(
                    "get",
                    "http://jenkins.example.com/job/my-job/42/api/json",
                    httpx.Response(
                        200,
                        json={
                            "id": "42",
                            "result": "SUCCESS",
                            "url": "http://jenkins.example.com/job/my-job/42/",
                            "timestamp": 1700000000000,
                            "duration": 120000,
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.SUCCESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "my-job/42"},
            routes=(
                _Route(
                    "get",
                    "http://jenkins.example.com/job/my-job/42/consoleText",
                    httpx.Response(200, text="line1\nline2\nline3\n"),
                ),
            ),
            kind="log",
            expect_min_lines=3,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "my-job"},
            routes=(
                _Route(
                    "get",
                    "http://jenkins.example.com/job/my-job/api/json",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_TEAMCITY = _ConnectorSpec(
    name="teamcity",
    factory=lambda: TeamCityConnector(token="secret", base_url="http://teamcity.example.com"),
    pipeline_id="MyBuild",
    run_id="42",
    cases=(
        _OpCase(
            op="trigger_run",
            kwargs={"pipeline_id": "MyBuild"},
            routes=(
                _Route(
                    "post",
                    "http://teamcity.example.com/app/rest/buildQueue",
                    httpx.Response(
                        200,
                        json={
                            "id": 42,
                            "buildTypeId": "MyBuild",
                            "href": "/app/rest/builds/id:42",
                            "state": "queued",
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.QUEUED,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "42"},
            routes=(
                _Route(
                    "get",
                    "http://teamcity.example.com/app/rest/builds/id:42",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.SUCCESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "42"},
            routes=(
                _Route(
                    "get",
                    "http://teamcity.example.com/app/rest/builds/id:42/text",
                    httpx.Response(200, text="line1\nline2\nline3\n"),
                ),
            ),
            kind="log",
            expect_min_lines=3,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "MyBuild"},
            routes=(
                _Route(
                    "get",
                    "http://teamcity.example.com/app/rest/builds",
                    httpx.Response(
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
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_AZURE = _ConnectorSpec(
    name="azure-pipelines",
    factory=lambda: AzurePipelinesConnector(token="apt_test", organization="myorg", project="myproject"),
    pipeline_id="1",
    run_id="1/101",
    cases=(
        _OpCase(
            op="trigger_run",
            kwargs={"pipeline_id": "1"},
            routes=(
                _Route(
                    "post",
                    "https://dev.azure.com/myorg/myproject/_apis/pipelines/1/runs",
                    httpx.Response(
                        200,
                        json={
                            "id": 101,
                            "pipeline": {"id": 1},
                            "state": "inProgress",
                            "_links": {
                                "web": {"href": "https://dev.azure.com/myorg/myproject/_build/results?buildId=101"}
                            },
                            "createdDate": "2026-01-01T00:00:00Z",
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.IN_PROGRESS,
        ),
        _OpCase(
            op="get_run_status",
            kwargs={"run_id": "1/101"},
            routes=(
                _Route(
                    "get",
                    "https://dev.azure.com/myorg/myproject/_apis/pipelines/1/runs/101",
                    httpx.Response(
                        200,
                        json={
                            "id": 101,
                            "pipeline": {"id": 1},
                            "state": "completed",
                            "result": "succeeded",
                            "_links": {
                                "web": {"href": "https://dev.azure.com/myorg/myproject/_build/results?buildId=101"}
                            },
                            "createdDate": "2026-01-01T00:00:00Z",
                        },
                    ),
                ),
            ),
            kind="run",
            expect_status=CIRunStatus.SUCCESS,
        ),
        _OpCase(
            op="get_run_logs",
            kwargs={"run_id": "1/101"},
            routes=(
                _Route(
                    "get",
                    "https://dev.azure.com/myorg/myproject/_apis/pipelines/1/runs/101/logs",
                    httpx.Response(
                        200,
                        json={
                            "value": [
                                {
                                    "id": 1,
                                    "name": "Job 1",
                                    "url": "https://dev.azure.com/myorg/myproject/_apis/pipelines/1/runs/101/logs/1",
                                },
                            ]
                        },
                    ),
                ),
                _Route(
                    "get",
                    "https://dev.azure.com/myorg/myproject/_apis/pipelines/1/runs/101/logs/1",
                    httpx.Response(200, text="Build step 1\nBuild step 2\n"),
                ),
            ),
            kind="log",
            expect_min_lines=2,
        ),
        _OpCase(
            op="list_runs",
            kwargs={"pipeline_id": "1"},
            routes=(
                _Route(
                    "get",
                    "https://dev.azure.com/myorg/myproject/_apis/pipelines/1/runs",
                    httpx.Response(
                        200,
                        json={
                            "value": [
                                {"id": 1, "pipeline": {"id": 1}, "state": "completed", "result": "succeeded"},
                                {"id": 2, "pipeline": {"id": 1}, "state": "completed", "result": "failed"},
                            ]
                        },
                    ),
                ),
            ),
            kind="run_list",
            expect_status=CIRunStatus.SUCCESS,
            expect_count=2,
        ),
    ),
)

_SPECS: tuple[_ConnectorSpec, ...] = (_GITHUB, _GITLAB, _CIRCLECI, _BUILDKITE, _JENKINS, _TEAMCITY, _AZURE)

_CASES = [(spec, case) for spec in _SPECS for case in spec.cases]
_CASE_IDS = [f"{spec.name}-{case.op}" for spec, case in _CASES]

# (allowed_operations, op) pairs that must be DENIED for every connector:
# read-only ACL blocks the write-class trigger; write-only ACL blocks the reads.
_DENY_CASES = [
    (spec, allowed, op)
    for spec in _SPECS
    for allowed, op in (("read", "trigger_run"), ("write", "get_run_status"), ("write", "list_runs"))
]
_DENY_IDS = [f"{spec.name}-{allowed}-{op}" for spec, allowed, op in _DENY_CASES]

_DENY_KWARGS: dict[str, dict[str, Any]] = {
    "trigger_run": lambda spec: {"pipeline_id": spec.pipeline_id},
    "get_run_status": lambda spec: {"run_id": spec.run_id},
    "list_runs": lambda spec: {"pipeline_id": spec.pipeline_id},
}


def _assert_result(case: _OpCase, result: Any) -> None:
    """Assert the result type and the op's distinguishing value (proves the real path ran)."""
    if case.kind == "run":
        assert isinstance(result, CIRun)
        assert result.status == case.expect_status
    elif case.kind == "log":
        assert isinstance(result, CIRunLog)
        assert len(result.lines) >= case.expect_min_lines
    else:
        assert isinstance(result, list)
        assert len(result) == case.expect_count
        for run in result:
            assert isinstance(run, CIRun)
        assert result[0].status == case.expect_status


# ---------------------------------------------------------------------------
# Parity 1: every connector defines all four dispatch ops concretely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _SPECS, ids=[s.name for s in _SPECS])
def test_connector_defines_all_four_dispatch_ops(spec: _ConnectorSpec) -> None:
    cls = type(spec.factory())
    for op in _DISPATCH_OPS:
        method = getattr(cls, op, None)
        assert callable(method), f"{cls.__name__} is missing {op!r}"
        assert not getattr(method, "__isabstractmethod__", False), f"{cls.__name__}.{op} is abstract"


def test_ci_runner_base_declares_the_four_ops_abstract() -> None:
    """The CIRunnerBase contract FORCES its subclasses to implement all four ops."""
    for op in _DISPATCH_OPS:
        base_method = getattr(CIRunnerBase, op)
        assert getattr(base_method, "__isabstractmethod__", False), f"CIRunnerBase.{op} must stay abstract"


# ---------------------------------------------------------------------------
# Parity 2: every op is traced + ACL-permitted + forwards over real HTTP
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(("spec", "case"), _CASES, ids=_CASE_IDS)
async def test_dispatch_op_traced_and_forwarded(
    spec: _ConnectorSpec,
    case: _OpCase,
    tracer,
    exporter: InMemorySpanExporter,
) -> None:
    inner = spec.factory()
    acl = ConnectorACL("org", allowed_operations=["read", "write"])
    traced = _TracedConnector(inner, tracer=tracer, acl=acl)
    for route in case.routes:
        getattr(respx, route.method)(route.url).mock(return_value=route.response)

    result = await getattr(traced, case.op)(**case.kwargs)

    _assert_result(case, result)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == f"connector.{inner.connector_type}.{case.op}"
    assert span.attributes is not None
    assert span.attributes.get("connector.operation") == case.op
    assert span.attributes.get("connector.type") == str(inner.connector_type)
    assert span.status.status_code == StatusCode.OK
    # sensitive payloads never land in span attributes
    assert "connector.variables" not in span.attributes
    assert "connector.lines" not in span.attributes


# ---------------------------------------------------------------------------
# Parity 4: run-id round-trip — the id trigger_run PRODUCES is the exact id
# get_run_status CONSUMES (the dispatch await_completion contract)
# ---------------------------------------------------------------------------

_TRIGGER_CASES: dict[str, _OpCase] = {
    spec.name: next(case for case in spec.cases if case.op == "trigger_run") for spec in _SPECS
}
_STATUS_CASES: dict[str, _OpCase] = {
    spec.name: next(case for case in spec.cases if case.op == "get_run_status") for spec in _SPECS
}


@respx.mock
@pytest.mark.parametrize("spec", _SPECS, ids=[s.name for s in _SPECS])
async def test_trigger_run_id_round_trips_into_get_run_status(spec: _ConnectorSpec) -> None:
    """``trigger_run`` → ``get_run_status(run.id)`` → terminal status, all seven providers.

    A dispatch node with ``await_completion`` feeds the id from ``trigger_run``'s
    result straight back into ``get_run_status`` (see
    ``node_runner._await_dispatch_terminal``), so a provider that PRODUCES a
    bare/unqualified id while its own reader REQUIRES a qualified one strands
    the wait on ``ValueError: Invalid run_id format`` while the external job
    runs fine. The second read proves the id ``get_run_status`` RETURNS is
    re-consumable too — the contract holds in both directions.
    """
    connector = spec.factory()
    trigger_case = _TRIGGER_CASES[spec.name]
    status_case = _STATUS_CASES[spec.name]
    for case in (trigger_case, status_case):
        for route in case.routes:
            getattr(respx, route.method)(route.url).mock(return_value=route.response)

    run = await connector.trigger_run(pipeline_id=spec.pipeline_id)
    assert run.id, "trigger_run must never hand back an empty/unusable run id"

    status = await connector.get_run_status(run_id=run.id)
    assert isinstance(status, CIRun)
    assert status.status == status_case.expect_status
    assert status.id == run.id, "the id get_run_status returns must equal the id it consumed"

    again = await connector.get_run_status(run_id=status.id)
    assert again.status == status.status


# ---------------------------------------------------------------------------
# Parity 3: ACL deny parity — the same op is denied for EVERY connector
# ---------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
@pytest.mark.parametrize(("spec", "allowed", "op"), _DENY_CASES, ids=_DENY_IDS)
async def test_dispatch_op_acl_denied(
    spec: _ConnectorSpec,
    allowed: str,
    op: str,
    tracer,
    exporter: InMemorySpanExporter,
) -> None:
    inner = spec.factory()
    acl = ConnectorACL("org", allowed_operations=[allowed])
    traced = _TracedConnector(inner, tracer=tracer, acl=acl)

    with pytest.raises(ConnectorPermissionError):
        await getattr(traced, op)(**_DENY_KWARGS[op](spec))

    # the ACL fires BEFORE any span and before any HTTP attempt (respx would
    # raise AllMockedAssertionError on an unmocked call, so reaching here means
    # the connector was never touched)
    assert not exporter.get_finished_spans()


# ---------------------------------------------------------------------------
# Parity 4: the dispatch-capability ACCEPT set must be buildable by the hub
# ---------------------------------------------------------------------------


def test_every_id_the_dispatch_accept_set_names_is_a_hub_buildable_type() -> None:
    """FAR-1141 FIX 3: ``_HUB_CI_RUNNER_TYPE_IDS`` is authoritative only if
    every id in it is one ``connector_hub._build_connector`` can actually build.

    The set carries ids the ``ConnectorType`` enum does not contain, which is
    exactly why a stale entry is dangerous: ``connector_type_supports_dispatch``
    short-circuits to ``True`` on membership, so an id the hub has NO arm for
    (the library's ``ci_runner`` family label) would be accepted at save time
    and then raise ``Unknown connector type`` on the first run. This pins the
    set to the hub's real ``case`` arms, in both directions.
    """
    from modulo.connectors.base import (
        _HUB_CI_RUNNER_TYPE_IDS,
        connector_type_supports_dispatch,
    )
    from modulo.core.connector_hub import _build_connector

    assert frozenset({"github_actions_ci", "gitlab_ci"}) == _HUB_CI_RUNNER_TYPE_IDS
    for type_id in sorted(_HUB_CI_RUNNER_TYPE_IDS):
        built = _build_connector(type_id, {}, {"token": "test-token"})  # nosec - fake credential
        assert isinstance(built, CIRunnerBase), f"{type_id!r} must build a CI runner"
        assert connector_type_supports_dispatch(type_id) is True

    # The library's family label has no hub arm: building it must fail, so the
    # capability check must fail CLOSED rather than admit it.
    with pytest.raises(ValueError, match="Unknown connector type"):
        _build_connector("ci_runner", {}, {"token": "test-token"})
    assert connector_type_supports_dispatch("ci_runner") is False
