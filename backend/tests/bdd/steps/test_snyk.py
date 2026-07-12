"""BDD step definitions for Snyk connector scenarios."""

import contextlib

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.snyk import SnykConnector

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/snyk.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def snyk_connector():
    return SnykConnector(token="snyk_test_token")


@pytest.fixture()
def connector(snyk_connector):
    return snyk_connector


# ---------------------------------------------------------------------------
# Shared / helper state
# ---------------------------------------------------------------------------

_given_auth_failure = False
_last_health_result = None
_last_query_result = None
_last_write_result = None
_last_error = None


@pytest.fixture(autouse=True)
def _reset_globals():
    global _given_auth_failure, _last_health_result, _last_query_result, _last_write_result, _last_error
    _given_auth_failure = False
    _last_health_result = None
    _last_query_result = None
    _last_write_result = None
    _last_error = None


# ---------------------------------------------------------------------------
# Given steps
# ---------------------------------------------------------------------------


@given("a Snyk connector with valid token")
def given_valid_connector(snyk_connector):
    return snyk_connector


@given("the Snyk API returns unauthorized")
def given_unauthorized():
    global _given_auth_failure
    _given_auth_failure = True


# ---------------------------------------------------------------------------
# When steps
# ---------------------------------------------------------------------------


@when("I perform a health check")
async def when_health_check(snyk_connector):
    global _last_health_result, _given_auth_failure
    _last_health_result = await snyk_connector.health_check()
    return _last_health_result


@when(parsers.parse('I query Snyk resource "{resource}" with org "{org}"'))
async def when_query_with_org(snyk_connector, resource, org):
    global _last_query_result
    _last_query_result = await snyk_connector.query(
        ConnectorQuery(resource=resource, filters={"org_id": org}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query Snyk resource "{resource}" with org "{org}" and project "{project}"'))
async def when_query_org_project(snyk_connector, resource, org, project):
    global _last_query_result
    _last_query_result = await snyk_connector.query(
        ConnectorQuery(resource=resource, filters={"org_id": org, "project_id": project}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query Snyk resource "{resource}" with limit {limit:d}'))
async def when_query_with_limit(snyk_connector, resource, limit):
    global _last_query_result
    _last_query_result = await snyk_connector.query(ConnectorQuery(resource=resource, limit=limit))
    return _last_query_result


@when(parsers.parse('I query Snyk resource "tests" with org "{org}"'))
async def when_query_tests(snyk_connector, resource, org):
    global _last_query_result
    _last_query_result = await snyk_connector.query(
        ConnectorQuery(resource=resource, filters={"org_id": org}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query Snyk resource "aggregated_issues" with org "{org}" and packages'))
async def when_query_aggregated(snyk_connector, resource, org):
    global _last_query_result
    _last_query_result = await snyk_connector.query(
        ConnectorQuery(
            resource=resource,
            filters={
                "org_id": org,
                "packages": [{"name": "requests", "version": "4.0.0", "ecosystem": "pypi"}],
            },
        )
    )
    return _last_query_result


@when(parsers.parse('I write Snyk resource "test" with org "{org}" and package "{pkg}" ecosystem "{eco}"'))
async def when_write_test(snyk_connector, resource, org, pkg, eco):
    global _last_write_result
    name, version = pkg.split("@")
    _last_write_result = await snyk_connector.write(
        ConnectorPayload(
            resource=resource,
            data={"org_id": org, "name": name, "version": version, "ecosystem": eco},
        )
    )
    return _last_write_result


@when(parsers.parse('I write Snyk resource "ignore" with org "{org}" project "{proj}" and issue "{issue}"'))
async def when_write_ignore(snyk_connector, resource, org, proj, issue):
    global _last_write_result
    _last_write_result = await snyk_connector.write(
        ConnectorPayload(
            resource=resource,
            data={"org_id": org, "project_id": proj, "issue_id": issue},
        )
    )
    return _last_write_result


@when(parsers.parse('I query Snyk resource "{resource}" without org filter'))
async def when_query_without_org(snyk_connector, resource):
    global _last_query_result, _last_error
    try:
        _last_query_result = await snyk_connector.query(ConnectorQuery(resource=resource))
        _last_error = None
    except ValueError as e:
        _last_error = e
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
    assert _last_error is not None
    assert _last_query_result is None
