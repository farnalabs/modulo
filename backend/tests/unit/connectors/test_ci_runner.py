"""Unit tests for CI-runner connectors using test doubles."""

import httpx
import pytest
import respx

from modulo.connectors.base import CIRunStatus, ConnectorType
from modulo.connectors.ci_runner import GitHubActionsCIRunner, GitLabCIRunner
from modulo.connectors.ci_runner.base import ConnectorTypeError
from modulo.connectors.ci_runner.github_actions import _GitHubActionsTestDouble
from modulo.connectors.ci_runner.gitlab_ci import _GitLabCITestDouble

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gh_runner():
    return GitHubActionsCIRunner(token="ghp_test")


@pytest.fixture
def gl_runner():
    return GitLabCIRunner(token="glpat_test")


@pytest.fixture
def gh_double():
    return _GitHubActionsTestDouble()


@pytest.fixture
def gl_double():
    return _GitLabCITestDouble()


# ---------------------------------------------------------------------------
# Connector type identity
# ---------------------------------------------------------------------------


def test_github_actions_connector_type(gh_runner):
    assert gh_runner.connector_type == ConnectorType.CI_RUNNER


def test_gitlab_ci_connector_type(gl_runner):
    assert gl_runner.connector_type == ConnectorType.CI_RUNNER


# ---------------------------------------------------------------------------
# GitHub Actions — health check (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gh_health_check_ok(gh_runner):
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"login": "octocat"}))
    result = await gh_runner.health_check()
    assert result.ok is True


@respx.mock
async def test_gh_health_check_fail(gh_runner):
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await gh_runner.health_check()
    assert result.ok is False
    assert "401" in result.detail


# ---------------------------------------------------------------------------
# GitHub Actions — trigger_run (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gh_trigger_run_workflow_dispatch(gh_runner):
    respx.post("https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/dispatches").mock(
        return_value=httpx.Response(204)
    )
    respx.get("https://api.github.com/repos/owner/repo/actions/runs").mock(
        return_value=httpx.Response(
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
        )
    )
    run = await gh_runner.trigger_run(
        pipeline_id="owner/repo/ci.yml",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.pipeline_id == "ci.yml"
    assert run.status == CIRunStatus.QUEUED


@respx.mock
async def test_gh_trigger_run_corrupt_workflow_runs_no_crash(gh_runner):
    """A corrupt/hostile runs response must not crash trigger_run — fall back to pending."""
    respx.post("https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/dispatches").mock(
        return_value=httpx.Response(204)
    )
    respx.get("https://api.github.com/repos/owner/repo/actions/runs").mock(
        return_value=httpx.Response(200, json=["corrupt", "list"])
    )
    run = await gh_runner.trigger_run(
        pipeline_id="owner/repo/ci.yml",
        branch="main",
    )
    assert run.status == CIRunStatus.PENDING
    assert not run.id


# ---------------------------------------------------------------------------
# GitHub Actions — get_run_status (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gh_get_run_status_success(gh_runner):
    respx.get("https://api.github.com/repos/owner/repo/actions/runs/12345").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "workflow_id": "ci.yml",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/owner/repo/actions/runs/12345",
                "head_branch": "main",
                "head_sha": "abc123",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:01:00Z",
                "actor": {"login": "octocat"},
            },
        )
    )
    run = await gh_runner.get_run_status("owner/repo/12345")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "12345"


@respx.mock
async def test_gh_get_run_status_failure(gh_runner):
    respx.get("https://api.github.com/repos/owner/repo/actions/runs/12345").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "workflow_id": "ci.yml",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/owner/repo/actions/runs/12345",
                "head_branch": "main",
                "head_sha": "abc123",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:01:00Z",
                "actor": {"login": "octocat"},
            },
        )
    )
    run = await gh_runner.get_run_status("owner/repo/12345")
    assert run.status == CIRunStatus.FAILURE


@respx.mock
async def test_gh_get_run_status_in_progress(gh_runner):
    respx.get("https://api.github.com/repos/owner/repo/actions/runs/12345").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "workflow_id": "ci.yml",
                "status": "in_progress",
                "html_url": "https://github.com/owner/repo/actions/runs/12345",
                "head_branch": "main",
                "head_sha": "abc123",
                "actor": {"login": "octocat"},
            },
        )
    )
    run = await gh_runner.get_run_status("owner/repo/12345")
    assert run.status == CIRunStatus.IN_PROGRESS


@respx.mock
async def test_gh_get_run_status_invalid_id_raises(gh_runner):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await gh_runner.get_run_status("bogus")


# ---------------------------------------------------------------------------
# GitHub Actions — get_run_logs (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gh_get_run_logs(gh_runner):
    log_text = "line1\nline2\nline3\n"
    respx.get("https://api.github.com/repos/owner/repo/actions/runs/12345/logs").mock(
        return_value=httpx.Response(200, text=log_text)
    )
    logs = await gh_runner.get_run_logs("owner/repo/12345")
    assert len(logs.lines) == 3
    assert logs.lines[0] == "line1"


@respx.mock
async def test_gh_get_run_logs_cursor_accumulates_offset(gh_runner):
    """next_cursor must keep the cumulative start_line offset, not reset it."""
    respx.get("https://api.github.com/repos/owner/repo/actions/runs/12345/logs").mock(
        return_value=httpx.Response(200, text="line5\nline6\n")
    )
    logs = await gh_runner.get_run_logs("owner/repo/12345", cursor="4")
    assert logs.lines == ["line5", "line6"]
    assert logs.next_cursor == "6"


# ---------------------------------------------------------------------------
# GitHub Actions — list_runs (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gh_list_runs(gh_runner):
    respx.get("https://api.github.com/repos/owner/repo/actions/runs").mock(
        return_value=httpx.Response(
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
        )
    )
    runs = await gh_runner.list_runs(pipeline_id="owner/repo")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


@respx.mock
async def test_gh_list_runs_corrupt_body_no_crash(gh_runner):
    """A corrupt/hostile non-dict body must degrade to an empty run list."""
    respx.get("https://api.github.com/repos/owner/repo/actions/runs").mock(
        return_value=httpx.Response(200, json=["not-a-dict"])
    )
    runs = await gh_runner.list_runs(pipeline_id="owner/repo")
    assert runs == []


@respx.mock
async def test_gh_list_runs_non_list_workflow_runs_no_crash(gh_runner):
    """A corrupt body placing a non-list in ``workflow_runs`` must fall back to an empty list."""
    respx.get("https://api.github.com/repos/owner/repo/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": "corrupt"})
    )
    runs = await gh_runner.list_runs(pipeline_id="owner/repo")
    assert runs == []


# ---------------------------------------------------------------------------
# GitLab CI — health check (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gl_health_check_ok(gl_runner):
    respx.get("https://gitlab.com/api/v4/projects?per_page=1").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await gl_runner.health_check()
    assert result.ok is True


@respx.mock
async def test_gl_health_check_fail(gl_runner):
    respx.get("https://gitlab.com/api/v4/projects?per_page=1").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await gl_runner.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


# ---------------------------------------------------------------------------
# GitLab CI — trigger_run (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gl_trigger_run(gl_runner):
    respx.post("https://gitlab.com/api/v4/projects/12345/pipeline").mock(
        return_value=httpx.Response(
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
        )
    )
    run = await gl_runner.trigger_run(
        pipeline_id="12345",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.PENDING
    assert run.pipeline_id == "12345"


# ---------------------------------------------------------------------------
# GitLab CI — get_run_status (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gl_get_run_status(gl_runner):
    respx.get("https://gitlab.com/api/v4/projects/12345/pipelines/67890").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 67890,
                "project_id": 12345,
                "status": "running",
                "web_url": "https://gitlab.com/owner/repo/-/pipelines/67890",
                "ref": "main",
                "sha": "abc123",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "user": {"username": "developer"},
            },
        )
    )
    run = await gl_runner.get_run_status("12345/67890")
    assert run.status == CIRunStatus.IN_PROGRESS


@respx.mock
async def test_gl_get_run_status_invalid_id_raises(gl_runner):
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await gl_runner.get_run_status("bogus")


# ---------------------------------------------------------------------------
# GitLab CI — get_run_logs (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gl_get_run_logs(gl_runner):
    respx.get("https://gitlab.com/api/v4/projects/12345/pipelines/67890/jobs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 111, "name": "build"},
                {"id": 222, "name": "test"},
            ],
        )
    )
    respx.get("https://gitlab.com/api/v4/projects/12345/jobs/111/trace").mock(
        return_value=httpx.Response(200, text="Build log line 1\nBuild log line 2\n")
    )
    respx.get("https://gitlab.com/api/v4/projects/12345/jobs/222/trace").mock(
        return_value=httpx.Response(200, text="Test log line 1\nTest log line 2\n")
    )
    logs = await gl_runner.get_run_logs("12345/67890")
    assert len(logs.lines) >= 6
    assert any("build" in line.lower() for line in logs.lines)
    assert any("Build log line 1" in line for line in logs.lines)


@respx.mock
async def test_gl_get_run_logs_cursor_accumulates_offset(gl_runner):
    """next_cursor must add the previous cumulative offset, not reset it."""
    respx.get("https://gitlab.com/api/v4/projects/12345/pipelines/67890/jobs").mock(
        return_value=httpx.Response(200, json=[{"id": 111, "name": "build"}])
    )
    respx.get("https://gitlab.com/api/v4/projects/12345/jobs/111/trace").mock(
        return_value=httpx.Response(200, text="line7\nline8\n")
    )
    logs = await gl_runner.get_run_logs("12345/67890", cursor="5")
    assert logs.next_cursor == "9"


# ---------------------------------------------------------------------------
# GitLab CI — list_runs (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_gl_list_runs(gl_runner):
    respx.get("https://gitlab.com/api/v4/projects/12345/pipelines").mock(
        return_value=httpx.Response(
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
        )
    )
    runs = await gl_runner.list_runs(pipeline_id="12345")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


# ---------------------------------------------------------------------------
# GitHub Actions — test double
# ---------------------------------------------------------------------------


async def test_gh_double_trigger_run(gh_double):
    run = await gh_double.trigger_run(
        pipeline_id="owner/repo/ci.yml",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.QUEUED
    assert len(gh_double._triggered) == 1


async def test_gh_double_get_run_status(gh_double):
    run = await gh_double.get_run_status("owner/repo/12345")
    assert run.status == CIRunStatus.QUEUED


async def test_gh_double_get_run_logs(gh_double):
    gh_double._run_logs = ["line1", "line2"]
    logs = await gh_double.get_run_logs("owner/repo/12345")
    assert logs.lines == ["line1", "line2"]


async def test_gh_double_list_runs(gh_double):
    runs = await gh_double.list_runs(pipeline_id="owner/repo/ci.yml")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


async def test_gh_double_health_check(gh_double):
    result = await gh_double.health_check()
    assert result.ok is True


# ---------------------------------------------------------------------------
# GitLab CI — test double
# ---------------------------------------------------------------------------


async def test_gl_double_trigger_run(gl_double):
    run = await gl_double.trigger_run(
        pipeline_id="12345",
        branch="main",
        variables={"KEY": "value"},
    )
    assert run.status == CIRunStatus.QUEUED
    assert len(gl_double._triggered) == 1


async def test_gl_double_get_run_status(gl_double):
    run = await gl_double.get_run_status("12345/67890")
    assert run.status == CIRunStatus.QUEUED


async def test_gl_double_get_run_logs(gl_double):
    gl_double._run_logs = ["line1", "line2"]
    logs = await gl_double.get_run_logs("12345/67890")
    assert logs.lines == ["line1", "line2"]


async def test_gl_double_list_runs(gl_double):
    runs = await gl_double.list_runs(pipeline_id="12345")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


async def test_gl_double_health_check(gl_double):
    result = await gl_double.health_check()
    assert result.ok is True


# ---------------------------------------------------------------------------
# Corrupt-payload guards — _parse_run must never raise on malformed responses
# ---------------------------------------------------------------------------


def test_gl_parse_run_non_dict_user(gl_runner):
    run = gl_runner._parse_run({"id": 1, "user": "alice"})
    assert not run.triggered_by


def test_gl_parse_run_null_user(gl_runner):
    run = gl_runner._parse_run({"id": 1, "user": None})
    assert not run.triggered_by


def test_gl_parse_run_corrupt_duration(gl_runner):
    run = gl_runner._parse_run({"id": 1, "duration": "not-a-number"})
    assert run.duration_seconds == 0


def test_gl_parse_run_non_finite_duration(gl_runner):
    run = gl_runner._parse_run({"id": 1, "duration": float("nan")})
    assert run.duration_seconds == 0


def test_gh_parse_run_non_dict_actor(gh_runner):
    run = gh_runner._parse_run({"id": 1, "actor": "alice"})
    assert not run.triggered_by


def test_gh_parse_run_null_actor(gh_runner):
    run = gh_runner._parse_run({"id": 1, "actor": None})
    assert not run.triggered_by


# ---------------------------------------------------------------------------
# CIRunStatus StrEnum
# ---------------------------------------------------------------------------


def test_ci_run_status_values():
    assert CIRunStatus.PENDING.value == "pending"
    assert CIRunStatus.SUCCESS.value == "success"
    assert CIRunStatus.FAILURE.value == "failure"
    assert CIRunStatus.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# Base class — query/write not implemented
# ---------------------------------------------------------------------------


async def test_ci_runner_query_not_implemented(gh_runner):
    with pytest.raises(ConnectorTypeError):
        await gh_runner.query(None)  # type: ignore[arg-type]


async def test_ci_runner_write_not_implemented(gh_runner):
    with pytest.raises(ConnectorTypeError):
        await gh_runner.write(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SSRF egress gate — gitlab_ci runner (CHANGES_REQUESTED #3)
# ---------------------------------------------------------------------------


@pytest.mark.real_ssrf_dns
async def test_gitlab_ci_refuses_blocked_egress(monkeypatch):
    """The GitLab CI runner (gitlab_ci) must gate a tenant-supplied base_url.

    A tenant pointing ``base_url`` at the cloud-metadata address must not get a
    usable client, and ``health_check`` must report unhealthy rather than raise.
    Removing the ``validate_outbound_url`` call from ``GitLabCIRunner._client``
    makes the first assertion fail — that is the prove-the-fix contract.
    """
    monkeypatch.delenv("SSRF_ALLOW_PRIVATE_RANGES", raising=False)
    runner = GitLabCIRunner(token="glpat_test", base_url="http://169.254.169.254")
    with pytest.raises(ValueError, match="private/internal"):
        runner._client()

    result = await runner.health_check()
    assert result.ok is False
    assert "169.254.169.254" in result.detail
