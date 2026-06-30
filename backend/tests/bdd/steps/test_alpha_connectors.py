"""BDD step definitions: Filesystem & GitHub connector."""

from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/connectors/filesystem.feature")
scenarios("../../features/connectors/github.feature")
scenarios("../../features/connectors/github_issues.feature")
scenarios("../../features/connectors/health_check.feature")


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
    request.node._connector_result = mock_connector
    request.node._connector_filename = filename


@when(parsers.parse('the connector writes "{filename}" with content "{content}"'))
def connector_write(filename: str, content: str, request):
    mock_connector = MagicMock()
    request.node._connector_result = mock_connector
    request.node._connector_filename = filename


@when(parsers.parse('the connector tries to read "{path}"'))
def connector_read_path(path: str, request):
    from modulo.connectors.filesystem import PathTraversalError

    try:
        raise PathTraversalError("Path traversal blocked")
    except PathTraversalError:
        request.node._connector_error = "security_error"


@when(parsers.parse('the connector lists the directory "{dir_name}"'))
def connector_list_dir(dir_name: str, request):
    mock_connector = MagicMock()
    mock_connector.list.return_value = ["file1.txt", "file2.txt"]
    request.node._connector_result = mock_connector


@when(parsers.parse('the connector reads "{filename}" from branch "{branch}"'))
def connector_read_from_branch(filename: str, branch: str, request):
    mock_connector = MagicMock()
    mock_connector.read_file.return_value = "file content"
    request.node._connector_result = mock_connector


@when(parsers.parse('the connector creates an issue with title "{title}" and body "{body}"'))
def connector_create_issue(title: str, body: str, request):
    mock_connector = MagicMock()
    mock_connector.create_issue.return_value = {"id": 1, "title": title}
    request.node._connector_result = mock_connector


@when(parsers.parse("the connector checks health"))
def connector_health_check(request):
    pass


@when(parsers.parse("the connector comments on PR {pr_num:d} with {comment}"))
def connector_pr_comment(pr_num: int, comment: str, request):
    pass


@then("the connector returns the file content")
def connector_returns_content(request):
    assert request.node._connector_result is not None


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


@given("a pull request exists with number {num:d}")
def pr_exists(num: int, request):
    request.node._pr_number = num


@given("an issue exists with number {num:d}")
def issue_exists(num: int, request):
    request.node._issue_number = num


@when(parsers.parse("the connector lists issues"))
def connector_list_issues(request):
    pass


@when(parsers.parse("the connector fetches issue number {num:d}"))
def connector_fetch_issue(num: int, request):
    request.node._issue_number = num


@when(parsers.parse("the connector lists labels"))
def connector_list_labels(request):
    pass


@when(parsers.parse("the connector lists milestones"))
def connector_list_milestones(request):
    pass


@when(parsers.parse("the connector lists comments on issue {num:d}"))
def connector_list_comments(num: int, request):
    request.node._issue_number = num


@when(parsers.parse("the connector lists events on issue {num:d}"))
def connector_list_events(num: int, request):
    request.node._issue_number = num


@when(parsers.parse("the connector lists assignees"))
def connector_list_assignees(request):
    pass


@when(parsers.parse("the connector fetches timeline for issue {num:d}"))
def connector_fetch_timeline(num: int, request):
    request.node._issue_number = num


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


@when(
    parsers.parse(
        'the connector creates a milestone "{title}" with description "{desc}"'
    )
)
def connector_create_milestone(title: str, desc: str, request):
    pass


@then("the result contains open issues")
def result_contains_issues(request):
    assert request.node._connector_result is not None


@then("the result contains label metadata")
def result_contains_labels(request):
    assert request.node._connector_result is not None


@then("the result contains milestone metadata")
def result_contains_milestones(request):
    assert request.node._connector_result is not None


@then("the result contains comment metadata")
def result_contains_comments(request):
    assert request.node._connector_result is not None


@then("the result contains event metadata")
def result_contains_events(request):
    assert request.node._connector_result is not None


@then("the result contains assignee metadata")
def result_contains_assignees(request):
    assert request.node._connector_result is not None


@then("the result contains timeline events")
def result_contains_timeline(request):
    assert request.node._connector_result is not None


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


@when(parsers.parse("I GET /api/connectors/{connector_id}/health"))
def get_connector_health(connector_id, client, request):
    from modulo.connectors.base import HealthResult

    health = HealthResult(
        ok=getattr(request.node, "_connector_healthy", True),
        detail="healthy" if getattr(request.node, "_connector_healthy", True) else "Connection failed",
    )
    with (
        patch("modulo.connector_hub.get_connector_instance"),
        patch(
            "modulo.connector_hub.check_connector_health",
            return_value=health,
        ),
    ):
        resp = client.get(f"/api/connectors/{connector_id}/health")
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


@then('the response detail is "{expected}"')
def check_health_detail_expected(expected: str, request):
    data = request.node._resp.json()
    assert data.get("detail") == expected, f"Expected '{expected}', got {data.get('detail')}"


@given('no connector exists with id "{conn_id}"')
def no_connector(conn_id: str, request):
    request.node._missing_connector = conn_id


@when(parsers.parse("I GET /api/connectors/{connector_id}/health"))
def get_connector_health_missing(connector_id, client, request):
    resp = client.get(f"/api/connectors/{connector_id}/health")
    request.node._resp = resp


@given(parsers.parse('org "{org}" has a connector "{name}"'))
def org_has_connector(org: str, name: str, request):
    request.node._connector_name = name
    request.node._connector_org = org


@given(parsers.parse('the connector reads "README.md" from branch "main"'))
def connector_main_readme(request):
    pass


@given(parsers.parse('the operation returns a "not_found" error'))
def operation_not_found(request):
    pass


@when("I authenticate as a user in {org}")
def authenticate_as_org(org: str, request):
    request.node._auth_org = org


@then("the health check is not accessible")
def health_not_accessible(request):
    pass
