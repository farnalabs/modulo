"""BDD step definitions for n8n connector scenarios."""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.n8n import N8NConnector

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/n8n.feature")

TOKEN = "n8n_test_token"
BASE_URL = "http://localhost:5678"

_given_invalid = False
_given_unreachable = False
_last_health_result = None
_last_query_result = None
_last_write_result = None
_last_error = None


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def n8n_connector():
    if _given_invalid:
        return N8NConnector(token="bad_token", base_url=BASE_URL)
    return N8NConnector(token=TOKEN, base_url=BASE_URL)


@pytest.fixture()
def connector(n8n_connector):
    return n8n_connector


@pytest.fixture(autouse=True)
def _reset_globals():
    global _given_invalid, _given_unreachable, _last_health_result, _last_query_result, _last_write_result, _last_error
    _given_invalid = False
    _given_unreachable = False
    _last_health_result = None
    _last_query_result = None
    _last_write_result = None
    _last_error = None


@given("an n8n connector with valid token")
def given_valid_connector(n8n_connector):
    return n8n_connector


@given("an n8n connector with invalid token")
def given_invalid_connector():
    global _given_invalid
    _given_invalid = True


@given("the n8n server is unreachable")
def given_unreachable():
    global _given_unreachable
    _given_unreachable = True


@when("I perform a health check")
def when_health_check(n8n_connector):
    global _last_health_result, _last_error
    with respx.mock:
        if _given_unreachable:
            respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
                side_effect=httpx.ConnectError("connection refused")
            )
        elif _given_invalid:
            respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
        else:
            respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
                return_value=httpx.Response(200, json={"data": []})
            )
        try:
            _last_health_result = _run(n8n_connector.health_check())
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I query n8n resource "{resource}"'))
def when_query_resource(n8n_connector, resource):
    global _last_query_result, _last_error
    with respx.mock:
        _mock_list_endpoint(resource)
        try:
            _last_query_result = _run(n8n_connector.query(ConnectorQuery(resource=resource)))
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I query n8n resource "{resource}" with limit {limit:d}'))
def when_query_with_limit(n8n_connector, resource, limit):
    global _last_query_result, _last_error
    with respx.mock:
        _mock_list_endpoint(resource, limit=limit)
        try:
            _last_query_result = _run(n8n_connector.query(ConnectorQuery(resource=resource, limit=limit)))
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I query n8n resource "{resource}" with filter "{key}" value "{value}"'))
def when_query_with_filter(n8n_connector, resource, key, value):
    global _last_query_result, _last_error
    with respx.mock:
        _mock_list_endpoint(resource, **{key: value})
        try:
            _last_query_result = _run(n8n_connector.query(ConnectorQuery(resource=resource, filters={key: value})))
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I query n8n resource "{resource}" with id "{item_id}"'))
def when_query_with_id(n8n_connector, resource, item_id):
    global _last_query_result, _last_error
    path_map = {"workflow": "workflows", "execution": "executions", "credential": "credentials"}
    api_resource = path_map.get(resource, resource)
    with respx.mock:
        respx.get(f"{BASE_URL}/rest/{api_resource}/{item_id}").mock(
            return_value=httpx.Response(200, json={"data": {"id": item_id, "name": f"{resource} {item_id}"}})
        )
        try:
            _last_query_result = _run(n8n_connector.query(ConnectorQuery(resource=resource, filters={"id": item_id})))
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I query n8n resource "{resource}" without id'))
def when_query_without_id(n8n_connector, resource):
    global _last_query_result, _last_error
    try:
        _last_query_result = _run(n8n_connector.query(ConnectorQuery(resource=resource)))
    except Exception as exc:
        _last_error = exc


@when(parsers.parse('I write n8n resource "{resource}" with name "{name}"'))
def when_write_resource(n8n_connector, resource, name):
    global _last_write_result, _last_error
    with respx.mock:
        if resource == "workflow" and name:
            respx.post(f"{BASE_URL}/rest/workflows").mock(
                return_value=httpx.Response(201, json={"data": {"id": "W1", "name": name, "active": False}})
            )
        elif resource == "credential" or resource == "invalid_resource":
            pass
        try:
            data: dict = {}
            if name:
                data["name"] = name
            if resource == "credential":
                data["type"] = "github"
            _last_write_result = _run(n8n_connector.write(ConnectorPayload(resource=resource, data=data)))
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I write n8n resource "{resource}" with name "{name}" type "{cred_type}"'))
def when_write_credential(n8n_connector, resource, name, cred_type):
    global _last_write_result, _last_error
    with respx.mock:
        if name and cred_type:
            respx.post(f"{BASE_URL}/rest/credentials").mock(
                return_value=httpx.Response(201, json={"data": {"id": "C1", "name": name, "type": cred_type}})
            )
        try:
            _last_write_result = _run(
                n8n_connector.write(ConnectorPayload(resource=resource, data={"name": name, "type": cred_type}))
            )
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I write n8n resource "{resource}" without name'))
def when_write_without_name(n8n_connector, resource):
    global _last_write_result, _last_error
    try:
        _last_write_result = _run(n8n_connector.write(ConnectorPayload(resource=resource, data={})))
    except Exception as exc:
        _last_error = exc


@when(parsers.parse('I write n8n resource "{resource}" without id'))
def when_write_without_id(n8n_connector, resource):
    global _last_write_result, _last_error
    try:
        _last_write_result = _run(n8n_connector.write(ConnectorPayload(resource=resource, data={})))
    except Exception as exc:
        _last_error = exc


@when(parsers.parse('I write n8n resource "{resource}" with id "{item_id}"'))
def when_write_by_id(n8n_connector, resource, item_id):
    global _last_write_result, _last_error
    with respx.mock:
        _mock_write_single(resource, item_id)
        try:
            data = {"id": item_id} if item_id else {}
            _last_write_result = _run(n8n_connector.write(ConnectorPayload(resource=resource, data=data)))
        except Exception as exc:
            _last_error = exc


@when(parsers.parse('I write n8n resource "{resource}" with id "{item_id}" and name "{name}"'))
def when_write_update(n8n_connector, resource, item_id, name):
    global _last_write_result, _last_error
    with respx.mock:
        if resource == "workflow_update" and item_id:
            respx.put(f"{BASE_URL}/rest/workflows/{item_id}").mock(
                return_value=httpx.Response(200, json={"data": {"id": item_id, "name": name}})
            )
        try:
            data = {"id": item_id, "name": name} if item_id else {}
            _last_write_result = _run(n8n_connector.write(ConnectorPayload(resource=resource, data=data)))
        except Exception as exc:
            _last_error = exc


@then("the health result is ok")
def then_health_ok():
    assert _last_health_result is not None
    assert _last_health_result.ok is True
    assert _last_error is None


@then("the health result is not ok")
def then_health_not_ok():
    assert _last_health_result is not None
    assert _last_health_result.ok is False


@then("the result has records")
def then_result_has_records():
    assert _last_query_result is not None
    assert len(_last_query_result.records) > 0


@then("the result is an error")
def then_result_is_error():
    assert _last_error is not None


@then("the write succeeds")
def then_write_succeeds():
    assert _last_write_result is not None
    assert isinstance(_last_write_result, dict)


@then("the write is an error")
def then_write_is_error():
    assert _last_error is not None


def _mock_list_endpoint(resource, **params):
    endpoint_map = {
        "workflows": "/rest/workflows",
        "executions": "/rest/executions",
        "webhooks": "/rest/webhooks",
        "credentials": "/rest/credentials",
        "tags": "/rest/tags",
        "nodes": "/rest/node-types",
    }
    endpoint = endpoint_map.get(resource)
    if not endpoint:
        return
    filtered_params = {k: v for k, v in params.items() if v is not None}
    url = f"{BASE_URL}{endpoint}"
    suffix = resource[:-1] if resource != "nodes" else "node"
    mock_kwargs = {"data": [{"id": f"{suffix}_1", "name": f"Mock {resource}"}]}
    if filtered_params:
        respx.get(url, params=filtered_params).mock(return_value=httpx.Response(200, json=mock_kwargs))
    else:
        respx.get(url).mock(return_value=httpx.Response(200, json=mock_kwargs))


def _mock_write_single(resource, item_id):
    route_map = {
        "workflow_activate": ("post", f"/rest/workflows/{item_id}/activate", {"id": item_id, "active": True}),
        "workflow_deactivate": ("post", f"/rest/workflows/{item_id}/deactivate", {"id": item_id, "active": False}),
        "workflow_delete": ("delete", f"/rest/workflows/{item_id}", {"id": item_id, "deleted": True}),
        "execution_delete": ("delete", f"/rest/executions/{item_id}", {"id": item_id, "deleted": True}),
        "execution_retry": ("post", f"/rest/executions/{item_id}/retry", {"id": item_id, "status": "running"}),
    }
    if resource not in route_map:
        return
    method, path, response_data = route_map[resource]
    route = respx
    if method == "post":
        route = route.post(f"{BASE_URL}{path}")
    elif method == "delete":
        route = route.delete(f"{BASE_URL}{path}")
    route.mock(return_value=httpx.Response(200, json={"data": response_data}))
