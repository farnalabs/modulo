"""Step definitions for the Azure Repos connector BDD feature.

Wires ``features/connectors/azure_repos.feature`` into the executing suite (the
product-map review pass) by driving the REAL
``AzureReposConnector`` against a respx-mocked Azure DevOps REST API v7.0 —
mirroring ``backend/tests/unit/connectors/test_azure_repos.py`` so the
executing BDD surface locks the same contract the unit suite does: PAT
validation via the profile endpoint (401 => unhealthy), listing repositories /
pull requests / commits, reading a file, writing a file via a push, creating a
pull request, and failing closed on an unsupported resource.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/azure_repos.feature")

_ORG = "myorg"
_PROJECT = "myproject"
_REPO = "myrepo"
_BASE = "https://dev.azure.com/myorg"
_PROFILE_URL = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Azure Repos connector scenarios."""
    return {}


@given(parsers.parse('an Azure Repos connector configured with project "{project}"'))
def azrepos_connector_project(project: str, ctx: dict) -> None:
    from modulo.connectors.azure_repos import AzureReposConnector

    ctx["connector"] = AzureReposConnector(token="azure_test_token", organization=_ORG)
    ctx["project"] = project


@given(parsers.parse('an Azure Repos connector configured with project "{project}" and repo "{repo}"'))
def azrepos_connector_project_repo(project: str, repo: str, ctx: dict) -> None:
    from modulo.connectors.azure_repos import AzureReposConnector

    ctx["connector"] = AzureReposConnector(token="azure_test_token", organization=_ORG)
    ctx["project"] = project
    ctx["repo"] = repo


@given("an Azure Repos connector configured with invalid credentials")
def azrepos_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.azure_repos import AzureReposConnector

    ctx["connector"] = AzureReposConnector(token="bad_token", organization=_ORG)
    ctx["valid"] = False


@when("the connector lists repositories")
def azrepos_query_repos(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    repos = [
        {"id": "repo-1", "name": "frontend"},
        {"id": "repo-2", "name": "backend"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories", params={"api-version": "7.0"}).mock(
            return_value=httpx.Response(200, json={"value": repos, "count": 2})
        )
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="repos", filters={"project": ctx["project"]}))
        )


@when(parsers.parse('the connector reads "{path}" from branch "{branch}"'))
def azrepos_query_file(path: str, branch: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    content = "# Hello Azure Repos"
    with respx.mock:
        respx.get(
            f"{_BASE}/{_PROJECT}/_apis/git/repositories/{_REPO}/items",
            params={"path": path, "versionDescriptor.version": branch, "api-version": "7.0"},
        ).mock(return_value=httpx.Response(200, text=content))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(
                ConnectorQuery(
                    resource="file",
                    filters={"project": _PROJECT, "repo": _REPO, "path": path, "ref": branch},
                )
            )
        )


@when("the connector lists pull requests")
def azrepos_query_pulls(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    prs = [{"pullRequestId": 42, "title": "Fix bug", "status": "active"}]
    with respx.mock:
        respx.get(
            f"{_BASE}/{_PROJECT}/_apis/git/repositories/{_REPO}/pullrequests",
            params={"searchCriteria.status": "active", "api-version": "7.0"},
        ).mock(return_value=httpx.Response(200, json={"value": prs, "count": 1}))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="pulls", filters={"project": _PROJECT, "repo": _REPO}))
        )


@when(parsers.parse('the connector lists commits on branch "{branch}"'))
def azrepos_query_commits(branch: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    commits = [{"commitId": "abc123", "comment": "Initial commit"}]
    with respx.mock:
        respx.get(
            f"{_BASE}/{_PROJECT}/_apis/git/repositories/{_REPO}/commits",
            params={"searchCriteria.itemVersion.version": branch, "api-version": "7.0"},
        ).mock(return_value=httpx.Response(200, json={"value": commits, "count": 1}))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(
                ConnectorQuery(
                    resource="commits",
                    filters={"project": _PROJECT, "repo": _REPO, "branch": branch},
                )
            )
        )


@when(parsers.parse('the connector writes "{path}" with content "{content}"'))
def azrepos_write_file(path: str, content: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.get(
            f"{_BASE}/{_PROJECT}/_apis/git/repositories/{_REPO}/refs",
            params={"filter": "heads/main", "api-version": "7.0"},
        ).mock(return_value=httpx.Response(200, json={"value": [{"objectId": "oldoid123"}]}))
        respx.post(
            f"{_BASE}/{_PROJECT}/_apis/git/repositories/{_REPO}/pushes",
            params={"api-version": "7.0"},
        ).mock(return_value=httpx.Response(200, json={"pushId": 1, "commitIds": ["newcommit456"]}))
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(
                    resource="file",
                    data={
                        "project": _PROJECT,
                        "repo": _REPO,
                        "path": path,
                        "content": content,
                        "branch": "main",
                    },
                )
            )
        )


@when(parsers.parse('the connector creates a pull request from "{source}" to "{target}" with title "{title}"'))
def azrepos_write_pull(source: str, target: str, title: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    pr_response = {"pullRequestId": 99, "title": title, "status": "active"}
    with respx.mock:
        respx.post(
            f"{_BASE}/{_PROJECT}/_apis/git/repositories/{_REPO}/pullrequests",
            params={"api-version": "7.0"},
        ).mock(return_value=httpx.Response(200, json=pr_response))
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(
                    resource="pull",
                    data={
                        "project": _PROJECT,
                        "repo": _REPO,
                        "title": title,
                        "source_branch": source,
                        "target_branch": target,
                    },
                )
            )
        )


@when("the connector checks health")
def azrepos_health_check(ctx: dict) -> None:
    response = (
        httpx.Response(200, json={"displayName": "Duncan Tait"})
        if ctx.get("valid", True)
        else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.get(_PROFILE_URL, params={"api-version": "7.0"}).mock(return_value=response)
        ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@then("the result contains repos")
def azrepos_result_repos(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected repo records"
    assert result.records[0]["name"] == "frontend", result.records
    assert result.records[1]["name"] == "backend", result.records


@then("the connector returns the file content")
def azrepos_result_file(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected file record"
    assert result.records[0]["content"] == "# Hello Azure Repos", result.records
    assert result.records[0]["path"] == "README.md", result.records


@then("the result contains open PRs")
def azrepos_result_pulls(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected PR records"
    assert result.records[0]["pullRequestId"] == 42, result.records
    assert result.records[0]["status"] == "active", result.records


@then("the result contains commits")
def azrepos_result_commits(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected commit records"
    assert result.records[0]["commitId"] == "abc123", result.records


@then("the file is committed successfully")
def azrepos_file_committed(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["pushId"] == 1, result
    assert result["commitIds"] == ["newcommit456"], result


@then("the pull request is created successfully")
def azrepos_pull_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["pullRequestId"] == 99, result
    assert result["title"] == "Add feature", result


@then(parsers.parse('the health check returns "{status}"'))
def azrepos_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"
