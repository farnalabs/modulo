"""BDD step definitions for SonarQube connector scenarios."""

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.sonarqube import SonarQubeConnector

try:
    scenarios("../../features/connectors/sonarqube.feature")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sonarqube_connector():
    return SonarQubeConnector(token="sqp_test_token", base_url="https://sonarqube.company.com")


@pytest.fixture()
def connector(sonarqube_connector):
    return sonarqube_connector


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


@given("a SonarQube connector with valid token")
def given_valid_connector(sonarqube_connector):
    return sonarqube_connector


@given("the SonarQube API returns unhealthy status")
def given_unhealthy():
    global _given_failure
    _given_failure = True


# ---------------------------------------------------------------------------
# When steps
# ---------------------------------------------------------------------------


@when("I perform a health check")
async def when_health_check(sonarqube_connector):
    global _last_health_result, _given_failure
    if _given_failure:
        _last_health_result = await sonarqube_connector.health_check()
    else:
        _last_health_result = await sonarqube_connector.health_check()
    return _last_health_result


@when(parsers.parse('I query resource "{resource}" with limit {limit:d}'))
async def when_query_with_limit(sonarqube_connector, resource, limit):
    global _last_query_result
    _last_query_result = await sonarqube_connector.query(
        ConnectorQuery(resource=resource, limit=limit)
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with project "{project}"'))
async def when_query_with_project(sonarqube_connector, resource, project):
    global _last_query_result
    _last_query_result = await sonarqube_connector.query(
        ConnectorQuery(resource=resource, filters={"project": project}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with component "{component}" and metricKeys "{keys}"'))
async def when_query_measures(sonarqube_connector, resource, component, keys):
    global _last_query_result
    _last_query_result = await sonarqube_connector.query(
        ConnectorQuery(resource=resource, filters={"component": component, "metricKeys": keys})
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with component "{component}"'))
async def when_query_issues(sonarqube_connector, resource, component):
    global _last_query_result
    _last_query_result = await sonarqube_connector.query(
        ConnectorQuery(resource=resource, filters={"component": component}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" with id "{gate_id}"'))
async def when_query_quality_gate(sonarqube_connector, resource, gate_id):
    global _last_query_result
    _last_query_result = await sonarqube_connector.query(
        ConnectorQuery(resource=resource, filters={"id": gate_id})
    )
    return _last_query_result


@when(parsers.parse('I query resource "{resource}" without project filter'))
async def when_query_without_project(sonarqube_connector, resource):
    global _last_query_result
    try:
        _last_query_result = await sonarqube_connector.query(ConnectorQuery(resource=resource))
    except ValueError:
        _last_query_result = None
    return _last_query_result


@when(parsers.parse('I write SonarQube resource "{resource}" with issue "{issue}" and text "{text}"'))
async def when_write_comment(sonarqube_connector, resource, issue, text):
    global _last_write_result
    _last_write_result = await sonarqube_connector.write(
        ConnectorPayload(resource=resource, data={"issue": issue, "text": text})
    )
    return _last_write_result


@when(parsers.parse('I write SonarQube resource "{resource}" with issue "{issue}" and transition "{transition}"'))
async def when_write_transition(sonarqube_connector, resource, issue, transition):
    global _last_write_result
    _last_write_result = await sonarqube_connector.write(
        ConnectorPayload(resource=resource, data={"issue": issue, "transition": transition})
    )
    return _last_write_result


@when(parsers.parse('I write SonarQube resource "{resource}" with name "{name}"'))
async def when_write_gate(sonarqube_connector, resource, name):
    global _last_write_result
    _last_write_result = await sonarqube_connector.write(
        ConnectorPayload(resource=resource, data={"name": name})
    )
    return _last_write_result


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
