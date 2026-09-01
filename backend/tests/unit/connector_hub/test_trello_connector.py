"""Unit tests for TrelloConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.trello import TrelloConnector

API_KEY = "trello_api_key"
TOKEN = "trello_token"
_BASE = "https://api.trello.com/1"


@pytest.fixture
def connector():
    return TrelloConnector(api_key=API_KEY, token=TOKEN)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/members/me").mock(
        return_value=httpx.Response(200, json={"id": "me123", "fullName": "Alice Smith"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice Smith"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/members/me").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_no_id(connector):
    respx.get(f"{_BASE}/members/me").mock(return_value=httpx.Response(200, json={"fullName": "No ID"}))
    result = await connector.health_check()
    assert result.ok is False
    assert "no 'id'" in result.detail


# ---------------------------------------------------------------------------
# query — boards
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_boards(connector):
    boards = [
        {"id": "b1", "name": "Board One", "closed": False},
        {"id": "b2", "name": "Board Two", "closed": True},
    ]
    respx.get(f"{_BASE}/members/me/boards").mock(return_value=httpx.Response(200, json=boards))
    result = await connector.query(ConnectorQuery(resource="boards"))
    assert result.total == 2
    assert result.records[0]["name"] == "Board One"


@respx.mock
async def test_query_boards_with_filter(connector):
    boards = [{"id": "b1", "name": "Open Board", "closed": False}]
    respx.get(f"{_BASE}/members/me/boards").mock(return_value=httpx.Response(200, json=boards))
    result = await connector.query(ConnectorQuery(resource="boards", filters={"filter": "open"}))
    assert result.total == 1
    assert result.records[0]["name"] == "Open Board"


# ---------------------------------------------------------------------------
# query — lists
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_lists(connector):
    lists = [
        {"id": "l1", "name": "To Do", "closed": False},
        {"id": "l2", "name": "Done", "closed": False},
    ]
    respx.get(f"{_BASE}/boards/b1/lists").mock(return_value=httpx.Response(200, json=lists))
    result = await connector.query(ConnectorQuery(resource="lists", filters={"board_id": "b1"}))
    assert result.total == 2
    assert result.records[0]["name"] == "To Do"


@respx.mock
async def test_query_lists_with_filter(connector):
    lists = [{"id": "l1", "name": "Open List", "closed": False}]
    respx.get(f"{_BASE}/boards/b1/lists").mock(return_value=httpx.Response(200, json=lists))
    result = await connector.query(ConnectorQuery(resource="lists", filters={"board_id": "b1", "filter": "open"}))
    assert result.total == 1


async def test_query_lists_missing_board_id(connector):
    with pytest.raises(ValueError, match="'board_id' filter"):
        await connector.query(ConnectorQuery(resource="lists"))


# ---------------------------------------------------------------------------
# query — cards
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_cards_by_board(connector):
    cards = [{"id": "c1", "name": "Card One"}, {"id": "c2", "name": "Card Two"}]
    respx.get(f"{_BASE}/boards/b1/cards").mock(return_value=httpx.Response(200, json=cards))
    result = await connector.query(ConnectorQuery(resource="cards", filters={"board_id": "b1"}))
    assert result.total == 2
    assert result.records[0]["name"] == "Card One"


@respx.mock
async def test_query_cards_by_list(connector):
    cards = [{"id": "c3", "name": "List Card"}]
    respx.get(f"{_BASE}/lists/l1/cards").mock(return_value=httpx.Response(200, json=cards))
    result = await connector.query(ConnectorQuery(resource="cards", filters={"list_id": "l1"}))
    assert result.total == 1


async def test_query_cards_missing_filter(connector):
    with pytest.raises(ValueError, match="'board_id' or 'list_id' filter"):
        await connector.query(ConnectorQuery(resource="cards"))


@respx.mock
async def test_query_cards_with_fields(connector):
    cards = [{"id": "c1", "name": "Card One"}]
    respx.get(f"{_BASE}/boards/b1/cards").mock(return_value=httpx.Response(200, json=cards))
    result = await connector.query(ConnectorQuery(resource="cards", filters={"board_id": "b1", "fields": "id,name"}))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — single card
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_card(connector):
    card = {"id": "c1", "name": "Single Card", "desc": "Description"}
    respx.get(f"{_BASE}/cards/c1").mock(return_value=httpx.Response(200, json=card))
    result = await connector.query(ConnectorQuery(resource="card", filters={"card_id": "c1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Single Card"


async def test_query_single_card_missing_id(connector):
    with pytest.raises(ValueError, match="'card_id' filter"):
        await connector.query(ConnectorQuery(resource="card"))


# ---------------------------------------------------------------------------
# query — members
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_members(connector):
    members = [{"id": "u1", "fullName": "Alice"}, {"id": "u2", "fullName": "Bob"}]
    respx.get(f"{_BASE}/boards/b1/members").mock(return_value=httpx.Response(200, json=members))
    result = await connector.query(ConnectorQuery(resource="members", filters={"board_id": "b1"}))
    assert result.total == 2


async def test_query_members_missing_board_id(connector):
    with pytest.raises(ValueError, match="'board_id' filter"):
        await connector.query(ConnectorQuery(resource="members"))


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Trello resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — create card
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_card(connector):
    created = {"id": "c_new", "name": "New Card", "idList": "l1", "url": "https://trello.com/c/c_new"}
    respx.post(f"{_BASE}/cards").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="card",
            data={"name": "New Card", "idList": "l1", "desc": "A new card"},
        )
    )
    assert result["id"] == "c_new"
    assert result["name"] == "New Card"


# ---------------------------------------------------------------------------
# write — update card
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_update_card(connector):
    updated = {"id": "c1", "name": "Updated Name", "desc": "Updated"}
    respx.put(f"{_BASE}/cards/c1").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(
            resource="card_update",
            data={"id": "c1", "name": "Updated Name"},
        )
    )
    assert result["name"] == "Updated Name"


async def test_write_update_card_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(ConnectorPayload(resource="card_update", data={"name": "Orphan"}))


# ---------------------------------------------------------------------------
# write — add comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_comment(connector):
    action = {"id": "act1", "type": "commentCard", "data": {"text": "Nice work!"}}
    respx.post(f"{_BASE}/cards/c1/actions/comments").mock(return_value=httpx.Response(200, json=action))
    result = await connector.write(
        ConnectorPayload(
            resource="comment",
            data={"card_id": "c1", "text": "Nice work!"},
        )
    )
    assert result["type"] == "commentCard"


async def test_write_comment_missing_card_id(connector):
    with pytest.raises(ValueError, match="'card_id' in data"):
        await connector.write(ConnectorPayload(resource="comment", data={"text": "Orphan"}))


# ---------------------------------------------------------------------------
# write — unknown resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Trello write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# connector type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.TRELLO


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_boards_http_error(connector):
    respx.get(f"{_BASE}/members/me/boards").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="boards"))


# ---------------------------------------------------------------------------
# FAR-507 — credentials must never leak into error detail
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error_detail_redacts_credentials(connector):
    respx.get(f"{_BASE}/members/me/boards").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await connector.query(ConnectorQuery(resource="boards"))
    message = str(exc_info.value)
    assert API_KEY not in message
    assert TOKEN not in message
    assert "***" in message


@respx.mock
async def test_query_http_error_detail_redacts_credentials_write(connector):
    respx.post(f"{_BASE}/cards").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await connector.write(ConnectorPayload(resource="card", data={"name": "n", "idList": "l1"}))
    message = str(exc_info.value)
    assert API_KEY not in message
    assert TOKEN not in message


@respx.mock
async def test_query_transport_error_detail_redacts_credentials(connector):
    respx.get(f"{_BASE}/members/me/boards").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(httpx.HTTPError) as exc_info:
        await connector.query(ConnectorQuery(resource="boards"))
    message = str(exc_info.value)
    assert API_KEY not in message
    assert TOKEN not in message


@respx.mock
async def test_health_check_transport_error_detail_redacts_credentials(connector):
    respx.get(f"{_BASE}/members/me").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(httpx.HTTPError):
        await connector.health_check()


@respx.mock
async def test_status_error_request_and_response_url_redacted(connector):
    """FAR-507: redaction is not __str__-only — the stored request/response URLs
    must also be scrubbed, since httpx sinks capture ``exc.request.url`` and
    ``exc.response.request.url`` directly."""
    respx.get(f"{_BASE}/members/me/boards").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await connector.query(ConnectorQuery(resource="boards"))
    exc = exc_info.value
    assert API_KEY not in str(exc.request.url)
    assert TOKEN not in str(exc.request.url)
    assert API_KEY not in str(exc.response.request.url)
    assert TOKEN not in str(exc.response.request.url)


@respx.mock
async def test_transport_error_request_url_redacted(connector):
    """FAR-507: the exact leak this PR fixes — a transport error (e.g.
    ``httpx.ReadTimeout`` / ``httpx.ConnectError``) carries a CLEAN message but
    the LIVE ``key``/``token`` still sits in ``exc.request.url``. The shared
    ``CredentialRedactor`` (``scrub_url=True``) must scrub it even though the
    message itself contains no credential."""
    respx.get(f"{_BASE}/members/me/boards").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(httpx.HTTPError) as exc_info:
        await connector.query(ConnectorQuery(resource="boards"))
    exc = exc_info.value
    assert hasattr(exc, "request")
    assert API_KEY not in str(exc.request.url)
    assert TOKEN not in str(exc.request.url)


@respx.mock
async def test_status_error_does_not_chain_raw_cause(connector):
    """FAR-507: ``raise ... from exc`` would chain the raw un-redacted exception
    as ``__cause__``, so a full-traceback render prints the credential URL. The
    sanitised wrapper must NOT chain the raw httpx exception."""
    respx.get(f"{_BASE}/members/me/boards").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await connector.query(ConnectorQuery(resource="boards"))
    exc = exc_info.value
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True

    # The redacted wrapper still carries a useful message.
    message = str(exc)
    assert "Internal Error" in message or "500" in message
    assert API_KEY not in message
    assert TOKEN not in message


@respx.mock
async def test_transport_error_does_not_chain_raw_cause(connector):
    respx.get(f"{_BASE}/members/me/boards").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(httpx.HTTPError) as exc_info:
        await connector.query(ConnectorQuery(resource="boards"))
    exc = exc_info.value
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
