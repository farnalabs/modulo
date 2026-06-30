"""Unit tests for MondayConnector — GraphQL API responses mocked via httpx/respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.monday import MondayConnector

API_KEY = "monday_api_key"
_MONDAY_API = "https://api.monday.com/v2/"
# Note: trailing slash required — MondayConnector builds the URL as base_url + "/"


@pytest.fixture()
def connector():
    return MondayConnector(api_key=API_KEY)


def _mock_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response({"me": {"name": "Alice Smith"}}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice Smith"


@respx.mock
async def test_health_check_fail(connector):
    respx.post(_MONDAY_API).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_no_user(connector):
    respx.post(_MONDAY_API).mock(return_value=_mock_response({"me": None}))
    result = await connector.health_check()
    assert result.ok is False
    assert "No user" in result.detail


# ---------------------------------------------------------------------------
# query — boards
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_boards(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {"boards": [{"id": "1", "name": "Board A"}, {"id": "2", "name": "Board B"}]}
        ),
    )
    result = await connector.query(ConnectorQuery(resource="boards"))
    assert result.total == 2
    assert result.records[0]["name"] == "Board A"


# ---------------------------------------------------------------------------
# query — board (single, with filters)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_board(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {
                "boards": [
                    {
                        "id": "10",
                        "name": "My Board",
                        "columns": [{"id": "col1", "title": "Status", "type": "text"}],
                        "groups": [{"id": "g1", "title": "Group 1"}],
                    }
                ]
            }
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="board", filters={"board_id": 10})
    )
    assert result.total == 1
    assert result.records[0]["id"] == "10"


async def test_query_board_missing_board_id(connector):
    with pytest.raises(ValueError, match="'board_id' filter"):
        await connector.query(ConnectorQuery(resource="board"))


# ---------------------------------------------------------------------------
# query — items
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_items(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {
                "boards": [
                    {
                        "items": [
                            {"id": "101", "name": "Item One"},
                            {"id": "102", "name": "Item Two"},
                        ]
                    }
                ]
            }
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="items", filters={"board_id": 10})
    )
    assert result.total == 2
    assert result.records[0]["name"] == "Item One"


async def test_query_items_missing_board_id(connector):
    with pytest.raises(ValueError, match="'board_id' filter"):
        await connector.query(ConnectorQuery(resource="items"))


# ---------------------------------------------------------------------------
# query — item (single)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_item(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {
                "items": [
                    {"id": "201", "name": "Single Item", "column_values": []}
                ]
            }
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="item", filters={"item_id": 201})
    )
    assert result.total == 1
    assert result.records[0]["name"] == "Single Item"


async def test_query_item_missing_item_id(connector):
    with pytest.raises(ValueError, match="'item_id' filter"):
        await connector.query(ConnectorQuery(resource="item"))


# ---------------------------------------------------------------------------
# query — users
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_users(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {
                "users": [
                    {"id": "u1", "name": "Alice", "email": "alice@example.com"},
                    {"id": "u2", "name": "Bob", "email": "bob@example.com"},
                ]
            }
        ),
    )
    result = await connector.query(ConnectorQuery(resource="users"))
    assert result.total == 2


# ---------------------------------------------------------------------------
# query — workspaces
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_workspaces(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {
                "workspaces": [
                    {"id": "ws1", "name": "Engineering"},
                    {"id": "ws2", "name": "Marketing"},
                ]
            }
        ),
    )
    result = await connector.query(ConnectorQuery(resource="workspaces"))
    assert result.total == 2


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match=r"Unsupported Monday\.com resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — create item
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_item(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {"create_item": {"id": "301", "name": "New Task"}}
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="item",
            data={"board_id": 10, "item_name": "New Task"},
        )
    )
    assert result["id"] == "301"
    assert result["name"] == "New Task"


@respx.mock
async def test_write_create_item_with_column_values(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {"create_item": {"id": "302", "name": "Task With Values"}}
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="item",
            data={
                "board_id": 10,
                "item_name": "Task With Values",
                "column_values": '{"status": "Done"}',
            },
        )
    )
    assert result["id"] == "302"


async def test_write_create_item_missing_fields(connector):
    with pytest.raises(ValueError, match="'board_id' and 'item_name'"):
        await connector.write(
            ConnectorPayload(resource="item", data={"board_id": 10})
        )


# ---------------------------------------------------------------------------
# write — update item column values
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_item_update(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {
                "change_multiple_column_values": {
                    "id": "301",
                    "name": "Updated Task",
                }
            }
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="item_update",
            data={"item_id": 301, "column_values": '{"status": "In Progress"}'},
        )
    )
    assert result["name"] == "Updated Task"


async def test_write_item_update_missing_fields(connector):
    with pytest.raises(ValueError, match="'item_id' and 'column_values'"):
        await connector.write(
            ConnectorPayload(resource="item_update", data={"item_id": 301})
        )


# ---------------------------------------------------------------------------
# write — change single column value
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_column_value(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {"change_simple_column_value": {"id": "301", "name": "Task"}}
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="column_value",
            data={"item_id": 301, "column_id": "status", "value": '"Done"'},
        )
    )
    assert result["id"] == "301"


async def test_write_column_value_missing_fields(connector):
    with pytest.raises(ValueError, match="'item_id', 'column_id', and 'value'"):
        await connector.write(
            ConnectorPayload(
                resource="column_value",
                data={"item_id": 301, "column_id": "status"},
            )
        )


# ---------------------------------------------------------------------------
# write — add update
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_update(connector):
    respx.post(_MONDAY_API).mock(
        return_value=_mock_response(
            {"create_update": {"id": "up1", "text": "Update body text"}}
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="update",
            data={"item_id": 301, "body": "Update body text"},
        )
    )
    assert result["text"] == "Update body text"


async def test_write_update_missing_fields(connector):
    with pytest.raises(ValueError, match="'item_id' and 'body'"):
        await connector.write(
            ConnectorPayload(resource="update", data={"item_id": 301})
        )


# ---------------------------------------------------------------------------
# write — unknown resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match=r"Unsupported Monday\.com write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# connector type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.MONDAY


# ---------------------------------------------------------------------------
# _graphql error handling (API-level errors in response body)
# ---------------------------------------------------------------------------


@respx.mock
async def test_graphql_errors(connector):
    respx.post(_MONDAY_API).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Invalid API key", "code": "unauthorized"}]},
        ),
    )
    with pytest.raises(ValueError, match=r"Monday\.com API error"):
        await connector.query(ConnectorQuery(resource="boards"))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_boards_http_error(connector):
    respx.post(_MONDAY_API).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="boards"))
