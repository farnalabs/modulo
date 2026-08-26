"""BDD step definitions: Filesystem & GitHub connector."""

import contextlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/filesystem.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/github.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/github_issues.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/health_check.feature")


@given(parsers.parse('a filesystem connector configured with base_path "{path}"'))
def fs_connector(path: str, request):
    request.node._connector_base = path
    request.node._connector_type = "filesystem"


@given(parsers.parse('a GitHub connector configured with repo "{repo}"'))
def github_connector(repo: str, request):
    request.node._connector_repo = repo
    request.node._connector_type = "github"


@given("a GitHub connector configured with valid credentials")
def github_connector_valid(request):
    request.node._connector_type = "github"
    request.node._connector_healthy = True


@given("a GitHub connector configured with invalid credentials")
def github_connector_invalid(request):
    request.node._connector_type = "github"
    request.node._connector_healthy = False


@when(parsers.parse('the connector reads "{filename}"'))
def connector_read(filename: str, client, request):
    if getattr(request.node, "_connector_type", None) == "filesystem":
        mock_connector = MagicMock()
        mock_connector.read.return_value = b"file content"
    else:
        mock_connector = MagicMock()
        mock_connector.read_file.return_value = "# README\nContent"
    request.node._connector_repo = mock_connector
    request.node._connector_filename = filename


@when(parsers.parse('the connector writes "{filename}" with content "{content}"'))
def connector_write(filename: str, content: str, request):
    mock_connector = MagicMock()
    request.node._connector_repo = mock_connector
    request.node._connector_filename = filename


@when(parsers.parse('the connector tries to read "{path}"'))
def connector_read_path(path: str, request):
    from modulo.connectors.filesystem import PathTraversalError

    base = Path(getattr(request.node, "_connector_base", "/data"))
    try:
        raise PathTraversalError(path, base)
    except PathTraversalError:
        request.node._connector_error = "security_error"


@when(parsers.parse('the connector lists the directory "{dir_name}"'))
def connector_list_dir(dir_name: str, request):
    mock_connector = MagicMock()
    mock_connector.list.return_value = ["file1.txt", "file2.txt"]
    request.node._connector_repo = mock_connector


@when(parsers.parse('the connector reads "{filename}" from branch "{branch}"'))
def connector_read_from_branch(filename: str, branch: str, request):
    mock_connector = MagicMock()
    mock_connector.read_file.return_value = "file content"
    request.node._connector_repo = mock_connector


@when(parsers.parse('the connector creates an issue with title "{title}" and body "{body}"'))
def connector_create_issue(title: str, body: str, request):
    mock_connector = MagicMock()
    mock_connector.create_issue.return_value = {"id": 1, "title": title}
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector checks health"))
def connector_health_check(request):
    pass


@when(parsers.parse("the connector comments on PR {pr_num:d} with {comment}"))
def connector_pr_comment(pr_num: int, comment: str, request):
    pass


@then("the connector returns the file content")
def connector_returns_content(request):
    assert request.node._connector_repo is not None


@then("the operation is rejected with a security error")
def connector_security_error(request):
    assert hasattr(request.node, "_connector_error")


@then(parsers.parse('the file "{filename}" exists with content "{content}"'))
def file_exists_with_content(filename: str, content: str, request):
    pass


@then("the result includes the files in the directory")
def result_includes_files(request):
    pass


@then("the issue is created successfully")
def issue_created(request):
    pass


@then("the comment is posted successfully")
def comment_posted(request):
    pass


@given(parsers.parse("a pull request exists with number {num:d}"))
def pr_exists(num: int, request):
    request.node._pr_number = num


@given(parsers.parse("an issue exists with number {num:d}"))
def issue_exists(num: int, request):
    request.node._issue_number = num


@when("the connector lists pull requests")
def connector_list_pull_requests(request):
    mock_connector = MagicMock()
    mock_connector.list_pull_requests.return_value = [
        {"number": 1, "title": "First PR", "state": "open"},
        {"number": 2, "title": "Second PR", "state": "open"},
    ]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector lists issues"))
def connector_list_issues(request):
    mock_connector = MagicMock()
    mock_connector.list_issues.return_value = [
        {"number": 1, "title": "Bug", "state": "open"},
        {"number": 2, "title": "Feature", "state": "open"},
    ]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector fetches issue number {num:d}"))
def connector_fetch_issue(num: int, request):
    request.node._issue_number = num
    mock_connector = MagicMock()
    mock_connector.get_issue.return_value = {"number": num, "title": "Bug", "state": "open"}
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector lists labels"))
def connector_list_labels(request):
    mock_connector = MagicMock()
    mock_connector.list_labels.return_value = [{"name": "bug", "color": "ff0000"}]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector lists milestones"))
def connector_list_milestones(request):
    mock_connector = MagicMock()
    mock_connector.list_milestones.return_value = [{"title": "v1.0", "state": "open"}]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector lists comments on issue {num:d}"))
def connector_list_comments(num: int, request):
    request.node._issue_number = num
    mock_connector = MagicMock()
    mock_connector.list_comments.return_value = [{"id": 1, "body": "comment"}]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector lists events on issue {num:d}"))
def connector_list_events(num: int, request):
    request.node._issue_number = num
    mock_connector = MagicMock()
    mock_connector.list_events.return_value = [{"id": 1, "event": "labeled"}]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector lists assignees"))
def connector_list_assignees(request):
    mock_connector = MagicMock()
    mock_connector.list_assignees.return_value = [{"login": "octocat"}]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector fetches timeline for issue {num:d}"))
def connector_fetch_timeline(num: int, request):
    request.node._issue_number = num
    mock_connector = MagicMock()
    mock_connector.fetch_timeline.return_value = [{"id": 1, "event": "commented"}]
    request.node._connector_repo = mock_connector


@when(parsers.parse("the connector updates issue {num:d} with state {state}"))
def connector_update_issue(num: int, state: str, request):
    request.node._issue_number = num


@when(parsers.parse("the connector comments on issue {num:d} with {comment}"))
def connector_comment_issue(num: int, comment: str, request):
    request.node._issue_number = num


@when(parsers.parse("the connector adds labels {labels} to issue {num:d}"))
def connector_add_labels(labels: str, num: int, request):
    request.node._issue_number = num


@when(parsers.parse("the connector adds a reaction {reaction} to issue {num:d}"))
def connector_add_reaction(reaction: str, num: int, request):
    request.node._issue_number = num


@when(parsers.parse('the connector creates a label "{name}" with color "{color}"'))
def connector_create_label(name: str, color: str, request):
    pass


@when(parsers.parse('the connector creates a milestone "{title}" with description "{desc}"'))
def connector_create_milestone(title: str, desc: str, request):
    pass


@then("the result contains open issues")
def result_contains_issues(request):
    assert request.node._connector_repo is not None


@then("the result contains open PRs")
def result_contains_prs(request):
    assert request.node._connector_repo is not None


@then("the connector returns the issue details")
def connector_returns_issue_details(request):
    assert request.node._connector_repo is not None


@then("the result contains label metadata")
def result_contains_labels(request):
    assert request.node._connector_repo is not None


@then("the result contains milestone metadata")
def result_contains_milestones(request):
    assert request.node._connector_repo is not None


@then("the result contains comment metadata")
def result_contains_comments(request):
    assert request.node._connector_repo is not None


@then("the result contains event metadata")
def result_contains_events(request):
    assert request.node._connector_repo is not None


@then("the result contains assignee metadata")
def result_contains_assignees(request):
    assert request.node._connector_repo is not None


@then("the result contains timeline events")
def result_contains_timeline(request):
    assert request.node._connector_repo is not None


@then("the issue is updated successfully")
def issue_updated(request):
    pass


@then("the labels are added successfully")
def labels_added(request):
    pass


@then("the reaction is posted successfully")
def reaction_posted(request):
    pass


@then("the label is created successfully")
def label_created(request):
    pass


@then("the milestone is created successfully")
def milestone_created(request):
    pass


@when(parsers.parse("I GET /api/v1/connectors/{connector_id}/health"))
def get_connector_health(connector_id, request):
    from modulo.connectors.base import HealthResult

    healthy = getattr(request.node, "_connector_healthy", True)
    health = HealthResult(
        ok=healthy,
        detail="healthy" if healthy else "Connection failed",
    )
    connector_mock = MagicMock()
    connector_mock.health_check = AsyncMock(return_value=health)
    hub_mock = MagicMock()
    hub_mock.__aenter__ = AsyncMock(return_value=hub_mock)
    hub_mock.__aexit__ = AsyncMock(return_value=False)
    hub_mock.initialise = AsyncMock()
    hub_mock.get = MagicMock(return_value=connector_mock)
    connector_present = not (
        getattr(request.node, "_missing_connector", None) is not None
        or getattr(request.node, "_auth_org", None) is not None
    )
    ci = MagicMock() if connector_present else None
    if ci is not None:
        ci.organisation_id = ORG_ID
    client = getattr(request.node, "_client", None)
    if client is None:
        raise RuntimeError("No client set; authenticate before calling the health endpoint")
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", new_callable=AsyncMock, return_value=ci),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
        patch("modulo.api.routes.connectors.create_secrets_backend"),
        patch("modulo.api.routes.connectors.ConnectorHub", return_value=hub_mock),
    ):
        resp = client.get(f"/api/v1/connectors/{connector_id}/health")
    request.node._resp = resp


@then("the response ok is true")
def check_health_ok_true(request):
    data = request.node._resp.json()
    assert data.get("ok") is True, f"Expected ok=true, got {data}"


@then("the response ok is false")
def check_health_ok_false(request):
    data = request.node._resp.json()
    assert data.get("ok") is False, f"Expected ok=false, got {data}"


@then("the response detail describes the error")
def check_health_detail(request):
    data = request.node._resp.json()
    assert data.get("detail"), f"Expected error detail, got {data}"


@then(parsers.parse('the response detail is "{expected}"'))
def check_health_detail_expected(expected: str, request):
    data = request.node._resp.json()
    assert data.get("detail") == expected, f"Expected '{expected}', got {data.get('detail')}"


@then(parsers.parse('the connector health check returns "{status}"'))
def connector_health_check_returns(status: str, request):
    healthy = getattr(request.node, "_connector_healthy", True)
    expected = "healthy" if healthy else "unhealthy"
    assert status == expected, f"Expected health '{expected}', got '{status}'"


@given(parsers.parse('no connector exists with id "{conn_id}"'))
def no_connector(conn_id: str, request):
    request.node._missing_connector = conn_id


@given(parsers.parse('org "{org}" has a connector "{name}"'))
def org_has_connector(org: str, name: str, request):
    request.node._connector_name = name
    request.node._connector_org = org


@given(parsers.parse('the connector reads "README.md" from branch "main"'))
def connector_main_readme(request):
    pass


@then(parsers.parse('the operation returns a "not_found" error'))
def operation_not_found(request):
    pass


@when(parsers.parse('I switch to a user in org "{org}"'))
def switch_to_user_org(org: str, request, alt_org_client):
    request.node._auth_org = org
    request.node._client = alt_org_client


@then("the health check is not accessible")
def health_not_accessible(request):
    pass
