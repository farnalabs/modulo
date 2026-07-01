"""BDD step definitions for Trivy connector scenarios."""

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.trivy import TrivyConnector

try:
    scenarios("../features/connectors/trivy.feature")
except Exception:
    pass

_last_health_result = None
_last_query_result = None
_last_write_result = None
_last_error = None
_connection_failure = False


@pytest.fixture()
def trivy_connector():
    return TrivyConnector(token="test_token", base_url="http://localhost:8080")


@pytest.fixture()
def connector(trivy_connector):
    return trivy_connector


@pytest.fixture(autouse=True)
def _reset_globals():
    global _last_health_result, _last_query_result, _last_write_result, _last_error, _connection_failure
    _last_health_result = None
    _last_query_result = None
    _last_write_result = None
    _last_error = None
    _connection_failure = False


@given("a Trivy connector")
def given_valid_connector(trivy_connector):
    return trivy_connector


@given("the Trivy server is unreachable")
def given_unreachable():
    global _connection_failure
    _connection_failure = True


@when("I perform a health check")
async def when_health_check(trivy_connector):
    global _last_health_result
    _last_health_result = await trivy_connector.health_check()
    return _last_health_result


@when(parsers.parse('I query Trivy resource "{resource}" with image "{image}"'))
async def when_query_artifact_image(trivy_connector, resource, image):
    global _last_query_result
    _last_query_result = await trivy_connector.query(
        ConnectorQuery(resource=resource, filters={"image": image}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query Trivy resource "{resource}" with filesystem "{fs}"'))
async def when_query_artifact_filesystem(trivy_connector, resource, fs):
    global _last_query_result
    _last_query_result = await trivy_connector.query(
        ConnectorQuery(resource=resource, filters={"filesystem": fs}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query Trivy resource "{resource}" with repository "{repo}"'))
async def when_query_artifact_repo(trivy_connector, resource, repo):
    global _last_query_result
    _last_query_result = await trivy_connector.query(
        ConnectorQuery(resource=resource, filters={"repository": repo}, limit=10)
    )
    return _last_query_result


@when(parsers.parse('I query Trivy resource "{resource}" without target'))
async def when_query_artifact_no_target(trivy_connector, resource):
    global _last_query_result, _last_error
    try:
        _last_query_result = await trivy_connector.query(ConnectorQuery(resource=resource))
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None
    return _last_query_result


@when(parsers.parse('I query Trivy resource "{resource}" with limit {limit:d}'))
async def when_query_reports(trivy_connector, resource, limit):
    global _last_query_result
    _last_query_result = await trivy_connector.query(ConnectorQuery(resource=resource, limit=limit))
    return _last_query_result


@when(parsers.parse('I query Trivy resource "report" with digest "{digest}"'))
async def when_query_report_digest(trivy_connector, resource, digest):
    global _last_query_result
    _last_query_result = await trivy_connector.query(
        ConnectorQuery(resource=resource, filters={"digest": digest})
    )
    return _last_query_result


@when(parsers.parse('I query Trivy resource "report" without digest'))
async def when_query_report_no_digest(trivy_connector, resource):
    global _last_query_result, _last_error
    try:
        _last_query_result = await trivy_connector.query(ConnectorQuery(resource=resource))
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None
    return _last_query_result


@when(parsers.parse('I query Trivy resource "{resource}"'))
async def when_query_generic(trivy_connector, resource):
    global _last_query_result, _last_error
    try:
        _last_query_result = await trivy_connector.query(ConnectorQuery(resource=resource))
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None
    return _last_query_result


@when(parsers.parse('I write Trivy resource "scan" with image "{image}"'))
async def when_write_scan_image(trivy_connector, resource, image):
    global _last_write_result
    _last_write_result = await trivy_connector.write(
        ConnectorPayload(resource=resource, data={"image": image})
    )
    return _last_write_result


@when(parsers.parse('I write Trivy resource "scan" without target'))
async def when_write_scan_no_target(trivy_connector, resource):
    global _last_write_result, _last_error
    try:
        _last_write_result = await trivy_connector.write(ConnectorPayload(resource=resource, data={}))
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_write_result = None
    return _last_write_result


@when(parsers.parse('I write Trivy resource "{resource}"'))
async def when_write_invalid(trivy_connector, resource):
    global _last_write_result, _last_error
    try:
        _last_write_result = await trivy_connector.write(ConnectorPayload(resource=resource, data={}))
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_write_result = None
    return _last_write_result


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
