"""Error-path coverage for FAR-1651 health-detail credential redaction.

The happy-path and non-2xx detail redaction are covered in
``test_health_detail_credential_redaction.py`` and
``test_connector_credential_redaction.py``. This module exercises the
EXCEPTION branches — transport errors, ``HTTPStatusError``, ``ValueError`` and
generic ``Exception`` — of the health-check / CI-runner methods whose error
detail was rewired through ``_redacted_detail`` (FAR-1651). Each test proves
the changed branch runs AND that the credential is masked, so the coverage
gate cannot be satisfied by a branch that merely executes without redacting.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from modulo.connectors.bitbucket import BitbucketConnector
from modulo.connectors.ci_runner import GitHubActionsCIRunner, GitLabCIRunner
from modulo.connectors.dropbox_paper import DropboxPaperConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.linear import LinearConnector
from modulo.connectors.monday import MondayConnector
from modulo.connectors.notion import NotionConnector
from modulo.connectors.sharepoint import SharePointConnector

_PLANTED = "far1651plantedcredentialvalue"

_BITBUCKET_API = "https://api.bitbucket.org/2.0"
_DROPBOX_API = "https://api.dropboxapi.com/2"
_NOTION_API = "https://api.notion.com/v1"
_SHAREPOINT_API = "https://graph.microsoft.com/v1.0"
_GH_API = "https://api.github.com"
_GL_API = "https://gitlab.com/api/v4"


def _assert_redacted(text: str) -> None:
    assert _PLANTED not in text
    assert "***" in text


# ---------------------------------------------------------------------------
# Bitbucket — recipe wire forms + ValueError health-check branch
# ---------------------------------------------------------------------------


def test_bitbucket_credential_values_bearer_is_single_value() -> None:
    connector = BitbucketConnector(token=_PLANTED)
    assert connector._credential_values() == (_PLANTED,)


def test_bitbucket_credential_values_basic_includes_wire_forms() -> None:
    connector = BitbucketConnector(username="user", app_password=_PLANTED)
    values = connector._credential_values()
    assert _PLANTED in values
    assert any(v.startswith("Basic ") for v in values)


@respx.mock
async def test_bitbucket_health_check_value_error_redacts_detail() -> None:
    connector = BitbucketConnector(token=_PLANTED)
    respx.get(f"{_BITBUCKET_API}/user").mock(side_effect=ValueError(f"bad request {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


# ---------------------------------------------------------------------------
# Health-check ValueError branches (health_check_failure path)
# ---------------------------------------------------------------------------


@respx.mock
async def test_dropbox_paper_health_check_value_error_redacts_detail() -> None:
    connector = DropboxPaperConnector(token=_PLANTED)
    respx.post(f"{_DROPBOX_API}/users/get_current_account").mock(side_effect=ValueError(f"bad request {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


@respx.mock
async def test_notion_health_check_value_error_redacts_detail() -> None:
    connector = NotionConnector(token=_PLANTED)
    respx.get(f"{_NOTION_API}/users").mock(side_effect=ValueError(f"bad request {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


@respx.mock
async def test_sharepoint_health_check_value_error_redacts_detail() -> None:
    connector = SharePointConnector(token=_PLANTED)
    respx.get(f"{_SHAREPOINT_API}/sites/root").mock(side_effect=ValueError(f"bad request {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


# ---------------------------------------------------------------------------
# Generic-Exception health-check branches (jira / linear / monday)
# ---------------------------------------------------------------------------


async def _raise_runtime_error(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError(f"upstream broke {_PLANTED}")


async def test_jira_health_check_generic_exception_redacts_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = JiraConnector(instance="example.atlassian.net", creds={"token": _PLANTED})
    monkeypatch.setattr(connector, "_call_api", _raise_runtime_error)
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


async def test_linear_health_check_generic_exception_redacts_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = LinearConnector(token=_PLANTED)
    monkeypatch.setattr(connector, "_graphql", _raise_runtime_error)
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


async def test_monday_health_check_generic_exception_redacts_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = MondayConnector(api_key=_PLANTED)
    monkeypatch.setattr(connector, "_graphql", _raise_runtime_error)
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


# ---------------------------------------------------------------------------
# GitLab connector — _projects_health_result non-401/403 failure branch
# ---------------------------------------------------------------------------


def test_gitlab_projects_health_result_non_auth_failure_redacts() -> None:
    connector = GitLabConnector(token=_PLANTED)
    response = httpx.Response(500, text=f"projects API boom {_PLANTED}")
    result = connector._projects_health_result(response)
    assert result is not None
    assert result.ok is False
    _assert_redacted(result.detail)


# ---------------------------------------------------------------------------
# GitHub Actions CI runner — HTTPError + HTTPStatusError branches
# ---------------------------------------------------------------------------


@respx.mock
async def test_gh_health_check_transport_error_redacts_detail() -> None:
    connector = GitHubActionsCIRunner(token=_PLANTED)
    respx.get(f"{_GH_API}/user").mock(side_effect=httpx.ConnectError(f"boom {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


@respx.mock
async def test_gh_trigger_run_http_status_error_redacts() -> None:
    connector = GitHubActionsCIRunner(token=_PLANTED)
    respx.post(f"{_GH_API}/repos/owner/repo/actions/workflows/ci.yml/dispatches").mock(
        return_value=httpx.Response(500, text=f"dispatch boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.trigger_run("owner/repo/ci.yml")
    _assert_redacted(str(exinfo.value))


@respx.mock
async def test_gh_get_run_status_http_status_error_redacts() -> None:
    connector = GitHubActionsCIRunner(token=_PLANTED)
    respx.get(f"{_GH_API}/repos/owner/repo/actions/runs/12345").mock(
        return_value=httpx.Response(500, text=f"status boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.get_run_status("owner/repo/12345")
    _assert_redacted(str(exinfo.value))


@respx.mock
async def test_gh_get_run_logs_http_status_error_redacts() -> None:
    connector = GitHubActionsCIRunner(token=_PLANTED)
    respx.get(f"{_GH_API}/repos/owner/repo/actions/runs/12345/logs").mock(
        return_value=httpx.Response(500, text=f"logs boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.get_run_logs("owner/repo/12345")
    _assert_redacted(str(exinfo.value))


@respx.mock
async def test_gh_list_runs_http_status_error_redacts() -> None:
    connector = GitHubActionsCIRunner(token=_PLANTED)
    respx.get(f"{_GH_API}/repos/owner/repo/actions/runs").mock(
        return_value=httpx.Response(500, text=f"list boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.list_runs("owner/repo")
    _assert_redacted(str(exinfo.value))


# ---------------------------------------------------------------------------
# GitLab CI runner — non-2xx + HTTPError + HTTPStatusError branches
# ---------------------------------------------------------------------------


@respx.mock
async def test_gl_health_check_non_2xx_redacts_detail() -> None:
    connector = GitLabCIRunner(token=_PLANTED)
    respx.get(f"{_GL_API}/projects").mock(return_value=httpx.Response(500, text=f"boom {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


@respx.mock
async def test_gl_health_check_transport_error_redacts_detail() -> None:
    connector = GitLabCIRunner(token=_PLANTED)
    respx.get(f"{_GL_API}/projects").mock(side_effect=httpx.ConnectError(f"boom {_PLANTED}"))
    result = await connector.health_check()
    assert result.ok is False
    _assert_redacted(result.detail)


@respx.mock
async def test_gl_trigger_run_http_status_error_redacts() -> None:
    connector = GitLabCIRunner(token=_PLANTED)
    respx.post(f"{_GL_API}/projects/123/pipeline").mock(
        return_value=httpx.Response(500, text=f"dispatch boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.trigger_run("123")
    _assert_redacted(str(exinfo.value))


@respx.mock
async def test_gl_get_run_status_http_status_error_redacts() -> None:
    connector = GitLabCIRunner(token=_PLANTED)
    respx.get(f"{_GL_API}/projects/123/pipelines/456").mock(
        return_value=httpx.Response(500, text=f"status boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.get_run_status("123/456")
    _assert_redacted(str(exinfo.value))


@respx.mock
async def test_gl_get_run_logs_http_status_error_redacts() -> None:
    connector = GitLabCIRunner(token=_PLANTED)
    respx.get(f"{_GL_API}/projects/123/pipelines/456/jobs").mock(
        return_value=httpx.Response(500, text=f"logs boom {_PLANTED}")
    )
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.get_run_logs("123/456")
    _assert_redacted(str(exinfo.value))


@respx.mock
async def test_gl_list_runs_http_status_error_redacts() -> None:
    connector = GitLabCIRunner(token=_PLANTED)
    respx.get(f"{_GL_API}/projects/123/pipelines").mock(return_value=httpx.Response(500, text=f"list boom {_PLANTED}"))
    with pytest.raises(ValueError, match="API error") as exinfo:
        await connector.list_runs("123")
    _assert_redacted(str(exinfo.value))
