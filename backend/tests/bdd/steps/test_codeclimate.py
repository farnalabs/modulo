"""BDD step definitions for Code Climate connector scenarios."""

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.codeclimate import CodeClimateConnector

try:
    scenarios("../features/connectors/codeclimate.feature")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def codeclimate_connector():
    return CodeClimateConnector(token="cc_test_token")


@pytest.fixture()
def connector(codeclimate_connector):
    return codeclimate_connector


# ---------------------------------------------------------------------------
# Shared / helper state
# ---------------------------------------------------------------------------

_given_failure = False
_last_health_result = None
_last_query_result = None
_last_write_result = None


@pytest.fixture(autouse=True)
def _reset_globals():
    global _given_failure, _last_health_result, _last_query_result, _last_write_result
    _given_failure = False
    _last_health_result = None
    _last_query_result = None
    _last_write_result = None


# ---------------------------------------------------------------------------
# Given steps
# ---------------------------------------------------------------------------


@given("a Code Climate connector with valid token")
def given_valid_connector(codeclimate_connector):
    return codeclimate_connector


@given("the Code Climate API returns unhealthy status")
def given_unhealthy():
    global _given_failure
    _given_failure = True


# ---------------------------------------------------------------------------
# When steps
# ---------------------------------------------------------------------------


@when("I perform a health check")
async def when_health_check(codeclimate_connector):
    global _last_health_result, _given_failure
    _last_health_result = await codeclimate_connector.health_check()
    return _last_health_result


@when(parsers.parse('I query resource "{resource}" with limit {limit:d}'))
async def when_query_with_limit(codeclimate_connector, resource, limit):
    global _last_query_result
    _last_query_result = await codeclimate_connector.query(
        ConnectorQuery(resource=resource, limit=limit)
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with github_slug "{github_slug}"'))
async def when_query_with_github_slug(codeclimate_connector, resource, github_slug):
    global _last_query_result
    _last_query_result = await codeclimate_connector.query(
        ConnectorQuery(resource=resource, filters={"github_slug": github_slug})
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with id "{item_id}"'))
async def when_query_with_id(codeclimate_connector, resource, item_id):
    global _last_query_result
    _last_query_result = await codeclimate_connector.query(
        ConnectorQuery(resource=resource, filters={"id": item_id})
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with repo_id "{repo_id}"'))
async def when_query_with_repo_id(codeclimate_connector, resource, repo_id):
    global _last_query_result
    _last_query_result = await codeclimate_connector.query(
        ConnectorQuery(resource=resource, filters={"repo_id": repo_id})
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with repo_id "{repo_id}" and snapshot_id "{snapshot_id}"'))
async def when_query_snapshot(codeclimate_connector, resource, repo_id, snapshot_id):
    global _last_query_result
    _last_query_result = await codeclimate_connector.query(
        ConnectorQuery(resource=resource, filters={"repo_id": repo_id, "id": snapshot_id})
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with repo_id "{repo_id}" and report_id "{report_id}"'))
async def when_query_test_report(codeclimate_connector, resource, repo_id, report_id):
    global _last_query_result
    _last_query_result = await codeclimate_connector.query(
        ConnectorQuery(resource=resource, filters={"repo_id": repo_id, "id": report_id})
    )
    return _last_query_result


@when(parsers.parse(
    'I write a test report for repo "{repo_id}" duration {duration:d} '
    'exit_code {exit_code:d} branch "{branch}" sha "{commit_sha}"'
))
async def when_write_test_report(codeclimate_connector, repo_id, duration, exit_code, branch, commit_sha):
    global _last_write_result
    _last_write_result = await codeclimate_connector.write(
        ConnectorPayload(
            resource="test_report",
            data={
                "repo_id": repo_id,
                "duration": duration,
                "exit_code": exit_code,
                "branch": branch,
                "commit_sha": commit_sha,
            },
        )
    )
    return _last_write_result


@when(parsers.parse('I query resource "{resource}" without id filter'))
async def when_query_without_id(codeclimate_connector, resource):
    global _last_query_result
    try:
        _last_query_result = await codeclimate_connector.query(ConnectorQuery(resource=resource))
    except ValueError:
        _last_query_result = None
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" without repo_id filter'))
async def when_query_without_repo_id(codeclimate_connector, resource):
    global _last_query_result
    try:
        _last_query_result = await codeclimate_connector.query(ConnectorQuery(resource=resource))
    except ValueError:
        _last_query_result = None
    return _last_query_result


# ---------------------------------------------------------------------------
# Then steps
# ---------------------------------------------------------------------------


@then("the health result is ok")
def then_health_ok():
    assert _last_health_result is not None
    assert _last_health_result.ok is True


@then("the health result is not ok")
def then_health_not_ok():
    assert _last_health_result is not None
    assert _last_health_result.ok is False


@then("the result has records")
def then_result_has_records():
    assert _last_query_result is not None
    assert len(_last_query_result.records) > 0


@then("the write succeeds")
def then_write_succeeds():
    assert _last_write_result is not None


@then("the result is an error")
def then_result_is_error():
    assert _last_query_result is None
