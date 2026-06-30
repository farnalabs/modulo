"""BDD step definitions for 1Password Connect connector scenarios."""

import asyncio

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.onepassword import OnePasswordConnector

try:
    scenarios("../features/connectors/onepassword.feature")
except Exception:
    pass

TOKEN = "op_test_token"
BASE_URL = "http://localhost:8080"

_given_auth_failure = False
_last_health_result = None
_last_query_result = None
_last_write_result = None
_last_error = None


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def op_connector():
    return OnePasswordConnector(token=TOKEN, base_url=BASE_URL)


@pytest.fixture()
def connector(op_connector):
    return op_connector


@pytest.fixture(autouse=True)
def _reset_globals():
    global _given_auth_failure, _last_health_result, _last_query_result, _last_write_result, _last_error
    _given_auth_failure = False
    _last_health_result = None
    _last_query_result = None
    _last_write_result = None
    _last_error = None


@given("a 1Password connector with valid token")
def given_valid_connector(op_connector):
    return op_connector


@when("I perform a health check")
def when_health_check(op_connector):
    with respx.mock:
        respx.get(f"{BASE_URL}/v1/vaults", params={"limit": 1}).mock(
            return_value=httpx.Response(200, json=[])
        )
        global _last_health_result
        _last_health_result = _run(op_connector.health_check())


@when(parsers.parse('I query 1Password resource "{resource}"'))
def when_query_resource(op_connector, resource):
    global _last_query_result, _last_error
    try:
        with respx.mock:
            if resource == "vaults":
                respx.get(f"{BASE_URL}/v1/vaults").mock(
                    return_value=httpx.Response(200, json=[{"id": "v1", "name": "Personal"}])
                )
            _last_query_result = _run(op_connector.query(ConnectorQuery(resource=resource)))
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(parsers.parse('I query 1Password resource "{resource}" with vault_id "{vault_id}"'))
def when_query_vault(op_connector, resource, vault_id):
    global _last_query_result, _last_error
    try:
        with respx.mock:
            if resource == "vault":
                respx.get(f"{BASE_URL}/v1/vaults/{vault_id}").mock(
                    return_value=httpx.Response(200, json={"id": vault_id, "name": "Personal"})
                )
            elif resource == "items":
                respx.get(f"{BASE_URL}/v1/vaults/{vault_id}/items").mock(
                    return_value=httpx.Response(200, json=[{"id": "i1", "title": "My Login"}])
                )
            _last_query_result = _run(
                op_connector.query(ConnectorQuery(resource=resource, filters={"vault_id": vault_id}))
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(parsers.parse('I query 1Password resource "{resource}" with vault_id "{vault_id}" and item_id "{item_id}"'))
def when_query_vault_item(op_connector, resource, vault_id, item_id):
    global _last_query_result, _last_error
    try:
        with respx.mock:
            if resource == "item":
                respx.get(f"{BASE_URL}/v1/vaults/{vault_id}/items/{item_id}").mock(
                    return_value=httpx.Response(200, json={"id": item_id, "title": "My Login"})
                )
            elif resource == "files":
                respx.get(f"{BASE_URL}/v1/vaults/{vault_id}/items/{item_id}/files").mock(
                    return_value=httpx.Response(200, json=[{"id": "f1", "name": "attachment.txt"}])
                )
            _last_query_result = _run(
                op_connector.query(
                    ConnectorQuery(resource=resource, filters={"vault_id": vault_id, "item_id": item_id})
                )
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(
    parsers.parse(
        'I query 1Password resource "{resource}" with vault_id "{vault_id}" and title "{title}"'
    )
)
def when_query_by_title(op_connector, resource, vault_id, title):
    global _last_query_result, _last_error
    try:
        with respx.mock:
            respx.get(f"{BASE_URL}/v1/vaults/{vault_id}/items", params={"filter[title]": title}).mock(
                return_value=httpx.Response(200, json=[{"id": "i1", "title": title}])
            )
            _last_query_result = _run(
                op_connector.query(
                    ConnectorQuery(resource=resource, filters={"vault_id": vault_id, "title": title})
                )
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(
    parsers.parse(
        'I query 1Password resource "{resource}" with vault_id "{vault_id}" item_id "{item_id}" and file_id "{file_id}"'
    )
)
def when_query_file(op_connector, resource, vault_id, item_id, file_id):
    global _last_query_result, _last_error
    try:
        with respx.mock:
            respx.get(f"{BASE_URL}/v1/vaults/{vault_id}/items/{item_id}/files/{file_id}/content").mock(
                return_value=httpx.Response(200, text="file content here")
            )
            _last_query_result = _run(
                op_connector.query(
                    ConnectorQuery(
                        resource=resource,
                        filters={"vault_id": vault_id, "item_id": item_id, "file_id": file_id},
                    )
                )
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(parsers.parse('I query 1Password resource "{resource}" without vault_id'))
def when_query_without_vault(op_connector, resource):
    global _last_query_result, _last_error
    try:
        _last_query_result = _run(op_connector.query(ConnectorQuery(resource=resource)))
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(parsers.parse('I query 1Password resource "{resource}" with vault_id "{vault_id}" without item_id'))
def when_query_without_item(op_connector, resource, vault_id):
    global _last_query_result, _last_error
    try:
        _last_query_result = _run(
            op_connector.query(ConnectorQuery(resource=resource, filters={"vault_id": vault_id}))
        )
        _last_error = None
    except ValueError as e:
        _last_error = e
        _last_query_result = None


@when(
    parsers.parse(
        'I write 1Password resource "{resource}" with vault_id "{vault_id}" title "{title}" type "{typ}"'
    )
)
def when_write_item(op_connector, resource, vault_id, title, typ):
    global _last_write_result, _last_error
    try:
        with respx.mock:
            respx.post(f"{BASE_URL}/v1/vaults/{vault_id}/items").mock(
                return_value=httpx.Response(200, json={"id": "new-item", "title": title, "type": typ})
            )
            _last_write_result = _run(
                op_connector.write(
                    ConnectorPayload(
                        resource=resource,
                        data={"vault_id": vault_id, "title": title, "type": typ, "fields": []},
                    )
                )
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_write_result = None


@when(
    parsers.parse(
        'I write 1Password resource "{resource}" with vault_id "{vault_id}" item_id "{item_id}" title "{title}"'
    )
)
def when_write_update(op_connector, resource, vault_id, item_id, title):
    global _last_write_result, _last_error
    try:
        with respx.mock:
            respx.put(f"{BASE_URL}/v1/vaults/{vault_id}/items/{item_id}").mock(
                return_value=httpx.Response(200, json={"id": item_id, "title": title})
            )
            _last_write_result = _run(
                op_connector.write(
                    ConnectorPayload(
                        resource=resource,
                        data={"vault_id": vault_id, "item_id": item_id, "title": title, "fields": []},
                    )
                )
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_write_result = None


@when(
    parsers.parse(
        'I write 1Password resource "{resource}" with vault_id "{vault_id}" and item_id "{item_id}"'
    )
)
def when_write_delete(op_connector, resource, vault_id, item_id):
    global _last_write_result, _last_error
    try:
        with respx.mock:
            respx.delete(f"{BASE_URL}/v1/vaults/{vault_id}/items/{item_id}").mock(
                return_value=httpx.Response(204)
            )
            _last_write_result = _run(
                op_connector.write(
                    ConnectorPayload(resource=resource, data={"vault_id": vault_id, "item_id": item_id})
                )
            )
            _last_error = None
    except ValueError as e:
        _last_error = e
        _last_write_result = None


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
